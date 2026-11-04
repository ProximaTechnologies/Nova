package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"text/template"
	"time"

	connect "connectrpc.com/connect"
	v1 "github.com/grafana/alloy-remote-config/api/gen/proto/go/collector/v1"
	collectorv1connect "github.com/grafana/alloy-remote-config/api/gen/proto/go/collector/v1/collectorv1connect"
	redis "github.com/redis/go-redis/v9"
)

type svc struct {
	rdb  *redis.Client
	tmpl *template.Template
	ttl  time.Duration // e.g. 15 * time.Minute for data key
}

// enabled features from db
type MimirCFG struct {
	URL string `json:"url"`
}
type LokiCFG struct {
	URL string `json:"url"`
}
type TempoCFG struct {
	URL string `json:"url"`
}

type LGTMCFG struct {
	Mimir MimirCFG `json:"mimir"`
	Loki  LokiCFG  `json:"loki"`
	Tempo TempoCFG `json:"tempo"`
}

type Features struct {
	SelfMonitor      bool `json:"selfMonitor"`
	LinuxMonitor     bool `json:"linuxMonitor"`
	WindowsMonitor   bool `json:"windowsMonitor"`
	ContainerMonitor bool `json:"containerMonitor"` //todo add config for docker socket
	JournalLog       bool `json:"journalLog"`
	WindowsEventLog  bool `json:"windowsEventLog"` //todo add config for windows event log
	DockerLog        bool `json:"dockerLogs"`      //todo add config for docker socket
	FileMonitor      bool `json:"fileMonitor"`     // todo add config for filepath. todo add support for decompressing files
}

// helper: random lock token
func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
func makeHash(version, content string) string {
	sum := sha256.Sum256([]byte(content))
	return version + ":" + hex.EncodeToString(sum[:])
}

// Safe unlock using Lua to ensure we only delete our own lock token
func safeUnlock(ctx context.Context, rdb *redis.Client, key, token string) error {
	const lua = `
if redis.call("GET", KEYS[1]) == ARGV[1] then
	return redis.call("DEL", KEYS[1])
else
	return 0
end`
	return rdb.Eval(ctx, lua, []string{key}, token).Err()
}

// GetConfig returns a dummy Alloy config. Adjust to your renderer/cache later.
func (s *svc) GetConfig(ctx context.Context, req *connect.Request[v1.GetConfigRequest]) (*connect.Response[v1.GetConfigResponse], error) {
	log.Printf("GetConfig: id=%s local_attrs=%v hash=%q", req.Msg.Id, req.Msg.LocalAttributes, req.Msg.Hash)
	// tenant_id comes from LocalAttributes
	attrs := req.Msg.LocalAttributes
	tenantID, _ := attrs["tenant_id"]
	if tenantID == "" {
		return nil, connect.NewError(connect.CodeInvalidArgument, fmt.Errorf("missing tenant_id"))
	}

	node := req.Msg.Id
	if node == "" {
		return nil, connect.NewError(connect.CodeInvalidArgument, fmt.Errorf("missing node Id"))
	}

	verKey := fmt.Sprintf("nova:ver:%s:%s", tenantID, node)

	// 1) Get or init version
	version, err := s.rdb.Get(ctx, verKey).Result()
	if err == redis.Nil {
		version = fmt.Sprintf("%d", time.Now().UnixMilli())
		if err := s.rdb.Set(ctx, verKey, version, 0).Err(); err != nil {
			return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("init version: %w", err))
		}
	} else if err != nil {
		return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("get version: %w", err))
	}

	dataKey := fmt.Sprintf("nova:cfg:%s:%s:%s", tenantID, node, version)
	lockKey := fmt.Sprintf("lock:%s:%s:%s", tenantID, node, version)

	// 2) Try cache
	//if data in cache, return it
	if cached, err := s.rdb.Get(ctx, dataKey).Result(); err == nil {
		// if the data is being used refresh the cache so we dont need
		//to rerender (render is expensive)
		_ = s.rdb.Expire(ctx, dataKey, s.ttl).Err()
		hash := makeHash(version, cached)
		if req.Msg.Hash == hash {
			return connect.NewResponse(&v1.GetConfigResponse{
				Hash:        hash,
				NotModified: true,
			}), nil
		}
		return connect.NewResponse(&v1.GetConfigResponse{
			Content: cached,
			Hash:    hash,
		}), nil
	} else if err != redis.Nil {
		return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("redis get data: %w", err))
	}

	// 3) Cache miss → lock and render
	lockTTL := 15 * time.Second
	lockToken := randHex(16)
	acquired, err := s.rdb.SetNX(ctx, lockKey, lockToken, lockTTL).Result()
	if err != nil {
		return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("acquire lock: %w", err))
	}

	// if we acquired the lock, render and cache
	if acquired {
		rendered, rerr := s.render(ctx, req, tenantID)
		if rerr != nil {
			_ = safeUnlock(ctx, s.rdb, lockKey, lockToken)
			return nil, connect.NewError(connect.CodeInternal, rerr)
		}

		if err := s.rdb.Set(ctx, dataKey, rendered, s.ttl).Err(); err != nil {
			_ = safeUnlock(ctx, s.rdb, lockKey, lockToken)
			return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("redis set data: %w", err))
		}

		_ = safeUnlock(ctx, s.rdb, lockKey, lockToken)

		hash := makeHash(version, rendered)
		if req.Msg.Hash == hash {
			return connect.NewResponse(&v1.GetConfigResponse{
				Hash:        hash,
				NotModified: true,
			}), nil
		}
		return connect.NewResponse(&v1.GetConfigResponse{
			Content: rendered,
			Hash:    hash,
		}), nil
	}

	// 4) Someone else rendering → wait and retry
	wait := 150 * time.Millisecond
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		time.Sleep(wait)
		if cached, err := s.rdb.Get(ctx, dataKey).Result(); err == nil {
			hash := makeHash(version, cached)
			if req.Msg.Hash == hash {
				return connect.NewResponse(&v1.GetConfigResponse{
					Hash:        hash,
					NotModified: true,
				}), nil
			}
			return connect.NewResponse(&v1.GetConfigResponse{
				Content: cached,
				Hash:    hash,
			}), nil
		} else if err != redis.Nil {
			return nil, connect.NewError(connect.CodeInternal, fmt.Errorf("redis get data(wait): %w", err))
		}
	}

	// Timeout → fallback render
	rendered, rerr := s.render(ctx, req, tenantID)
	if rerr != nil {
		return nil, connect.NewError(connect.CodeInternal, rerr)
	}
	hash := makeHash(version, rendered)
	return connect.NewResponse(&v1.GetConfigResponse{
		Content: rendered,
		Hash:    hash,
	}), nil
}

