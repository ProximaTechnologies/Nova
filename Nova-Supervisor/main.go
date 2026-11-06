package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	appName                 = "nova-supervisor"
	defaultStatePath        = "/var/lib/nova-supervisor/state.json"
	defaultHTTPTimeout      = 65 * time.Second
	defaultPollTimeout      = 55 * time.Second
	defaultPollInterval     = 10 * time.Second
	defaultAlloyService     = "alloy"
	defaultSupervisorUnit   = "nova-supervisor"
	defaultAlloyUser        = "alloy"
	defaultDockerGroup      = "docker"
	maxHTTPTimeout          = 5 * time.Minute
	maxPollTimeout          = 2 * time.Minute
	maxPollInterval         = 24 * time.Hour
	maxActionTimeout        = 30 * time.Minute
	defaultAlloyHTTPBaseURL = "http://127.0.0.1:12345"
	defaultAlloyHTTPTimeout = 10 * time.Second
	maxAlloyHTTPTimeout     = 2 * time.Minute
	alloyHTTPRetryInterval  = 500 * time.Millisecond
	maxDesiredStateBytes    = 2 << 20
	maxErrorBodyLogBytes    = 1024
	maxCommandLogBytes      = 2048
	maxDownloadBytes        = 512 << 20
)

var (
	buildVersion = "dev"
	buildCommit  = "unknown"

	bareAlloySemverRE = regexp.MustCompile(`^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\+[0-9A-Za-z.-]+)?$`)
)

type config struct {
	NovaUIURL         string
	DesiredPath       string
	NodeID            string
	Username          string
	Password          string
	StatePath         string
	AlloyService      string
	SupervisorService string
	AllowActions      bool
	AllowSelfUpdate   bool
	AllowAlloyUpdate  bool
	AllowedTools      map[string]struct{}
	PollTimeout       time.Duration
	PollInterval      time.Duration
	HTTPTimeout       time.Duration
	Once              bool
}

type runtimeConfig struct {
	NovaUIURL         string       `json:"nova_ui_url"`
	DesiredPath       string       `json:"desired_path"`
	NodeID            string       `json:"node_id"`
	Username          string       `json:"username"`
	Password          string       `json:"password"`
	AlloyService      string       `json:"alloy_service"`
	SupervisorService string       `json:"supervisor_service"`
	AllowActions      bool         `json:"allow_actions"`
	AllowSelfUpdate   bool         `json:"allow_self_update"`
	AllowAlloyUpdate  bool         `json:"allow_alloy_update"`
	AllowedTools      []string     `json:"allowed_tools"`
	PollTimeout       durationJSON `json:"poll_timeout"`
	PollInterval      durationJSON `json:"poll_interval"`
	HTTPTimeout       durationJSON `json:"http_timeout"`
}

type persistedState struct {
	Runtime           runtimeConfig     `json:"runtime"`
	DesiredVersion    string            `json:"desired_version"`
	AppliedActions    map[string]string `json:"applied_actions"`
	AlloyVersion      string            `json:"alloy_version"`
	SupervisorVersion string            `json:"supervisor_version"`
	UpdatedAt         time.Time         `json:"updated_at"`
}

type desiredState struct {
	Version         string            `json:"version"`
	PollAfter       durationJSON      `json:"poll_after"`
	PollAfterSecond int               `json:"poll_after_seconds"`
	Actions         []desiredAction   `json:"actions"`
	Alloy           alloyDesiredState `json:"alloy"`
	Supervisor      supervisorDesired `json:"supervisor"`
	Features        featureFlags      `json:"features"`
	Metadata        map[string]string `json:"metadata"`
}

type desiredAction struct {
	ID          string            `json:"id"`
	Tool        string            `json:"tool"`
	Args        []string          `json:"args"`
	Env         map[string]string `json:"env"`
	WorkingDir  string            `json:"working_dir"`
	Timeout     durationJSON      `json:"timeout"`
	RunOnce     bool              `json:"run_once"`
	Description string            `json:"description"`
}

type alloyDesiredState struct {
	Version    string                 `json:"version"`
	Service    string                 `json:"service"`
	Update     bool                   `json:"update"`
	Restart    bool                   `json:"restart"`
	SHA256     string                 `json:"sha256"`
	PackageURL string                 `json:"package_url"`
	HTTP       *alloyHTTPDesiredState `json:"http"`
}

type alloyHTTPDesiredState struct {
	BaseURL string       `json:"base_url"`
	Reload  bool         `json:"reload"`
	Ready   bool         `json:"ready"`
	Healthy bool         `json:"healthy"`
	Timeout durationJSON `json:"timeout"`
}

func (h *alloyHTTPDesiredState) requested() bool {
	return h != nil && (h.Reload || h.Ready || h.Healthy)
}

type supervisorDesired struct {
	Version        string   `json:"version"`
	Update         bool     `json:"update"`
	ManualUpdate   bool     `json:"manual_update"`
	AssetURL       string   `json:"asset_url"`
	SHA256         string   `json:"sha256"`
	Restart        bool     `json:"restart"`
	RestartCommand []string `json:"restart_command"`
}

type featureFlags map[string]json.RawMessage

type durationJSON struct {
	time.Duration
}

type bootstrapResponse struct {
	Runtime runtimeConfig `json:"runtime"`
}

type supervisor struct {
	cfg        config
	httpClient *http.Client
	stateMu    sync.Mutex
	state      persistedState
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "bootstrap" {
		if err := bootstrap(os.Args[2:]); err != nil {
			log.Fatalf("bootstrap: %v", err)
		}
		return
	}

	cfg, state, err := loadServiceConfig(os.Args[1:])
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	s := &supervisor{
		cfg: cfg,
		httpClient: &http.Client{
			Timeout: cfg.HTTPTimeout,
		},
		state: state,
	}

	ctx, stop := signalContext(context.Background())
	defer stop()

	if cfg.Once {
		if err := s.pollAndReconcile(ctx); err != nil {
			log.Fatalf("sync failed: %v", err)
		}
		return
	}

	log.Printf("%s starting version=%s commit=%s node=%s", appName, buildVersion, buildCommit, cfg.NodeID)
	s.run(ctx)
}

func loadServiceConfig(args []string) (config, persistedState, error) {
	fs := flag.NewFlagSet(appName, flag.ExitOnError)
	var statePath string
	var once bool
	showVersion := fs.Bool("version", false, "print version and exit")
	fs.StringVar(&statePath, "state-path", defaultStatePath, "local supervisor enrollment state file")
	fs.BoolVar(&once, "once", false, "poll and reconcile once, then exit")
	if err := fs.Parse(args); err != nil {
		return config{}, persistedState{}, err
	}

	if *showVersion {
		fmt.Printf("%s %s %s\n", appName, buildVersion, buildCommit)
		os.Exit(0)
	}

	state, err := readState(statePath)
	if err != nil {
		return config{}, persistedState{}, err
	}
	cfg, err := state.Runtime.config(statePath, once)
	if err != nil {
		return config{}, persistedState{}, err
	}
	return cfg, state, nil
}

