from django.urls import path

from hosts import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("api/v1/overview.json", views.overview_json, name="overview-json"),
    path("hosts/add/", views.host_add, name="host-add"),
    path("hosts/collect-all/", views.host_collect_all, name="host-collect-all"),
    path("hosts/example/", views.host_example, name="host-example"),
    path("hosts/<int:host_id>/", views.host_detail, name="host-detail"),
    path("hosts/<int:host_id>/settings/", views.host_settings, name="host-settings"),
    path("hosts/<int:host_id>/collect/", views.host_collect, name="host-collect"),
    path("hosts/<int:host_id>/rotate/", views.host_rotate, name="host-rotate"),
    path("hosts/<int:host_id>/revoke/", views.host_revoke, name="host-revoke"),
    path("hosts/<int:host_id>/delete/", views.host_delete, name="host-delete"),
    path(
        "api/v1/hosts/<int:host_id>/status.json",
        views.host_status_json,
        name="host-status-json",
    ),
]
