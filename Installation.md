# Kubernetes bootstrap

Nova's Argo CD bootstrap is in [`gitops/`](gitops/README.md). It installs the
platform operators and services, then the Nova chart and the Forgejo-managed
runtime manifests. Only the `dev` environment is enabled. The `demo`, `stage`,
and `prod` values are reference configurations, not complete deployment profiles.

Prerequisites: Bash, Helm, kubectl, Python 3 with PyYAML for validation, and a
Kubernetes cluster. For local creation, also install kind and run Docker. The
kind configuration binds host ports 80 and 443 and uses the `standard` storage
class. Existing clusters need a suitable storage class and ingress configuration.

1. Choose where Argo CD reads the deployment configuration. By default it uses
   `gitops/` in `https://github.com/ProximaTechnologies/Nova.git`, branch `main`.
   Commit and push these files there before installing. To use a separate GitOps
   repository or another branch, use the external repository workflow below;
   it configures all matching references automatically.
2. Review `gitops/env/dev/` and `gitops/overlays/nova/dev/`. The imported profile
   contains local development passwords, `*.domain.com` hostnames, self-signed
   TLS, and disabled TLS verification. Configure secrets, DNS, TLS, image access,
   storage and resource limits for your deployment before exposing it. Nova's
   chart is sourced from `oci://ghcr.io/proximatechnologies/novacharts/nova`,
   pinned to version `0.1.0`. The GHCR package and configured container images
   must be accessible to the cluster. Public GitHub repository visibility does
   not by itself grant anonymous access to a GHCR package.
3. Validate and commit/push the configuration to the configured repository and branch:

   ```sh
   python3 -m venv /tmp/nova-gitops-venv
   /tmp/nova-gitops-venv/bin/pip install -r gitops/scripts/requirements-validation.txt
   export PYTHON=/tmp/nova-gitops-venv/bin/python
   gitops/scripts/validate-gitops.sh
   python3 -m unittest discover -s gitops/tests -v
   ```

## Repository configuration

The default Nova repository workflow remains:

```sh
# After committing and pushing gitops/ to Nova's main branch:
KUBE_CONTEXT=my-context gitops/bootstrap.sh install
# Or install into an existing kind cluster:
gitops/bootstrap.sh install --kind argo
```

To keep each installation's configuration in a separate repository, create an
empty repository on your Git hosting service first (or use an existing one).
The bootstrap CLI uses your local Git SSH agent or credential helper for clone
and push. Configure Git's `user.name` and `user.email` for commits. The CLI does
not create a GitHub/GitLab repository or change its visibility.

```sh
# Generate a separate checkout; source files in Nova are left unchanged.
gitops/bootstrap.sh prepare \
  --repo https://github.com/YOUR-ORG/nova-deployment.git \
  --branch main \
  --directory /tmp/nova-deployment

# Review and customize /tmp/nova-deployment/gitops/env/dev/ and overlays/.
# Commit the generated bundle and push it to the configured branch.
gitops/bootstrap.sh publish --directory /tmp/nova-deployment

# Install using that published configuration on an existing cluster.
KUBE_CONTEXT=my-context gitops/bootstrap.sh install --directory /tmp/nova-deployment
# Or, for an existing kind cluster:
gitops/bootstrap.sh install --directory /tmp/nova-deployment --kind argo
```

`prepare --repo` defaults to the Nova URL and `--branch` defaults to `main`.
The target keeps the same `gitops/` layout. Preparation rewrites the root
Application, repository registration, ApplicationSet Git sources/revisions,
and AppProject allowlists. External chart versions and the Forgejo runtime
repository are preserved. An existing remote branch is checked out; a new
branch starts from the checkout's current commit (the remote default branch
for a fresh clone). Empty repositories are supported.

Preparation requires a clean checkout and refuses an existing `gitops/` folder
unless you specify `--replace`. That flag replaces the entire target bundle
with the source template, including customized values; use it only when that is
intended. Other target-repository files are preserved. `publish` rejects staged
changes or modifications outside `gitops/`, commits only the bundle, and pushes
without force. `install --directory` verifies the bundle is clean and matches
the remote branch before making cluster changes. Further configuration changes
can be made in the target checkout, published, and reconciled by Argo CD.

For a new kind cluster with an external repository, publish first, then run:

```sh
/tmp/nova-deployment/gitops/argo-cfg/create-kind.sh argo
```

Private repository access from Argo CD is separate from your local Git login:
pass `ARGOCD_REPO_PASSWORD` (or `GITHUB_TOKEN`) when installing for HTTPS access.
SSH URLs require Argo CD SSH credentials to be configured separately. Do not
embed tokens or passwords in `--repo` URLs.

## Cluster bootstrap

For a new local kind cluster:

```sh
gitops/argo-cfg/create-kind.sh argo
```

This creates the cluster, configures in-cluster demo DNS rewrites, installs Argo
CD, and waits for application health. Add the demo hostnames to your host DNS or
hosts file at `127.0.0.1` to use the mapped ingress ports. CoreDNS rewrites only
affect pods. Set `RUN_DEPLOYMENT_CHECK=false` to skip the application health wait.
Optional `NOVA_CFG_IMAGE` and `NOVA_UI_IMAGE` load already-built local Docker
images into kind before Argo CD creates the workloads; the image references must
match your Nova chart values. The loader exports only the node architecture to
handle Docker caches with incomplete multi-platform indexes. It requires a
Docker CLI supporting `docker image save --platform`. To load additional cached
images into an existing kind cluster:

```sh
gitops/argo-cfg/load-kind-images.sh argo IMAGE[:TAG] [IMAGE[:TAG] ...]
```

Before application images are released, build them from this checkout and use
local images for kind. The development values name the GHCR repositories used
by this repository's image workflows; building a tag locally does not publish it.

