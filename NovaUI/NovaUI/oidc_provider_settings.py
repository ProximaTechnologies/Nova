import logging

from config.oidc_claims import tenant_claims_for_user

logger = logging.getLogger(__name__)


def userinfo(claims, user):
    # Populate claims dict.
    full_name = f"{user.first_name} {user.last_name}".strip() or user.get_username()
    claims.setdefault('sub', str(user.pk))
    claims['preferred_username'] = user.get_username()
    claims['login'] = user.get_username()
    claims['name'] = full_name
    claims['given_name'] = user.first_name
    claims['family_name'] = user.last_name
    claims['email'] = user.email

    return claims


from django.utils.translation import gettext_lazy as _
from oidc_provider.lib.claims import ScopeClaims


class CustomScopeClaims(ScopeClaims):
    info_groups = (
        _(u'Groups'),
        _(u'Groups that you are member of.'),
    )

    def scope_groups(self):
        # self.user - Django user instance.
        # self.userinfo - Dict returned by OIDC_USERINFO function.
        # self.scopes - List of scopes requested.
        # self.client - Client requesting this claims.
        dic = {"groups": tenant_claims_for_user(self.user)["groups"]}
        logger.debug(f"scope groups: {dic}")
        return dic

    info_roles = (
        _(u'Roles'),
        _(u'Nova tenant roles assigned to this user.'),
    )

    def scope_roles(self):
        dic = {"roles": tenant_claims_for_user(self.user)["roles"]}
        logger.debug(f"scope roles: {dic}")
        return dic

    # If you want to change the description of the profile scope, you can redefine it.
    info_profile = (
        _(u'Profile'),
        _(u'Another description.'),
    )
