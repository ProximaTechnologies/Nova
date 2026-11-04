import hashlib
import json
import subprocess
import tempfile
from contextlib import contextmanager

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class SealedSecretError(Exception):
    pass


def secret_data_fingerprint(data):
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()


def seal_secret_data(*, name, namespace, data):
    """
    Return Bitnami SealedSecrets encryptedData for a Kubernetes Secret.

    Values are sealed with kubeseal --raw so raw credentials are never written
    into GitOps manifests.
    """
    if not data:
        return {}

    with _kubeseal_cert_file() as cert_path:
        return {
            key: _seal_value(
                value=value,
                name=name,
                namespace=namespace,
                cert_path=cert_path,
            )
            for key, value in data.items()
        }


@contextmanager
def _kubeseal_cert_file():
    cert_file = getattr(settings, "GITOPS_SEALED_SECRETS_CERT_FILE", "")
    cert_pem = getattr(settings, "GITOPS_SEALED_SECRETS_PUBLIC_CERT", "")
    if cert_file:
        yield cert_file
        return
    if cert_pem:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".pem") as temp_file:
            temp_file.write(cert_pem)
            temp_file.flush()
            yield temp_file.name
        return
    raise ImproperlyConfigured(
        "Set GITOPS_SEALED_SECRETS_CERT_FILE or GITOPS_SEALED_SECRETS_PUBLIC_CERT "
        "before rendering APISIX credential SealedSecrets."
    )


def _seal_value(*, value, name, namespace, cert_path):
    kubeseal_binary = getattr(settings, "GITOPS_KUBESEAL_BINARY", "kubeseal")
    scope = getattr(settings, "GITOPS_SEALED_SECRETS_SCOPE", "strict")
    command = [
        kubeseal_binary,
        "--raw",
        "--scope",
        scope,
        "--name",
        name,
        "--namespace",
        namespace,
        "--cert",
        cert_path,
        "--from-file=/dev/stdin",
    ]
    try:
        result = subprocess.run(
            command,
            input=str(value).encode("utf-8"),
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ImproperlyConfigured(f"{kubeseal_binary} is required to render SealedSecrets.") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise SealedSecretError(f"kubeseal failed for {namespace}/{name}: {stderr}")

    sealed = result.stdout.decode("utf-8").strip()
    if not sealed:
        raise SealedSecretError(f"kubeseal returned empty encrypted data for {namespace}/{name}.")
    return sealed
