#!/usr/bin/env bash
# Install on an existing cluster without changing the current kube context.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
NAMESPACE="${ARGOCD_NAMESPACE:-argocd}"
HELM_RELEASE="${ARGOCD_HELM_RELEASE:-argocd}"
HELM_CHART_VERSION="${ARGOCD_HELM_CHART_VERSION:-9.5.15}"
VALUES_FILE="${ARGOCD_HELM_VALUES:-$SCRIPT_DIR/values.yaml}"
REPO_PASSWORD="${ARGOCD_REPO_PASSWORD:-${GITHUB_TOKEN:-}}"

if [[ "${1:-}" == '--help' ]]; then
  echo "Usage: KUBE_CONTEXT=<context> $0"
  echo "Optional: ARGOCD_HELM_VALUES, ARGOCD_HELM_CHART_VERSION, ARGOCD_HELM_TIMEOUT,"
  echo "ARGOCD_REPO_PASSWORD (or GITHUB_TOKEN), ARGOCD_REPO_USERNAME, ARGOCD_REPO_CREDS_URL."
  exit 0
fi
[[ $# -eq 0 ]] || { echo 'Unexpected arguments; use --help.' >&2; exit 2; }
[[ -n "$KUBE_CONTEXT" ]] || { echo 'Set KUBE_CONTEXT explicitly.' >&2; exit 2; }
# Namespaced resources and the Forgejo webhook refer to this namespace/release.
[[ "$NAMESPACE" == argocd && "$HELM_RELEASE" == argocd ]] || {
  echo 'This manifest bundle requires namespace and Helm release argocd.' >&2; exit 2;
}
for command in kubectl helm; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 127; }
done
[[ -f "$VALUES_FILE" ]] || { echo "Missing values file: $VALUES_FILE" >&2; exit 2; }
kubectl config get-contexts -o name | grep -Fxq -- "$KUBE_CONTEXT" || {
  echo "Unknown Kubernetes context: $KUBE_CONTEXT" >&2; exit 2;
}
KUBECTL=(kubectl --context "$KUBE_CONTEXT")
"${KUBECTL[@]}" get namespace kube-system >/dev/null

helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
helm repo update argo >/dev/null
helm upgrade --install "$HELM_RELEASE" argo/argo-cd \
  --kube-context "$KUBE_CONTEXT" --namespace "$NAMESPACE" --create-namespace \
  --version "$HELM_CHART_VERSION" --values "$VALUES_FILE" --wait \
  --timeout "${ARGOCD_HELM_TIMEOUT:-10m}"

# Register both the monorepo and the separately maintained Nova chart source.
"${KUBECTL[@]}" apply -f "$SCRIPT_DIR/repository.yaml"
if [[ -n "$REPO_PASSWORD" ]]; then
  b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
  # Default to this repository only. Set an organization URL explicitly if a
  # private NovaCharts repository must share the same credential template.
  repo_url="${ARGOCD_REPO_CREDS_URL:-$(awk '/    repoURL:/ {print $2; exit}' "$SCRIPT_DIR/root-app.yaml")}"
  # Generated manifests quote the URL as a JSON-compatible YAML scalar.
  if [[ "$repo_url" == \"* ]]; then
    repo_url="$(printf '%s' "$repo_url" | "${PYTHON:-python3}" -c 'import json, sys; print(json.load(sys.stdin))')"
  fi
  cat <<SECRET | "${KUBECTL[@]}" apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: nova-repo-creds
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repo-creds
type: Opaque
data:
  type: $(b64 git)
  url: $(b64 "$repo_url")
  username: $(b64 "${ARGOCD_REPO_USERNAME:-git}")
  password: $(b64 "$REPO_PASSWORD")
SECRET
fi
"${KUBECTL[@]}" apply -f "$SCRIPT_DIR/clusters.yaml"
"${KUBECTL[@]}" apply -f "$SCRIPT_DIR/root-app.yaml"
cat <<SUMMARY
Argo CD installed on context '$KUBE_CONTEXT'. Application reconciliation is asynchronous.
Check: KUBE_CONTEXT='$KUBE_CONTEXT' $SCRIPT_DIR/../scripts/test-deployments.sh --include-root --wait 1800
UI: kubectl --context '$KUBE_CONTEXT' -n argocd port-forward svc/argocd-server 8080:80
Open http://localhost:8080
Password: kubectl --context '$KUBE_CONTEXT' -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
SUMMARY
