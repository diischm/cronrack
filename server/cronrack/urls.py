from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path

from cronrack import dev_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # Dev-only: 404 unless DEBUG=1 (checked inside the view).
    path("dev/preview/", dev_views.preview, name="dev-preview"),
]

# uvicorn does not serve static files (the admin's CSS/JS) by itself.
# This helper adds a static-file route only when DEBUG=1; in production
# static files are served separately (decided in the deployment phase).
urlpatterns += staticfiles_urlpatterns()
