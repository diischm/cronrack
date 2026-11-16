"""Public files that make CronRack installable as an app on a phone (a PWA).

No login, no session, no cookies: a phone fetches these before anyone logs in. None of
them contains anything about the user, the hosts or this server (no hostnames, no
version, no settings): the manifest and the offline page are the same for everyone, and
sw.js only lists the offline page and its static files.

- GET /manifest.webmanifest: name, colours and icons for "Install app" / home screen.
- GET /sw.js: the service worker. It shows the offline page when the server can't be
  reached and caches nothing else (see pwa/sw.js).
- GET /offline/: that offline page. Rendered without the request, so no context
  processor (user, version, pod name) can ever put anything personal into it.
- GET /install: the page the phone-install QR code opens (Settings -> Phone app, the
  terminal). It shows the "Install app" button (install.js). Public and rendered without
  the request too: no user, version or hostname, no cookie, no CSRF token.
"""

import hashlib
import json
from pathlib import Path

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.views.decorators.http import require_GET, require_safe

from pwa.qr import is_loopback_host

OFFLINE_PATH = "/offline/"
GROUND = "#0b0c0f"

# sw.js is a template with two placeholders, filled in per request (the static URLs are
# hashed in production, so they are only known at run time). Read per request, so an edit
# shows up without a restart; it is a 3 KB file, and only fetched when a browser checks
# for a worker update.
SW_TEMPLATE = Path(__file__).with_name("sw.js")
CACHE_PLACEHOLDER = "__CRONRACK_CACHE__"
PRECACHE_PLACEHOLDER = "__CRONRACK_PRECACHE__"
CACHE_PREFIX = "cronrack-shell-v1-"

# The only files the service worker stores: the offline page and what it loads.
PRECACHE_STATIC = (
    "cronrack/css/offline.css",
    "cronrack/js/offline.js",
    "cronrack/fonts/manrope-latin-wght.woff2",
    "cronrack/icons/icon-192.png",
    "cronrack/icons/icon-512.png",
    "cronrack/icons/icon-maskable-512.png",
    "cronrack/icons/apple-touch-icon.png",
    "cronrack/icons/favicon-32.png",
)


def _icon(name: str, size: int, purpose: str) -> dict[str, str]:
    return {
        "src": static(f"cronrack/icons/{name}"),
        "sizes": f"{size}x{size}",
        "type": "image/png",
        "purpose": purpose,
    }


@require_GET
def manifest(request: HttpRequest) -> HttpResponse:
    """GET /manifest.webmanifest: the same JSON for everyone (no orientation lock)."""
    data = {
        "name": "CronRack",
        "short_name": "CronRack",
        "description": "What needs my attention? Your homelab on one calm screen.",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": GROUND,
        "theme_color": GROUND,
        "icons": [
            _icon("icon-192.png", 192, "any"),
            _icon("icon-512.png", 512, "any"),
            _icon("icon-maskable-512.png", 512, "maskable"),
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


def render_offline_page() -> str:
    """The offline page's HTML. No request: no context processors, no CSRF token."""
    return render_to_string("pwa/offline.html")


def precache_urls() -> list[str]:
    """What the worker stores: the offline page, then its static files (hashed URLs)."""
    return [OFFLINE_PATH, *(static(name) for name in PRECACHE_STATIC)]


def service_worker_source() -> str:
    """sw.js with its cache name and precache list filled in (JSON, so always valid JS)."""
    precache = precache_urls()
    # The cache name changes whenever a precached file (hashed URL) or the offline page
    # changes, so an updated worker drops the old copies on activate.
    digest = hashlib.sha256(
        (json.dumps(precache) + render_offline_page()).encode("utf-8")
    ).hexdigest()[:8]
    source = SW_TEMPLATE.read_text(encoding="utf-8")
    return source.replace(CACHE_PLACEHOLDER, json.dumps(CACHE_PREFIX + digest)).replace(
        PRECACHE_PLACEHOLDER, json.dumps(precache)
    )


@require_GET
def service_worker(request: HttpRequest) -> HttpResponse:
    """GET /sw.js: the service worker, allowed to control the whole site."""
    response = HttpResponse(
        service_worker_source(), content_type="application/javascript; charset=utf-8"
    )
    # The browser must always re-check sw.js, so a fixed or updated worker arrives at once.
    response["Cache-Control"] = "no-cache"
    # The worker may control the whole site ("/"), which is also its own folder.
    response["Service-Worker-Allowed"] = "/"
    return response


@require_GET
def offline(request: HttpRequest) -> HttpResponse:
    """GET /offline/: the page the worker shows when the server can't be reached."""
    return HttpResponse(render_offline_page())


@require_safe  # GET and HEAD (uptime checkers and link previews ask with HEAD)
def install(request: HttpRequest) -> HttpResponse:
    """GET /install: how to put CronRack on the home screen.

    Browsers only offer to install an app over HTTPS (or on localhost), so on plain http
    the page says that and shows nothing else. The check uses the Host header, which Django
    has already matched against ALLOWED_HOSTS.
    """
    secure = request.is_secure() or is_loopback_host(request.get_host())
    return HttpResponse(render_to_string("pwa/install.html", {"secure": secure}))
