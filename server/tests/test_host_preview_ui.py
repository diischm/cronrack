"""The overview's host preview: markup and script rules (the behaviour is checked in e2e)."""

import re
from pathlib import Path

import pytest
from django.test import Client

from .test_host_views import make_host
from .test_preview import XSS_NAME, scan

pytestmark = pytest.mark.django_db

STATIC_JS = Path(__file__).resolve().parent.parent / "static/cronrack/js"
PREVIEW_JS = STATIC_JS / "host-preview.js"


def test_overview_has_one_preview_dialog_and_its_script(alice_client: Client, alice):
    make_host(alice, "nas", "green", None)
    html = alice_client.get("/").content.decode()

    assert html.count('<dialog class="host-preview surface" id="host-preview"') == 1
    assert 'aria-labelledby="host-preview-name"' in html
    assert '<h2 class="host-preview__name" id="host-preview-name" tabindex="-1"></h2>' in html
    assert '<template id="host-preview-template">' in html
    assert '<script src="/static/cronrack/js/host-preview.js" defer></script>' in html
    # Units stay real links, so the page works without JavaScript.
    assert re.search(r'<a class="rack-unit card-press" href="/hosts/\d+/">', html)

    page = scan(html)
    assert all(script.get("src") for script in page.scripts)
    assert page.inline_script_text == ""
    assert page.inline_styles == []
    assert page.event_handlers == []


def test_preview_dialog_holds_no_host_data(alice_client: Client, alice):
    make_host(alice, XSS_NAME, "green", None)
    html = alice_client.get("/").content.decode()

    dialog = html[html.index('<dialog class="host-preview') : html.index("</dialog>")]
    template = html[
        html.index('<template id="host-preview-template">') : html.index(
            "</template>", html.index('<template id="host-preview-template">')
        )
    ]
    for part in (dialog, template):
        assert "script" not in part.lower()
        assert "&lt;script&gt;" not in part  # the name is only put in by JS, as text


def test_welcome_example_is_not_a_host_link(alice_client: Client):
    html = alice_client.get("/").content.decode()
    # host-preview.js only takes over /hosts/<digits>/ links; the example is /hosts/example/.
    assert 'class="rack-unit card-press rack-unit--example" href="/hosts/example/"' in html


def test_preview_js_builds_dom_safely():
    source = PREVIEW_JS.read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in source, banned
    assert "console." not in source
    assert "dataset" not in source  # nothing stored in data- attributes
    assert ".style" not in source  # motion via element.animate(), never style=""
    assert "startViewTransition" not in source
    # Navigation only to the unit's own /hosts/<digits>/ link, checked by this pattern.
    assert r"const HOST_PATH = /^\/hosts\/(\d+)\/$/;" in source
    assert "location.assign(url)" in source
    assert "location.search" not in source and "URLSearchParams" not in source


@pytest.mark.parametrize("name", ["overview.js", "copy.js"])
def test_touched_scripts_stay_safe(name):
    source = (STATIC_JS / name).read_text()
    assert "innerHTML" not in source
    assert "console." not in source
