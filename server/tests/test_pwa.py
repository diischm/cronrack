"""The installable app (PWA): web app manifest, icons, service worker, offline page.

- /manifest.webmanifest, /sw.js and /offline/ are public: a phone fetches them before
  anyone logs in, and they hold nothing about the user, the hosts or the server.
- The icons are committed PNGs (scripts/make_pwa_icons.py draws them once). Their size
  and colour type are read from the PNG header with the standard library, so the tests
  need no Pillow.
- The service worker's behaviour is checked by running sw.js in Node with fake caches
  and a fake network (tests/sw_harness.mjs). Those tests are skipped without Node.
"""

import json
import os
import re
import shutil
import struct
import subprocess  # nosec B404: runs the local Node test harness with fixed arguments
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.test import Client

from cronrack import SERVER_VERSION
from hosts.models import Host

SERVER = Path(settings.BASE_DIR)
STATIC = SERVER / "static" / "cronrack"
ICONS = STATIC / "icons"
OLD_ICONS = (STATIC / "apple-touch-icon.png", STATIC / "favicon-32.png")
HARNESS = Path(__file__).with_name("sw_harness.mjs")
NODE = shutil.which("node")
# The Node tests are the only check of what the worker does, so in CI (GitHub sets CI=true,
# and its runners have Node) a missing Node is a failure, not a skip.
IN_CI = os.environ.get("CI", "").lower() == "true"
needs_node = pytest.mark.skipif(NODE is None and not IN_CI, reason="Node is not installed")

PUBLIC_PATHS = ("/manifest.webmanifest", "/sw.js", "/offline/", "/install")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_RGB = 2  # IHDR colour type: 2 = RGB (no alpha channel), 6 = RGBA


def png_info(path: Path) -> dict:
    """Width, height, colour type and chunk names of a PNG, read from its bytes."""
    data = path.read_bytes()
    assert data[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    chunks = []
    offset = 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunks.append(data[offset + 4 : offset + 8].decode("ascii"))
        offset += 12 + length  # length + type + data + CRC
    assert chunks[0] == "IHDR"
    width, height, _depth, colour_type = struct.unpack(">IIBB", data[16:26])
    return {"size": (width, height), "colour_type": colour_type, "chunks": chunks}


def static_file_for(url: str) -> Path | None:
    """The source file behind a /static/... URL (plain storage in tests)."""
    prefix = "/" + settings.STATIC_URL.lstrip("/")
    assert url.startswith(prefix), url
    found = finders.find(url[len(prefix) :])
    return Path(found) if found else None


# --- Manifest ---------------------------------------------------------------------------


@pytest.fixture
def manifest(client: Client, alice):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    return response


def test_manifest_content_type(manifest):
    assert manifest["Content-Type"].split(";")[0] == "application/manifest+json"


def test_manifest_fields(manifest):
    data = json.loads(manifest.content)
    assert data["name"] == "CronRack"
    assert data["short_name"] == "CronRack"
    assert data["description"] == "What needs my attention? Your homelab on one calm screen."
    assert data["id"] == "/"
    assert data["start_url"] == "/"
    assert data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["background_color"] == "#0b0c0f"
    assert data["theme_color"] == "#0b0c0f"


def test_manifest_has_no_orientation_lock(manifest):
    assert "orientation" not in json.loads(manifest.content)


def test_manifest_icons(manifest):
    icons = json.loads(manifest.content)["icons"]
    found = {(icon["sizes"], icon["purpose"]) for icon in icons}
    assert ("192x192", "any") in found
    assert ("512x512", "any") in found
    assert ("512x512", "maskable") in found
    for icon in icons:
        assert icon["type"] == "image/png"
        path = static_file_for(icon["src"])
        assert path is not None and path.is_file(), icon["src"]
        width, height = png_info(path)["size"]
        assert icon["sizes"] == f"{width}x{height}"


def test_manifest_builds_icon_urls_with_static(manifest):
    srcs = {icon["src"] for icon in json.loads(manifest.content)["icons"]}
    assert srcs == {
        static("cronrack/icons/icon-192.png"),
        static("cronrack/icons/icon-512.png"),
        static("cronrack/icons/icon-maskable-512.png"),
    }


# --- Icons ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("icon-192.png", (192, 192)),
        ("icon-512.png", (512, 512)),
        ("icon-maskable-512.png", (512, 512)),
        ("apple-touch-icon.png", (180, 180)),
        ("favicon-32.png", (32, 32)),
    ],
)
def test_icon_sizes(name, size):
    assert png_info(ICONS / name)["size"] == size


