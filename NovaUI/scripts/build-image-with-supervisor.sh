#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'USAGE'
Build a NovaUI image with Nova Supervisor Linux binaries embedded.

Usage:
  IMAGE_TAG=novaui:with-supervisor scripts/build-image-with-supervisor.sh

Environment:
  IMAGE_TAG                 Docker image tag to build.
                            Default: novaui:supervisor-${SUPERVISOR_VERSION}
  SUPERVISOR_REPO           Nova Supervisor repository path.
                            Default: ../Nova-Supervisor relative to NovaUI.
  SUPERVISOR_VERSION        Version embedded in the Supervisor binaries.
                            Default: VERSION or latest.
  SUPERVISOR_COMMIT         Commit embedded in the Supervisor binaries.
                            Default: git rev-parse --short HEAD in SUPERVISOR_REPO, or unknown.
  DOCKER_PLATFORM           Optional docker build --platform value, such as linux/amd64.
  DOCKERFILE                Dockerfile path.
                            Default: Dockerfile
  DOCKER                    Docker CLI.
                            Default: docker
USAGE
}

err() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || err "missing command: $1"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -gt 0 ]]; then
  err "unexpected arguments; use environment variables to configure this script"
fi

SUPERVISOR_REPO="${SUPERVISOR_REPO:-${ROOT_DIR}/../Nova-Supervisor}"
[[ -d "$SUPERVISOR_REPO" ]] || err "Supervisor repo not found: $SUPERVISOR_REPO"
SUPERVISOR_REPO="$(cd "$SUPERVISOR_REPO" && pwd)"
SUPERVISOR_VERSION="${SUPERVISOR_VERSION:-${VERSION:-latest}}"
IMAGE_TAG="${IMAGE_TAG:-novaui:supervisor-${SUPERVISOR_VERSION}}"
DOCKERFILE="${DOCKERFILE:-${ROOT_DIR}/Dockerfile}"
DOCKER="${DOCKER:-docker}"

[[ -f "${SUPERVISOR_REPO}/go.mod" ]] || err "Supervisor repo does not look like a Go module: $SUPERVISOR_REPO"
if [[ -z "${SUPERVISOR_COMMIT:-}" ]]; then
  if command -v git >/dev/null 2>&1 && git -C "$SUPERVISOR_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    SUPERVISOR_COMMIT="$(git -C "$SUPERVISOR_REPO" rev-parse --short HEAD)"
  else
    SUPERVISOR_COMMIT="unknown"
  fi
fi
need "$DOCKER"

docker_args=(
  build
  -f "$DOCKERFILE"
  -t "$IMAGE_TAG"
  --build-context "nova-supervisor=${SUPERVISOR_REPO}"
  --build-arg "NOVA_SUPERVISOR_VERSION=${SUPERVISOR_VERSION}"
  --build-arg "NOVA_SUPERVISOR_COMMIT=${SUPERVISOR_COMMIT}"
)

if [[ -n "${DOCKER_PLATFORM:-}" ]]; then
  docker_args+=(--platform "$DOCKER_PLATFORM")
fi

docker_args+=("$ROOT_DIR")

printf 'Building NovaUI image %s with Nova Supervisor %s (%s)\n' "$IMAGE_TAG" "$SUPERVISOR_VERSION" "$SUPERVISOR_COMMIT"
"$DOCKER" "${docker_args[@]}"

printf 'Built image: %s\n' "$IMAGE_TAG"
