#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APPSETS_DIR="$REPO_ROOT/appsets"

ARGOCD_NAMESPACE="${ARGOCD_NAMESPACE:-argocd}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
ROOT_APP="${ROOT_APP:-nova-appsets}"
INCLUDE_ROOT_APP="${INCLUDE_ROOT_APP:-false}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-0}"
WAIT_INTERVAL="${WAIT_INTERVAL:-10}"
ALLOW_CONDITIONS="${ALLOW_CONDITIONS:-false}"

APPS_FILTER="${APPS:-}"
ENVS_FILTER="${ENVS:-}"
SELECTED_APPS=()
SELECTED_ENVS=()

usage() {
  cat <<USAGE
Usage: $0 [options]

Verifies expected Argo CD Application deployment state for this GitOps repo.
Expected applications are derived from active ApplicationSet manifests as <app>-<env>.

Options:
  --namespace, -n NAME   Argo CD namespace (default: $ARGOCD_NAMESPACE)
  --context NAME         kube context to use (default: current context)
  --app NAME             appset/service name to check; repeatable (default: discover all)
  --env NAME             environment to check; repeatable (default: discover all)
  --include-root         also check the root Argo CD application ($ROOT_APP)
  --skip-root            do not check the root Argo CD application (default)
  --wait SECONDS         poll until healthy/synced or timeout (default: $WAIT_TIMEOUT)
  --interval SECONDS     poll interval when --wait is set (default: $WAIT_INTERVAL)
  --allow-conditions     do not fail when Argo CD application conditions are present
  -h, --help             show this help

Environment overrides:
  ARGOCD_NAMESPACE, KUBE_CONTEXT, APPS, ENVS, ROOT_APP, INCLUDE_ROOT_APP,
  WAIT_TIMEOUT, WAIT_INTERVAL, ALLOW_CONDITIONS

Examples:
  $0
  $0 --env dev --wait 300
  KUBE_CONTEXT=kind-argo ENVS=dev $0 --include-root
USAGE
}

die() {
  echo "error: $*" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      ARGOCD_NAMESPACE="$2"
      shift 2
      ;;
    --context)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      KUBE_CONTEXT="$2"
      shift 2
      ;;
    --app)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      SELECTED_APPS+=("$2")
      shift 2
      ;;
    --env)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      SELECTED_ENVS+=("$2")
      shift 2
      ;;
    --include-root)
      INCLUDE_ROOT_APP="true"
      shift
      ;;
    --skip-root)
      INCLUDE_ROOT_APP="false"
      shift
      ;;
    --wait)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      WAIT_TIMEOUT="$2"
      shift 2
      ;;
    --interval)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      WAIT_INTERVAL="$2"
      shift 2
      ;;
    --allow-conditions)
      ALLOW_CONDITIONS="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ "$WAIT_TIMEOUT" =~ ^[0-9]+$ ]] || die "--wait must be a non-negative integer"
[[ "$WAIT_INTERVAL" =~ ^[0-9]+$ ]] || die "--interval must be a non-negative integer"
[[ "$WAIT_INTERVAL" -gt 0 ]] || die "--interval must be greater than zero"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "missing required command: kubectl" >&2
  exit 127
fi

KUBECTL=(kubectl)
if [[ -n "$KUBE_CONTEXT" ]]; then
  KUBECTL+=(--context "$KUBE_CONTEXT")
fi

