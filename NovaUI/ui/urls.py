# myapp/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("", views.tenant_list, name="tenant_list"),
    path("<slug:tenant_id_secure>/", views.dashboard, name="dashboard"),
    path("<slug:tenant_id_secure>/collectors/", views.collector_list, name="list_collectors"),  # your table view
    path("<slug:tenant_id_secure>/collectors/add", views.collector_add, name="add_collector"),
    path("<slug:tenant_id_secure>/collectors/add/platform", views.collector_add_platform_step,
         name="add_collector_platform_step"),
    path("<slug:tenant_id_secure>/collectors/add/features", views.collector_add_features_step,
         name="add_collector_features_step"),
    path("<slug:tenant_id_secure>/collectors/<slug:node_id>/delete/", views.collector_delete, name="delete_collector"),
    path("<slug:tenant_id_secure>/collectors/<slug:node_id>/edit/", views.collector_edit, name="edit_collector"),
    path("<slug:tenant_id_secure>/collectors/<slug:node_id>/run_script/", views.collector_run_script,
         name="run_script_collector"),
    path("<slug:tenant_id_secure>/debug/oidc-claims/", views.oidc_claims_debug, name="oidc_claims_debug"),

    path("<slug:tenant_id_secure>/settings/team/", views.settings_team, name="settings_team"),
    path("<slug:tenant_id_secure>/settings/team/add/", views.settings_team_add, name="settings_team_add"),
    path("<slug:tenant_id_secure>/settings/team/delete/<slug:tu_id>/", views.settings_team_delete,
         name="settings_team_delete"),
    path("<slug:tenant_id_secure>/settings/team/edit/<slug:tu_id>/", views.settings_team_edit,
         name="settings_team_edit"),

    path("<slug:tenant_id_secure>/settings/limits/", views.settings_team_limits, name="settings_team_limits"),
    path("<slug:tenant_id_secure>/settings/billing/", views.settings_billing, name="settings_billing"),
    path("<slug:tenant_id_secure>/settings/audit/", views.settings_audit, name="settings_audit"),
]
