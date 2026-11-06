"""Email configuration must work for installations outside the original domain."""
import os
import subprocess
import sys


def test_email_environment_configuration():
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "NovaUI.settings",
        "NOVA_ENV": "development",
        "DEBUG": "True",
        "EMAIL_HOST": "smtp.example.test",
        "EMAIL_PORT": "465",
        "EMAIL_USE_SSL": "True",
        "EMAIL_USE_TLS": "False",
        "EMAIL_HOST_USER": "mailer",
        "EMAIL_HOST_PASSWORD": "test-only",
        "DEFAULT_FROM_EMAIL": "Nova <nova@example.test>",
    }
    check = subprocess.run(
        [sys.executable, "-c", """
from NovaUI import settings as s
assert s.EMAIL_HOST == 'smtp.example.test'
assert s.EMAIL_PORT == 465
assert s.EMAIL_USE_SSL and not s.EMAIL_USE_TLS
assert s.EMAIL_HOST_USER == 'mailer'
assert s.EMAIL_HOST_PASSWORD == 'test-only'
assert s.DEFAULT_FROM_EMAIL == 'Nova <nova@example.test>'
"""], env=env, capture_output=True, text=True,
    )
    assert check.returncode == 0, check.stderr
    env["EMAIL_USE_TLS"] = "True"
    invalid = subprocess.run(
        [sys.executable, "-c", "from NovaUI import settings"],
        env=env, capture_output=True, text=True,
    )
    assert invalid.returncode != 0
    assert "mutually exclusive" in invalid.stderr
