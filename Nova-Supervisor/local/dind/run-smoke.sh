#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/var/nova-supervisor-local"
TOOL_DIR=/usr/local/bin/nova-supervisor-test-tools
TOOL_LOG=/tmp/nova-supervisor-tool.log
ALLOY_HTTP_LOG=/tmp/nova-supervisor-alloy-http.log

if [[ "${EUID}" -ne 0 ]]; then
  echo "run this through Docker Compose, or as root if you are intentionally running it on the host:" >&2
  echo "  docker compose -f local/dind/compose.yml up --build --abort-on-container-exit --exit-code-from nova-supervisor-dind" >&2
  exit 1
fi

mkdir -p "$STATE_DIR"
chmod 0777 "${SCRIPT_DIR}/var" "$STATE_DIR"
rm -f "${STATE_DIR}"/*.json "$TOOL_LOG" "$ALLOY_HTTP_LOG"

groupadd -f docker
if ! id alloy >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/sh alloy
fi

mkdir -p "$TOOL_DIR"
cat > "${TOOL_DIR}/systemctl" <<'SH'
#!/usr/bin/env bash
echo "systemctl $*" >> /tmp/nova-supervisor-tool.log
exit 0
SH

for tool in kubectl helm argocd; do
  cat > "${TOOL_DIR}/${tool}" <<'SH'
#!/usr/bin/env bash
echo "$(basename "$0") $*" >> /tmp/nova-supervisor-tool.log
exit 42
SH
done
chmod 0755 "${TOOL_DIR}/systemctl" "${TOOL_DIR}/kubectl" "${TOOL_DIR}/helm" "${TOOL_DIR}/argocd"
export PATH="${TOOL_DIR}:${PATH}"
command -v systemctl >/dev/null

write_state() {
  local node_id="$1"
  local allowed_tools_json="$2"
  local state_path="$3"

  cat > "$state_path" <<JSON
{
  "runtime": {
    "nova_ui_url": "http://127.0.0.1:18080",
    "desired_path": "/agent/supervisor/desired",
    "node_id": "${node_id}",
    "username": "user",
    "password": "pass",
    "alloy_service": "alloy",
    "supervisor_service": "nova-supervisor",
    "allow_actions": true,
    "allow_self_update": false,
    "allow_alloy_update": false,
    "allowed_tools": ${allowed_tools_json},
    "poll_timeout": "5s",
    "poll_interval": "1s",
    "http_timeout": "10s"
  }
}
JSON
  chmod 0600 "$state_path"
}

run_success_case() {
  local name="$1"
  local node_id="$2"
  local allowed_tools_json="$3"
  local state_path="${STATE_DIR}/${name}.json"

  write_state "$node_id" "$allowed_tools_json" "$state_path"
  echo "case ${name}: expecting success"
  nova-supervisor -state-path "$state_path" -once
}

run_reject_case() {
  local name="$1"
  local node_id="$2"
  local allowed_tools_json="$3"
  local expected="$4"
  local forbidden_tool="$5"
  local state_path="${STATE_DIR}/${name}.json"
  local output
  local before_count=0
  local after_count=0

  write_state "$node_id" "$allowed_tools_json" "$state_path"
  echo "case ${name}: expecting rejection"
  if [[ -n "$forbidden_tool" ]] && [[ -f "$TOOL_LOG" ]]; then
    before_count="$(grep -c "^${forbidden_tool} " "$TOOL_LOG" || true)"
  fi
  if output="$(nova-supervisor -state-path "$state_path" -once 2>&1)"; then
    echo "expected ${name} to fail" >&2
    exit 1
  fi
  echo "$output"
  if ! grep -Fq "$expected" <<<"$output"; then
    echo "expected ${name} output to contain: ${expected}" >&2
    exit 1
  fi
  if [[ -n "$forbidden_tool" ]] && [[ -f "$TOOL_LOG" ]]; then
    after_count="$(grep -c "^${forbidden_tool} " "$TOOL_LOG" || true)"
  fi
  if [[ "$after_count" != "$before_count" ]]; then
    echo "expected ${forbidden_tool} not to execute in ${name}" >&2
    exit 1
  fi
}

python3 /usr/local/bin/fake-nova.py &
fake_nova_pid=$!

python3 - <<'PY' &
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

LOG = "/tmp/nova-supervisor-alloy-http.log"
ready_calls = 0


class Handler(BaseHTTPRequestHandler):
    def record(self, entry):
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(entry + "\n")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/-/reload":
            self.record("POST /-/reload")
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        global ready_calls
        path = urlparse(self.path).path
        if path == "/-/ready":
            ready_calls += 1
            self.record("GET /-/ready")
            if ready_calls == 1:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"warming up")
                return
            self.send_response(200)
            self.end_headers()
            return
        if path == "/-/healthy":
            self.record("GET /-/healthy")
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        print("fake-alloy-http:", fmt % args, flush=True)


HTTPServer(("127.0.0.1", 18081), Handler).serve_forever()
PY
fake_alloy_http_pid=$!

dockerd --host=unix:///var/run/docker.sock >/tmp/dockerd.log 2>&1 &
dockerd_pid=$!

cleanup() {
  kill "$fake_nova_pid" "$fake_alloy_http_pid" "$dockerd_pid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker info >/dev/null 2>&1; then
  echo "dockerd did not become ready" >&2
  tail -100 /tmp/dockerd.log >&2 || true
  exit 1
fi

echo "before: $(id -nG alloy)"
run_success_case "docker-enabled" "node-docker-enabled" '[]'
echo "after: $(id -nG alloy)"

if ! id -nG alloy | tr ' ' '\n' | grep -qx docker; then
  echo "expected alloy to be in docker group" >&2
  exit 1
fi

runuser -u alloy -- docker ps >/dev/null
echo "ok: alloy is in docker group and can query the DinD daemon"

run_success_case "docker-disabled" "node-docker-disabled" '[]'
if id -nG alloy | tr ' ' '\n' | grep -qx docker; then
  echo "expected alloy to be removed from docker group" >&2
  exit 1
fi
echo "ok: alloy is removed from docker group when Docker features are disabled"

rm -f "$TOOL_LOG"
run_success_case "systemctl" "node-systemctl" '["systemctl"]'
if ! grep -qx "systemctl restart alloy.service" "$TOOL_LOG"; then
  echo "expected systemctl restart alloy.service to execute through the fake systemctl" >&2
  exit 1
fi

rm -f "$ALLOY_HTTP_LOG"
run_success_case "alloy-http" "node-alloy-http" '[]'
if [[ ! -f "$ALLOY_HTTP_LOG" ]]; then
  echo "expected Alloy HTTP hook log to be created" >&2
  exit 1
fi
reload_count="$(grep -cx "POST /-/reload" "$ALLOY_HTTP_LOG" || true)"
ready_count="$(grep -cx "GET /-/ready" "$ALLOY_HTTP_LOG" || true)"
healthy_count="$(grep -cx "GET /-/healthy" "$ALLOY_HTTP_LOG" || true)"
if [[ "$reload_count" != "1" ]]; then
  echo "expected one Alloy reload call, got ${reload_count}" >&2
  exit 1
fi
if (( ready_count < 2 )); then
  echo "expected Alloy ready to be retried, got ${ready_count}" >&2
  exit 1
fi
if [[ "$healthy_count" != "1" ]]; then
  echo "expected one Alloy healthy call, got ${healthy_count}" >&2
  exit 1
fi

run_reject_case "systemctl-reject" "node-systemctl-reject" '["systemctl"]' 'systemctl command "enable" is not allowed' "systemctl"
run_reject_case "default-deny" "node-default-deny" '[]' 'tool "systemctl" is not allowed' "systemctl"
run_reject_case "kubectl" "node-kubectl" '["kubectl"]' 'kubectl actions require a narrower action implementation' "kubectl"
run_reject_case "helm" "node-helm" '["helm"]' 'helm actions require a narrower action implementation' "helm"
run_reject_case "argocd" "node-argocd" '["argocd"]' 'argocd actions require a narrower action implementation' "argocd"

echo "ok: tool policy smoke tests passed"
