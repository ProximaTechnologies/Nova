import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

from .grafana_urls import grafana_external_url

GRAFANA_RESPONSE_TYPE = "code"
GRAFANA_RESPONSE_TYPE_DESCRIPTION = "code (Authorization Code Flow)"
GRAFANA_SCOPES = ("openid", "profile", "email", "groups", "roles")


@dataclass(frozen=True)
class GrafanaOidcBootstrapResult:
    client: object
    client_created: bool
    client_secret_rotated: bool
    rsa_key: object
    rsa_key_created: bool
    redirect_uri: str


def default_grafana_redirect_uri() -> str:
    external_url = grafana_external_url()
    if not external_url:
        raise ImproperlyConfigured(
            "GRAFANA_EXTERNAL_URL or GRAFANA_URL must be configured to build the Grafana OAuth redirect URI."
        )
    return f"{external_url}/login/generic_oauth"


def generate_rsa_private_key_pem() -> str:
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _generated_client_id() -> str:
    return secrets.token_urlsafe(24)


def _generated_client_secret() -> str:
    return secrets.token_hex(32)


def bootstrap_grafana_oidc_client(
        *,
        client_name: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        rotate_client_secret: bool = False,
) -> GrafanaOidcBootstrapResult:
    from oidc_provider.models import Client, RSAKey, ResponseType

    resolved_client_name = client_name or settings.GRAFANA_OAUTH_CLIENT_NAME
    resolved_redirect_uri = redirect_uri or default_grafana_redirect_uri()

    with transaction.atomic():
        response_type, _ = ResponseType.objects.get_or_create(
            value=GRAFANA_RESPONSE_TYPE,
            defaults={"description": GRAFANA_RESPONSE_TYPE_DESCRIPTION},
        )
        client = Client.objects.filter(name=resolved_client_name).order_by("pk").first()
        client_created = client is None
        secret_rotated = False

        if client_created:
            client = Client(
                name=resolved_client_name,
                client_id=client_id or _generated_client_id(),
                client_secret=client_secret or _generated_client_secret(),
            )
        else:
            if client_id:
                client.client_id = client_id
            if client_secret or rotate_client_secret:
                client.client_secret = client_secret or _generated_client_secret()
                secret_rotated = True

        client.client_type = "confidential"
        client.jwt_alg = "RS256"
        client.require_consent = False
        client.reuse_consent = True
        client.redirect_uris = [resolved_redirect_uri]
        client.scope = list(GRAFANA_SCOPES)
        client.website_url = grafana_external_url() or ""
        client.save()
        client.response_types.set([response_type])

        rsa_key = RSAKey.objects.order_by("pk").first()
        rsa_key_created = rsa_key is None
        if rsa_key is None:
            rsa_key = RSAKey.objects.create(key=generate_rsa_private_key_pem())

    return GrafanaOidcBootstrapResult(
        client=client,
        client_created=client_created,
        client_secret_rotated=secret_rotated,
        rsa_key=rsa_key,
        rsa_key_created=rsa_key_created,
        redirect_uri=resolved_redirect_uri,
    )


def get_grafana_oauth_client_credentials() -> tuple[str, str]:
    from oidc_provider.models import Client

    client = Client.objects.filter(name=settings.GRAFANA_OAUTH_CLIENT_NAME).order_by("pk").first()
    if not client or not client.client_id or not client.client_secret:
        raise ImproperlyConfigured(
            "Grafana OIDC client is not configured. Run "
            "`python manage.py bootstrap_grafana_oidc`."
        )

    return client.client_id, client.client_secret
