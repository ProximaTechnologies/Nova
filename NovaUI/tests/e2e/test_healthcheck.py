from django.urls import reverse


def test_healthz_is_public(client):
    response = client.get(reverse("healthz"))

    assert response.status_code == 200
    assert response.content == b"ok\n"
