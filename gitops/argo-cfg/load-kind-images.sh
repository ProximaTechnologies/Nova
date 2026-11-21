#!/usr/bin/env bash
# Export only the node's architecture: Docker's image cache may contain a
# multi-platform index without the other platforms' blobs.
set -euo pipefail
[[ $# -ge 2 ]] || { echo "Usage: $0 CLUSTER IMAGE [IMAGE ...]" >&2; exit 2; }
cluster="$1"
shift
nodes="$(kind get nodes --name "$cluster")"
[[ -n "$nodes" ]] || { echo "No nodes found for kind cluster '$cluster'." >&2; exit 2; }
while IFS= read -r node; do
  architecture="$(docker exec "$node" uname -m)"
  case "$architecture" in
    aarch64|arm64) platform=linux/arm64 ;;
    x86_64|amd64) platform=linux/amd64 ;;
    *) echo "Unsupported kind node architecture: $architecture" >&2; exit 2 ;;
  esac
  docker image save --platform "$platform" "$@" |
    docker exec -i "$node" ctr --namespace=k8s.io images import --platform "$platform" -
done <<< "$nodes"
