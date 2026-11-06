# Security reports

Do not put credentials, installer tokens, or exploit details in a public issue.
Use the repository's Security > Report a vulnerability feature when enabled.
If private reporting is unavailable, ask a maintainer for a private channel
without disclosing the vulnerability. Include the affected revision, impact,
and a minimal reproduction with synthetic data.

NovaUI is pre-release software. There is no published support lifetime or
security response SLA yet. Review fixes against the current main branch.

Before public release, maintainers must enable private vulnerability reporting,
review secret-scanner results for both the working tree and Git history, and
rotate any credentials that were previously committed. Deleting a credential
from the latest revision does not remove it from history.
