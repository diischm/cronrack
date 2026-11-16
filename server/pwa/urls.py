from django.urls import path

from pwa import views

# All four are public (no login) and skip the first-run redirect: see
# accounts.middleware.PUBLIC_PATHS. They must live at the root: a service worker only
# controls the path it is served from and below, so /sw.js can control the whole app.
urlpatterns = [
    path("manifest.webmanifest", views.manifest, name="pwa-manifest"),
    path("sw.js", views.service_worker, name="pwa-service-worker"),
    path("offline/", views.offline, name="pwa-offline"),
    # Exactly /install: no trailing slash (the QR code holds this address, so there is one).
    path("install", views.install, name="pwa-install"),
]
