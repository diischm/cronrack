from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path

from cronrack import dev_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("accounts.urls")),
    path("", include("hosts.urls")),
    path("", include("activity.urls")),
    path("", include("prefs.urls")),
    # Public: install.sh and the agent package (no secrets inside).
    path("", include("agents.urls")),
    # Dev-only: 404 unless DEBUG=1 (checked inside the view).
    path("dev/preview/", dev_views.preview, name="dev-preview"),
]

# uvicorn does not serve static files (CSS/JS, the admin's styles) by itself.
# In development (DEBUG=1) Django serves them from /static/. In production this
# route doesn't exist; static files are served separately (deployment phase).
# (staticfiles_urlpatterns() also checks DEBUG; the explicit `if` makes it obvious.)
if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()
