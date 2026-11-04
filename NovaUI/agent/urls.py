# myapp/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("installer/<slug:token>", views.installer, name="installer"),
    path("config/refresh", views.refresh_config, name="agent_config_refresh"),
    path("supervisor/bootstrap/<str:node_id>", views.supervisor_bootstrap, name="agent_supervisor_bootstrap"),
    path("supervisor/desired/<str:node_id>", views.supervisor_desired, name="agent_supervisor_desired"),
    path(
        "supervisor/version/<str:node_id>/<str:version>",
        views.report_supervisor_version,
        name="agent_report_supervisor_version",
    ),
    path(
        "supervisor/version/<str:node_id>/<str:version>/",
        views.report_supervisor_version,
        name="agent_report_supervisor_version_slash",
    ),
    path("supervisor/binary/<str:version>/<slug:arch>", views.supervisor_binary, name="agent_supervisor_binary"),
    path("check-in/<str:node_id>/<str:version>", views.check_in, name="agent_check_in"),
    path("check-in/<str:node_id>/<str:version>/", views.check_in, name="agent_check_in_slash"),
    path("version/<str:node_id>/<str:version>", views.report_version, name="agent_report_version"),
    path("version/<str:node_id>/<str:version>/", views.report_version, name="agent_report_version_slash"),
]