@pytest.mark.parametrize("name", ["icon-maskable-512.png", "apple-touch-icon.png"])
def test_full_bleed_icons_have_no_transparency(name):
    # Plain RGB, and no tRNS chunk (which could still make one RGB colour transparent).
    info = png_info(ICONS / name)
    assert info["colour_type"] == PNG_RGB
    assert "tRNS" not in info["chunks"]


def test_old_icons_are_gone():
    for path in OLD_ICONS:
        assert not path.exists(), path


def test_favicon_svg_uses_the_glass_orange():
    svg = (STATIC / "favicon.svg").read_text().lower()
    assert "#f26b2e" in svg
    assert "#d9541e" not in svg


def test_icon_script_is_a_manual_tool_and_pillow_is_no_dependency():
    script = (SERVER.parent / "scripts" / "make_pwa_icons.py").read_text()
    docstring = script.split('"""')[1]
    assert "Pillow" in docstring
    for requirements in (SERVER.parent / "requirements-dev.txt", SERVER / "requirements.txt"):
        assert "pillow" not in requirements.read_text().lower(), requirements


# --- Service worker: what the server sends ----------------------------------------------


@pytest.fixture
def sw(client: Client, alice):
    response = client.get("/sw.js")
    assert response.status_code == 200
    return response


def precache_of(sw_text: str) -> list[str]:
    match = re.search(r"^const PRECACHE = (\[.*\]);$", sw_text, re.MULTILINE)
    assert match, "sw.js has no PRECACHE line"
    return json.loads(match.group(1))


def expected_precache() -> list[str]:
    return [
        "/offline/",
        static("cronrack/css/offline.css"),
        static("cronrack/js/offline.js"),
        static("cronrack/fonts/manrope-latin-wght.woff2"),
        static("cronrack/icons/icon-192.png"),
        static("cronrack/icons/icon-512.png"),
        static("cronrack/icons/icon-maskable-512.png"),
        static("cronrack/icons/apple-touch-icon.png"),
        static("cronrack/icons/favicon-32.png"),
    ]


def test_sw_headers(sw):
    assert sw["Content-Type"].split(";")[0] == "application/javascript"
    assert "no-cache" in sw["Cache-Control"]
    assert sw["Service-Worker-Allowed"] == "/"
    assert sw["X-Content-Type-Options"] == "nosniff"


def test_static_url_is_a_path_on_this_origin():
    # sw.js matches precached files by url.pathname: STATIC_URL must stay a plain path.
    assert static("cronrack/css/offline.css").startswith("/")
    assert "//" not in static("cronrack/css/offline.css")


def test_sw_precaches_only_the_offline_page_and_its_files(sw):
    precache = precache_of(sw.content.decode())
    assert precache == expected_precache()
    for url in precache[1:]:
        path = static_file_for(url)
        assert path is not None and path.is_file(), url


def test_sw_precache_has_no_app_or_api_paths(sw):
    static_prefix = "/" + settings.STATIC_URL.lstrip("/")
    for url in precache_of(sw.content.decode()):
        assert url == "/offline/" or url.startswith(static_prefix), url
        assert "/api/" not in url


def test_sw_cache_name_is_versioned(sw):
    pattern = r'^const CACHE = "cronrack-shell-v1-[0-9a-f]{8}";$'
    assert re.search(pattern, sw.content.decode(), re.MULTILINE)


def test_sw_cache_name_changes_with_the_offline_page(client: Client, alice, monkeypatch):
    from pwa import views

    first = client.get("/sw.js").content
    real = views.render_offline_page
    monkeypatch.setattr(views, "render_offline_page", lambda: real() + "<!-- changed -->")
    assert client.get("/sw.js").content != first


