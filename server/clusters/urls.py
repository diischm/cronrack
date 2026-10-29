from django.urls import path

from clusters import views

urlpatterns = [
    path("clusters/", views.cluster_list, name="clusters"),
    path("clusters/<int:cluster_id>/", views.cluster_detail, name="cluster-detail"),
    path("clusters/<int:cluster_id>/settings/", views.cluster_settings, name="cluster-settings"),
    path("clusters/<int:cluster_id>/delete/", views.cluster_delete, name="cluster-delete"),
    path(
        "api/v1/clusters/<int:cluster_id>/detail.json",
        views.cluster_detail_json,
        name="cluster-detail-json",
    ),
]
