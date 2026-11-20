#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" != --force || $# -gt 2 ]]; then
  echo "Usage: $0 --force [cluster-name]" >&2
  echo 'Deletes the named kind cluster and all its data, then recreates it.' >&2
  exit 2
fi
CLUSTER_NAME="${2:-${KIND_CLUSTER_NAME:-argo}}"
for command in kind kubectl helm; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 127; }
done
[[ -f "${KIND_CONFIG:-$SCRIPT_DIR/kind-config.yaml}" ]] || { echo 'Missing kind config.' >&2; exit 2; }
kind delete cluster --name "$CLUSTER_NAME"
exec "$SCRIPT_DIR/create-kind.sh" "$CLUSTER_NAME"
