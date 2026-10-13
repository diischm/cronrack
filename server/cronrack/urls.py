from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]

# uvicorn does not serve static files (the admin's CSS/JS) by itself.
# This helper adds a static-file route only when DEBUG=1; in production
# static files are served separately (decided in the deployment phase).
urlpatterns += staticfiles_urlpatterns()
