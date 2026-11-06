# NovaUI Platform Administrator Manual

This manual is for the first private beta. It assumes billing is handled manually outside NovaUI. Use NovaUI as the
control plane for tenants, users, collectors, Grafana SSO, GitOps CRDs, and collector enrollment.

## 1. Platform Overview

Nova is split into these responsibilities:

- `NovaUI`: Django control plane for tenants, users, collector enrollment, Redis feature contract, OIDC, and GitOps
  CRDs.
- `NovaCFG`: remote Alloy configuration service. NovaUI writes the contract NovaCFG reads from Redis.
- Redis: stores node feature/config contract keys.
- Postgres: production database for NovaUI.
- Grafana: tenant dashboards and SSO target.
- Mimir: metrics storage.
- Loki: log storage.
- APISIX: gateway/auth layer for collector config, Mimir, and Loki access.
- Forgejo: GitOps repository host for generated CRDs.
- ArgoCD or equivalent GitOps controller: applies CRDs from Forgejo to the cluster.
- SealedSecrets controller: decrypts committed `SealedSecret` CRDs into Kubernetes `Secret` objects.

The expected beta flow is:

```text
Create tenant in NovaUI
-> NovaUI creates Grafana org
-> NovaUI updates Grafana OIDC org mappings
-> NovaUI writes Grafana CRDs to Forgejo
-> Create collector in NovaUI
-> NovaUI creates CollectorAuth credentials
-> NovaUI writes APISIX consumers and SealedSecrets to Forgejo
-> ArgoCD applies CRDs to the cluster
-> Customer runs installer
-> Alloy polls NovaCFG
-> NovaCFG reads Redis feature contract
-> Alloy sends metrics/logs through APISIX to Mimir/Loki
-> Tenant views Grafana dashboards
```

## 2. Administrator Rules

- Do not use self-service billing for the first beta. Track billing in a spreadsheet or accounting system.
- Do not let public users sign up directly. Signups are disabled by the allauth adapter.
- Create users deliberately, then add them to tenants by email.
- Treat installer tokens, collector passwords, Grafana credentials, APISIX credentials, and SealedSecrets certs as
  sensitive.
- Do not commit `.env`, tokens, database dumps, private keys, or generated plaintext credentials.
- Do not create tenants unless Grafana API credentials are configured and Grafana is reachable.
- Run GitOps sync after changes that affect Grafana/APISIX CRDs if the automatic hook did not run.
- For beta, keep platform admins as Django staff/superusers. Tenant customer users should not be Django staff unless you
  intentionally want them to provision collectors.

## 3. Infrastructure Prerequisites

Provision these before starting NovaUI in a real beta environment:

- A Linux host or Kubernetes workload for NovaUI.
- Postgres database.
- Redis instance.
- Grafana instance reachable by NovaUI internal API URL and by users through public URL.
- Mimir and Loki deployed behind APISIX or your chosen gateway path.
- NovaCFG deployed and able to read the same Redis used by NovaUI.
- Forgejo instance and access token with permission to create/update repositories and files.
- ArgoCD configured to watch the Nova-managed Forgejo repo.
- SealedSecrets controller installed in the cluster where APISIX CRDs are applied.
- `kubeseal` binary available to the NovaUI runtime or job that runs `sync_gitops_resources`.

Fetch the SealedSecrets public cert from the target cluster:

```bash
kubeseal --fetch-cert --controller-name sealed-secrets --controller-namespace kube-system > sealed-secrets-public.pem
```

Adjust controller name/namespace if your cluster uses different values.

## 4. Local Admin Workstation Setup

From the repository root:

