# NovaUI release checklist

Release scope is NovaUI and its required source/build dependencies in this
monorepo. A NovaUI-only source archive cannot build the bundled supervisor
without separately supplying SUPERVISOR_REPO.

Before the first public release:

- Select a project license and add its full text as LICENSE. Confirm the grant
  covers all contributed source. This is still pending; do not describe the
  project as licensed open source until it is done.
- Preserve vendored license notices listed in THIRD_PARTY_NOTICES.md.
- Enable private vulnerability reporting and review working-tree and historical
  secret scans. Rotate real exposed secrets before publishing history.
- Pass NovaUI CI and the existing security workflow. CI tests PostgreSQL;
  local tests default to SQLite. Review dependency and container findings.
- Exercise tenant creation, Grafana OIDC login, collector enrollment, Redis
  delivery to NovaCFG, GitOps sync, and billing PDF generation in a disposable
  deployment. Mocked tests do not establish live integration compatibility.
- Build from the release checkout with an explicit supervisor SemVer, register
  its artifacts using bootstrap_install, and verify binary checksums.
- Record the revision, dependency versions, image digest, supported architecture,
  migration steps, and known limitations in the release notes.

No release, image push, or deployment is performed by the local setup commands.
Pushes to dev and main publish NovaUI images to ghcr.io/<owner>/novaui
using GITHUB_TOKEN, tagged with the commit SHA and latest-dev or latest.
