from django.urls import path

from network import views

urlpatterns = [
    path("network/", views.network_page, name="network"),
    path("network/view/", views.network_view, name="network-view"),
    path("network/settings/", views.network_settings, name="network-settings"),
    path("api/v1/network.json", views.network_json, name="network-json"),
]