func (r runtimeConfig) config(statePath string, once bool) (config, error) {
	cfg := config{
		NovaUIURL:         r.NovaUIURL,
		DesiredPath:       firstNonEmpty(r.DesiredPath, "/agent/supervisor/desired"),
		NodeID:            r.NodeID,
		Username:          r.Username,
		Password:          r.Password,
		StatePath:         statePath,
		AlloyService:      firstNonEmpty(r.AlloyService, defaultAlloyService),
		SupervisorService: firstNonEmpty(r.SupervisorService, defaultSupervisorUnit),
		AllowActions:      r.AllowActions,
		AllowSelfUpdate:   r.AllowSelfUpdate,
		AllowAlloyUpdate:  r.AllowAlloyUpdate,
		AllowedTools:      toolsMap(r.AllowedTools),
		PollTimeout:       firstDuration(r.PollTimeout.Duration, defaultPollTimeout),
		PollInterval:      firstDuration(r.PollInterval.Duration, defaultPollInterval),
		HTTPTimeout:       firstDuration(r.HTTPTimeout.Duration, defaultHTTPTimeout),
		Once:              once,
	}
	cfg.NovaUIURL = strings.TrimRight(cfg.NovaUIURL, "/")
	cfg.DesiredPath = "/" + strings.TrimLeft(cfg.DesiredPath, "/")

	switch {
	case cfg.NovaUIURL == "":
		return cfg, errors.New("missing nova_ui_url in supervisor enrollment state")
	case cfg.NodeID == "":
		return cfg, errors.New("missing node_id in supervisor enrollment state")
	case cfg.Username == "":
		return cfg, errors.New("missing username in supervisor enrollment state")
	case cfg.Password == "":
		return cfg, errors.New("missing password in supervisor enrollment state")
	case !isSafeURL(cfg.NovaUIURL):
		return cfg, fmt.Errorf("nova_ui_url must use https or local development http: %s", redactURL(cfg.NovaUIURL))
	case cfg.PollTimeout <= 0:
		return cfg, errors.New("poll timeout must be positive")
	case cfg.PollTimeout > maxPollTimeout:
		return cfg, fmt.Errorf("poll timeout must be <= %s", maxPollTimeout)
	case cfg.PollInterval <= 0:
		return cfg, errors.New("poll interval must be positive")
	case cfg.PollInterval > maxPollInterval:
		return cfg, fmt.Errorf("poll interval must be <= %s", maxPollInterval)
	case cfg.HTTPTimeout <= 0:
		return cfg, errors.New("http timeout must be positive")
	case cfg.HTTPTimeout > maxHTTPTimeout:
		return cfg, fmt.Errorf("http timeout must be <= %s", maxHTTPTimeout)
	}

	return cfg, nil
}

func bootstrap(args []string) error {
	fs := flag.NewFlagSet("bootstrap", flag.ExitOnError)
	var novaUIURL, nodeID, username, password, alloyService, statePath string
	fs.StringVar(&novaUIURL, "nova-ui-url", "", "NovaUI base URL")
	fs.StringVar(&nodeID, "node-id", "", "collector node ID")
	fs.StringVar(&username, "username", "", "collector config username")
	fs.StringVar(&password, "password", "", "collector config password")
	fs.StringVar(&alloyService, "alloy-service", defaultAlloyService, "local Alloy systemd service name")
	fs.StringVar(&statePath, "state-path", defaultStatePath, "local supervisor enrollment state file")
	if err := fs.Parse(args); err != nil {
		return err
	}
	novaUIURL = strings.TrimRight(novaUIURL, "/")
	switch {
	case novaUIURL == "":
		return errors.New("-nova-ui-url is required")
	case nodeID == "":
		return errors.New("-node-id is required")
	case username == "":
		return errors.New("-username is required")
	case password == "":
		return errors.New("-password is required")
	case !isSafeURL(novaUIURL):
		return fmt.Errorf("-nova-ui-url must use https or local development http: %s", redactURL(novaUIURL))
	}

	endpoint, err := url.Parse(novaUIURL + "/agent/supervisor/bootstrap/" + nodeID)
	if err != nil {
		return err
	}
	query := endpoint.Query()
	query.Set("alloy_service", alloyService)
	query.Set("supervisor_version", buildVersion)
	query.Set("supervisor_os", runtime.GOOS)
	query.Set("supervisor_arch", runtime.GOARCH)
	endpoint.RawQuery = query.Encode()

	ctx, cancel := context.WithTimeout(context.Background(), defaultHTTPTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint.String(), nil)
	if err != nil {
		return err
	}
	req.SetBasicAuth(username, password)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", appName+"/"+buildVersion)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("bootstrap failed: status=%d body=%s", resp.StatusCode, readRedactedBody(resp.Body, maxErrorBodyLogBytes))
	}

	var payload bootstrapResponse
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 1<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&payload); err != nil {
		return err
	}
	if payload.Runtime.Password == "" {
		payload.Runtime.Password = password
	}
	if payload.Runtime.Username == "" {
		payload.Runtime.Username = username
	}
	if _, err := payload.Runtime.config(statePath, false); err != nil {
		return err
	}

	state := persistedState{
		Runtime:           payload.Runtime,
		AppliedActions:    map[string]string{},
		SupervisorVersion: buildVersion,
		UpdatedAt:         time.Now().UTC(),
	}
	if err := writeState(statePath, state); err != nil {
		return err
	}
	log.Printf("wrote supervisor enrollment state to %s", statePath)
	return nil
}

func (s *supervisor) run(ctx context.Context) {
	failureBackoff := time.Duration(0)
	for {
		start := time.Now()
		if err := s.pollAndReconcile(ctx); err != nil {
			if errors.Is(err, context.Canceled) {
				return
			}
			log.Printf("sync failed: %v", err)
			if failureBackoff == 0 {
				failureBackoff = s.cfg.PollInterval
			} else {
				failureBackoff *= 2
			}
			if failureBackoff > time.Minute {
				failureBackoff = time.Minute
			}
		} else {
			failureBackoff = 0
		}

		wait := s.cfg.PollInterval
		if failureBackoff > wait {
			wait = failureBackoff
		}
		if elapsed := time.Since(start); elapsed < wait {
			timer := time.NewTimer(wait - elapsed)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
		}
	}
}

func (s *supervisor) pollAndReconcile(ctx context.Context) error {
	desired, changed, err := s.pollDesiredState(ctx)
	if err != nil {
		return err
	}
	if !changed {
		if err := s.reportVersions(ctx); err != nil {
			log.Printf("version report failed: %v", err)
		}
		return nil
	}

	if desired.Version == "" {
		return errors.New("desired state missing version")
	}

	log.Printf("desired state changed version=%s actions=%d alloy=%s supervisor=%s", desired.Version, len(desired.Actions), desired.Alloy.Version, desired.Supervisor.Version)
	if err := s.validateDesiredState(desired); err != nil {
		return err
	}

	if err := s.applyDesiredState(ctx, desired); err != nil {
		return err
	}
	s.stateMu.Lock()
	s.state.DesiredVersion = desired.Version
	s.state.UpdatedAt = time.Now().UTC()
	s.stateMu.Unlock()
	return s.saveState()
}

