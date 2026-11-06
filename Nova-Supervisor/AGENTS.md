# AGENTS.md

## Project

Nova Supervisor is a small Go 1.22 Linux agent. It long-polls NovaUI for desired state, reconciles the local machine, manages local persisted state, and can update Alloy or itself when explicitly allowed by NovaUI.

Most production behavior is in `main.go`; unit tests are in `main_test.go`. The public bootstrap, desired-state, version-reporting, and binary-serving contracts are documented in `README.md`.

## Commands

- Run tests: `go test ./...`
- Format Go files: `gofmt -w main.go main_test.go`
- Run once after a local enrollment state exists: `go run . -once`
- Run once with a custom state path: `go run . -state-path /path/to/state.json -once`
- Build Linux release artifacts: `VERSION=local ./build-linux.sh`
- Run the Docker-in-Docker smoke test from the repository root:
  `docker compose -f local/dind/compose.yml up --build --abort-on-container-exit --exit-code-from nova-supervisor-dind`

`build-linux.sh` writes generated artifacts under `dist/`. Treat `dist/` as build output unless the user explicitly asks to package or inspect it.

## Code Guidelines

- Keep the service as a single small Go binary unless the change clearly benefits from extraction.
- Prefer standard-library Go. This module currently has no third-party dependencies.
- Run `gofmt` on modified Go files.
- Add or update focused tests in `main_test.go` when changing validation, state persistence, polling, update behavior, command execution, or security boundaries.
- Keep user-facing logs useful but avoid leaking credentials, tokens, passwords, authorization headers, signatures, access keys, or full untrusted command output.

## Safety Boundaries

- Remote actions must not run through a shell.
- Keep the default action policy deny-by-default when `allowed_tools` is omitted or empty.
- Preserve local argument validation for supported tools, especially `systemctl`.
- Keep `kubectl`, `helm`, and `argocd` rejected until narrower action implementations exist.
- Preserve SHA-256 verification for downloaded Alloy and supervisor artifacts.
- Preserve state-file permission checks: state must not be group/world readable and must be owned by root or the current service user.
- Only allow HTTPS URLs except explicitly local development HTTP URLs.
- Be careful with self-update, package installation, user/group changes, and service restarts; these affect the host.

## Local Smoke Harness

The `local/dind` directory runs the supervisor in an Ubuntu 24.04 privileged Docker-in-Docker container against a fake NovaUI server. It verifies Docker group reconciliation, command execution behavior, and validation gates.

The Compose setup mounts `local/dind/alloy` as the test `alloy` executable. If the file is local or untracked, do not remove or overwrite it without explicit user direction.

