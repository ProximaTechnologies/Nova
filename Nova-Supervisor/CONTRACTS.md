# Nova Supervisor Contracts

This is the canonical data-contract reference for Nova Supervisor. `README.md` stays focused on runtime usage and points here for complete JSON shapes.

## Installer Binary

NovaUI serves release binaries from:

```text
GET /agent/supervisor/binary/<version>/<arch>
```

By default, NovaUI reads `NOVA_SUPERVISOR_BINARY_DIR` and looks for files named:

```text
nova-supervisor-<version>-linux-<arch>
```

Supported Linux installer architectures are `amd64`, `arm64`, `ppc64le`, and `s390x`. For `latest`, NovaUI may also fall back to `nova-supervisor-linux-<arch>` and then `nova-supervisor`.

## Bootstrap

The supervisor bootstrap command calls:

```text
GET /agent/supervisor/bootstrap/<node_id>?alloy_service=alloy&supervisor_version=<version>&supervisor_os=linux&supervisor_arch=amd64
Authorization: Basic <config credentials>
Accept: application/json
```

NovaUI returns the runtime settings persisted by the supervisor:

```json
{
  "runtime": {
    "nova_ui_url": "https://nova.example.com",
    "desired_path": "/agent/supervisor/desired",
    "node_id": "<node uuid without dashes>",
    "username": "<collector config username>",
    "password": "<collector config password>",
    "alloy_service": "alloy",
    "supervisor_service": "nova-supervisor",
    "allow_actions": true,
    "allow_self_update": true,
    "allow_alloy_update": true,
    "allowed_tools": ["systemctl"],
    "poll_timeout": "55s",
    "poll_interval": "10s",
    "http_timeout": "65s"
  }
}
```

`nova_ui_url` must use HTTPS, except loopback HTTP URLs are allowed for local development. Duration fields accept Go duration strings such as `55s` or numeric seconds.

## Desired State

The supervisor long-polls NovaUI with:

```text
GET /agent/supervisor/desired/<node_id>?current_version=<last>&wait_seconds=55&supervisor_version=<version>&supervisor_os=linux&supervisor_arch=amd64&alloy_package_format=deb&version=<alloy-version>
Authorization: Basic <config credentials>
Accept: application/json
```

NovaUI should return `204 No Content` or `304 Not Modified` when there is no change. On change, return a desired state with a new non-empty `version`:

```json
{
  "version": "desired-state-version",
  "poll_after": "10s",
  "poll_after_seconds": 10,
  "alloy": {
    "version": "v1.11.0",
    "service": "alloy",
    "update": true,
    "restart": true,
    "sha256": "<hex sha256>",
    "package_url": "https://example.com/alloy.deb",
    "http": {
      "base_url": "http://127.0.0.1:12345",
      "reload": true,
      "ready": true,
      "healthy": true,
      "timeout": "15s"
    }
  },
  "supervisor": {
    "version": "0.2.0",
    "update": true,
    "manual_update": false,
    "asset_url": "https://example.com/nova-supervisor-linux-amd64",
    "sha256": "<hex sha256>",
    "restart": true
  },
  "features": {
    "containerMonitor": true,
    "dockerLogs": false
  },
  "metadata": {
    "containerMonitor": "true"
  },
  "actions": [
    {
      "id": "restart-alloy",
      "tool": "systemctl",
      "args": ["restart", "alloy.service"],
      "env": {},
      "working_dir": "/",
      "run_once": true,
      "timeout": "30s",
      "description": "Restart Alloy"
    }
  ]
}
```

All omitted object fields keep their zero-value behavior. Unknown JSON fields are rejected.

### Alloy Package Update

When `alloy.update` is true, `alloy.version` and `alloy.sha256` are required. `alloy.package_url` is optional; when omitted, the supervisor derives the Grafana Alloy release package URL from the requested version, local OS, architecture, and package format. Package downloads must use HTTPS, except loopback HTTP URLs are allowed for local development. Downloaded packages are verified with SHA-256 before installation.

