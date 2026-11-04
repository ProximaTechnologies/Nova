from .factories import UserFactory


def test_create_user(db):
    user = UserFactory()
    assert user.email != ""
    assert user.username != ""
