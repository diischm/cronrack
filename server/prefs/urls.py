from django.urls import path

from prefs import views

urlpatterns = [
    path("settings/", views.prefs_index, name="prefs"),
    path("settings/test-alert/", views.test_alert, name="test-alert"),
]
