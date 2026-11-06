package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestRuntimeConfigRejectsNonTLSNovaURL(t *testing.T) {
	rt := runtimeConfig{
		NovaUIURL:    "http://example.com",
		NodeID:       "node-1",
		Username:     "user",
		Password:     "pass",
		PollTimeout:  durationJSON{Duration: time.Second},
		PollInterval: durationJSON{Duration: time.Second},
		HTTPTimeout:  durationJSON{Duration: time.Second},
	}
	if _, err := rt.config(filepath.Join(t.TempDir(), "state.json"), false); err == nil {
		t.Fatal("expected non-local HTTP nova_ui_url to be rejected")
	}
	rt.NovaUIURL = "http://127.0.0.1:8080"
	if _, err := rt.config(filepath.Join(t.TempDir(), "state.json"), false); err != nil {
		t.Fatalf("expected local HTTP nova_ui_url to be allowed: %v", err)
	}
}

func TestValidateDesiredStateRequiresHashesAndRejectsRestartCommand(t *testing.T) {
	s := testSupervisor(true, []string{"systemctl"})
	desired := validDesiredState()
	desired.Alloy.Update = true
	desired.Alloy.SHA256 = ""
	if err := s.validateDesiredState(desired); err == nil || !strings.Contains(err.Error(), "sha256 is required for alloy") {
		t.Fatalf("expected missing alloy sha256 error, got %v", err)
	}

	desired = validDesiredState()
	desired.Supervisor.RestartCommand = []string{"/bin/true"}
	if err := s.validateDesiredState(desired); err == nil || !strings.Contains(err.Error(), "restart_command") {
		t.Fatalf("expected restart_command rejection, got %v", err)
	}
}

func TestValidateDesiredStateRejectsInvalidAlloyHTTPHook(t *testing.T) {
	s := testSupervisor(true, []string{"systemctl"})
	for _, tc := range []struct {
		name string
		hook alloyHTTPDesiredState
		want string
	}{
		{
			name: "external host",
			hook: alloyHTTPDesiredState{BaseURL: "https://example.com", Ready: true},
			want: "host must be loopback",
		},
		{
			name: "metadata address",
			hook: alloyHTTPDesiredState{BaseURL: "http://169.254.169.254:12345", Ready: true},
			want: "host must be loopback",
		},
		{
			name: "userinfo",
			hook: alloyHTTPDesiredState{BaseURL: "http://user:pass@127.0.0.1:12345", Ready: true},
			want: "must not include userinfo",
		},
		{
			name: "query",
			hook: alloyHTTPDesiredState{BaseURL: "http://127.0.0.1:12345?token=secret", Ready: true},
			want: "must not include a query",
		},
		{
			name: "fragment",
			hook: alloyHTTPDesiredState{BaseURL: "http://127.0.0.1:12345/#ready", Ready: true},
			want: "must not include a fragment",
		},
		{
			name: "timeout too large",
			hook: alloyHTTPDesiredState{Ready: true, Timeout: durationJSON{Duration: maxAlloyHTTPTimeout + time.Second}},
			want: "alloy.http.timeout must be <= 2m0s",
		},
		{
			name: "negative timeout",
			hook: alloyHTTPDesiredState{Ready: true, Timeout: durationJSON{Duration: -time.Second}},
			want: "alloy.http.timeout must not be negative",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			desired := validDesiredState()
			desired.Alloy.HTTP = &tc.hook
			err := s.validateDesiredState(desired)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("expected %q error, got %v", tc.want, err)
			}
		})
	}
}

func TestValidateDesiredStateRejectsDuplicateActionsBeforeExecution(t *testing.T) {
	s := testSupervisor(true, []string{"systemctl"})
	desired := validDesiredState()
	desired.Actions = append(desired.Actions, desired.Actions[0])
	if err := s.validateDesiredState(desired); err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("expected duplicate action rejection, got %v", err)
	}
}

