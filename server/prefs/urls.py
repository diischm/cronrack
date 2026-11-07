from django.urls import path

from notifications import views as notification_views
from prefs import views

urlpatterns = [
    path("settings/", views.prefs_index, name="prefs"),
    path("settings/test-alert/", views.test_alert, name="test-alert"),
    path("settings/appearance/", views.appearance, name="appearance"),
    path("settings/notifications/", notification_views.index, name="notifications"),
    path(
        "settings/notifications/preset/",
        notification_views.preset,
        name="notifications-preset",
    ),
]