```bash
cd /home/maomao/PycharmProjects/NovaUI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

For local development only, SQLite is acceptable:

```bash
python manage.py migrate
python manage.py runserver
```

For any beta/staging/production deployment, use Postgres and Redis through environment variables.

## 5. Production Environment Variables

Use environment variables, not committed files, for beta/prod configuration.

Core Django:

```bash
export NOVA_ENV=production
export DEBUG=False
export SECRET_KEY='<long-random-secret>'
export ALLOWED_HOSTS='nova.example.com'
export CSRF_TRUSTED_ORIGINS='https://nova.example.com'
export DATABASE_URL='postgresql://novaui:<password>@postgres.example.internal:5432/novaui'
export REDIS_URL='redis://redis.example.internal:6379/0'
export UI_URL='https://nova.example.com'
export CONFIGURATION_URL='https://cfg.example.com'
```

Grafana:

```bash
export GRAFANA_PROTOCOL='https'
export GRAFANA_HOST='grafana.example.internal'
export GRAFANA_EXTERNAL_URL='https://grafana.example.com'
export GRAFANA_USER='<grafana-admin-user>'
export GRAFANA_PASSWORD='<grafana-admin-password>'
export GRAFANA_OAUTH_CLIENT_NAME='Grafana OAuth'
export NOVA_PUBLIC_URL='https://nova.example.com'
export NOVA_OIDC_ISSUER_URL='https://nova.example.com'
```

Celery:

```bash
export CELERY_BROKER_URL='redis://redis.example.internal:6379/1'
export CELERY_BACKEND='redis://redis.example.internal:6379/1'
```

Forgejo and GitOps:

```bash
export GITOPS_FORGEJO_ENABLED=True
export GITOPS_FORGEJO_URL='https://forgejo.example.com'
export GITOPS_FORGEJO_TOKEN='<forgejo-token>'
# Or, if token bootstrapping is not used:
export GITOPS_FORGEJO_USERNAME='gitea_admin'
export GITOPS_FORGEJO_PASSWORD='<forgejo-admin-password>'
export GITOPS_FORGEJO_ORG='nova'
export GITOPS_FORGEJO_NOVA_REPO_NAME='nova'
export GITOPS_FORGEJO_DEFAULT_BRANCH='main'
export GITOPS_FORGEJO_REPO_PRIVATE=True
```

SealedSecrets:

```bash
export GITOPS_KUBESEAL_BINARY='kubeseal'
export GITOPS_SEALED_SECRETS_CERT_FILE='/run/secrets/sealed-secrets-public.pem'
export GITOPS_SEALED_SECRETS_SCOPE='strict'
```

Grafana Operator and APISIX namespaces:

```bash
export GITOPS_GRAFANA_NAMESPACE='monitoring'
export GITOPS_APISIX_NAMESPACE='ingress-apisix'
export GITOPS_APISIX_INGRESS_CLASS='apisix'
export GITOPS_MIMIR_DATASOURCE_URL='http://mimir-nginx.monitoring.svc:9009/prometheus'
export GITOPS_LOKI_DATASOURCE_URL='http://loki-gateway.monitoring.svc'
```

Bootstrap owner account:

```bash
export NOVA_BOOTSTRAP_EMAIL='admin@example.com'
export NOVA_BOOTSTRAP_USERNAME='admin'
export NOVA_BOOTSTRAP_PASSWORD='<strong-admin-password>'
export NOVA_BOOTSTRAP_TENANT='Nova'
```

## 6. Initial Database Setup

Run migrations:

```bash
source .venv/bin/activate
python manage.py migrate
```

Validate settings:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
```

For production settings:

```bash
DJANGO_SETTINGS_MODULE=NovaUI.settings_prod python manage.py check
```

## 7. First Install Bootstrap

Run:

```bash
python manage.py bootstrap_install --sync-grafana
```

This command:

- Creates or updates the owner user.
- Marks the owner email as verified.
- Promotes the owner to Django staff/superuser by default.
- Creates the initial tenant, default `Nova`.
- Creates the default billing plan required by tenant limits.
- Creates tenant usage and limits if missing.
- Adds the owner as tenant admin.
- Creates or updates the Grafana OIDC client.
- Creates an OIDC signing key if needed.
- Pushes Grafana OIDC settings and org mappings when `--sync-grafana` is used.

Expected output includes:

```text
tenant_id_secure=<random-prefix>-Nova
grafana_oidc=created/updated client_id=...
grafana_sync=yes
```

Record the `tenant_id_secure` value. Tenant URLs use it.

If the install already has tenants and you intentionally want another tenant through bootstrap:

```bash
python manage.py bootstrap_install --tenant-name '<tenant-name>' --allow-existing-tenants --sync-grafana
```

## 8. Run NovaUI Services

For local development:

```bash
python manage.py runserver
```

For production, run through your process manager or container platform:

```bash
daphne NovaUI.asgi:application
```

Run Celery worker and beat:

```bash
celery -A NovaUI worker -l INFO -B
```

Celery currently handles token cleanup and daily GitOps sync.

## 9. Verify Grafana SSO

Push OIDC settings and org mappings:

```bash
python manage.py sync_grafana_sso
```

If OIDC client credentials need to be recreated or rotated:

```bash
python manage.py bootstrap_grafana_oidc --sync-grafana
```

Rotate client secret only when you are prepared to update Grafana:

```bash
python manage.py bootstrap_grafana_oidc --rotate-client-secret --sync-grafana
```

Check these in Grafana:

- Generic OAuth is enabled.
- Auth URL points to `https://nova.example.com/openid/authorize`.
- Token URL points to `https://nova.example.com/openid/token`.
- Userinfo URL points to `https://nova.example.com/openid/userinfo`.
- Org mappings use `nova-<tenant-prefix>-admin`, `nova-<tenant-prefix>-editor`, and `nova-<tenant-prefix>-viewer`.

Staff-only OIDC debug page:

```text
https://nova.example.com/<tenant_id_secure>/debug/oidc-claims/
```

Use it to confirm current user claims and expected Grafana org mapping.

## 10. Forgejo Repo Layout

NovaUI writes Nova-managed CRDs to one shared repo:

```text
repo: nova/nova
```

Each tenant gets its own folder:

```text
tenants/nova-<tenant-prefix>/
```

Inside each tenant folder:

```text
tenants/nova-<tenant-prefix>/apisix/
tenants/nova-<tenant-prefix>/grafana/
```

Examples:

```text
tenants/nova-acme/apisix/apisixconsumer-<node>-mimir.yaml
tenants/nova-acme/apisix/sealedsecret-<node>-mimir-basic-auth.yaml
tenants/nova-acme/grafana/grafanadatasource-nova-acme-mimir.yaml
tenants/nova-acme/grafana/grafanadashboard-nova-acme-node-exporter.yaml
```

Customer-editable dashboard repo is still per tenant:

```text
<tenant-slug>-custom
```

For the first beta, treat customer-editable dashboards as future work unless you explicitly enable that workflow.

## 11. GitOps Sync

Render and write all configured GitOps resources:

```bash
python manage.py sync_gitops_resources --reason manual
```

Only Grafana CRDs:

```bash
python manage.py sync_gitops_resources --plugin grafana --tenant-id <tenant_pk> --reason tenant_setup
```

Only APISIX CRDs:

```bash
python manage.py sync_gitops_resources --plugin apisix --tenant-id <tenant_pk> --reason collector_created
```

Render to database without writing to Git:

```bash
python manage.py sync_gitops_resources --plugin grafana --tenant-id <tenant_pk> --render-only
```

Admin checks after sync:

- `GitOpsResource` rows exist in Django admin.
- `sync_status` is `synced`.
- Forgejo repo has files under the expected tenant folder.
- ArgoCD sees and applies the CRDs.
- SealedSecrets controller creates real Kubernetes Secrets from committed `SealedSecret` resources.

## 12. Creating Tenants

Preferred first beta approach:

1. Log into Django admin as platform admin.
2. Create a `Tenant`.
3. Let NovaUI create the Grafana org through the `Tenant.save()` side effect.
4. Confirm `grafana_org_id` is populated.
5. Confirm `TenantLimit` and `TenantUsage` exist.
6. Confirm Grafana org mapping was updated.
7. If Forgejo is enabled, confirm the shared `nova` repo has the tenant folder.

Important:

- Tenant creation calls Grafana. If Grafana is down or credentials are wrong, tenant creation can fail.
- Do not edit `random_prefix` after tenant creation. OIDC group names depend on it.
- Tenant URL uses:

```text
<tenant.random_prefix>-<tenant.name>
```

Example:

```text
https://nova.example.com/acme-Acme/
```

## 13. Creating Platform and Tenant Users

Public signup is disabled. Use Django admin or command-line user creation.

Create a user in Django admin:

1. Go to `/admin/auth/user/add/`.
2. Set username and password.
3. Save.
4. Edit the user and set email.
5. For platform administrators, enable `is_staff` and `is_superuser`.
6. For normal tenant users, do not enable staff/superuser.

Add tenant membership:

1. Open the tenant in NovaUI.
2. Go to `Settings -> Team`.
3. Add member by email.
4. Choose role:
    - `admin`: manage users, collectors, limits, Grafana access.
    - `editor`: manage collectors/configuration.
    - `viewer`: view dashboards only.

After role changes, NovaUI attempts to log the user out of Grafana so the next login uses fresh claims.

For beta collector provisioning, note the current code only allows collector provisioning when the user is both:

- tenant `admin` or `editor`
- Django `is_staff=True`

That means platform admins should provision collectors for customers, or you must intentionally mark a tenant operator
as staff for the beta.

## 14. Manual Billing Policy for First Beta

Ignore tenant-facing billing for now.

Use this manual process:

1. Keep a spreadsheet with tenant name, contact, plan, host count, included hosts, extra hosts, agreed price, due date,
   and payment status.
2. Set `TenantLimit.max_nodes` manually to enforce the agreed host cap.
3. Use `Tenant.billing_status` only for operational enforcement:
    - `active`: normal.
    - `grace`: payment late but service still allowed.
    - `restricted`: internal warning state.
    - `suspended`: blocks new installer token generation and excludes APISIX resources for ingestion.