func TestDefaultAllowedToolsDenyActions(t *testing.T) {
	rt := runtimeConfig{
		NovaUIURL:    "https://nova.example.com",
		NodeID:       "node-1",
		Username:     "user",
		Password:     "pass",
		AllowActions: true,
		PollTimeout:  durationJSON{Duration: time.Second},
		PollInterval: durationJSON{Duration: time.Second},
		HTTPTimeout:  durationJSON{Duration: time.Second},
	}
	cfg, err := rt.config(filepath.Join(t.TempDir(), "state.json"), false)
	if err != nil {
		t.Fatal(err)
	}
	s := &supervisor{cfg: cfg}
	err = s.validateAction(desiredAction{ID: "a1", Tool: "systemctl", Args: []string{"restart", "alloy.service"}})
	if err == nil || !strings.Contains(err.Error(), "not allowed") {
		t.Fatalf("expected default-deny action policy, got %v", err)
	}
}

func TestAlloyDockerGroupRequiredFromFeaturesAndMetadata(t *testing.T) {
	desired := desiredState{
		Features: featureFlags{
			"containerMonitor": json.RawMessage(`false`),
			"dockerLogs":       json.RawMessage(`true`),
		},
	}
	required, known, err := desired.alloyDockerGroupRequired()
	if err != nil {
		t.Fatal(err)
	}
	if !known || !required {
		t.Fatalf("expected Docker group to be required, known=%v required=%v", known, required)
	}

	desired = desiredState{
		Metadata: map[string]string{
			"containerMonitor": "false",
			"dockerLogs":       "false",
		},
	}
	required, known, err = desired.alloyDockerGroupRequired()
	if err != nil {
		t.Fatal(err)
	}
	if !known || required {
		t.Fatalf("expected Docker group to be disabled, known=%v required=%v", known, required)
	}
}

func TestAlloyDockerGroupRequiredIgnoresPartialDisable(t *testing.T) {
	desired := desiredState{
		Features: featureFlags{
			"containerMonitor": json.RawMessage(`false`),
		},
	}
	required, known, err := desired.alloyDockerGroupRequired()
	if err != nil {
		t.Fatal(err)
	}
	if known || required {
		t.Fatalf("expected partial false feature set to be unknown, known=%v required=%v", known, required)
	}
}

func TestValidateDesiredStateSupportsSupervisorUpdateAlias(t *testing.T) {
	s := testSupervisor(false, nil)
	desired := validDesiredState()
	desired.Actions = nil
	desired.Alloy.Update = false
	desired.Supervisor.ManualUpdate = false
	desired.Supervisor.Update = true
	if err := s.validateDesiredState(desired); err != nil {
		t.Fatalf("expected supervisor update alias to validate: %v", err)
	}
}

