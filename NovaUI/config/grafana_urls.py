from django.conf import settings
from urllib.parse import urlparse


def _clean_url(url):
    return (url or "").rstrip("/")


def _has_scheme(url):
    parsed = urlparse(url or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def grafana_internal_url():
    protocol = getattr(settings, "GRAFANA_PROTOCOL", None)
    host = getattr(settings, "GRAFANA_HOST", None)
    if protocol and host:
        return _clean_url(f"{protocol}://{host}")

    configured_url = _clean_url(getattr(settings, "GRAFANA_INTERNAL_URL", None))
    if configured_url:
        return configured_url

    return _clean_url(getattr(settings, "GRAFANA_URL", None))


def grafana_external_url():
    external_candidates = (
        _clean_url(getattr(settings, "GRAFANA_EXTERNAL_URL", None)),
        _clean_url(getattr(settings, "GRAFANA_PUBLIC_URL", None)),
    )
    for url in external_candidates:
        if _has_scheme(url):
            return url

    configured_url = _clean_url(getattr(settings, "GRAFANA_URL", None))
    if configured_url:
        return configured_url

    for url in external_candidates:
        if url:
            return url

    return grafana_internal_url()
