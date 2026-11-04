from django.utils import timezone

from .models import CollectorAuth
from .utils import generate_password

APISIX_CRD_PROVISIONER = "apisix-crd"


def collector_consumer_name(node, service):
    return f"{node.node_id.hex}-{service}"


def collector_auth_defaults(node):
    return {
        "config_consumer_name": collector_consumer_name(node, "config"),
        "mimir_consumer_name": collector_consumer_name(node, "mimir"),
        "loki_consumer_name": collector_consumer_name(node, "loki"),
        "provisioner_backend": APISIX_CRD_PROVISIONER,
        "provisioned_at": timezone.now(),
        "config_password": generate_password(),
        "mimir_password": generate_password(),
        "loki_password": generate_password(),
        "tempo_password": generate_password(),
    }


def repair_missing_collector_auth(auth, node):
    defaults = {
        "config_consumer_name": collector_consumer_name(node, "config"),
        "mimir_consumer_name": collector_consumer_name(node, "mimir"),
        "loki_consumer_name": collector_consumer_name(node, "loki"),
        "provisioner_backend": APISIX_CRD_PROVISIONER,
    }
    password_fields = ("config_password", "mimir_password", "loki_password", "tempo_password")
    for field in password_fields:
        if not getattr(auth, field):
            defaults[field] = generate_password()

    if auth.provisioned_at is None:
        defaults["provisioned_at"] = timezone.now()

    update_fields = []
    for field, value in defaults.items():
        if not getattr(auth, field):
            setattr(auth, field, value)
            update_fields.append(field)

    if update_fields:
        auth.save(update_fields=update_fields + ["updated_at"])

    return bool(update_fields)


def ensure_collector_auth(node):
    auth, created = CollectorAuth.objects.get_or_create(
        node=node,
        defaults=collector_auth_defaults(node),
    )
    repaired = False if created else repair_missing_collector_auth(auth, node)
    return auth, created, repaired
