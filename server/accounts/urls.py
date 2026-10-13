from django.contrib.auth.views import LogoutView
from django.urls import path

from accounts import views

urlpatterns = [
    path("setup/", views.first_run, name="setup"),
    path("login/", views.CronRackLoginView.as_view(), name="login"),
    # Django's LogoutView only accepts POST, so a link or <img> on another site
    # can't log the user out.
    path("logout/", LogoutView.as_view(), name="logout"),
]
