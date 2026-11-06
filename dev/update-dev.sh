#!/usr/bin/env bash
set -euo pipefail

# Explicit targets prevent this helper from restarting an unrelated cluster.
: "${KIND_CLUSTER:?Set KIND_CLUSTER to the local kind cluster name}"
: "${KUBE_NAMESPACE:?Set KUBE_NAMESPACE to the NovaUI namespace}"
: "${KUBE_DEPLOYMENT:?Set KUBE_DEPLOYMENT to the NovaUI deployment name}"
export IMAGE_TAG="${IMAGE_TAG:-novaui:local}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${ROOT_DIR}/local-build.sh" "${SUPERVISOR_VERSION:-0.0.0-local}"
kind load docker-image "$IMAGE_TAG" --name "$KIND_CLUSTER"
kubectl --context "kind-${KIND_CLUSTER}" -n "$KUBE_NAMESPACE" \
  rollout restart "deployment/${KUBE_DEPLOYMENT}"
