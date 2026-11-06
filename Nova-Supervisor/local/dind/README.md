# Nova Supervisor DinD Smoke Test

This runs the Linux supervisor binary in an Ubuntu 24.04 container with a local Docker daemon. A fake NovaUI desired-state endpoint serves one desired state per test case.

The happy-path Docker group case sends NovaUI feature flags:

```json
{"features": {"containerMonitor": true, "dockerLogs": false}}
```

The harness also validates the Alloy HTTP desired-state hook with a fake loopback Alloy server:

- `POST /-/reload` is called once.
- `GET /-/ready` returns `503` once and is retried until `200`.
- `GET /-/healthy` returns `200`.

The harness checks the local action policy for the rest of the current tool surface:

- `systemctl restart alloy.service` is accepted and executed through a fake `systemctl` shim.
- unsupported `systemctl` commands are rejected before execution.
- empty `allowed_tools` rejects actions by default.
- `kubectl`, `helm`, and `argocd` are rejected even when explicitly allowed, pending narrower action implementations.

Run from the repository root:

```bash
VERSION=local ./build-linux.sh
docker compose -f local/dind/compose.yml up --build --abort-on-container-exit --exit-code-from nova-supervisor-dind
```

The Compose service is privileged because Docker-in-Docker needs access to kernel features that are not available in a normal unprivileged container.

This test verifies Docker group reconciliation, Alloy HTTP hook behavior, command execution behavior, and local validation gates.
