# NovaUI Agent Guide

## Project Shape

- Django 5.2 project rooted at `NovaUI/`; `NovaUI/urls.py` mounts the tenant UI at `/`, allauth at `/accounts/`,
  installer endpoints at `/agent/`, and `django-oidc-provider` at `/openid/`.
- Main domain code lives in `config/`: tenants, tenant users, limits/usage, collector nodes, node features, one-time
  node tokens, Grafana/OIDC integration, Redis feature publishing, audit logs, and Celery tasks.
- `ui/` is server-rendered HTML for tenant-scoped dashboards/settings/collectors. Templates live under `templates/ui/`
  and use Tailwind browser v4 plus vendored DaisyUI from `static/vendor/`.
- `agent/` serves the Linux installer script. `billing/` is mostly admin-driven invoice/receipt PDF generation with
  WeasyPrint.
- `NovaUI/` also contains Django settings, ASGI/WSGI, Celery setup, OIDC settings, and adapters.
- `templates/` contains Django templates using existing component/layout patterns; shared form components live in
  `templates/components/`.
- `static/` contains static assets, including local Tailwind and DaisyUI vendor files.

## Working Rules

- Preserve existing user changes. Check `git status --short` before editing and do not revert unrelated files.
- Keep edits scoped to the requested behavior. Avoid opportunistic refactors unless they remove direct risk in touched
  code.
- Follow the existing Django patterns in this repo before introducing new abstractions.
- Prefer explicit, readable model/query/view code over clever indirection.
- Do not commit secrets. Treat `.env`, `.CF_TOKEN`, tokens, private keys, database dumps, and local credentials as
  sensitive.
- Do not edit generated cache files, `__pycache__/`, `.pytest_cache/`, local SQLite data, or `celerybeat-schedule`
  unless explicitly asked.
- Use ASCII for new text unless the edited file already clearly uses non-ASCII content.
- Be terse, dont over explain. give 1 quick summary of the changes at the end. 

## Tenant, Auth, and Grafana

- Creating a `config.models.Tenant` has side effects: `Tenant.save()` calls `config.integration.create_tenant()`, which
  creates a Grafana org, creates `TenantLimit`/`TenantUsage`, stores `grafana_org_id`, then refreshes Grafana SSO org
  mappings.
- Deleting a tenant calls Grafana deletion in `tenant_pre_delete` and refreshes mappings in `tenant_post_delete`.
- In tests, patch `config.models.create_tenant`, `delete_tenant`, `update_grafana_org_mapping`, and
  `logout_user_from_grafana_using_email` before creating tenants.
- Tenant URLs use `tenant_id_secure = "{random_prefix}-{tenant.name}"`. Resolve and authorize with
  `ui.utils.get_user_tenant()` or `@tenant_role_required(...)`; collector queries must also filter by the resolved
  tenant.
- Grafana OIDC claims are built in `config/oidc_claims.py`: groups look like `nova-{tenant.random_prefix}-{role}` and
  roles are `admin`, `editor`, `viewer`.
- `config.integration.update_grafana_org_mapping()` maps those groups to Grafana org roles.
- Manual Grafana SSO resync command: `python manage.py sync_grafana_sso`.

## Collector and Redis Flow

- `ui.views.collector_add()` creates `Node`, `NodeFeatures`, a one-time `NodeToken`, increments `TenantUsage.nodes`,
  records audit, then redirects to the installer command page.
- `Node.generate_token()` revokes existing active tokens inside a transaction before creating a new token. Tokens expire
  after `settings.NODE_TOKEN_TTL_MINUTES` and are single-use via `NodeToken.validate_usable()`/`mark_used()`.
- `agent.views.installer()` consumes `/agent/installer/<token>`, creates `CollectorAuth` credentials, marks the token
  used, pushes feature state to Redis on commit, and renders `templates/agent/install.sh.j2`.
- `NodeFeatures._push_to_redis()` writes `nova:feat:{tenant.name}:{node.node_id.hex}` JSON and bumps
  `nova:ver:{tenant.name}:{node.node_id.hex}` with the current timestamp.
- Use `transaction.on_commit()` for new DB-to-Redis side effects.
- Node status is computed, not stored: disabled wins; otherwise `last_config_poll_at`/`last_seen_at` <=5m is online, <
  =30m warning, older offline.

## Billing and Enforcement

- Billing is opt-in via `BILLING_ENABLED=true` (default false). When disabled, billing pages/admin and overdue
  enforcement are disabled, and billing state does not block collectors or tokens. Resource limits still apply.
- V1 tenant billing status is on `Tenant.billing_status` (`active`, `grace`, `restricted`, `suspended`). With billing enabled, suspended
  tenants cannot create collectors or installer tokens.
- Invoice enforcement command: `python manage.py update_invoice_statuses`.
- Billing thresholds are in `NovaUI/settings.py` as `BILLING_GRACE_DAYS`, `BILLING_RESTRICTED_DAYS`, and
  `BILLING_SUSPENDED_DAYS`.
- Billing PDFs are generated from `billing.models.Billing.create_invoice()` / `create_receipt()` and exposed through
  custom Django admin download URLs in `billing/admin.py`.
- For billing changes, verify invoice/receipt numbering, status transitions, due dates, and tenant billing-state side
  effects.

## Local Setup

- Install dependencies from `requirements.txt`; use the existing virtual environment when available:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

- Default development settings use SQLite at `db.sqlite3`.
- Redis cache defaults to `redis://127.0.0.1:6379/0`; Celery broker/backend use Redis database `1`.
- The local time zone is `Asia/Bangkok`.
- Local SMTP defaults to `localhost:1025`.
- Production settings are `NovaUI.settings_prod`; they require `DEBUG=False`, Postgres `DATABASE_URL`, explicit
  hosts/origins, Redis, Grafana credentials, and public OIDC URLs.

