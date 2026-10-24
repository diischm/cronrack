"""Development static files: the browser must re-check them on every load (DEBUG only)."""

import importlib

import pytest
from django.test import Client, override_settings
from django.urls import clear_url_caches

import cronrack.urls

URL = "/static/cronrack/js/host-preview.js"


def rebuild_urls():
    """cronrack/urls.py reads DEBUG once, at import: import it again under the current setting."""
    importlib.reload(cronrack.urls)
    clear_url_caches()


@pytest.fixture
def debug_urls():
    """The URL list a DEBUG=1 dev server has (pytest-django runs tests with DEBUG off)."""
    with override_settings(DEBUG=True):
        rebuild_urls()
    yield  # DEBUG is back off here: tests turn it on themselves where they need it
    rebuild_urls()


def test_debug_static_files_are_revalidated(client: Client, debug_urls):
    with override_settings(DEBUG=True):
        response = client.get(URL)
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-cache"
    assert response.has_header("Last-Modified")  # so the re-check can answer 304


def test_unchanged_file_answers_304_with_no_cache(client: Client, debug_urls):
    with override_settings(DEBUG=True):
        last_modified = client.get(URL)["Last-Modified"]
        response = client.get(URL, headers={"If-Modified-Since": last_modified})
    assert response.status_code == 304
    assert response["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("url", [URL, "/static/cronrack/css/components.css"])
def test_without_debug_django_has_no_static_route(client: Client, url):
    """Production: only WhiteNoise serves static files, and only the collected ones
    (tests/test_static_manifest.py); Django's dev route doesn't exist."""
    rebuild_urls()  # built with DEBUG off, like production
    response = client.get(url)  # the test STATIC_ROOT is empty
    assert response.status_code == 404
    assert not response.has_header("Last-Modified")  # the dev view never ran


def test_dev_view_refuses_when_debug_is_off(client: Client, debug_urls):
    """Even if the route exists, the view itself checks DEBUG again."""
    response = client.get(URL)  # DEBUG is off here
    assert response.status_code == 404
    assert not response.has_header("Last-Modified")