func (s *supervisor) pollDesiredState(ctx context.Context) (desiredState, bool, error) {
	ctx, cancel := context.WithTimeout(ctx, s.cfg.PollTimeout+5*time.Second)
	defer cancel()

	endpoint, err := url.Parse(s.cfg.NovaUIURL + s.cfg.DesiredPath + "/" + s.cfg.NodeID)
	if err != nil {
		return desiredState{}, false, err
	}
	query := endpoint.Query()
	query.Set("current_version", s.state.DesiredVersion)
	query.Set("wait_seconds", strconv.Itoa(int(s.cfg.PollTimeout.Seconds())))
	query.Set("supervisor_version", buildVersion)
	query.Set("supervisor_os", runtime.GOOS)
	query.Set("supervisor_arch", runtime.GOARCH)
	if format, err := packageFormat(); err == nil {
		query.Set("alloy_package_format", format)
	}
	query.Set("version", normalizeAlloyVersion(s.state.AlloyVersion))
	endpoint.RawQuery = query.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint.String(), nil)
	if err != nil {
		return desiredState{}, false, err
	}
	req.SetBasicAuth(s.cfg.Username, s.cfg.Password)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", appName+"/"+buildVersion)
	if s.state.DesiredVersion != "" {
		req.Header.Set("If-None-Match", quoteETag(s.state.DesiredVersion))
	}

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return desiredState{}, false, err
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusNoContent, http.StatusNotModified:
		return desiredState{}, false, nil
	case http.StatusOK:
	default:
		return desiredState{}, false, fmt.Errorf("desired-state request failed: status=%d body=%s", resp.StatusCode, readRedactedBody(resp.Body, maxErrorBodyLogBytes))
	}

	var desired desiredState
	decoder := json.NewDecoder(io.LimitReader(resp.Body, maxDesiredStateBytes))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&desired); err != nil {
		return desiredState{}, false, fmt.Errorf("decode desired state: %w", err)
	}

	return desired, desired.Version != "" && subtle.ConstantTimeCompare([]byte(desired.Version), []byte(s.state.DesiredVersion)) != 1, nil
}

func (s *supervisor) validateDesiredState(desired desiredState) error {
	if strings.TrimSpace(desired.Version) == "" {
		return errors.New("desired state missing version")
	}
	if desired.PollAfter.Duration < 0 {
		return errors.New("poll_after must not be negative")
	}
	if desired.PollAfter.Duration > maxPollInterval {
		return fmt.Errorf("poll_after must be <= %s", maxPollInterval)
	}
	if desired.PollAfterSecond < 0 {
		return errors.New("poll_after_seconds must not be negative")
	}
	if desired.Alloy.Update {
		if strings.TrimSpace(desired.Alloy.Version) == "" {
			return errors.New("alloy update missing version")
		}
		if strings.TrimSpace(desired.Alloy.SHA256) == "" {
			return errors.New("sha256 is required for alloy updates")
		}
		if err := validateSHA256String(desired.Alloy.SHA256); err != nil {
			return fmt.Errorf("alloy sha256: %w", err)
		}
		if desired.Alloy.PackageURL != "" && !isSafeURL(desired.Alloy.PackageURL) {
			return fmt.Errorf("alloy package_url must use https or local development http: %s", redactURL(desired.Alloy.PackageURL))
		}
	}
	if err := validateAlloyHTTPDesiredState(desired.Alloy.HTTP); err != nil {
		return err
	}
	if desired.Supervisor.updateRequested() {
		if strings.TrimSpace(desired.Supervisor.AssetURL) == "" {
			return errors.New("supervisor update missing asset_url")
		}
		if !isSafeURL(desired.Supervisor.AssetURL) {
			return fmt.Errorf("supervisor asset_url must use https or local development http: %s", redactURL(desired.Supervisor.AssetURL))
		}
		if strings.TrimSpace(desired.Supervisor.SHA256) == "" {
			return errors.New("sha256 is required for supervisor updates")
		}
		if err := validateSHA256String(desired.Supervisor.SHA256); err != nil {
			return fmt.Errorf("supervisor sha256: %w", err)
		}
	}
	if len(desired.Supervisor.RestartCommand) > 0 {
		return errors.New("supervisor restart_command is not supported")
	}
	if _, _, err := desired.alloyDockerGroupRequired(); err != nil {
		return err
	}
	seen := map[string]struct{}{}
	for _, action := range desired.Actions {
		id := strings.TrimSpace(action.ID)
		if id == "" {
			return errors.New("desired action missing id")
		}
		if _, ok := seen[id]; ok {
			return fmt.Errorf("duplicate desired action id %q", id)
		}
		seen[id] = struct{}{}
		if s.cfg.AllowActions {
			if err := s.validateAction(action); err != nil {
				return fmt.Errorf("action %s: %w", id, err)
			}
		}
	}
	return nil
}

func (s *supervisor) validateAction(action desiredAction) error {
	tool := strings.TrimSpace(action.Tool)
	if tool == "" {
		return errors.New("missing tool")
	}
	if _, allowed := s.cfg.AllowedTools[tool]; !allowed {
		return fmt.Errorf("tool %q is not allowed", tool)
	}
	if action.Timeout.Duration < 0 {
		return errors.New("timeout must not be negative")
	}
	if action.Timeout.Duration > maxActionTimeout {
		return fmt.Errorf("timeout must be <= %s", maxActionTimeout)
	}
	if action.WorkingDir != "" && !filepath.IsAbs(action.WorkingDir) {
		return errors.New("working_dir must be absolute")
	}
	for key := range action.Env {
		if key == "" || strings.Contains(key, "=") {
			return fmt.Errorf("invalid env name %q", key)
		}
	}
	return validateToolArgs(tool, action.Args)
}

func (s supervisorDesired) updateRequested() bool {
	return s.Update || s.ManualUpdate
}

func (d desiredState) alloyDockerGroupRequired() (bool, bool, error) {
	metrics, metricsKnown, err := d.boolFeature(
		"containerMonitor",
		"containerMetrics",
		"container_metrics",
		"dockerMetrics",
		"docker_metrics",
	)
	if err != nil {
		return false, false, err
	}
	logs, logsKnown, err := d.boolFeature(
		"dockerLogs",
		"docker_logs",
		"containerLogs",
		"container_logs",
	)
	if err != nil {
		return false, false, err
	}

	if (metricsKnown && metrics) || (logsKnown && logs) {
		return true, true, nil
	}
	if metricsKnown && logsKnown {
		return false, true, nil
	}
	return false, false, nil
}

func (d desiredState) boolFeature(keys ...string) (bool, bool, error) {
	if value, ok, err := d.Features.boolAny(keys...); ok || err != nil {
		return value, ok, err
	}
	return metadataBoolAny(d.Metadata, keys...)
}