## Common Commands

Run the development server:

```bash
python manage.py migrate
python manage.py runserver
```

Run Django checks:

```bash
python manage.py check
```

Run pytest:

```bash
pytest
pytest config/tests/test_v1_security.py
```

Run Django tests:

```bash
python manage.py test
```

Create migrations after model changes:

```bash
python manage.py makemigrations
```

Validate migrations without writing files:

```bash
python manage.py makemigrations --check --dry-run
```

Apply migrations locally:

```bash
python manage.py migrate
```

Collect static files when changing static configuration or deployment behavior:

```bash
python manage.py collectstatic --noinput
```

Run background token cleanup locally with beat embedded:

```bash
celery -A NovaUI worker -l INFO -B
```

## Testing Expectations

- Prefer focused tests close to the changed app.
- Use `pytest` for the existing pytest-django setup. `pytest.ini` sets `DJANGO_SETTINGS_MODULE = NovaUI.settings`.
- For model changes, add or update migration files and run `python manage.py makemigrations --check --dry-run`.
- For view or template changes, add coverage for permissions, tenant scoping, form validation, and rendered state when
  practical.
- For integration changes, avoid live external calls in tests. Mock Grafana, Redis, APISIX, HTTP, and OIDC boundaries.
- If Redis or another service is required and unavailable locally, state that limitation in the final handoff.

## Django Conventions

- Keep tenant isolation explicit. Queries that expose tenant, node, billing, or user data must be scoped to the active
  tenant/user role.
- Avoid broad `Model.objects.all()` in request handlers unless the view is intentionally global admin-only.
- Use transactions for multi-step workflows that create or update related records.
- Make external side effects idempotent when possible, especially Grafana org/user mapping, Redis cache updates, token
  exchange, and billing transitions.
- Put reusable domain behavior near the app that owns it, such as `config/utils.py`, `config/integration.py`, app
  models, or app-specific services/helpers.
- Keep model `save()` overrides and signals small. Prefer explicit service functions for workflows with external calls.
- When adding migrations, verify they are deterministic and do not depend on local data.

## Security and Data Handling

- Never log raw credentials, tokens, generated passwords, session identifiers, or secrets.
- Treat node tokens and APISIX/Grafana credentials as sensitive. Redact them in logs, errors, templates, and tests.
- Preserve CSRF protection for browser forms.
- Confirm authentication and authorization on every endpoint that mutates tenant, node, billing, or user state.
- Validate and normalize user-controlled input before using it in shell scripts, generated install commands, URLs,
  headers, or external API calls.
- Do not add new `ALLOWED_HOSTS = ["*"]`, insecure cookies, disabled TLS checks, or debug-only behavior to production
  settings.

## Templates and UI

- This app uses Django templates with DaisyUI/Tailwind styling. Match existing template structure and component usage.
- Prefer shared components in `templates/components/` when adding forms or repeated UI patterns.
- Keep forms in `config/forms.py` and render them with DaisyUI classes/widgets.
- Keep server-rendered pages functional without adding unnecessary frontend build tooling.
- Do not introduce CDN dependencies for assets already vendored under `static/vendor/`.
- Ensure forms display validation errors and preserve submitted values where appropriate.

## External Integrations

Take care around code that touches:

- Grafana organization, users, dashboards, and data sources.
- OIDC provider behavior under `NovaUI/oidc_provider_settings.py`.
- Redis cache keys and feature/version invalidation.
- Celery tasks in `config/tasks.py`.
- Agent install scripts in `templates/agent/`.
- Billing artifacts rendered with WeasyPrint.

For changes in these areas, prefer tests with mocked boundaries and include a short note about any behavior that was not
exercised against real services.

## Deployment and CI

- NovaUI CI runs pytest, migration checks, static collection, production checks, and a container build.
- Security scans run separately in `security.yml`. Harbor image pushes for `dev` and `main` require the repository
  variable `ENABLE_HARBOR_PUBLISH=true` and configured secrets.
- Before handing off deployment-sensitive changes, run the closest local equivalent that is practical:

```bash
python manage.py check
pytest
python manage.py makemigrations --check --dry-run
```

- If production settings are relevant, also validate with:

```bash
python manage.py check --settings=NovaUI.settings_prod
```

- Only run Docker builds or network-dependent dependency installs when explicitly needed.

## Conventions to Preserve

- Use `record_audit()` for tenant/user/node/token lifecycle changes; existing actions are strings like
  `node_token.generated`, `node_token.used`, and `tenant_user.role_changed`.
- UI permissions are role strings from `TenantRole`; use `@tenant_role_required("admin", "editor")` rather than ad hoc
  checks when adding tenant-scoped views.
- Avoid direct external calls in tests. Existing tests use pytest fixtures and `monkeypatch`/`Mock` to isolate Grafana,
  Redis feature pushes, and Grafana logout.

## Handoff Format

When finishing work, report:

- Files changed.
- Tests or checks run.
- Any checks that could not be run and why.
- Any migrations, manual steps, environment variables, or service dependencies the user needs to know about.

## References

These are the reference projects that are used within the Nova Project

NovaCFG:

- The config server that consumes contract from redis
- local path: [NovaCFG](../NovaCFG)

NovaChart:

- Helm chart to deploy Nova
- local path [NovaCharts](../NovaCharts)

Nova-GitOps:

- Gitops that bootstrap argocd and deploys infrastructure
- [Nova-GitOps](../Nova-GitOps)