from unittest.mock import patch
from .factories import TenantFactory


@patch("config.models.create_tenant")
def test_tenant_creation(
        mock_create_tenant,
        db,
):
    tenant = TenantFactory()  # this will call Tenant.save(), but create_tenant is mocked

    mock_create_tenant.assert_called_once_with(tenant.name, tenant)


@patch("config.models.create_tenant")
@patch("config.models.delete_tenant")
@patch("config.models.update_grafana_org_mapping")
def test_tenant_deletion(
        mock_update_grafana_org_mapping,
        mock_delete_tenant,
        mock_create_tenant,
        db,
):
    tenant = TenantFactory()
    mock_create_tenant.assert_called_once_with(tenant.name, tenant)
    tenant.delete()

    mock_delete_tenant.assert_called_once_with(tenant)
    mock_update_grafana_org_mapping.assert_called_once()