4. Do not rely on the tenant billing page for beta.
5. Do not run payment automation.

If you need to suspend a tenant:

1. Set `Tenant.billing_status=suspended`.
2. Disable affected nodes if needed.
3. Run APISIX GitOps sync:

```bash
python manage.py sync_gitops_resources --plugin apisix --tenant-id <tenant_pk> --reason tenant_suspended
```

4. Confirm APISIX resources for suspended tenant are pruned or no longer rendered.
5. Confirm new installer token generation is blocked.

## 15. Collector Onboarding

Create a collector:

1. Log in as a platform admin.
2. Open the tenant dashboard.
3. Go to `Collectors`.
4. Click `Add collector`.
5. Enter node name.
6. Select Linux features:
    - Linux metrics.
    - Journal logs.
    - File logs if required.
    - Docker metrics/logs only if the host needs Docker monitoring.
7. Add file paths when file logs are enabled.
8. Submit.

NovaUI will:

- Create `Node`.
- Create `NodeFeatures`.
- Create `CollectorAuth`.
- Create one-time `NodeToken`.
- Increment `TenantUsage.nodes`.
- Push feature contract to Redis after commit.
- Trigger APISIX GitOps sync after commit.

Before handing the command to the customer:

1. Confirm APISIX GitOps resources synced.
2. Confirm ArgoCD applied APISIX resources.
3. Confirm SealedSecrets decrypted into Kubernetes Secrets.
4. Confirm NovaCFG is reachable from the target host.

The installer page shows a curl/bash command using a one-time token.

Token behavior:

- Token expires according to `NODE_TOKEN_TTL_MINUTES`, default 10 minutes.
- Token is single use.
- Reopening the install page can generate a fresh token if the old one expired or was used.
- Suspended tenants cannot generate or use installer tokens.

## 16. Customer Host Installation

On the customer Linux host, run the generated command from the collector install page.

The installer:

- Installs Grafana Alloy.
- Writes `/etc/alloy/config.alloy`.
- Configures Alloy `remotecfg`.
- Uses NovaCFG URL and per-node config credentials.
- Starts/restarts the Alloy systemd service.

After install, verify on the host:

```bash
systemctl status alloy --no-pager --full
journalctl -u alloy -n 100 --no-pager
cat /etc/alloy/config.alloy
```

Do not share `/etc/alloy/config.alloy` publicly. It contains basic auth credentials for NovaCFG.

## 17. Post-Onboarding Verification

In NovaUI:

- Collector status changes from `Never connected` to `Online`.
- `last_seen_at` updates.
- `alloy_version` updates after check-in.

In Redis:

Expected keys:

```text
nova:feat:<tenant.name>:<node.node_id.hex>
nova:ver:<tenant.name>:<node.node_id.hex>
```

The feature JSON should include:

- enabled feature flags
- file monitor list
- Alloy version
- Healthcheck URL

In Grafana:

- Open tenant dashboard from NovaUI.
- Confirm user lands in correct Grafana org.
- Confirm Mimir datasource works.
- Confirm Loki datasource works.
- Confirm Linux dashboard shows the new host.
- Confirm logs appear for journal/file paths.

In cluster:

```bash
kubectl get apisixconsumer -n ingress-apisix
kubectl get sealedsecret -n ingress-apisix
kubectl get secret -n ingress-apisix
```

Adjust namespace if `GITOPS_APISIX_NAMESPACE` differs.

## 18. Daily Administrator Workflow

Morning checks:

1. Open NovaUI admin dashboard.
2. Check tenant list and collector status.
3. Check any `warning`, `offline`, or `never connected` collectors.
4. Check Celery worker is running.
5. Check Forgejo sync status in Django admin.
6. Check ArgoCD health for Nova-managed repo.
7. Check Grafana, Mimir, Loki, APISIX, Redis, and Postgres health.

When adding a customer:

1. Create tenant.
2. Create owner user.
3. Add tenant membership.
4. Set tenant limits.
5. Sync Grafana.
6. Verify Grafana org and SSO.
7. Add collector.
8. Confirm APISIX/Grafana CRDs are in Forgejo.
9. Run installer on customer host.
10. Verify metrics/logs.
11. Record customer in manual billing spreadsheet.

When removing a customer:

1. Disable or delete collectors.
2. Remove team memberships.
3. Set tenant billing/status according to your support decision.
4. Sync APISIX and Grafana GitOps.
5. Confirm no active ingestion credentials remain.
6. Archive manual billing record.

## 19. Common Admin Commands