func TestReconcileAlloyDockerGroupAddsAndRemovesUser(t *testing.T) {
	groupsPath, logPath := installFakeGroupTools(t, "alloy")
	s := &supervisor{}

	changed, err := s.reconcileAlloyDockerGroup(context.Background(), desiredState{
		Features: featureFlags{
			"containerMonitor": json.RawMessage(`true`),
			"dockerLogs":       json.RawMessage(`false`),
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !changed {
		t.Fatal("expected add to change group membership")
	}
	if got := strings.TrimSpace(mustReadFile(t, groupsPath)); got != "alloy docker" {
		t.Fatalf("expected alloy docker groups, got %q", got)
	}
	if got := mustReadFile(t, logPath); !strings.Contains(got, "usermod -aG docker alloy") {
		t.Fatalf("expected usermod call, got %q", got)
	}

	changed, err = s.reconcileAlloyDockerGroup(context.Background(), desiredState{
		Features: featureFlags{
			"containerMonitor": json.RawMessage(`false`),
			"dockerLogs":       json.RawMessage(`false`),
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !changed {
		t.Fatal("expected remove to change group membership")
	}
	if got := strings.TrimSpace(mustReadFile(t, groupsPath)); got != "alloy" {
		t.Fatalf("expected alloy group only, got %q", got)
	}
	if got := mustReadFile(t, logPath); !strings.Contains(got, "gpasswd -d alloy docker") {
		t.Fatalf("expected gpasswd call, got %q", got)
	}
}

func TestRedactAndLimitMasksSecrets(t *testing.T) {
	got := redactAndLimit("run --token secret password=hunter2 ok", 200)
	if strings.Contains(got, "secret") || strings.Contains(got, "hunter2") {
		t.Fatalf("expected secrets to be redacted, got %q", got)
	}
}

func TestDownloadFileRejectsOversizedContentLength(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "536870913")
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	s := &supervisor{httpClient: server.Client()}
	err := s.downloadFile(context.Background(), server.URL, filepath.Join(t.TempDir(), "artifact"))
	if err == nil || !strings.Contains(err.Error(), "download too large") {
		t.Fatalf("expected oversized download rejection, got %v", err)
	}
}

func TestApplyAlloyHTTPCallsEndpointsWithoutAuthAndRetries(t *testing.T) {
	var reloadCalls int32
	var readyCalls int32
	var healthyCalls int32

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "" {
			t.Errorf("expected no authorization header, got %q", got)
		}
		if _, _, ok := r.BasicAuth(); ok {
			t.Error("expected no basic auth credentials")
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read request body: %v", err)
		}
		if len(body) != 0 {
			t.Errorf("expected empty request body, got %q", string(body))
		}

		switch r.URL.Path {
		case "/-/reload":
			if r.Method != http.MethodPost {
				t.Errorf("expected POST reload, got %s", r.Method)
			}
			atomic.AddInt32(&reloadCalls, 1)
			w.WriteHeader(http.StatusNoContent)
		case "/-/ready":
			if r.Method != http.MethodGet {
				t.Errorf("expected GET ready, got %s", r.Method)
			}
			attempt := atomic.AddInt32(&readyCalls, 1)
			if attempt == 1 {
				w.WriteHeader(http.StatusServiceUnavailable)
				_, _ = w.Write([]byte("warming up"))
				return
			}
			w.WriteHeader(http.StatusOK)
		case "/-/healthy":
			if r.Method != http.MethodGet {
				t.Errorf("expected GET healthy, got %s", r.Method)
			}
			atomic.AddInt32(&healthyCalls, 1)
			w.WriteHeader(http.StatusOK)
		default:
			t.Errorf("unexpected path %q", r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	s := &supervisor{
		cfg:        config{Username: "nova-user", Password: "nova-password"},
		httpClient: server.Client(),
	}
	hook := &alloyHTTPDesiredState{
		BaseURL: server.URL,
		Reload:  true,
		Ready:   true,
		Healthy: true,
		Timeout: durationJSON{Duration: 2 * time.Second},
	}
	if err := s.applyAlloyHTTP(context.Background(), hook); err != nil {
		t.Fatal(err)
	}
	if got := atomic.LoadInt32(&reloadCalls); got != 1 {
		t.Fatalf("expected one reload call, got %d", got)
	}
	if got := atomic.LoadInt32(&readyCalls); got < 2 {
		t.Fatalf("expected ready to retry, got %d call(s)", got)
	}
	if got := atomic.LoadInt32(&healthyCalls); got != 1 {
		t.Fatalf("expected one healthy call, got %d", got)
	}
}

func TestApplyAlloyHTTPPreservesBasePathPrefix(t *testing.T) {
	var readyCalls int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/prefix/-/ready" {
			t.Errorf("expected prefixed ready path, got %q", r.URL.Path)
			http.NotFound(w, r)
			return
		}
		atomic.AddInt32(&readyCalls, 1)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	s := &supervisor{httpClient: server.Client()}
	hook := &alloyHTTPDesiredState{
		BaseURL: server.URL + "/prefix",
		Ready:   true,
		Timeout: durationJSON{Duration: time.Second},
	}
	if err := s.applyAlloyHTTP(context.Background(), hook); err != nil {
		t.Fatal(err)
	}
	if got := atomic.LoadInt32(&readyCalls); got != 1 {
		t.Fatalf("expected one ready call, got %d", got)
	}
}

func TestApplyAlloyHTTPReloadFailureStopsBeforeChecks(t *testing.T) {
	var reloadCalls int32
	var readyCalls int32
	var healthyCalls int32

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/-/reload":
			atomic.AddInt32(&reloadCalls, 1)
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = w.Write([]byte("reload failed password=hunter2"))
		case "/-/ready":
			atomic.AddInt32(&readyCalls, 1)
			w.WriteHeader(http.StatusOK)
		case "/-/healthy":
			atomic.AddInt32(&healthyCalls, 1)
			w.WriteHeader(http.StatusOK)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	s := &supervisor{httpClient: server.Client()}
	hook := &alloyHTTPDesiredState{
		BaseURL: server.URL,
		Reload:  true,
		Ready:   true,
		Healthy: true,
		Timeout: durationJSON{Duration: time.Second},
	}
	err := s.applyAlloyHTTP(context.Background(), hook)
	if err == nil || !strings.Contains(err.Error(), "reload status=500") {
		t.Fatalf("expected reload failure, got %v", err)
	}
	if strings.Contains(err.Error(), "hunter2") {
		t.Fatalf("expected response body to be redacted, got %v", err)
	}
	if got := atomic.LoadInt32(&reloadCalls); got != 1 {
		t.Fatalf("expected one reload call, got %d", got)
	}
	if got := atomic.LoadInt32(&readyCalls); got != 0 {
		t.Fatalf("expected ready not to run after reload failure, got %d", got)
	}
	if got := atomic.LoadInt32(&healthyCalls); got != 0 {
		t.Fatalf("expected healthy not to run after reload failure, got %d", got)
	}
}

func TestApplyAlloyHTTPNoopDoesNotCallServer(t *testing.T) {
	var calls int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	s := &supervisor{httpClient: server.Client()}
	if err := s.applyAlloyHTTP(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if err := s.applyAlloyHTTP(context.Background(), &alloyHTTPDesiredState{BaseURL: server.URL}); err != nil {
		t.Fatal(err)
	}
	if got := atomic.LoadInt32(&calls); got != 0 {
		t.Fatalf("expected no HTTP calls, got %d", got)
	}
}

func TestApplyAlloyHTTPReadyTimeoutIncludesStatusAndBody(t *testing.T) {
	var readyCalls int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/-/ready" {
			http.NotFound(w, r)
			return
		}
		atomic.AddInt32(&readyCalls, 1)
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("warming up"))
	}))
	defer server.Close()

	s := &supervisor{httpClient: server.Client()}
	hook := &alloyHTTPDesiredState{
		BaseURL: server.URL,
		Ready:   true,
		Timeout: durationJSON{Duration: 50 * time.Millisecond},
	}
	err := s.applyAlloyHTTP(context.Background(), hook)
	if err == nil {
		t.Fatal("expected ready timeout")
	}
	for _, want := range []string{"ready timed out", "last status=503", "warming up"} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("expected error to contain %q, got %v", want, err)
		}
	}
	if got := atomic.LoadInt32(&readyCalls); got == 0 {
		t.Fatal("expected at least one ready request")
	}
}

func TestReadStateRejectsGroupReadableFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	state := persistedState{
		Runtime: runtimeConfig{
			NovaUIURL: "https://nova.example.com",
			NodeID:    "node-1",
			Username:  "user",
			Password:  "pass",
		},
	}
	if err := writeState(path, state); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0640); err != nil {
		t.Fatal(err)
	}
	if _, err := readState(path); err == nil || !strings.Contains(err.Error(), "permissions") {
		t.Fatalf("expected state permission rejection, got %v", err)
	}
}

func TestNormalizeAlloyVersionAddsVToStableSemver(t *testing.T) {
	for _, tc := range []struct {
		in   string
		want string
	}{
		{in: "1.11.0", want: "v1.11.0"},
		{in: "v1.11.0", want: "v1.11.0"},
		{in: "latest", want: "latest"},
	} {
		if got := normalizeAlloyVersion(tc.in); got != tc.want {
			t.Fatalf("normalizeAlloyVersion(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestPollDesiredStateSendsNormalizedAlloyVersion(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("version"); got != "v1.11.0" {
			t.Fatalf("expected normalized version query, got %q", got)
		}
		if got := r.URL.Query().Get("supervisor_version"); got != buildVersion {
			t.Fatalf("expected supervisor version query, got %q", got)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	s := &supervisor{
		cfg: config{
			NovaUIURL:   server.URL,
			DesiredPath: "/desired",
			NodeID:      "node-1",
			PollTimeout: time.Second,
		},
		httpClient: server.Client(),
		state: persistedState{
			AlloyVersion: "1.11.0",
		},
	}

	if _, _, err := s.pollDesiredState(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestReportSupervisorVersionPostsBuildVersionAndPlatform(t *testing.T) {
	originalBuildVersion := buildVersion
	buildVersion = "0.9.0"
	defer func() { buildVersion = originalBuildVersion }()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/agent/supervisor/version/node-1/0.9.0" {
			t.Fatalf("expected supervisor version path, got %q", r.URL.Path)
		}
		if got := r.URL.Query().Get("supervisor_arch"); got == "" {
			t.Fatal("expected supervisor_arch query")
		}
		if got := r.URL.Query().Get("supervisor_os"); got == "" {
			t.Fatal("expected supervisor_os query")
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	s := &supervisor{
		cfg: config{
			NovaUIURL: server.URL,
			NodeID:    "node-1",
			StatePath: filepath.Join(t.TempDir(), "state.json"),
		},
		httpClient: server.Client(),
		state: persistedState{
			SupervisorVersion: "0.8.0",
		},
	}

	if err := s.reportSupervisorVersion(context.Background()); err != nil {
		t.Fatal(err)
	}
	if s.state.SupervisorVersion != "0.9.0" {
		t.Fatalf("expected persisted supervisor version, got %q", s.state.SupervisorVersion)
	}
}

func TestReportAlloyVersionPostsNormalizedVersion(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/agent/version/node-1/v1.11.0" {
			t.Fatalf("expected normalized version path, got %q", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	s := &supervisor{
		cfg: config{
			NovaUIURL: server.URL,
			NodeID:    "node-1",
			StatePath: filepath.Join(t.TempDir(), "state.json"),
		},
		httpClient: server.Client(),
		state: persistedState{
			AlloyVersion: "1.11.0",
		},
	}

	if err := s.reportAlloyVersion(context.Background()); err != nil {
		t.Fatal(err)
	}
	if s.state.AlloyVersion != "v1.11.0" {
		t.Fatalf("expected normalized persisted version, got %q", s.state.AlloyVersion)
	}
}

func testSupervisor(allowActions bool, allowedTools []string) *supervisor {
	return &supervisor{
		cfg: config{
			AllowActions: allowActions,
			AllowedTools: toolsMap(allowedTools),
		},
	}
}

func validDesiredState() desiredState {
	hash := strings.Repeat("a", 64)
	return desiredState{
		Version: "v1",
		Alloy: alloyDesiredState{
			Version: "v1.11.0",
			Update:  true,
			Restart: true,
			SHA256:  hash,
		},
		Supervisor: supervisorDesired{
			Version:      "0.2.0",
			ManualUpdate: true,
			AssetURL:     "https://example.com/nova-supervisor-linux-amd64",
			SHA256:       hash,
			Restart:      true,
		},
		Actions: []desiredAction{
			{ID: "restart-alloy", Tool: "systemctl", Args: []string{"restart", "alloy.service"}, RunOnce: true},
		},
	}
}

func installFakeGroupTools(t *testing.T, initialGroups string) (string, string) {
	t.Helper()
	dir := t.TempDir()
	groupsPath := filepath.Join(dir, "groups")
	logPath := filepath.Join(dir, "tools.log")
	if err := os.WriteFile(groupsPath, []byte(initialGroups+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	writeExecutable(t, filepath.Join(dir, "id"), `#!/bin/sh
if [ "$1" = "-nG" ] && [ "$2" = "alloy" ]; then
  cat "$NOVA_TEST_GROUPS"
  exit 0
fi
exit 1
`)
	writeExecutable(t, filepath.Join(dir, "usermod"), `#!/bin/sh
echo "usermod $*" >> "$NOVA_TEST_LOG"
printf "alloy docker\n" > "$NOVA_TEST_GROUPS"
`)
	writeExecutable(t, filepath.Join(dir, "gpasswd"), `#!/bin/sh
echo "gpasswd $*" >> "$NOVA_TEST_LOG"
printf "alloy\n" > "$NOVA_TEST_GROUPS"
`)
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("NOVA_TEST_GROUPS", groupsPath)
	t.Setenv("NOVA_TEST_LOG", logPath)
	return groupsPath, logPath
}

func writeExecutable(t *testing.T, path, contents string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(contents), 0755); err != nil {
		t.Fatal(err)
	}
}

func mustReadFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}