func (s *svc) render(ctx context.Context, req *connect.Request[v1.GetConfigRequest], tenantID string) (string, error) {
	//enabled features from db
	// feat key will never be accessed unless a render is needed
	// render will be called if the version changes
	// so we do not need to worry about locking
	// django will update the feature key before updating the version key
	featureKey := fmt.Sprintf("nova:feat:%s:%s", tenantID, req.Msg.Id)
	featureString, err := s.rdb.Get(ctx, featureKey).Result()
	if err == redis.Nil {
		log.Println("feature key not found")
	}
	var features Features
	jerr := json.Unmarshal([]byte(featureString), &features)
	if jerr != nil {
		log.Println("parse json error", jerr)
		return "", fmt.Errorf("parse json error: %w", jerr)
	}
	fmt.Println(features)

	//call api to get credentials
	credentials := map[string]string{"mimir_password": "password", "loki_password": "password", "tempo_password": "password"}

	lgtmcfg := LGTMCFG{}
	lgtmcfg.Mimir.URL = os.Getenv("MIMIR_URL") //write for prom push for mimir
	lgtmcfg.Loki.URL = os.Getenv("LOKI_URL")
	lgtmcfg.Tempo.URL = os.Getenv("TEMPO_URL")

	data := map[string]any{
		"TenantID":       tenantID,
		"ID":             req.Msg.Id,
		"LocalAttrs":     req.Msg.LocalAttributes,
		"RequestedHash":  req.Msg.Hash,
		"RequestedAtUTC": time.Now().UTC().Format(time.RFC3339),
		"Features":       features,
		"Credentials":    credentials,
		"LGTMCfg":        lgtmcfg,
	}
	var buf bytes.Buffer
	if err := s.tmpl.ExecuteTemplate(&buf, "config.alloy.gotmpl", data); err != nil {
		return "", fmt.Errorf("render template: %w", err)
	}
	return buf.String(), nil
}

func (s *svc) RegisterCollector(ctx context.Context, req *connect.Request[v1.RegisterCollectorRequest]) (*connect.Response[v1.RegisterCollectorResponse], error) {
	log.Printf("RegisterCollector: id=%s local_attrs=%v", req.Msg.Id, req.Msg.LocalAttributes)
	return connect.NewResponse(&v1.RegisterCollectorResponse{}), nil
}

func (s *svc) UnregisterCollector(ctx context.Context, req *connect.Request[v1.UnregisterCollectorRequest]) (*connect.Response[v1.UnregisterCollectorResponse], error) {
	log.Printf("UnregisterCollector: id=%s", req.Msg.Id)
	return connect.NewResponse(&v1.UnregisterCollectorResponse{}), nil
}

func main() {

	// Parse templates from folder
	tmpl := template.Must(template.ParseGlob("templates/*.gotmpl"))

	// Set up Redis
	rdb := redis.NewClient(&redis.Options{
		Addr: os.Getenv("REDIS_URL"),
	})

	mux := http.NewServeMux()
	path, handler := collectorv1connect.NewCollectorServiceHandler(&svc{
		rdb:  rdb,
		tmpl: tmpl,
		ttl:  15 * time.Minute,
	})
	mux.Handle(path, handler)

	log.Println("NovaCFG server listening on :8080")
	log.Fatal(http.ListenAndServe("0.0.0.0:8080", mux))
}
