# Nova GitOps

Integrated from the supplied `Nova-GitOps-main.zip`. See
[Installation](INSTALLATION.md) for prerequisites, configuration, bootstrap,
verification, and reset commands.

- `bootstrap.sh`: prepare/publish an external GitOps repository, or install using
  the Nova repository configuration by default (`bootstrap.sh install`).
- `argo-cfg/`: Argo CD install scripts, root Application, repository registration,
  and kind configuration.
- `appsets/`: platform and Nova ApplicationSets plus environment AppProjects.
- `env/`: Helm values; only `dev` is active.
- `overlays/`: runtime resources consumed by the Nova Application.
- `scripts/`: offline manifest validation and live deployment health checks.
- `tests/`: bootstrap regression tests using fake CLIs.

All monorepo sources resolve paths from the repository root (`gitops/...`).
Nova uses the published OCI chart `ghcr.io/proximatechnologies/novacharts/nova`
at version `0.1.0`. Other external Helm charts retain the archive versions. Forgejo's generated runtime repository remains
separate from this source repository.

External repository example (requires Git and the validation Python dependencies):

```sh
gitops/bootstrap.sh prepare --repo https://github.com/YOUR-ORG/nova-deployment.git --directory /tmp/nova-deployment
# Review/customize the generated environment values before publishing.
gitops/bootstrap.sh publish --directory /tmp/nova-deployment
KUBE_CONTEXT=my-context gitops/bootstrap.sh install --directory /tmp/nova-deployment
```

The remote repository must already exist; it can be empty. Use `--branch` during
preparation to select a branch other than `main`. Local Git authentication is
used for publishing; Argo CD repository credentials are configured at install.