func (f featureFlags) boolAny(keys ...string) (bool, bool, error) {
	if len(f) == 0 {
		return false, false, nil
	}
	for _, key := range keys {
		raw, ok := f[key]
		if !ok {
			continue
		}
		value, err := parseJSONBool(raw)
		if err != nil {
			return false, true, fmt.Errorf("features.%s: %w", key, err)
		}
		return value, true, nil
	}
	return false, false, nil
}

func metadataBoolAny(metadata map[string]string, keys ...string) (bool, bool, error) {
	if len(metadata) == 0 {
		return false, false, nil
	}
	for _, key := range keys {
		raw, ok := metadata[key]
		if !ok {
			continue
		}
		value, err := parseBoolString(raw)
		if err != nil {
			return false, true, fmt.Errorf("metadata.%s: %w", key, err)
		}
		return value, true, nil
	}
	return false, false, nil
}

func parseJSONBool(raw json.RawMessage) (bool, error) {
	var value bool
	if err := json.Unmarshal(raw, &value); err == nil {
		return value, nil
	}

	var text string
	if err := json.Unmarshal(raw, &text); err == nil {
		return parseBoolString(text)
	}

	return false, errors.New("must be a boolean")
}

func parseBoolString(raw string) (bool, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "1", "true", "yes", "on", "enabled":
		return true, nil
	case "0", "false", "no", "off", "disabled":
		return false, nil
	default:
		return false, fmt.Errorf("must be a boolean, got %q", raw)
	}
}

func (s *supervisor) applyDesiredState(ctx context.Context, desired desiredState) error {
	dockerGroupRestartRequired, err := s.reconcileAlloyDockerGroup(ctx, desired)
	if err != nil {
		return fmt.Errorf("alloy docker group: %w", err)
	}

	if desired.Alloy.Version != "" && desired.Alloy.Update {
		if !s.cfg.AllowAlloyUpdate {
			log.Printf("alloy update requested to %s but disabled", desired.Alloy.Version)
		} else if err := s.updateAlloy(ctx, desired.Alloy); err != nil {
			return fmt.Errorf("alloy update: %w", err)
		}
	}

	if len(desired.Actions) > 0 {
		if !s.cfg.AllowActions {
			log.Printf("desired state includes %d action(s), but actions are disabled", len(desired.Actions))
		} else if err := s.runActions(ctx, desired.Actions); err != nil {
			return err
		}
	}

	if dockerGroupRestartRequired {
		service := firstNonEmpty(desired.Alloy.Service, s.cfg.AlloyService)
		if err := runSystemctl(ctx, "restart", service+".service"); err != nil {
			return fmt.Errorf("restart alloy after docker group change: %w", err)
		}
	}

	if err := s.applyAlloyHTTP(ctx, desired.Alloy.HTTP); err != nil {
		return fmt.Errorf("alloy http hook: %w", err)
	}

	if desired.Supervisor.updateRequested() {
		if !s.cfg.AllowSelfUpdate {
			log.Printf("supervisor update requested to %s but disabled", desired.Supervisor.Version)
		} else if err := s.updateSupervisor(ctx, desired.Supervisor); err != nil {
			return fmt.Errorf("supervisor update: %w", err)
		}
	}

	return s.reportVersions(ctx)
}

func (s *supervisor) reconcileAlloyDockerGroup(ctx context.Context, desired desiredState) (bool, error) {
	required, known, err := desired.alloyDockerGroupRequired()
	if err != nil {
		return false, err
	}
	if !known {
		return false, nil
	}

	if required {
		changed, err := ensureUserInGroup(ctx, defaultAlloyUser, defaultDockerGroup)
		if err != nil {
			return false, err
		}
		if changed {
			log.Printf("added %s user to %s group for Docker logs or metrics", defaultAlloyUser, defaultDockerGroup)
		}
		return true, nil
	}

	changed, err := ensureUserNotInGroup(ctx, defaultAlloyUser, defaultDockerGroup)
	if err != nil {
		return false, err
	}
	if changed {
		log.Printf("removed %s user from %s group because Docker logs and metrics are disabled", defaultAlloyUser, defaultDockerGroup)
	}
	return true, nil
}

func (s *supervisor) runActions(ctx context.Context, actions []desiredAction) error {
	for _, action := range actions {
		if action.RunOnce && s.state.AppliedActions[action.ID] != "" {
			log.Printf("skipping already-applied action id=%s", action.ID)
			continue
		}
		if err := s.runAction(ctx, action); err != nil {
			return fmt.Errorf("action %s: %w", action.ID, err)
		}

		s.stateMu.Lock()
		if s.state.AppliedActions == nil {
			s.state.AppliedActions = map[string]string{}
		}
		s.state.AppliedActions[action.ID] = time.Now().UTC().Format(time.RFC3339)
		s.stateMu.Unlock()
		if err := s.saveState(); err != nil {
			return err
		}
	}
	return nil
}

func (s *supervisor) runAction(ctx context.Context, action desiredAction) error {
	tool := strings.TrimSpace(action.Tool)
	if tool == "" {
		return errors.New("missing tool")
	}
	if _, allowed := s.cfg.AllowedTools[tool]; !allowed {
		return fmt.Errorf("tool %q is not allowed", tool)
	}
	if err := validateToolArgs(tool, action.Args); err != nil {
		return err
	}
	timeout := action.Timeout.Duration
	if timeout <= 0 {
		timeout = 2 * time.Minute
	} else if timeout > maxActionTimeout {
		return fmt.Errorf("timeout must be <= %s", maxActionTimeout)
	}

	actionCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(actionCtx, tool, action.Args...)
	if action.WorkingDir != "" {
		cmd.Dir = action.WorkingDir
	}
	cmd.Env = os.Environ()
	for key, value := range action.Env {
		if key == "" || strings.Contains(key, "=") {
			return fmt.Errorf("invalid env name %q", key)
		}
		cmd.Env = append(cmd.Env, key+"="+value)
	}

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	log.Printf("running action id=%s tool=%s arg_count=%d", action.ID, tool, len(action.Args))
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%w stderr=%s", err, redactAndLimit(stderr.String(), maxCommandLogBytes))
	}
	if out := strings.TrimSpace(stdout.String()); out != "" {
		log.Printf("action id=%s output=%s", action.ID, redactAndLimit(out, maxCommandLogBytes))
	}
	return nil
}

func (s *supervisor) applyAlloyHTTP(ctx context.Context, hook *alloyHTTPDesiredState) error {
	if !hook.requested() {
		return nil
	}
	if err := validateAlloyHTTPDesiredState(hook); err != nil {
		return err
	}

	baseURL, err := alloyHTTPBaseURL(hook.BaseURL)
	if err != nil {
		return err
	}
	timeout := firstDuration(hook.Timeout.Duration, defaultAlloyHTTPTimeout)

	if hook.Reload {
		if err := s.callAlloyHTTPEndpoint(ctx, baseURL, http.MethodPost, "reload", timeout); err != nil {
			return err
		}
	}
	if hook.Ready {
		if err := s.waitAlloyHTTPEndpoint(ctx, baseURL, "ready", timeout); err != nil {
			return err
		}
	}
	if hook.Healthy {
		if err := s.waitAlloyHTTPEndpoint(ctx, baseURL, "healthy", timeout); err != nil {
			return err
		}
	}
	return nil
}

