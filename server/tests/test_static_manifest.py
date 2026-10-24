"""Cache-busting: collectstatic with the manifest storage that production uses.

The rest of the suite uses plain StaticFilesStorage (settings_test.py) so it never needs
collectstatic. This file runs collectstatic once into a temporary STATIC_ROOT and checks
what production gets: hashed names, rewritten font URLs, {% static %} using them, and
WhiteNoise serving those files with the right headers.
"""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.template import Context, Template
from django.test import Client, override_settings

from cronrack.middleware import CONTENT_SECURITY_POLICY, PERMISSIONS_POLICY

MANIFEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
# components.css -> components.<12 hex characters>.css
HASHED = re.compile(r"\.[0-9a-f]{12}\.")


@pytest.fixture(scope="module")
def collected(tmp_path_factory):
    """STATIC_ROOT after `collectstatic` with the manifest storage, and its manifest."""
    root = tmp_path_factory.mktemp("staticfiles")
    with override_settings(STATIC_ROOT=str(root), STORAGES=MANIFEST_STORAGES):
        call_command("collectstatic", interactive=False, verbosity=0)
    paths = json.loads((root / "staticfiles.json").read_text())["paths"]
    return root, paths


@pytest.fixture
def manifest_storage(collected):
    """Production-like settings (DEBUG off, manifest storage) pointed at that STATIC_ROOT."""
    root, paths = collected
    with override_settings(DEBUG=False, STATIC_ROOT=str(root), STORAGES=MANIFEST_STORAGES):
        yield paths


def test_css_gets_a_hashed_name(collected):
    root, paths = collected
    hashed = paths["cronrack/css/components.css"]
    assert HASHED.search(hashed), hashed
    assert (root / hashed).is_file()


def test_font_urls_in_css_point_at_hashed_fonts(collected):
    # The @font-face rules live in tokens.css.
    root, paths = collected
    css = (root / paths["cronrack/css/tokens.css"]).read_text()
    for font in (
        "manrope-latin-wght.woff2",
        "manrope-latin-ext-wght.woff2",
        "jetbrains-mono-latin-wght.woff2",
    ):
        hashed_font = paths[f"cronrack/fonts/{font}"]
        assert HASHED.search(hashed_font)
        assert f'url("../fonts/{Path(hashed_font).name}")' in css
        assert f'url("../fonts/{font}")' not in css


def test_static_tag_uses_hashed_names(manifest_storage):
    html = Template("{% load static %}{% static 'cronrack/js/theme.js' %}").render(Context())
    assert html == "/static/" + manifest_storage["cronrack/js/theme.js"]
    assert HASHED.search(html)


@pytest.mark.django_db
def test_rendered_page_links_hashed_files(manifest_storage, client, alice):
    # alice exists, so /login/ renders instead of redirecting to first-run setup.
    html = client.get("/login/").content.decode()
    for name in (
        "cronrack/css/tokens.css",
        "cronrack/css/components.css",
        "cronrack/css/pages.css",
        "cronrack/js/theme-init.js",
        "cronrack/favicon.svg",
    ):
        assert f'"/static/{manifest_storage[name]}"' in html, name
    assert '"/static/cronrack/css/components.css"' not in html


def test_debug_uses_plain_names_without_a_manifest(tmp_path):
    # Development (DEBUG=1): no collectstatic has run, {% static %} still works.
    with override_settings(DEBUG=True, STATIC_ROOT=str(tmp_path), STORAGES=MANIFEST_STORAGES):
        html = Template("{% load static %}{% static 'cronrack/css/components.css' %}").render(
            Context()
        )
    assert html == "/static/cronrack/css/components.css"


def test_hard_coded_paths_in_500_page_still_exist(collected):
    # 500.html uses plain /static/... paths on purpose (no template tags that could fail).
    root, _ = collected
    page = (Path(settings.BASE_DIR) / "templates" / "500.html").read_text()
    paths = re.findall(r'"/static/([^"]+)"', page)
    assert paths, "500.html links no static files any more; drop this test"
    for path in paths:
        assert (root / path).is_file(), path


# --- serving the collected files (WhiteNoise) ------------------------------------------


@pytest.fixture
def served(collected):
    """A client for production-like settings: DEBUG off, collected files, one host name.

    WhiteNoise reads STATIC_ROOT when the middleware is created, which a new Client does
    on its first request, inside these settings.
    """
    root, paths = collected
    with override_settings(
        DEBUG=False,
        STATIC_ROOT=str(root),
        STORAGES=MANIFEST_STORAGES,
        ALLOWED_HOSTS=["cronrack.lan"],
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SECURE_SSL_REDIRECT=True,
        SECURE_HSTS_SECONDS=31_536_000,
    ):
        yield Client(HTTP_HOST="cronrack.lan", HTTP_X_FORWARDED_PROTO="https"), paths


def test_hashed_file_is_served_and_cached_forever(served):
    client, paths = served
    response = client.get("/static/" + paths["cronrack/css/components.css"])
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/css")
    assert "immutable" in response["Cache-Control"]
    assert "public" in response["Cache-Control"]


def test_static_files_get_the_security_headers(served):
    # The same headers Caddy used to add (or pass on) for /static/*.
    client, paths = served
    response = client.get("/static/" + paths["cronrack/css/components.css"])
    assert response["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response["Permissions-Policy"] == PERMISSIONS_POLICY
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Strict-Transport-Security"].startswith("max-age=31536000")
    assert response["Referrer-Policy"] == "same-origin"
    assert not response.cookies


def test_plain_name_is_revalidated(served):
    # Plain names (the 500 page uses them) may change with the next image.
    client, _ = served
    response = client.get("/static/cronrack/css/components.css")
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-cache"


def test_gzip_for_browsers_that_accept_it(served):
    client, paths = served
    url = "/static/" + paths["cronrack/css/components.css"]
    response = client.get(url, HTTP_ACCEPT_ENCODING="gzip")
    assert response["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in response["Vary"]


def test_plain_http_is_redirected_to_https(served):
    client, paths = served
    response = client.get(
        "/static/" + paths["cronrack/css/components.css"], HTTP_X_FORWARDED_PROTO="http"
    )
    assert response.status_code == 301
    assert response["Location"].startswith("https://cronrack.lan/static/")


@pytest.mark.parametrize("host", ["10.42.0.17", "10.42.0.17:8000", "evil.example"])
def test_static_files_need_an_allowed_host(served, host):
    # Served only for CRONRACK_DOMAIN, like every page (WhiteNoise after CommonMiddleware).
    client, paths = served
    response = client.get("/static/" + paths["cronrack/css/components.css"], HTTP_HOST=host)
    assert response.status_code == 400


def test_missing_static_file_is_404(served):
    client, _ = served
    assert client.get("/static/cronrack/css/nope.css").status_code == 404