# Parse actual generator elements instead of an app/environment Cartesian product.
DISCOVERY_ARGS=(--applications)
if [[ ${#SELECTED_APPS[@]} -gt 0 ]]; then
  for app in "${SELECTED_APPS[@]}"; do DISCOVERY_ARGS+=(--app "$app"); done
elif [[ -n "$APPS_FILTER" ]]; then
  read -r -a SELECTED_APPS <<< "$APPS_FILTER"
  for app in "${SELECTED_APPS[@]}"; do DISCOVERY_ARGS+=(--app "$app"); done
fi
if [[ ${#SELECTED_ENVS[@]} -gt 0 ]]; then
  for env in "${SELECTED_ENVS[@]}"; do DISCOVERY_ARGS+=(--env "$env"); done
elif [[ -n "$ENVS_FILTER" ]]; then
  read -r -a SELECTED_ENVS <<< "$ENVS_FILTER"
  for env in "${SELECTED_ENVS[@]}"; do DISCOVERY_ARGS+=(--env "$env"); done
fi
expected="$("${PYTHON:-python3}" "$SCRIPT_DIR/validate_gitops.py" "${DISCOVERY_ARGS[@]}")" || exit 2
EXPECTED_APPLICATIONS=()
if [[ "$INCLUDE_ROOT_APP" == "true" ]]; then
  EXPECTED_APPLICATIONS+=("$ROOT_APP")
fi
while IFS= read -r app; do EXPECTED_APPLICATIONS+=("$app"); done <<< "$expected"

if ! "${KUBECTL[@]}" -n "$ARGOCD_NAMESPACE" get applications.argoproj.io >/dev/null 2>&1; then
  echo "unable to list Argo CD applications in namespace '$ARGOCD_NAMESPACE'." >&2
  echo "Check kube context, namespace, and that Argo CD CRDs are installed." >&2
  exit 2
fi

jsonpath() {
  local app_name="$1"
  local path="$2"

  "${KUBECTL[@]}" -n "$ARGOCD_NAMESPACE" get application.argoproj.io "$app_name" -o "jsonpath=$path" 2>/dev/null || true
}

check_once() {
  local failures=0
  local app_name sync health operation conditions result detail
  DETAILS=()

  echo "Checking Argo CD applications in namespace '$ARGOCD_NAMESPACE'"
  if [[ -n "$KUBE_CONTEXT" ]]; then
    echo "Using kube context '$KUBE_CONTEXT'"
  fi
  printf '%-32s %-12s %-12s %-12s %s\n' "APPLICATION" "SYNC" "HEALTH" "OPERATION" "RESULT"

  for app_name in "${EXPECTED_APPLICATIONS[@]}"; do
    if ! "${KUBECTL[@]}" -n "$ARGOCD_NAMESPACE" get application.argoproj.io "$app_name" >/dev/null 2>&1; then
      printf '%-32s %-12s %-12s %-12s %s\n' "$app_name" "-" "-" "-" "missing"
      DETAILS+=("$app_name: Application resource is missing")
      failures=$((failures + 1))
      continue
    fi

    sync="$(jsonpath "$app_name" '{.status.sync.status}')"
    health="$(jsonpath "$app_name" '{.status.health.status}')"
    operation="$(jsonpath "$app_name" '{.status.operationState.phase}')"
    conditions="$(jsonpath "$app_name" '{range .status.conditions[*]}{.type}{": "}{.message}{"; "}{end}')"

    [[ -n "$sync" ]] || sync="Unknown"
    [[ -n "$health" ]] || health="Unknown"
    [[ -n "$operation" ]] || operation="-"

    result="ok"
    detail=""

    if [[ "$sync" != "Synced" ]]; then
      result="failed"
      detail+="sync=$sync "
    fi

    if [[ "$health" != "Healthy" ]]; then
      result="failed"
      detail+="health=$health "
    fi

    if [[ "$operation" != "-" && "$operation" != "Succeeded" ]]; then
      result="failed"
      detail+="operation=$operation "
    fi

    if [[ -n "$conditions" && "$ALLOW_CONDITIONS" != "true" ]]; then
      result="failed"
      detail+="conditions-present "
    fi

    printf '%-32s %-12s %-12s %-12s %s\n' "$app_name" "$sync" "$health" "$operation" "$result"

    if [[ "$result" != "ok" ]]; then
      failures=$((failures + 1))
      DETAILS+=("$app_name: ${detail:-deployment state check failed}")
      if [[ -n "$conditions" ]]; then
        DETAILS+=("$app_name conditions: $conditions")
      fi
    fi
  done

  if [[ ${#DETAILS[@]} -gt 0 ]]; then
    echo
    echo "Failure details:"
    printf '  - %s\n' "${DETAILS[@]}"
  fi

  return "$failures"
}

start_time=$SECONDS
while true; do
  check_once
  status=$?

  if [[ "$status" -eq 0 ]]; then
    echo
    echo "All expected applications are Synced and Healthy."
    exit 0
  fi

  if [[ "$WAIT_TIMEOUT" -eq 0 || $((SECONDS - start_time)) -ge "$WAIT_TIMEOUT" ]]; then
    echo
    echo "$status application state check(s) failed." >&2
    exit 1
  fi

  sleep_for="$WAIT_INTERVAL"
  remaining=$((WAIT_TIMEOUT - (SECONDS - start_time)))
  if [[ "$remaining" -lt "$sleep_for" ]]; then
    sleep_for="$remaining"
  fi

  echo
  echo "Waiting ${sleep_for}s before rechecking..."
  sleep "$sleep_for"
  echo
done
