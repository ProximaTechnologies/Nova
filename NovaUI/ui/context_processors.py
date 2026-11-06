from django.conf import settings


def billing(request):
    return {"billing_enabled": settings.BILLING_ENABLED}