func (s *supervisor) callAlloyHTTPEndpoint(ctx context.Context, baseURL *url.URL, method, endpoint string, timeout time.Duration) error {
	endpointCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	status, body, err := s.doAlloyHTTPRequest(endpointCtx, method, alloyHTTPEndpointURL(baseURL, endpoint))
	if err != nil {
		return fmt.Errorf("%s request: %w", endpoint, err)
	}
	if status < 200 || status >= 300 {
		if body != "" {
			return fmt.Errorf("%s status=%d body=%s", endpoint, status, body)
		}
		return fmt.Errorf("%s status=%d", endpoint, status)
	}
	log.Printf("alloy http %s status=%d", endpoint, status)
	return nil
}

func (s *supervisor) waitAlloyHTTPEndpoint(ctx context.Context, baseURL *url.URL, endpoint string, timeout time.Duration) error {
	endpointCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	endpointURL := alloyHTTPEndpointURL(baseURL, endpoint)
	var lastStatus int
	var lastBody string
	var lastErr error

	for {
		status, body, err := s.doAlloyHTTPRequest(endpointCtx, http.MethodGet, endpointURL)
		if err == nil && status >= 200 && status < 300 {
			log.Printf("alloy http %s status=%d", endpoint, status)
			return nil
		}
		if err != nil {
			lastErr = err
		} else {
			lastStatus = status
			lastBody = body
			lastErr = nil
		}

		select {
		case <-endpointCtx.Done():
			return alloyHTTPRetryError(endpoint, timeout, lastStatus, lastBody, lastErr)
		case <-time.After(alloyHTTPRetryInterval):
		}
	}
}

func (s *supervisor) doAlloyHTTPRequest(ctx context.Context, method, endpointURL string) (int, string, error) {
	req, err := http.NewRequestWithContext(ctx, method, endpointURL, nil)
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("User-Agent", appName+"/"+buildVersion)

	client := s.httpClient
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxErrorBodyLogBytes))
		return resp.StatusCode, "", nil
	}
	return resp.StatusCode, readRedactedBody(resp.Body, maxErrorBodyLogBytes), nil
}

func alloyHTTPRetryError(endpoint string, timeout time.Duration, status int, body string, lastErr error) error {
	if lastErr != nil {
		return fmt.Errorf("%s timed out after %s: last error: %w", endpoint, timeout, lastErr)
	}
	if status != 0 {
		if body != "" {
			return fmt.Errorf("%s timed out after %s: last status=%d body=%s", endpoint, timeout, status, body)
		}
		return fmt.Errorf("%s timed out after %s: last status=%d", endpoint, timeout, status)
	}
	return fmt.Errorf("%s timed out after %s", endpoint, timeout)
}

func (s *supervisor) updateAlloy(ctx context.Context, desired alloyDesiredState) error {
	targetVersion := normalizeAlloyVersion(desired.Version)
	if targetVersion == "" {
		return errors.New("missing version")
	}
	if targetVersion == normalizeAlloyVersion(s.state.AlloyVersion) {
		log.Printf("alloy already at desired version=%s", targetVersion)
		return nil
	}
	desired.Version = targetVersion

	pkgURL, pkgName, err := alloyPackageURL(desired)
	if err != nil {
		return err
	}
	if strings.TrimSpace(desired.SHA256) == "" {
		return errors.New("sha256 is required for alloy updates")
	}
	if err := validateSHA256String(desired.SHA256); err != nil {
		return err
	}

	tmpDir, err := os.MkdirTemp("", "nova-alloy-update-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(tmpDir)

	pkgPath := filepath.Join(tmpDir, pkgName)
	log.Printf("downloading alloy version=%s url=%s", targetVersion, redactURL(pkgURL))
	if err := s.downloadFile(ctx, pkgURL, pkgPath); err != nil {
		return err
	}
	if err := verifySHA256(pkgPath, desired.SHA256); err != nil {
		return err
	}

	if err := installPackage(ctx, pkgPath); err != nil {
		return err
	}

	service := firstNonEmpty(desired.Service, s.cfg.AlloyService)
	if desired.Restart {
		if err := runSystemctl(ctx, "daemon-reload"); err != nil {
			log.Printf("systemctl daemon-reload failed: %v", err)
		}
		if err := runSystemctl(ctx, "restart", service+".service"); err != nil {
			return err
		}
	}

	s.stateMu.Lock()
	s.state.AlloyVersion = targetVersion
	s.state.UpdatedAt = time.Now().UTC()
	s.stateMu.Unlock()
	if err := s.saveState(); err != nil {
		return err
	}
	return s.reportAlloyVersion(ctx)
}

func (s *supervisor) updateSupervisor(ctx context.Context, desired supervisorDesired) error {
	if desired.AssetURL == "" {
		return errors.New("missing asset_url")
	}
	if len(desired.RestartCommand) > 0 {
		return errors.New("restart_command is not supported")
	}
	if strings.TrimSpace(desired.SHA256) == "" {
		return errors.New("sha256 is required for supervisor updates")
	}
	if err := validateSHA256String(desired.SHA256); err != nil {
		return err
	}
	if desired.Version != "" && desired.Version == buildVersion {
		log.Printf("supervisor already at desired version=%s", desired.Version)
		return nil
	}

	currentPath, err := os.Executable()
	if err != nil {
		return err
	}
	currentPath, err = filepath.EvalSymlinks(currentPath)
	if err != nil {
		return err
	}

	tmpDir, err := os.MkdirTemp(filepath.Dir(currentPath), ".nova-supervisor-update-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(tmpDir)

	nextPath := filepath.Join(tmpDir, filepath.Base(currentPath))
	log.Printf("downloading supervisor update version=%s url=%s", desired.Version, redactURL(desired.AssetURL))
	if err := s.downloadFile(ctx, desired.AssetURL, nextPath); err != nil {
		return err
	}
	if err := verifySHA256(nextPath, desired.SHA256); err != nil {
		return err
	}
	if err := os.Chmod(nextPath, 0755); err != nil {
		return err
	}

	backupPath := currentPath + ".bak"
	_ = os.Remove(backupPath)
	if err := os.Rename(currentPath, backupPath); err != nil {
		return err
	}
	if err := os.Rename(nextPath, currentPath); err != nil {
		_ = os.Rename(backupPath, currentPath)
		return err
	}

	s.stateMu.Lock()
	if desired.Version != "" {
		s.state.SupervisorVersion = desired.Version
	}
	s.state.UpdatedAt = time.Now().UTC()
	s.stateMu.Unlock()
	if err := s.saveState(); err != nil {
		return err
	}

	if desired.Restart {
		return s.restartSupervisor(ctx)
	}
	return nil
}

func (s *supervisor) restartSupervisor(ctx context.Context) error {
	return runSystemctl(ctx, "restart", s.cfg.SupervisorService+".service")
}

func (s *supervisor) reportAlloyVersion(ctx context.Context) error {
	version := normalizeAlloyVersion(s.state.AlloyVersion)
	if version != s.state.AlloyVersion {
		s.stateMu.Lock()
		s.state.AlloyVersion = version
		s.state.UpdatedAt = time.Now().UTC()
		s.stateMu.Unlock()
		_ = s.saveState()
	}
	if version == "" {
		detected, err := detectAlloyVersion(ctx)
		if err != nil {
			log.Printf("alloy version detection failed: %v", redactAndLimit(err.Error(), maxCommandLogBytes))
			return nil
		}
		version = normalizeAlloyVersion(detected)
		s.stateMu.Lock()
		s.state.AlloyVersion = version
		s.state.UpdatedAt = time.Now().UTC()
		s.stateMu.Unlock()
		_ = s.saveState()
	}
	if version == "" {
		return nil
	}

	endpoint := fmt.Sprintf("%s/agent/version/%s/%s", s.cfg.NovaUIURL, s.cfg.NodeID, url.PathEscape(version))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, nil)
	if err != nil {
		return err
	}
	req.SetBasicAuth(s.cfg.Username, s.cfg.Password)
	req.Header.Set("User-Agent", appName+"/"+buildVersion)

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("version report status=%d body=%s", resp.StatusCode, readRedactedBody(resp.Body, maxErrorBodyLogBytes))
	}
	return nil
}

