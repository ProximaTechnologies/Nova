#!/bin/sh
set -eu

if [ -n "${GITOPS_SEALED_SECRETS_CERT_FILE:-}" ] \
    && [ ! -s "${GITOPS_SEALED_SECRETS_CERT_FILE}" ] \
    && [ -n "${GITOPS_SEALED_SECRETS_CERT_URL:-}" ]; then
    mkdir -p "$(dirname "${GITOPS_SEALED_SECRETS_CERT_FILE}")"
    curl -fsSL \
        --retry 10 \
        --retry-delay 2 \
        --max-time 10 \
        -o "${GITOPS_SEALED_SECRETS_CERT_FILE}" \
        "${GITOPS_SEALED_SECRETS_CERT_URL}"
fi

exec "$@"
