# syntax=docker/dockerfile:1.7

FROM --platform=$BUILDPLATFORM golang:1.22-alpine AS supervisor-builder

WORKDIR /src/nova-supervisor
ARG NOVA_SUPERVISOR_VERSION=latest
ARG NOVA_SUPERVISOR_COMMIT=unknown
COPY --from=nova-supervisor . .
RUN set -eux; \
    mkdir -p /out; \
    for arch in amd64 arm64 ppc64le s390x; do \
        CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build \
          -trimpath \
          -ldflags "-s -w -X main.buildVersion=${NOVA_SUPERVISOR_VERSION} -X main.buildCommit=${NOVA_SUPERVISOR_COMMIT}" \
          -o "/out/nova-supervisor-${NOVA_SUPERVISOR_VERSION}-linux-${arch}" .; \
    done; \
    cd /out; \
    sha256sum nova-supervisor-"${NOVA_SUPERVISOR_VERSION}"-linux-* > SHA256SUMS

FROM python:3.12-alpine

WORKDIR /app
COPY requirements.txt .
ARG KUBESEAL_VERSION=0.37.0
ARG TARGETARCH
RUN apk add --no-cache \
    ca-certificates \
    cairo \
    curl \
    fontconfig \
    gdk-pixbuf \
    glib \
    pango \
    ttf-dejavu
RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    case "$arch" in \
        amd64|arm64) ;; \
        *) echo "unsupported kubeseal architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/kubeseal.tar.gz \
        "https://github.com/bitnami-labs/sealed-secrets/releases/download/v${KUBESEAL_VERSION}/kubeseal-${KUBESEAL_VERSION}-linux-${arch}.tar.gz"; \
    tar -xzf /tmp/kubeseal.tar.gz -C /tmp kubeseal; \
    install -m 0755 /tmp/kubeseal /usr/local/bin/kubeseal; \
    rm -f /tmp/kubeseal /tmp/kubeseal.tar.gz; \
    kubeseal --version
RUN AUTOBAHN_USE_NVX=0 pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn

# Environment settings
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE=NovaUI.settings_prod
ARG NOVA_SUPERVISOR_VERSION=latest
ENV NOVA_SUPERVISOR_VERSION=${NOVA_SUPERVISOR_VERSION}
COPY --from=supervisor-builder /out/ /app/agent/binaries/
# Copy project files
COPY . .

# Collect static files into /app/staticfiles for WhiteNoise.
RUN DJANGO_SETTINGS_MODULE=NovaUI.settings DEBUG=1 python manage.py collectstatic --noinput

# Create a non-root user and adjust permissions
# GitOps auto-commit/push invokes git at runtime.
RUN apk add --no-cache git \
    && chmod +x /app/docker-entrypoint.sh \
    && addgroup -S app \
    && adduser -S app -G app \
    && chown -R app:app /app
USER app

# Expose Gunicorn port
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/healthz/ || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Start Gunicorn with an ASGI worker so dashboard WebSockets are served.
CMD ["gunicorn", "NovaUI.asgi:application", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
