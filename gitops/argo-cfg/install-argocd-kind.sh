#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == '--help' ]]; then
  echo "Usage: $0 [kind-cluster-name] (default: argo)"
  exit 0
fi
[[ $# -le 1 ]] || { echo 'Expected at most one cluster name.' >&2; exit 2; }
CLUSTER_NAME="${1:-${KIND_CLUSTER_NAME:-argo}}"
export KUBE_CONTEXT="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
export ARGOCD_HELM_VALUES="${ARGOCD_HELM_VALUES:-$SCRIPT_DIR/values-kind.yaml}"
exec "$SCRIPT_DIR/install-argocd.sh"