func (s *supervisor) reportSupervisorVersion(ctx context.Context) error {
	version := strings.TrimSpace(buildVersion)
	if version == "" {
		return nil
	}

	s.stateMu.Lock()
	if s.state.SupervisorVersion != version {
		s.state.SupervisorVersion = version
		s.state.UpdatedAt = time.Now().UTC()
		s.stateMu.Unlock()
		_ = s.saveState()
	} else {
		s.stateMu.Unlock()
	}

	endpoint := fmt.Sprintf("%s/agent/supervisor/version/%s/%s", s.cfg.NovaUIURL, s.cfg.NodeID, url.PathEscape(version))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, nil)
	if err != nil {
		return err
	}
	query := req.URL.Query()
	query.Set("supervisor_os", runtime.GOOS)
	query.Set("supervisor_arch", runtime.GOARCH)
	req.URL.RawQuery = query.Encode()
	req.SetBasicAuth(s.cfg.Username, s.cfg.Password)
	req.Header.Set("User-Agent", appName+"/"+buildVersion)

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("supervisor version report status=%d body=%s", resp.StatusCode, readRedactedBody(resp.Body, maxErrorBodyLogBytes))
	}
	return nil
}

func (s *supervisor) reportVersions(ctx context.Context) error {
	return errors.Join(
		s.reportSupervisorVersion(ctx),
		s.reportAlloyVersion(ctx),
	)
}

func (s *supervisor) downloadFile(ctx context.Context, sourceURL, destPath string) error {
	if !isSafeURL(sourceURL) {
		return fmt.Errorf("download URL must use https or local development http: %s", redactURL(sourceURL))
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, sourceURL, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", appName+"/"+buildVersion)

	resp, err := s.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("download failed status=%d", resp.StatusCode)
	}
	if resp.ContentLength > maxDownloadBytes {
		return fmt.Errorf("download too large: content_length=%d max=%d", resp.ContentLength, maxDownloadBytes)
	}

	out, err := os.OpenFile(destPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer func() {
		_ = out.Close()
	}()

	reader := &io.LimitedReader{R: resp.Body, N: maxDownloadBytes + 1}
	written, err := io.Copy(out, reader)
	if err != nil {
		_ = os.Remove(destPath)
		return err
	}
	if written > maxDownloadBytes {
		_ = os.Remove(destPath)
		return fmt.Errorf("download too large: max=%d", maxDownloadBytes)
	}
	return nil
}

func (s *supervisor) loadState() error {
	state, err := readState(s.cfg.StatePath)
	if err != nil {
		return err
	}
	s.state = state
	return nil
}

func (s *supervisor) saveState() error {
	s.stateMu.Lock()
	state := s.state
	s.stateMu.Unlock()

	return writeState(s.cfg.StatePath, state)
}

func readState(path string) (persistedState, error) {
	state := persistedState{
		AppliedActions:    map[string]string{},
		SupervisorVersion: buildVersion,
	}

	if err := validateStateFile(path); err != nil {
		return state, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return state, fmt.Errorf("supervisor is not enrolled; run %q from the NovaUI installer", appName+" bootstrap")
		}
		return state, err
	}
	if err := json.Unmarshal(data, &state); err != nil {
		return state, err
	}
	if state.AppliedActions == nil {
		state.AppliedActions = map[string]string{}
	}
	if state.SupervisorVersion == "" {
		state.SupervisorVersion = buildVersion
	}
	return state, nil
}

func validateStateFile(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("state path is not a regular file: %s", path)
	}
	if info.Mode().Perm()&0077 != 0 {
		return fmt.Errorf("state file permissions must not allow group or world access: %s mode=%o", path, info.Mode().Perm())
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return nil
	}
	currentUID := uint32(os.Geteuid())
	if stat.Uid != 0 && stat.Uid != currentUID {
		return fmt.Errorf("state file must be owned by root or current service user: %s uid=%d", path, stat.Uid)
	}
	return nil
}