# --- Offline page -----------------------------------------------------------------------


@pytest.fixture
def offline(client: Client, alice):
    response = client.get("/offline/")
    assert response.status_code == 200
    return response.content.decode()


def test_offline_page_text(offline):
    assert "Can't reach your CronRack server." in offline
    assert "Check your connection or VPN, then try again." in offline
    assert re.search(r'<button type="button"[^>]*>\s*Try again\s*</button>', offline)
    # Feedback for "Try again" (offline.js fills it), announced to screen readers.
    assert re.search(r'<p [^>]*id="offline-status" role="status" aria-live="polite"></p>', offline)


def test_offline_page_loads_only_precached_files(offline):
    precache = set(expected_precache())
    urls = re.findall(r'(?:href|src)="([^"]+)"', offline)
    assert urls, "the offline page links nothing"
    for url in urls:
        assert url in precache, url


def test_offline_page_has_no_inline_script_or_style(offline):
    for tag in re.findall(r"<script\b[^>]*>", offline):
        assert "src=" in tag, tag
    assert "<style" not in offline
    assert "style=" not in offline


def test_offline_page_has_no_status_dots_glows_or_section_titles(offline):
    for word in ("lamp", "status-icon", "g-glow", "g-backdrop", "section-title"):
        assert word not in offline, word


# --- Public, and leak-free --------------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_for_an_anonymous_client(client: Client, alice, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "Location" not in response


@pytest.mark.django_db
@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_before_the_first_account_exists(client: Client, path):
    # FirstRunMiddleware sends every other page to /setup/ until an account exists.
    response = client.get(path)
    assert response.status_code == 200
    assert "Location" not in response


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_responses_set_no_cookie(client: Client, alice, path):
    assert not client.get(path).cookies


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_responses_leak_nothing(
    client: Client, django_user_model, path, settings, monkeypatch
):
    user = django_user_model.objects.create_user(username="leaky-user-7f3a", password="x-unused")
    Host.objects.create(owner=user, name="leaky-hostname-7f3a")
    settings.CRONRACK_DOMAIN = "leaky-domain-7f3a.example"
    monkeypatch.setenv("POD_NAME", "leaky-pod-7f3a")
    monkeypatch.setenv("NODE_NAME", "leaky-node-7f3a")
    client.force_login(user)  # even for a logged-in user nothing personal comes back
    body = client.get(path).content.decode()
    assert "leaky-" not in body, path
    # The version as a whole token (a hex hash in sw.js could contain its digits by chance).
    assert not re.search(rf"(?<![\w.]){re.escape(SERVER_VERSION)}(?![\w.])", body), path


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_paths_refuse_post(client: Client, alice, path):
    assert client.post(path).status_code == 405


# --- Base template ----------------------------------------------------------------------


@pytest.fixture
def login_page(client: Client, alice):
    return client.get("/login/").content.decode()


def test_base_head_has_the_pwa_tags(login_page):
    for tag in (
        '<link rel="manifest" href="/manifest.webmanifest">',
        '<meta name="theme-color" content="#0b0c0f">',
        '<meta name="mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-capable" content="yes">',
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">',
        '<meta name="apple-mobile-web-app-title" content="CronRack">',
        f'<link rel="apple-touch-icon" href="{static("cronrack/icons/apple-touch-icon.png")}">',
    ):
        assert tag in login_page, tag
    assert static("cronrack/icons/favicon-32.png") in login_page


def test_base_viewport_keeps_scale_and_covers_the_notch(login_page):
    match = re.search(r'<meta name="viewport" content="([^"]+)">', login_page)
    assert match
    parts = {part.strip() for part in match.group(1).split(",")}
    assert {"width=device-width", "initial-scale=1", "viewport-fit=cover"} <= parts


def test_base_registers_the_sw_from_a_static_file(login_page):
    assert f'<script src="{static("cronrack/js/sw-register.js")}" defer></script>' in login_page
    js = (STATIC / "js" / "sw-register.js").read_text()
    assert 'navigator.serviceWorker.register("/sw.js", { scope: "/" })' in js


