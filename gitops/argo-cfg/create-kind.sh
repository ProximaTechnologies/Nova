#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == '--help' ]]; then
  echo "Usage: $0 [cluster-name] (default: argo); creates a new kind development cluster."
  exit 0
fi
[[ $# -le 1 ]] || { echo 'Expected at most one cluster name.' >&2; exit 2; }
CLUSTER_NAME="${1:-${KIND_CLUSTER_NAME:-argo}}"
KUBE_CONTEXT="kind-$CLUSTER_NAME"
export KUBE_CONTEXT
KUBECTL=(kubectl --context "$KUBE_CONTEXT")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIND_CONFIG="${KIND_CONFIG:-$SCRIPT_DIR/kind-config.yaml}"
NOVA_CFG_IMAGE="${NOVA_CFG_IMAGE:-}"
NOVA_UI_IMAGE="${NOVA_UI_IMAGE:-}"
RUN_DEPLOYMENT_CHECK="${RUN_DEPLOYMENT_CHECK:-true}"
DEPLOYMENT_WAIT_TIMEOUT="${DEPLOYMENT_WAIT_TIMEOUT:-1800}"
DEPLOYMENT_WAIT_INTERVAL="${DEPLOYMENT_WAIT_INTERVAL:-20}"

configure_demo_dns() {
  local namespace="kube-system"
  local configmap="coredns"
  local gateway_host="apisix-gateway.apisix.svc.cluster.local"
  local corefile
  local updated_corefile
  local hosts=(
    argocd.domain.com
    apisix.domain.com
    cfg.domain.com
    forgejo.domain.com
    grafana.domain.com
    loki.domain.com
    mimir.domain.com
    nova.domain.com
  )

  corefile="$(mktemp)"
  updated_corefile="$(mktemp)"

  "${KUBECTL[@]}" -n "$namespace" get configmap "$configmap" -o jsonpath='{.data.Corefile}' >"$corefile"

  if ! awk -v hosts="${hosts[*]}" -v gateway_host="$gateway_host" '
    /# BEGIN Nova demo domain rewrites/ {
      skip = 1
      next
    }
    /# END Nova demo domain rewrites/ {
      skip = 0
      next
    }
    skip {
      next
    }
    /^[[:space:]]*kubernetes[[:space:]]+cluster\.local/ && !inserted {
      indent = substr($0, 1, match($0, /[^[:space:]]/) - 1)
      print indent "# BEGIN Nova demo domain rewrites"
      host_count = split(hosts, host_list, " ")
      for (i = 1; i <= host_count; i++) {
        print indent "rewrite stop name exact " host_list[i] " " gateway_host
      }
      print indent "# END Nova demo domain rewrites"
      inserted = 1
    }
    {
      print
    }
    END {
      if (!inserted) {
        exit 1
      }
    }
  ' "$corefile" >"$updated_corefile"; then
    echo "failed to patch CoreDNS Corefile with Nova demo DNS rewrites" >&2
    rm -f "$corefile" "$updated_corefile"
    exit 1
  fi

  "${KUBECTL[@]}" -n "$namespace" create configmap "$configmap" \
    --from-file=Corefile="$updated_corefile" \
    --dry-run=client \
    -o yaml | "${KUBECTL[@]}" apply -f -

  "${KUBECTL[@]}" -n "$namespace" rollout restart deployment/coredns
  "${KUBECTL[@]}" -n "$namespace" rollout status deployment/coredns --timeout=2m

  rm -f "$corefile" "$updated_corefile"
}

for command in kind kubectl helm; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 127; }
done
[[ -f "$KIND_CONFIG" ]] || { echo "Missing kind config: $KIND_CONFIG" >&2; exit 2; }
if kind get clusters | grep -Fxq -- "$CLUSTER_NAME"; then
  echo "Cluster '$CLUSTER_NAME' already exists. Use install-argocd-kind.sh or reset.sh --force." >&2
  exit 2
fi
kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG"
configure_demo_dns
"$SCRIPT_DIR/install-argocd-kind.sh" "$CLUSTER_NAME"
if [[ -n "$NOVA_CFG_IMAGE" ]]; then kind load docker-image "$NOVA_CFG_IMAGE" --name "$CLUSTER_NAME"; fi
if [[ -n "$NOVA_UI_IMAGE" ]]; then kind load docker-image "$NOVA_UI_IMAGE" --name "$CLUSTER_NAME"; fi

if [[ "$RUN_DEPLOYMENT_CHECK" == "true" ]]; then
  "$SCRIPT_DIR/../scripts/test-deployments.sh" \
    --context "$KUBE_CONTEXT" \
    --include-root \
    --env dev \
    --wait "$DEPLOYMENT_WAIT_TIMEOUT" \
    --interval "$DEPLOYMENT_WAIT_INTERVAL"
fi