func writeState(path string, state persistedState) error {
	if err := os.MkdirAll(filepath.Dir(path), 0750); err != nil {
		return err
	}
	tmp := path + ".tmp"
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	file, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	if _, err := file.Write(data); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func alloyPackageURL(desired alloyDesiredState) (string, string, error) {
	if desired.PackageURL != "" {
		parsed, err := url.Parse(desired.PackageURL)
		if err != nil {
			return "", "", err
		}
		name := filepath.Base(parsed.Path)
		if name == "." || name == "/" || name == "" {
			return "", "", errors.New("package_url must include a package filename")
		}
		return desired.PackageURL, name, nil
	}

	version := desired.Version
	verNum := strings.TrimPrefix(version, "v")
	format, arch, err := packageFormatAndArch()
	if err != nil {
		return "", "", err
	}
	pkgName := fmt.Sprintf("alloy-%s-1.%s.%s", verNum, arch, format)
	return fmt.Sprintf("https://github.com/grafana/alloy/releases/download/%s/%s", version, pkgName), pkgName, nil
}

func packageFormatAndArch() (string, string, error) {
	format, err := packageFormat()
	if err != nil {
		return "", "", err
	}
	switch format {
	case "deb":
		switch runtime.GOARCH {
		case "amd64":
			return format, "amd64", nil
		case "arm64":
			return format, "arm64", nil
		case "ppc64le":
			return format, "ppc64el", nil
		default:
			return "", "", fmt.Errorf("unsupported deb arch: %s", runtime.GOARCH)
		}
	case "rpm":
		switch runtime.GOARCH {
		case "amd64":
			return format, "x86_64", nil
		case "arm64":
			return format, "aarch64", nil
		case "ppc64le", "s390x":
			return format, runtime.GOARCH, nil
		default:
			return "", "", fmt.Errorf("unsupported rpm arch: %s", runtime.GOARCH)
		}
	default:
		return "", "", fmt.Errorf("unsupported package format: %s", format)
	}
}

func packageFormat() (string, error) {
	data, err := os.ReadFile("/etc/os-release")
	if err != nil {
		return "", err
	}
	fields := parseOSRelease(data)
	idLike := fields["ID_LIKE"]
	if idLike == "" {
		idLike = fields["ID"]
	}
	idLike = strings.ToLower(idLike)
	switch {
	case strings.Contains(idLike, "debian"), strings.Contains(idLike, "ubuntu"):
		return "deb", nil
	case strings.Contains(idLike, "rhel"), strings.Contains(idLike, "fedora"), strings.Contains(idLike, "centos"), strings.Contains(idLike, "rocky"), strings.Contains(idLike, "alma"), strings.Contains(idLike, "suse"), strings.Contains(idLike, "amzn"), strings.Contains(idLike, "amazon"):
		return "rpm", nil
	default:
		return "", fmt.Errorf("unsupported distro ID/ID_LIKE: %s", idLike)
	}
}

func installPackage(ctx context.Context, pkgPath string) error {
	switch filepath.Ext(pkgPath) {
	case ".deb":
		if err := runCommand(ctx, "dpkg", "-i", pkgPath); err != nil {
			if _, lookErr := exec.LookPath("apt-get"); lookErr != nil {
				return err
			}
			if fixErr := runCommand(ctx, "apt-get", "install", "-f", "-y"); fixErr != nil {
				return fmt.Errorf("dpkg failed: %v; apt-get install -f failed: %w", err, fixErr)
			}
			return runCommand(ctx, "dpkg", "-i", pkgPath)
		}
		return nil
	case ".rpm":
		if _, err := exec.LookPath("dnf"); err == nil {
			return runCommand(ctx, "dnf", "-y", "install", pkgPath)
		}
		if _, err := exec.LookPath("yum"); err == nil {
			return runCommand(ctx, "yum", "-y", "localinstall", pkgPath)
		}
		return runCommand(ctx, "rpm", "-Uvh", "--nodeps", pkgPath)
	default:
		return fmt.Errorf("unsupported package extension: %s", filepath.Ext(pkgPath))
	}
}

func runSystemctl(ctx context.Context, args ...string) error {
	return runCommand(ctx, "systemctl", args...)
}

func runCommand(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	var stderr bytes.Buffer
	cmd.Stdout = os.Stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%s %q failed: %w stderr=%s", name, args, err, strings.TrimSpace(stderr.String()))
	}
	return nil
}

func ensureUserInGroup(ctx context.Context, user, group string) (bool, error) {
	inGroup, err := userInGroup(ctx, user, group)
	if err != nil {
		return false, err
	}
	if inGroup {
		return false, nil
	}
	if err := runCommand(ctx, "usermod", "-aG", group, user); err != nil {
		return false, err
	}
	return true, nil
}

func ensureUserNotInGroup(ctx context.Context, user, group string) (bool, error) {
	inGroup, err := userInGroup(ctx, user, group)
	if err != nil {
		return false, err
	}
	if !inGroup {
		return false, nil
	}

	var lastErr error
	if _, err := exec.LookPath("gpasswd"); err == nil {
		if err := runCommand(ctx, "gpasswd", "-d", user, group); err == nil {
			return true, nil
		} else {
			lastErr = err
		}
	}
	if _, err := exec.LookPath("deluser"); err == nil {
		if err := runCommand(ctx, "deluser", user, group); err == nil {
			return true, nil
		} else {
			lastErr = err
		}
	}
	if lastErr != nil {
		return false, lastErr
	}
	return false, errors.New("neither gpasswd nor deluser found to remove user from group")
}

func userInGroup(ctx context.Context, user, group string) (bool, error) {
	out, err := exec.CommandContext(ctx, "id", "-nG", user).CombinedOutput()
	if err != nil {
		return false, fmt.Errorf("id -nG %s failed: %w output=%s", user, err, redactAndLimit(strings.TrimSpace(string(out)), maxCommandLogBytes))
	}
	for _, name := range strings.Fields(string(out)) {
		if name == group {
			return true, nil
		}
	}
	return false, nil
}

func detectAlloyVersion(ctx context.Context) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "alloy", "--version").CombinedOutput()
	if err != nil {
		return "", err
	}
	fields := strings.Fields(string(out))
	for _, field := range fields {
		version := normalizeAlloyVersion(field)
		if strings.HasPrefix(version, "v") && len(version) > 1 {
			return version, nil
		}
	}
	return "", nil
}

func normalizeAlloyVersion(version string) string {
	version = strings.TrimSpace(version)
	if version == "" || strings.HasPrefix(version, "v") {
		return version
	}
	if bareAlloySemverRE.MatchString(version) {
		return "v" + version
	}
	return version
}

func verifySHA256(path, expected string) error {
	expected = strings.TrimSpace(strings.ToLower(expected))
	if strings.HasPrefix(expected, "sha256:") {
		expected = strings.TrimPrefix(expected, "sha256:")
	}
	if err := validateSHA256String(expected); err != nil {
		return fmt.Errorf("%w for %s", err, path)
	}

	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()

	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return err
	}
	actual := hex.EncodeToString(hash.Sum(nil))
	if subtle.ConstantTimeCompare([]byte(actual), []byte(expected)) != 1 {
		return fmt.Errorf("sha256 mismatch: expected=%s actual=%s", expected, actual)
	}
	return nil
}

func validateSHA256String(expected string) error {
	expected = strings.TrimSpace(strings.ToLower(expected))
	if strings.HasPrefix(expected, "sha256:") {
		expected = strings.TrimPrefix(expected, "sha256:")
	}
	if len(expected) != sha256.Size*2 {
		return errors.New("invalid sha256 length")
	}
	if _, err := hex.DecodeString(expected); err != nil {
		return fmt.Errorf("invalid sha256 hex: %w", err)
	}
	return nil
}

func validateAlloyHTTPDesiredState(hook *alloyHTTPDesiredState) error {
	if hook == nil {
		return nil
	}
	if hook.Timeout.Duration < 0 {
		return errors.New("alloy.http.timeout must not be negative")
	}
	if hook.Timeout.Duration > maxAlloyHTTPTimeout {
		return fmt.Errorf("alloy.http.timeout must be <= %s", maxAlloyHTTPTimeout)
	}
	if _, err := alloyHTTPBaseURL(hook.BaseURL); err != nil {
		return fmt.Errorf("alloy.http.base_url: %w", err)
	}
	return nil
}

