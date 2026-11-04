import factory
from django.contrib.auth.models import User
from config.models import TenantUser, Tenant


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    username = factory.Faker("user_name")
    email = factory.Faker("email")
    password = factory.Faker("password")


class TenantFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Tenant

    name = factory.Faker("name")
    grafana_org_id = factory.Faker("random_int")
    random_prefix = factory.Faker("word")


class TenantUserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TenantUser
