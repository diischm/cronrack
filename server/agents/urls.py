from django.urls import path

from agents import views

urlpatterns = [
    path("agent/install.sh", views.install_sh, name="agent-install-sh"),
    path("agent/download/cronrack-agent.tar.gz", views.tarball, name="agent-tarball"),
    path(
        "agent/download/cronrack-agent.tar.gz.sha256",
        views.tarball_sha256,
        name="agent-tarball-sha256",
    ),
]
