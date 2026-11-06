# Contributing to NovaUI

Start with the setup in README.md. Use Python 3.12 and the pinned dependencies
in requirements.txt. Keep pyproject.toml and uv.lock consistent when changing
Python dependencies. Run commands from NovaUI/:

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py collectstatic --noinput
python -m pytest -q
```

Tests mock external integrations; do not point them at production services.
CI additionally runs against PostgreSQL and builds the image with the sibling
Nova-Supervisor source. Security scans are in ../.github/workflows/security.yml.

For pull requests, explain the problem, resulting behavior, and checks run.
Include migrations for model changes and focused tests for authorization,
tenant isolation, and provisioning changes. Keep credentials out of fixtures,
logs, screenshots, and issues. Report vulnerabilities using SECURITY.md.
