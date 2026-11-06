# Nova Supervisor

Small Go agent that long-polls NovaUI for node desired state and reconciles the local machine. It is installed by the NovaUI Linux installer script as `/usr/local/bin/nova-supervisor` with a `nova-supervisor.service` systemd unit.

Complete public JSON contracts for bootstrap, desired state, version reporting, local state, and binary serving are documented in `CONTRACTS.md`.

## Runtime

The service does not use a runtime config file or environment file. The NovaUI installer enrolls it once:

```bash
nova-supervisor bootstrap \
  -nova-ui-url "https://nova.example.com" \
  -node-id "<node uuid without dashes>" \
  -username "<collector config username>" \
  -password "<collector config password>" \
  -alloy-service alloy
```

Bootstrap calls NovaUI, stores the returned enrollment state in `/var/lib/nova-supervisor/state.json`, and systemd then runs the service with no flags:

```bash
/usr/local/bin/nova-supervisor
```

For local development after bootstrap, run once with:

```bash
go run . -once
```

To use a custom local state path:

```bash
go run . -state-path /path/to/state.json -once
```

## Installer Binary

NovaUI serves installer binaries from:

```text
GET /agent/supervisor/binary/<version>/<arch>
```

Supported Linux installer architectures are `amd64`, `arm64`, `ppc64le`, and `s390x`. See `CONTRACTS.md` for the exact lookup rules.

## Bootstrap, Polling, and Version Reports

Bootstrap fetches runtime settings from NovaUI with Basic Auth. The supervisor then long-polls the desired-state endpoint and reports both supervisor and Alloy versions back to NovaUI. Desired-state versions are persisted only after reconciliation succeeds, so failed updates, actions, or health checks are retried on the next changed poll.

The detailed request paths, query parameters, JSON fields, defaults, and validation rules are in `CONTRACTS.md`.

## Desired-State Overview

A desired state may request Alloy package updates, supervisor self-updates, validated local actions, Docker group reconciliation for Alloy, and Alloy local HTTP hook calls. Actions do not run through a shell. If `allowed_tools` is omitted or empty, remote actions are denied by default.

Example Alloy HTTP hook:

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

The hook is local-only. It can call `POST /-/reload`, retry `GET /-/ready`, and retry `GET /-/healthy` against a loopback Alloy HTTP server. Full semantics are documented under the Alloy HTTP Hook section of `CONTRACTS.md`.

## Local State

Enrollment and applied state are stored at `/var/lib/nova-supervisor/state.json` by default. The state file must not be group/world readable and must be owned by root or the current service user.