Django checks:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
```

Run migrations:

```bash
python manage.py migrate
```

Bootstrap install:

```bash
python manage.py bootstrap_install --sync-grafana
```

Sync Grafana SSO:

```bash
python manage.py sync_grafana_sso
```

Sync all GitOps resources:

```bash
python manage.py sync_gitops_resources --reason manual
```

Sync tenant Grafana CRDs:

```bash
python manage.py sync_gitops_resources --plugin grafana --tenant-id <tenant_pk>
```

Sync tenant APISIX CRDs:

```bash
python manage.py sync_gitops_resources --plugin apisix --tenant-id <tenant_pk>
```

Run tests:

```bash
pytest
pytest config/tests/test_v1_security.py
```

Collect static:

```bash
python manage.py collectstatic --noinput
```

## 20. Troubleshooting

### Tenant Creation Fails

Check:

- `GRAFANA_HOST`, `GRAFANA_PROTOCOL`, `GRAFANA_USER`, `GRAFANA_PASSWORD`.
- Grafana API is reachable from NovaUI.
- Default `TenantBilling` plan exists.
- Database transaction did not partially create records.

Retry after fixing Grafana. Do not manually fake `grafana_org_id` unless you know the Grafana org exists.

### Grafana Login Works but User Lands in Wrong Org

Run:

```bash
python manage.py sync_grafana_sso
```

Then check:

- User has exactly the intended `TenantUser` role.
- Tenant `random_prefix` matches the Grafana org mapping group.
- OIDC debug page shows expected groups.
- Grafana session was refreshed after role change.

### APISIX Credentials Do Not Exist in Cluster

Check:

- `sync_gitops_resources --plugin apisix` completed.
- Forgejo has `SealedSecret` files under `tenants/<tenant>/apisix/`.
- ArgoCD applied the repo.
- SealedSecrets controller is healthy.
- `GITOPS_SEALED_SECRETS_CERT_FILE` or `GITOPS_SEALED_SECRETS_PUBLIC_CERT` matches the target cluster.

### GitOps Sync Fails on SealedSecrets

Check:

- `kubeseal` is installed in the NovaUI runtime.
- `GITOPS_KUBESEAL_BINARY` points to the binary.
- SealedSecrets public cert is mounted and readable.
- Scope/name/namespace match the cluster where the `SealedSecret` will be applied.

### Collector Installer Token Fails

Check:

- Token has not expired.
- Token was not already used.
- Node is enabled.
- Tenant is not suspended.
- Installer URL points to the correct NovaUI public URL.

Regenerate by opening the collector install command page again as an authorized staff admin/editor.

### Collector Installed but Stays Never Connected

Check on host:

```bash
systemctl status alloy --no-pager --full
journalctl -u alloy -n 100 --no-pager
```

Check platform:

- NovaCFG is reachable from the host.
- APISIX config consumer exists.
- Secret exists and has expected username/password keys.
- Redis feature key exists.
- NovaCFG can read Redis.

### Metrics or Logs Missing in Grafana

Check:

- Alloy service is running.
- NovaCFG generated config includes the expected features.
- Mimir and Loki APISIX consumers exist.
- Mimir/Loki Secrets exist.
- Grafana datasources exist in the tenant org.
- Tenant labels/scope org ID match what Mimir/Loki expect.

## 21. Known First Beta Limitations

- Billing is manual and external.
- Tenant-facing billing page is not considered product-ready.
- Alerts are not complete yet.
- Grafana dashboard set is still minimal.
- Public signup is disabled.
- Collector provisioning currently requires Django staff plus tenant admin/editor role.
- Some UI/security E2E tests are known failing and should be fixed before widening beta.
- Windows monitoring is out of V1 scope.
- Kubernetes monitoring is out of V1 scope.
- Custom dashboards are out of first beta unless explicitly enabled.

## 22. Beta Readiness Checklist

Before onboarding a beta customer:

- `python manage.py check` passes.
- `python manage.py makemigrations --check --dry-run` passes.
- Grafana OIDC sync succeeds.
- Tenant creation creates Grafana org.
- Forgejo shared `nova` repo exists.
- Tenant folder exists under `tenants/nova-<prefix>/`.
- Grafana CRDs are synced under tenant `grafana/` folder.
- APISIX CRDs and SealedSecrets are synced under tenant `apisix/` folder.
- ArgoCD applies generated CRDs.
- SealedSecrets become Kubernetes Secrets.
- One test collector can be installed from generated command.
- Metrics appear in Mimir/Grafana.
- Logs appear in Loki/Grafana.
- Manual billing spreadsheet has customer entry and node limit.