### Alloy HTTP Hook

`alloy.http` is a first-class desired-state hook for Alloy's local HTTP server:

```json
{
  "version": "desired-state-version",
  "alloy": {
    "http": {
      "base_url": "http://127.0.0.1:12345",
      "reload": true,
      "ready": true,
      "healthy": true,
      "timeout": "15s"
    }
  }
}
```

Semantics:

- `base_url` is optional and defaults to `http://127.0.0.1:12345`.
- `reload: true` sends `POST <base_url>/-/reload` once with no request body and no NovaUI credentials.
- `ready: true` retries `GET <base_url>/-/ready` until a 2xx response or timeout.
- `healthy: true` retries `GET <base_url>/-/healthy` until a 2xx response or timeout.
- `timeout` is per endpoint, accepts the same duration string or numeric seconds syntax as other duration fields, defaults to `10s` when omitted or zero, and is capped at `2m`.
- Omitted `alloy.http`, or a block where `reload`, `ready`, and `healthy` are all false, is a no-op.
- `base_url` must use `http` or `https`, must not include userinfo, query, or fragment, and must have a loopback host such as `localhost`, `127.0.0.1`, `::1`, or another IP where `net.IP.IsLoopback()` is true.
- Optional path prefixes are allowed. For example, `http://localhost:12345/prefix` with `ready: true` calls `/prefix/-/ready`.
- Any non-2xx reload response or ready/healthy timeout fails reconciliation. The desired-state version is not persisted, so NovaUI can retry on the next poll.
- NovaUI can trigger another reload by returning a new desired-state `version` with `alloy.http.reload: true`.

The hook runs after Alloy package updates, generic actions, and Docker-group service restarts, and before supervisor self-update and version reporting.

### Actions

Actions do not run through a shell. The executable must be present in the `allowed_tools` list returned during bootstrap, and the supervisor applies local argument validation for supported tools. If `allowed_tools` is omitted or empty, remote actions are denied by default.

Current local policy:

- `systemctl` supports only validated service operations such as `restart`, `reload`, `try-reload-or-restart`, `status`, and `is-active` against safe `.service` unit names.
- `kubectl`, `helm`, and `argocd` are rejected even when explicitly allowed, pending narrower action implementations.

### Feature Semantics

If `features.containerMonitor` or `features.dockerLogs` is true, the supervisor adds the `alloy` user to the `docker` group and restarts Alloy so the service picks up the new group membership. If both flags are known and false, it removes the `alloy` user from the `docker` group and restarts Alloy. Older `metadata` keys with the same names are also accepted.

## Version Reporting

The supervisor periodically reports the running supervisor binary version and the detected Alloy version:

```text
POST /agent/supervisor/version/<node_id>/<supervisor-version>?supervisor_os=linux&supervisor_arch=amd64
POST /agent/version/<node_id>/<alloy-version>
Authorization: Basic <config credentials>
```

Version reports expect a 2xx response. Non-2xx responses are logged with redacted response body snippets.

## Local State

Enrollment and applied state are stored at `/var/lib/nova-supervisor/state.json` by default:

```json
{
  "runtime": {
    "nova_ui_url": "https://nova.example.com",
    "desired_path": "/agent/supervisor/desired",
    "node_id": "<node uuid without dashes>",
    "username": "<collector config username>",
    "password": "<collector config password>",
    "alloy_service": "alloy",
    "supervisor_service": "nova-supervisor",
    "allow_actions": true,
    "allow_self_update": true,
    "allow_alloy_update": true,
    "allowed_tools": ["systemctl"],
    "poll_timeout": "55s",
    "poll_interval": "10s",
    "http_timeout": "65s"
  },
  "desired_version": "desired-state-version",
  "applied_actions": {
    "restart-alloy": "desired-state-version"
  },
  "alloy_version": "v1.11.0",
  "supervisor_version": "0.2.0",
  "updated_at": "2026-01-01T00:00:00Z"
}
```

The state file must not be group/world readable and must be owned by root or the current service user. Override the state path only for local development.