func alloyHTTPBaseURL(raw string) (*url.URL, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		raw = defaultAlloyHTTPBaseURL
	}
	u, err := url.Parse(raw)
	if err != nil {
		return nil, err
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return nil, errors.New("must use http or https")
	}
	if u.Host == "" {
		return nil, errors.New("must include a host")
	}
	if u.User != nil {
		return nil, errors.New("must not include userinfo")
	}
	if u.RawQuery != "" || u.ForceQuery {
		return nil, errors.New("must not include a query")
	}
	if u.Fragment != "" {
		return nil, errors.New("must not include a fragment")
	}
	if !isLoopbackHost(u.Hostname()) {
		return nil, errors.New("host must be loopback")
	}
	return u, nil
}

func isLoopbackHost(host string) bool {
	host = strings.TrimSpace(strings.ToLower(host))
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func alloyHTTPEndpointURL(baseURL *url.URL, endpoint string) string {
	u := *baseURL
	prefix := strings.TrimRight(u.Path, "/")
	if prefix == "" {
		u.Path = "/-/" + endpoint
	} else {
		u.Path = prefix + "/-/" + endpoint
	}
	u.RawPath = ""
	u.RawQuery = ""
	u.Fragment = ""
	u.User = nil
	return u.String()
}

func isSafeURL(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	switch u.Scheme {
	case "https":
		return u.Host != ""
	case "http":
		host := strings.ToLower(u.Hostname())
		return host == "localhost" || host == "127.0.0.1" || host == "::1"
	default:
		return false
	}
}

func redactURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return redactAndLimit(raw, maxCommandLogBytes)
	}
	u.User = nil
	if u.RawQuery != "" {
		q := u.Query()
		for key := range q {
			if isSensitiveName(key) {
				q.Set(key, "REDACTED")
			}
		}
		u.RawQuery = q.Encode()
	}
	return redactAndLimit(u.String(), maxCommandLogBytes)
}

func readRedactedBody(r io.Reader, limit int64) string {
	body, _ := io.ReadAll(io.LimitReader(r, limit))
	return redactAndLimit(strings.TrimSpace(string(body)), int(limit))
}

func redactAndLimit(value string, limit int) string {
	value = redactSecrets(value)
	if limit > 0 && len(value) > limit {
		return value[:limit] + "...[truncated]"
	}
	return value
}

func redactSecrets(value string) string {
	fields := strings.Fields(value)
	for i, field := range fields {
		lower := strings.ToLower(field)
		if isSensitiveFlag(lower) {
			fields[i] = fieldName(field)
			if !strings.Contains(field, "=") && i+1 < len(fields) {
				fields[i+1] = "REDACTED"
			}
			continue
		}
		if key, _, ok := strings.Cut(field, "="); ok && isSensitiveName(strings.TrimLeft(key, "-")) {
			fields[i] = key + "=REDACTED"
		}
	}
	return strings.Join(fields, " ")
}

func isSensitiveFlag(value string) bool {
	name := strings.TrimLeft(value, "-")
	if key, _, ok := strings.Cut(name, "="); ok {
		name = key
	}
	return isSensitiveName(name)
}

func isSensitiveName(name string) bool {
	name = strings.ToLower(name)
	return strings.Contains(name, "token") ||
		strings.Contains(name, "secret") ||
		strings.Contains(name, "password") ||
		strings.Contains(name, "passwd") ||
		strings.Contains(name, "authorization") ||
		strings.Contains(name, "credential") ||
		strings.Contains(name, "signature") ||
		strings.Contains(name, "access_key")
}

func fieldName(field string) string {
	if key, _, ok := strings.Cut(field, "="); ok {
		return key + "=REDACTED"
	}
	return field
}

func validateToolArgs(tool string, args []string) error {
	switch tool {
	case "systemctl":
		return validateSystemctlArgs(args)
	case "kubectl", "helm", "argocd":
		return fmt.Errorf("%s actions require a narrower action implementation and are not supported by the local supervisor", tool)
	default:
		return nil
	}
}

func validateSystemctlArgs(args []string) error {
	if len(args) < 2 {
		return errors.New("systemctl action requires a command and service")
	}
	if len(args) > 3 {
		return errors.New("systemctl action allows at most one optional flag plus command and service")
	}
	offset := 0
	if strings.HasPrefix(args[0], "--") {
		if args[0] != "--no-block" {
			return fmt.Errorf("systemctl flag %q is not allowed", args[0])
		}
		offset = 1
	}
	if len(args)-offset != 2 {
		return errors.New("systemctl action requires command and service")
	}
	switch args[offset] {
	case "restart", "reload", "try-reload-or-restart", "status", "is-active":
	default:
		return fmt.Errorf("systemctl command %q is not allowed", args[offset])
	}
	if !isSafeServiceName(args[offset+1]) {
		return fmt.Errorf("invalid systemctl service %q", args[offset+1])
	}
	return nil
}

func isSafeServiceName(service string) bool {
	if !strings.HasSuffix(service, ".service") {
		return false
	}
	if strings.Contains(service, "/") || strings.Contains(service, "..") {
		return false
	}
	for _, r := range service {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' {
			continue
		}
		switch r {
		case '.', '-', '_', '@':
			continue
		default:
			return false
		}
	}
	return true
}

func parseOSRelease(data []byte) map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		value = strings.Trim(value, `"`)
		out[key] = value
	}
	return out
}

func toolsMap(tools []string) map[string]struct{} {
	out := map[string]struct{}{}
	for _, tool := range tools {
		tool = strings.TrimSpace(tool)
		if tool == "" {
			continue
		}
		out[tool] = struct{}{}
	}
	return out
}

func firstDuration(value, fallback time.Duration) time.Duration {
	if value > 0 {
		return value
	}
	return fallback
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func quoteETag(value string) string {
	value = strings.ReplaceAll(value, `"`, "")
	return `"` + value + `"`
}

func (d *durationJSON) UnmarshalJSON(data []byte) error {
	var raw any
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	switch value := raw.(type) {
	case string:
		if value == "" {
			d.Duration = 0
			return nil
		}
		parsed, err := time.ParseDuration(value)
		if err != nil {
			return err
		}
		d.Duration = parsed
	case float64:
		d.Duration = time.Duration(value * float64(time.Second))
	case nil:
		d.Duration = 0
	default:
		return fmt.Errorf("unsupported duration JSON type %T", raw)
	}
	return nil
}

func (d durationJSON) MarshalJSON() ([]byte, error) {
	if d.Duration <= 0 {
		return []byte(`""`), nil
	}
	return json.Marshal(d.Duration.String())
}

func signalContext(parent context.Context) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(parent)
	signals := make(chan os.Signal, 2)
	signalNotify(signals)
	go func() {
		select {
		case <-signals:
			cancel()
		case <-ctx.Done():
		}
	}()
	return ctx, func() {
		cancel()
		signalStop(signals)
		close(signals)
	}
}

func signalNotify(ch chan<- os.Signal) {
	signalNotifyImpl(ch)
}

func signalStop(ch chan<- os.Signal) {
	signalStopImpl(ch)
}

var signalNotifyImpl = func(ch chan<- os.Signal) {
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
}

var signalStopImpl = func(ch chan<- os.Signal) {
	signal.Stop(ch)
}
