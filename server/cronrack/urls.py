from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
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
    # Dev-only: 404 unless DEBUG=1 (checked inside each view).
    path("dev/preview/", dev_views.preview, name="dev-preview"),
    path("dev/styleguide/", dev_views.styleguide, name="dev-styleguide"),
]

# uvicorn does not serve static files (CSS/JS, the admin's styles) by itself.
# In development (DEBUG=1) Django serves them from /static/. In production this
# route doesn't exist: Caddy serves the collected files (see caddy/Caddyfile).
# dev_views.static is Django's static view plus "Cache-Control: no-cache" (and it checks
# DEBUG again itself).
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, view=dev_views.static)
