#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'USAGE'
Build the local NovaUI image with Nova Supervisor binaries embedded.

Usage:
  ./local-build.sh 0.4.0
  SUPERVISOR_VERSION=0.4.0 ./local-build.sh

Optional environment overrides:
  IMAGE_TAG=novaui:local
  SUPERVISOR_REPO=../Nova-Supervisor
  SUPERVISOR_VERSION=0.4.0
  SUPERVISOR_BINARY_DIR=agent/binaries
  ARCHIVE_SUPERVISOR_BINARIES=1
  REGISTER_SUPERVISOR_VERSION=0
  DOCKER_PLATFORM=linux/amd64

Supervisor versions must be SemVer without a leading "v", for example:
  0.4.0
  1.2.3-rc.1
  1.2.3+build.4

With ARCHIVE_SUPERVISOR_BINARIES=1, this script stores each built version in
agent/binaries and rewrites SHA256SUMS for all stored versions. Older versions
remain available to NovaUI's /agent/supervisor/binary/<version>/<arch> endpoint.

With REGISTER_SUPERVISOR_VERSION=1, this script also upserts the local DB
SupervisorVersion and amd64/arm64/ppc64le/s390x artifacts.

Verify the built image:
  docker run --rm --entrypoint sh novaui:local \
    -c 'ls -l /app/agent/binaries && cat /app/agent/binaries/SHA256SUMS'
USAGE
  exit 0
fi

if [[ $# -gt 1 ]]; then
  echo "error: expected at most one positional supervisor version" >&2
  exit 2
fi

if [[ $# -eq 1 ]]; then
  SUPERVISOR_VERSION="$1"
fi

IMAGE_TAG="${IMAGE_TAG:-novaui:local}"
SUPERVISOR_REPO="${SUPERVISOR_REPO:-${ROOT_DIR}/../Nova-Supervisor}"
SUPERVISOR_VERSION="${SUPERVISOR_VERSION:-0.0.0-local}"
SUPERVISOR_BINARY_DIR="${SUPERVISOR_BINARY_DIR:-${ROOT_DIR}/agent/binaries}"
ARCHIVE_SUPERVISOR_BINARIES="${ARCHIVE_SUPERVISOR_BINARIES:-1}"
REGISTER_SUPERVISOR_VERSION="${REGISTER_SUPERVISOR_VERSION:-0}"

if [[ "$SUPERVISOR_BINARY_DIR" != /* ]]; then
  SUPERVISOR_BINARY_DIR="${ROOT_DIR}/${SUPERVISOR_BINARY_DIR}"
fi

if [[ ! -x "${SUPERVISOR_REPO}/build-linux.sh" ]]; then
  echo "error: Supervisor build script not found: ${SUPERVISOR_REPO}/build-linux.sh" >&2
  exit 2
fi
SUPERVISOR_REPO="$(cd "$SUPERVISOR_REPO" && pwd)"

SEMVER_RE='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-((0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?(\+([0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*))?$'
if [[ ! "$SUPERVISOR_VERSION" =~ $SEMVER_RE ]]; then
  echo "error: SUPERVISOR_VERSION must be SemVer without a leading v, got: ${SUPERVISOR_VERSION}" >&2
  exit 2
fi

if [[ "$ARCHIVE_SUPERVISOR_BINARIES" == "1" ]]; then
  mkdir -p "$SUPERVISOR_BINARY_DIR"
  DIST_DIR="$SUPERVISOR_BINARY_DIR" VERSION="$SUPERVISOR_VERSION" "${SUPERVISOR_REPO}/build-linux.sh"
  (
    cd "$SUPERVISOR_BINARY_DIR"
    sha256sum nova-supervisor-*-linux-* > SHA256SUMS
  )
  echo "Stored supervisor binaries in ${SUPERVISOR_BINARY_DIR}"
  echo "Stored versions:"
  find "$SUPERVISOR_BINARY_DIR" -maxdepth 1 -type f -name 'nova-supervisor-*-linux-amd64' \
    -exec basename {} \; | sed -E 's/^nova-supervisor-(.*)-linux-amd64$/  \1/' | sort
fi

if [[ "$REGISTER_SUPERVISOR_VERSION" == "1" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="${PYTHON_BIN:-python3}"
  fi
  (
    cd "$ROOT_DIR"
    SUPERVISOR_VERSION="$SUPERVISOR_VERSION" \
    SUPERVISOR_BINARY_DIR="$SUPERVISOR_BINARY_DIR" \
    "$PYTHON_BIN" manage.py shell -c '
import os
from pathlib import Path
from config.models import SupervisorVersion, SupervisorVersionArtifact

version = os.environ["SUPERVISOR_VERSION"]
binary_dir = Path(os.environ["SUPERVISOR_BINARY_DIR"])
release, _ = SupervisorVersion.objects.update_or_create(version=version, defaults={"enabled": True})
for line in (binary_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
    parts = line.split()
    if len(parts) < 2:
        continue
    sha, filename = parts[0], Path(parts[1]).name
    prefix = f"nova-supervisor-{version}-linux-"
    if not filename.startswith(prefix):
        continue
    arch = filename.removeprefix(prefix)
    SupervisorVersionArtifact.objects.update_or_create(
        version=release,
        arch=arch,
        defaults={"sha256": sha, "enabled": True},
    )
print(f"Registered supervisor {version} with {release.artifacts.count()} artifact(s).")
'
  )
fi

IMAGE_TAG="$IMAGE_TAG" \
SUPERVISOR_REPO="$SUPERVISOR_REPO" \
SUPERVISOR_VERSION="$SUPERVISOR_VERSION" \
"${ROOT_DIR}/scripts/build-image-with-supervisor.sh"
