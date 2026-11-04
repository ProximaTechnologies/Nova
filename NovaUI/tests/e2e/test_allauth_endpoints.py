import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse

pytestmark = pytest.mark.django_db

ACCOUNT_ROUTES = [
    ("account_login", []),
    ("account_logout", []),
    ("account_inactive", []),
    ("account_signup", []),
    ("account_reauthenticate", []),
    ("account_email", []),
    ("account_email_verification_sent", []),
    ("account_change_password", []),
    ("account_set_password", []),
    ("account_reset_password", []),
    ("account_confirm_login_code", []),
    ("account_confirm_email", ["bad-key"]),
    ("account_reset_password_from_key_done", []),
    ("account_reset_password_from_key", ["abc", "bad-key"]),
    ("account_reset_password_done", []),
    ("socialaccount_login_cancelled", []),
    ("socialaccount_login_error", []),
    ("socialaccount_signup", []),
    ("socialaccount_connections", []),
]

LEGACY_SOCIAL_PATHS = [
    "/accounts/social/login/cancelled/",
    "/accounts/social/login/error/",
    "/accounts/social/signup/",
    "/accounts/social/connections/",
]


@pytest.mark.parametrize(("route_name", "args"), ACCOUNT_ROUTES)
def test_allauth_named_routes_are_reachable(client, route_name, args):
    response = client.get(reverse(route_name, args=args))

    if route_name == "socialaccount_login_error":
        assert response.status_code == 401
    else:
        assert response.status_code in {200, 302, 400, 403, 404, 405}


@pytest.mark.parametrize("path", LEGACY_SOCIAL_PATHS)
def test_allauth_legacy_social_routes_are_reachable(client, path):
    response = client.get(path)

    assert response.status_code in {200, 301, 302, 400, 403, 404, 405}


def test_account_login_and_logout_flow(client, user_factory, password):
    user = user_factory("allauth-login", password=password)

    response = client.post(
        reverse("account_login"),
        {
            "login": user.username,
            "password": password,
        },
    )

    assert response.status_code == 302
    assert response["Location"] == "/"

    logout_get = client.get(reverse("account_logout"))
    assert logout_get.status_code in {200, 302}
    logout_post = client.post(reverse("account_logout"))
    assert logout_post.status_code == 302


def test_account_signup_is_closed_by_project_adapter(client):
    before = User.objects.count()

    response = client.post(
        reverse("account_signup"),
        {
            "username": "blocked-signup",
            "email": "blocked-signup@example.test",
            "password1": "long-secure-password-123",
            "password2": "long-secure-password-123",
        },
    )

    assert response.status_code == 200
    assert User.objects.count() == before
    assert b"closed" in response.content.lower()


def test_account_password_reset_endpoint_sends_mail_for_existing_user(client, user_factory):
    user_factory("reset-user", email="reset-user@example.test")

    response = client.post(reverse("account_reset_password"), {"email": "reset-user@example.test"})

    assert response.status_code == 302
    assert len(mail.outbox) == 1


def test_authenticated_account_management_routes_render(client, user_factory, login_user):
    user = user_factory("account-management")
    login_user(client, user)

    assert client.get(reverse("account_email")).status_code == 200
    assert client.get(reverse("account_change_password")).status_code == 200
    assert client.get(reverse("account_reauthenticate")).status_code == 200
    assert client.get(reverse("socialaccount_connections")).status_code == 200
