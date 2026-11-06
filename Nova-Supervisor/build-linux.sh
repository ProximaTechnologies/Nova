#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="${DIST_DIR:-${ROOT_DIR}/dist}"
VERSION="${VERSION:-latest}"
ARCHES=(amd64 arm64 ppc64le s390x)

if [[ -z "${COMMIT:-}" ]]; then
  if command -v git >/dev/null 2>&1 && git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    COMMIT="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
  else
    COMMIT="unknown"
  fi
fi

if ! command -v go >/dev/null 2>&1; then
  echo "go is required but was not found on PATH" >&2
  exit 1
fi

mkdir -p "$DIST_DIR"
rm -f "$DIST_DIR"/nova-supervisor-"$VERSION"-linux-* "$DIST_DIR"/SHA256SUMS

echo "Building Nova Supervisor ${VERSION} (${COMMIT})"
echo "Output: ${DIST_DIR}"

for arch in "${ARCHES[@]}"; do
  output="${DIST_DIR}/nova-supervisor-${VERSION}-linux-${arch}"
  echo "  linux/${arch} -> ${output}"
  (
    cd "$ROOT_DIR"
    CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build \
      -trimpath \
      -ldflags "-s -w -X main.buildVersion=${VERSION} -X main.buildCommit=${COMMIT}" \
      -o "$output" .
  )
done

(
  cd "$DIST_DIR"
  sha256sum nova-supervisor-"$VERSION"-linux-* > SHA256SUMS
)

echo
echo "Built artifacts:"
ls -lh "$DIST_DIR"/nova-supervisor-"$VERSION"-linux-* "$DIST_DIR"/SHA256SUMS
