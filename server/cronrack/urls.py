from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from cronrack import dev_views

urlpatterns = [
    path("", include("accounts.urls")),
    path("", include("hosts.urls")),
    path("", include("clusters.urls")),
    path("", include("activity.urls")),
    path("", include("prefs.urls")),
    # Public: install.sh and the agent package (no secrets inside).
    path("", include("agents.urls")),
    # Dev-only: 404 unless DEBUG=1 (checked inside each view).
    path("dev/preview/", dev_views.preview, name="dev-preview"),
    path("dev/styleguide/", dev_views.styleguide, name="dev-styleguide"),
]

# Django's admin only when CRONRACK_ENABLE_ADMIN allows it (see settings); otherwise
# /admin/ doesn't exist and answers 404.
if settings.CRONRACK_ENABLE_ADMIN:
    urlpatterns.insert(0, path("admin/", admin.site.urls))

# Static files (CSS/JS, the admin's styles): in production WhiteNoise serves the files
# collectstatic gathered into the image (settings.MIDDLEWARE), and this route doesn't
# exist. In development (DEBUG=1) Django serves the source files from /static/.
# dev_views.static is Django's static view plus "Cache-Control: no-cache" (and it checks
# DEBUG again itself).
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, view=dev_views.static)
