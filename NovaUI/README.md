# NovaUI

NovaUI is the Django control plane for Nova v1. It manages tenants, users, collector onboarding, feature state, Grafana
SSO, Grafana dashboard desired state, and APISIX per-collector authentication through GitOps.

## Development setup

NovaUI is pre-release. Python 3.12 is the tested runtime. Clone the full Nova
repository so `NovaUI/` and `Nova-Supervisor/` are siblings.

Install WeasyPrint's native libraries first:

```sh
# Debian/Ubuntu
sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0
# macOS (Homebrew)
brew install pango
```

From `NovaUI/`:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Replace SECRET_KEY with output from:
python -c 'import secrets; print(secrets.token_urlsafe(50))'
# Edit .env, then export it (Django does not automatically load .env).
set -a
. ./.env
set +a
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

On Apple Silicon, if WeasyPrint cannot find `libgobject`, set
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` in your shell. Use `python -m pytest`
so tests run with the activated interpreter.

Open <http://localhost:8000/admin/>. Public signup is disabled; an administrator
creates users and tenant memberships. The admin login and health endpoint can
be inspected before provisioning a tenant. Redis at `127.0.0.1:6379` and Grafana
at `localhost:3000` with working administrator credentials are needed for the
integrated tenant/collector workflow. Set `GRAFANA_PASSWORD` in your environment.
Creating a tenant makes real Grafana API calls.

For first tenant setup, inspect `python manage.py bootstrap_install --help`, then
supply your owner email and `NOVA_BOOTSTRAP_PASSWORD` and run
`python manage.py bootstrap_install --email owner@example.com --noinput`.
This creates a billing plan, tenant, owner membership, and OIDC client. Configure
Grafana's public URL first; `--sync-grafana` pushes SSO configuration to Grafana.
Development bootstrap has a fixed fallback password: always supply your own.
The console email backend in the example prints development mail to the terminal.

Full collector enrollment additionally needs NovaCFG, APISIX, observability
backends, and the GitOps/Sealed Secrets setup described below. This repository
does not yet provide a standalone one-command deployment of that entire stack.
Run Celery with `celery -A NovaUI worker -l INFO`; run one scheduler with
`celery -A NovaUI beat -l INFO` when scheduled tasks are required.

## Container and production setup

Build from `NovaUI/` using the sibling supervisor source, without private checkout
credentials or a registry login:

```sh
IMAGE_TAG=novaui:local SUPERVISOR_VERSION=0.1.0 bash scripts/build-image-with-supervisor.sh
```

Use the actual supervisor release version for a release build. The image embeds
Linux supervisor binaries and their checksums. Docker BuildKit is required.
`local-build.sh` is an optional helper that also archives binaries locally.
`update-dev.sh` requires explicit `KIND_CLUSTER`, `KUBE_NAMESPACE`, and
`KUBE_DEPLOYMENT` targets and uses that kind context.

Start from `.env.production.example`; replace all placeholder domains and
credentials. Production requires PostgreSQL, Redis, Grafana, public HTTPS URLs,
and a stable secret key. Configure SMTP for account emails. Terminate TLS at a
trusted reverse proxy that overwrites `X-Forwarded-Proto`; do not expose the
application directly to untrusted proxy headers. Forward WebSocket upgrades.

Run migrations as a separate deployment step before starting web/worker services:

```sh
docker run --rm --env-file .env.production novaui:local python manage.py migrate --noinput
docker run --rm --env-file .env.production novaui:local python manage.py check --deploy
```

The example commands assume the configured services are reachable from the
container; add your deployment network as appropriate. The image runs as a
non-root user, includes static files, and serves ASGI on port 8000. Its health
probe requires `127.0.0.1` in `ALLOWED_HOSTS`. Back up PostgreSQL and GitOps state
before upgrades. Review [RELEASING.md](RELEASING.md) before publishing.

See [CONTRIBUTING.md](CONTRIBUTING.md) for checks and [SECURITY.md](SECURITY.md)
for vulnerability reporting. Licensing is pending maintainer selection; no
project license grant is included yet. Third-party assets retain their own licenses.

## Runtime Flow

### 1. Tenant Setup

Tenants are created in NovaUI, usually by staff for v1 private beta.

When `config.models.Tenant` is created:

- NovaUI creates a Grafana organization through `config.integration.create_tenant()`.
- The Grafana org id is stored on the tenant.
- `TenantLimit` and `TenantUsage` are created.
- Grafana SSO org mappings are refreshed.
- Grafana GitOps resources are bootstrapped after commit for dashboards, folders, datasources, and alerts.

Tenant identity used by metrics/logs is derived from the tenant prefix and name through `tenant_observability_id()`.

### 2. User and Grafana SSO Flow

NovaUI is the identity source.

Users log in to NovaUI, and `TenantUser` stores their tenant role:

- `admin`
- `editor`
- `viewer`

Grafana uses NovaUI as an OIDC provider under `/openid/`. OIDC claims are generated from tenant membership and include
groups like:

```text
nova-{tenant.random_prefix}-{role}
```

Grafana maps those groups to the matching organization role. When tenant membership changes, NovaUI logs the user out of
Grafana so the next login gets fresh claims.

### 3. Collector Creation Flow

Tenant admins and editors create Linux collectors from the NovaUI collector page.

On collector creation, NovaUI:

- Checks node limits and, when billing is enabled, tenant billing state.
- Creates a `Node` scoped to the active tenant.
- Creates `NodeFeatures`.
- Creates or repairs `CollectorAuth` credentials for config, Mimir, and Loki.
- Generates one active one-time `NodeToken`, revoking older active tokens for that node.
- Increments `TenantUsage.nodes`.
- Schedules APISIX GitOps sync after commit.
- Redirects to the install command page.

Suspended tenants cannot create collectors or generate installer tokens.

### 4. Installer Flow

The install command fetches:

```text
/agent/installer/<token>
```

The installer endpoint:

- Validates that the token is unused, unrevoked, unexpired, node-enabled, and tenant-not-suspended.
- Creates or repairs `CollectorAuth` credentials if needed.
- Marks the token as used.
- Pushes the node feature and credential payload to Redis after commit.
- Resyncs APISIX GitOps if credentials were newly created or repaired.
- Renders `templates/agent/install.sh.j2`.

The rendered script installs Grafana Alloy and Nova Supervisor, writes Alloy remote config, and enrolls the supervisor
with NovaUI.

### 5. Redis Config Contract

`NodeFeatures._push_to_redis()` writes the config contract consumed by NovaCFG:

```text
nova:feat:{tenant.name}:{node.node_id.hex}
nova:cred:{tenant.name}:{node.node_id.hex}
nova:ver:{tenant.name}:{node.node_id.hex}
```

The feature payload includes enabled monitoring/logging flags, file monitors, healthcheck URL, Alloy version, and the
observability tenant id.

The credential payload includes Mimir/Loki/Tempo passwords for the collector.

The version key is bumped with the current timestamp so NovaCFG can detect config changes.

### 6. APISIX GitOps Flow

APISIX is managed by GitOps only. NovaUI does not call the APISIX Admin API.

`config/gitops/plugins/apisix.py` renders:

- `ApisixConsumer` per collector service credential:
  - `config`
  - `mimir`
  - `loki`
- `SealedSecret` for each APISIX basic-auth username/password pair.
- Global `ApisixPluginConfig` resources:
  - `nova-config-auth`
  - `nova-mimir-auth`
  - `nova-loki-auth`

The plugin configs enable:

- `basic-auth`
- `consumer-restriction`
- `attach-consumer-label` for Mimir and Loki only

For Mimir and Loki, APISIX attaches:

```text
X-Scope-OrgID: $observability_tenant_id
```

This is how per-collector auth is tied to tenant isolation for observability ingestion.

APISIX consumers are excluded when:

- The node is disabled.
- Billing is enabled and the tenant billing status is suspended.
- The node has no usable collector auth credentials.

NovaUI resyncs APISIX desired state when collectors are created, installed with repaired credentials, disabled, deleted,
or when tenant billing status changes with billing enabled.

### 7. Kubernetes Deployment Contract

For v1 Kubernetes deployments:

- NovaCharts owns the APISIX routes for NovaUI, NovaCFG, Mimir, Loki, and Grafana.
- NovaUI owns APISIX consumers, credential SealedSecrets, and plugin configs.
- ArgoCD/GitOps applies the rendered resources to the cluster.
- Sealed Secrets must be installed, and NovaUI must have the public sealing cert.
- `kubeseal` must be available in the NovaUI runtime image.

Important environment values:

```text
GITOPS_APISIX_NAMESPACE=apisix
GITOPS_APISIX_INGRESS_CLASS=apisix
GITOPS_APISIX_CONFIG_PLUGIN_CONFIG_NAME=nova-config-auth
GITOPS_APISIX_MIMIR_PLUGIN_CONFIG_NAME=nova-mimir-auth
GITOPS_APISIX_LOKI_PLUGIN_CONFIG_NAME=nova-loki-auth
GITOPS_SEALED_SECRETS_CERT_FILE=/tmp/sealed-secrets-public.pem
GITOPS_SEALED_SECRETS_CERT_URL=http://sealed-secrets-controller.sealed-secrets.svc:8080/v1/cert.pem
```

The APISIX plugin config names must match the route references in NovaCharts.

### 8. Dashboard and Operations Flow

Collectors report status through NovaUI agent endpoints. Node status is computed from `last_config_poll_at`,
`last_seen_at`, and `enabled`.

NovaUI shows tenant-scoped:

- Collector health.
- Feature coverage.
- Usage and limits.
- Grafana link scoped to the tenant org.
- Team management.
- Billing status.
- Audit log.

Grafana dashboards and datasources are rendered by the Grafana GitOps plugin.

### Optional billing

Billing is disabled by default for self-hosted installations. Resource limits still apply,
but billing states do not block provisioning, installer tokens, or collector access.
The billing page and invoice/receipt admin are hidden, and `update_invoice_statuses`
is a no-op. Existing billing data and plan records are preserved; no migration is required.

Operators offering a paid service can set `BILLING_ENABLED=true` and restart the web
and worker processes to enable manual billing, invoice/receipt PDFs, and overdue
enforcement. Run `python manage.py update_invoice_statuses` on your own schedule;
the thresholds are `BILLING_GRACE_DAYS`, `BILLING_RESTRICTED_DAYS`, and
`BILLING_SUSPENDED_DAYS` in Django settings. PDF generation requires WeasyPrint and
its system libraries. No payment gateway is included.

After changing this setting on an existing deployment, run
`python manage.py sync_gitops_resources --plugin apisix` to reconcile collector
access, then let your GitOps controller apply the changes.

## Local Commands

Use the project virtualenv when available:

```bash
source .venv/bin/activate
python manage.py check
pytest
python manage.py makemigrations --check --dry-run
```

Run the development server:

```bash
python manage.py migrate
python manage.py runserver
```

## Security Scanning

GitHub Actions runs layered security checks in `.github/workflows/security.yml` on pull requests, pushes to `main`, and
manual dispatch:

- `semgrep`: fast SAST through `semgrep ci`. Configure `SEMGREP_APP_TOKEN` in GitHub secrets and keep the initial
  blocking policy scoped to high-confidence, high-severity findings.
- `codeql`: deeper semantic and data-flow SAST. The workflow scans Python now and automatically adds Go when Go files
  are
  committed to this repository.
- `trivy`: filesystem, dependency, secret, license, IaC/config, and Docker image scanning. SARIF includes all
  severities;
  the initial hard gate is `HIGH,CRITICAL`.
- `gitleaks`: secret detection across git history with redacted SARIF output.

The Trivy image scan builds from the sibling `Nova-Supervisor/` directory in
this checkout. No private checkout token is required. Image publishing uses GitHub Container Registry with the automatic
`GITHUB_TOKEN`: pushes to `dev` publish `latest-dev`, and pushes to `main`
publish `latest` under `ghcr.io/<owner>/novaui`.

Local developer commands:

```bash
semgrep scan --config auto .
trivy fs .
trivy config .
gitleaks detect --source .
```

Triage findings from the scanner output first. Use an LLM agent only to explain exploitability, review likely false
positives, and draft minimal patches; do not treat the LLM as the scanner. For Semgrep suppressions, prefer fixing the
code; use `# nosemgrep` only with a short reason on confirmed false positives. For Gitleaks, rotate any real secret and
only add `.gitleaks.toml` allowlists or a baseline for verified false positives. For Trivy, prefer upgrades or config
fixes before adding ignore entries.

OWASP ZAP is intentionally not part of the first SAST stage. Add it later as DAST against a staging/test deployment,
with authentication where needed, starting with passive or baseline scans and reserving active scans for owned test
environments.