def test_500_page_and_styleguide_use_the_new_icons():
    page = (SERVER / "templates" / "500.html").read_text()
    assert "/static/cronrack/icons/apple-touch-icon.png" in page
    assert "/static/cronrack/icons/favicon-32.png" in page
    styleguide = (SERVER / "templates" / "dev" / "_styleguide_set.html").read_text()
    for text in (page, styleguide, (SERVER / "templates" / "base.html").read_text()):
        assert "cronrack/apple-touch-icon.png" not in text
        assert "cronrack/favicon-32.png" not in text


# --- CSP --------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/login/", *PUBLIC_PATHS])
def test_csp_allows_manifest_and_worker_from_self_only(client: Client, alice, path):
    csp = client.get(path)["Content-Security-Policy"]
    assert "manifest-src 'self'" in csp
    assert "worker-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp


# --- Safe areas -------------------------------------------------------------------------


def test_safe_area_insets_pad_the_bars_and_the_page():
    pages = (STATIC / "css" / "pages.css").read_text()
    glass = (STATIC / "css" / "glass.css").read_text()
    for side in ("top", "left", "right"):
        assert f"env(safe-area-inset-{side}" in pages, side
        assert f"env(safe-area-inset-{side}" in glass, side
    assert "env(safe-area-inset-bottom" in pages  # the bottom nav
    for css in (pages, glass):
        assert "display-mode: standalone" not in css  # same look installed or in a tab


# --- Service worker: behaviour, run in Node ---------------------------------------------


def run_harness(sw_text: str) -> dict:
    result = subprocess.run(  # nosec B603: fixed argument list, no shell, local file
        [NODE, str(HARNESS)],
        input=sw_text,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def behaviour(sw):
    return run_harness(sw.content.decode())


@needs_node
def test_sw_install_precaches_and_skips_waiting(behaviour, sw):
    install = behaviour["install"]
    assert install["failed"] is False
    assert install["added"] == precache_of(sw.content.decode())
    assert install["offlineStored"] == "offline"
    assert install["skipWaiting"] is True


@needs_node
@pytest.mark.parametrize("behaviour_name", ["redirect", "error500"])
def test_sw_install_refuses_a_redirected_or_failed_offline_page(behaviour, behaviour_name):
    # Never store a redirect (e.g. to /setup/ or /login/) or an error page as "offline".
    result = behaviour["install"][behaviour_name]
    assert result["failed"] is True
    assert result["skipWaiting"] is False
    assert result["offlineStored"] is None


@needs_node
def test_sw_activate_deletes_only_its_old_caches_and_claims(behaviour):
    assert behaviour["activate"]["deleted"] == ["cronrack-shell-v1-00000000"]
    assert behaviour["activate"]["claimed"] is True


@needs_node
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("navigate_ok", "network"),
        ("navigate_network_error", "offline"),
        ("navigate_hang", "offline"),  # a VPN that is down: no answer at all
        ("navigate_502", "offline"),
        ("navigate_503", "offline"),
        ("navigate_504", "offline"),
        ("navigate_500", "network"),
        ("navigate_404", "network"),
        ("navigate_redirect", "network"),
        ("navigate_network_error_offline_not_cached", "error"),
        ("navigate_502_offline_not_cached", "network"),
        ("post_navigate", "not-handled"),
        ("cross_origin_navigate", "not-handled"),
        ("api_get", "not-handled"),
        ("app_css_get", "not-handled"),
        ("cross_origin_get", "not-handled"),
        ("precached_css_get", "cache"),
        ("precached_css_with_query", "not-handled"),
    ],
)
def test_sw_fetch_handling(behaviour, case, expected):
    assert behaviour["fetch"][case] == expected


@needs_node
def test_sw_never_writes_responses_into_the_cache(behaviour):
    # Only install's addAll fills the cache; no fetch ever calls put() or add().
    assert behaviour["writesAfterInstall"] == 0
