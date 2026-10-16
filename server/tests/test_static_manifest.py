"""Cache-busting: collectstatic with the manifest storage that production uses.

The rest of the suite uses plain StaticFilesStorage (settings_test.py) so it never needs
collectstatic. This file runs collectstatic once into a temporary STATIC_ROOT and checks
what production gets: hashed names, rewritten font URLs, and {% static %} using them.
"""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.template import Context, Template
from django.test import override_settings

MANIFEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
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