```sh
IMAGE_TAG=ghcr.io/proximatechnologies/novaui:latest \
  NovaUI/scripts/build-image-with-supervisor.sh
docker build -t ghcr.io/proximatechnologies/novacfg:latest NovaCFG/main
NOVA_UI_IMAGE=ghcr.io/proximatechnologies/novaui:latest \
NOVA_CFG_IMAGE=ghcr.io/proximatechnologies/novacfg:latest \
  gitops/argo-cfg/create-kind.sh argo
```

For an existing cluster, load those tags with `load-kind-images.sh` instead.
The `NOVA_UI_IMAGE` and `NOVA_CFG_IMAGE` settings control which images are loaded;
they do not rewrite Helm values. Custom tags must also be set in the target
repository's `gitops/env/dev/Nova.yaml` before publishing.

For an existing kind cluster, install without recreating it:

```sh
gitops/argo-cfg/install-argocd-kind.sh argo
```

For an existing Kubernetes cluster:

```sh
KUBE_CONTEXT=my-context gitops/argo-cfg/install-argocd.sh
```

The installer uses the supplied context for every cluster operation without
switching kubectl's current context. It installs the development profile into
that cluster using a single Argo CD cluster alias, `dev`. It does not install a
storage provisioner, configure external DNS, or modify CoreDNS on existing
clusters. Adapt the environment values first. The manifest bundle currently
requires the Argo CD namespace and Helm release to both be `argocd`.

Private Git repositories can use `ARGOCD_REPO_PASSWORD` or `GITHUB_TOKEN` and
optional `ARGOCD_REPO_USERNAME`. Credentials are sent to a Kubernetes Secret,
not written into the repository. `ARGOCD_REPO_CREDS_URL` defaults to the Nova
repository URL. These Git credentials do not configure Helm OCI registry access. SSH repositories require separately configured Argo CD SSH
credentials. An absent token leaves existing credentials unchanged.

`ARGOCD_HELM_VALUES` selects a custom Argo CD values file.
`ARGOCD_HELM_CHART_VERSION` defaults to the archive's `9.5.15` and
`ARGOCD_HELM_TIMEOUT` to `10m`. Existing clusters default to `values.yaml`;
kind uses `values-kind.yaml`. The Forgejo webhook secret in the Argo CD values
must match `novaWebhook.secret` in the Forgejo environment values.

Check reconciliation and access Argo CD:

```sh
KUBE_CONTEXT=kind-argo gitops/scripts/test-deployments.sh --include-root --env dev --wait 1800
kubectl --context kind-argo -n argocd port-forward svc/argocd-server 8080:80
kubectl --context kind-argo -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

Open `http://localhost:8080` and sign in as `admin` using the retrieved password.
A successful installer exit means Argo CD is installed and the root Application
was submitted; use the deployment check to verify the complete stack. On first
sync, operators and their CRDs may require retries before Nova becomes healthy.
The Forgejo runtime Application becomes available after Nova creates its repository.

To deliberately delete and recreate a local cluster, including all its data:

```sh
gitops/argo-cfg/reset.sh --force argo
```

Before enabling stage or prod, supply real secrets and a complete runtime overlay,
register distinct target clusters, and update the matching AppProject destinations
and ApplicationSet generators. Do not alias multiple environments to the same
cluster: the imported services share namespace and resource names.

## Live verification, 2026-11-21

Verified on a dedicated ARM64 kind cluster with Kubernetes v1.37.0 and Argo CD
chart 9.5.15. The deployment health check passed for all 14 Applications,
including the root and Forgejo-generated runtime repository. Nova returned its
login redirect over the HTTPS ingress; Grafana reported a healthy database;
Loki and Mimir readiness endpoints returned HTTP 200.

The run exercised external-repository preparation and publishing against a
local bare Git repository. Argo CD read a test-only Git mirror on the Docker
network, including NovaCharts revision
`0162a4c7b5c34c757b82ae3551bbd5f607dd657b`. This avoided putting the user's SSH
credentials in the cluster. Anonymous HTTPS access to NovaCharts failed, so a
that historical run used the mirror. Bootstrap now targets the versioned GHCR
OCI chart instead of the Git chart source.
The default repository URLs in this checkout were not changed for the test.

Cached platform and application images were imported into kind. The uncached
MinIO pull returned HTTP 401 from Quay during this run; cached images allowed
functional verification to finish. This verifies application reconciliation and
startup, but does not establish that every image is anonymously downloadable
from a clean machine. Ensure the configured registries are accessible.

## Public OCI verification, 2026-11-23

Retested with the public OCI chart `nova:0.1.0` (digest
`sha256:d3185a153c0750dbe58471acc047e617c17838c051ea8a4c5b78dacc67e3cba8`).
Argo CD read configuration directly from the public Nova repository's isolated
`codex/oci-bootstrap-test-20261121` branch; no local Git mirror was used.

NovaUI and NovaCFG images had not been released, so this run built both from
this checkout and loaded the `bootstrap-test` tags into kind. The test branch
uses those tags; the development defaults in this checkout use the `latest`
tags targeted by the image publishing workflows. These defaults require either
local builds loaded into kind or published, accessible GHCR images.

All 14 Applications reached `Synced`, `Healthy`, and `Succeeded`. Nova's
`/healthz/` returned HTTP 200 and Grafana reported its database healthy. Reducing
the development Dragonfly request to 512Mi allowed all three database instances
to schedule on the single-node test cluster.

MinIO's configured images still failed anonymous downloads, including a retry
without Docker credentials. The existing local MinIO images were loaded to
complete the functional test. An entirely uncached installation still requires
an accessible MinIO image source (or a separately configured S3 backend).
