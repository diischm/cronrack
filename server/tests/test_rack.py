"""The overview rack, the Welcome example and /hosts/example/."""

import json
import re
from pathlib import Path

import pytest
from django.test import Client, override_settings

from hosts.models import Host
from hosts.samples import EXAMPLE_DETAIL_CONTEXT, PREVIEW_CONTEXT

from .test_preview import scan

STATIC = Path(__file__).resolve().parent.parent / "static/cronrack/js"
EXAMPLE_NAME = "Welcome to CronRack!"
pytestmark = pytest.mark.django_db


# --- /hosts/example/ ---------------------------------------------------------------


def test_example_page_renders_for_a_user_without_hosts(alice_client: Client):
    response = alice_client.get("/hosts/example/")

    assert response.status_code == 200
    html = response.content.decode()
    assert "This is an example. Nothing here is real." in html
    assert '<a class="btn btn--primary" href="/hosts/add/">Add your first host</a>' in html
    assert EXAMPLE_NAME in html
    # Two yellow issues (no red), so the header says "Heads up" like the amber Welcome lamp.
    assert re.search(r'class="chip chip--amber">yellow</span>\s*Reboot required', html)
    assert re.search(r'class="chip chip--amber">yellow</span>\s*Disk / at 87%', html)
    assert "chip--red" not in html
    assert '<span class="lamp lamp--amber" aria-hidden="true"></span>' in html
    assert '<span class="chip chip--amber">Heads up</span>' in html
    for section in ("Containers", "Systemd", "Disk"):
        assert f'<h2 class="section-title">{section}</h2>' in html
    # No agent and no settings behind it: both keys are hidden.
    assert "Check now" not in html
    assert "Host settings" not in html
    assert "/collect/" not in html


def test_example_page_is_csp_clean(alice_client: Client):
    page = scan(alice_client.get("/hosts/example/").content.decode())
    assert all(script.get("src") for script in page.scripts)
    assert page.inline_script_text == ""
    assert page.inline_styles == []
    assert page.event_handlers == []


def test_example_page_redirects_when_the_user_has_hosts(alice_client: Client, alice):
    Host.objects.create(owner=alice, name="nas")

    response = alice_client.get("/hosts/example/")

    assert response.status_code == 302
    assert response["Location"] == "/"


def test_example_page_only_counts_my_own_hosts(alice_client: Client, bob):
    Host.objects.create(owner=bob, name="bobs-host")  # someone else's host doesn't count
    assert alice_client.get("/hosts/example/").status_code == 200


def test_example_page_needs_login(client: Client, alice):
    response = client.get("/hosts/example/")
    assert response.status_code == 302
    assert response["Location"].startswith("/login/")


def test_example_page_is_get_only(alice_client: Client):
    assert alice_client.post("/hosts/example/").status_code == 405


def test_example_page_writes_nothing(alice_client: Client):
    alice_client.get("/hosts/example/")
    assert not Host.objects.exists()


# --- the Welcome example on the overview -------------------------------------------


def test_overview_without_hosts_shows_the_example_and_an_empty_slot(alice_client: Client):
    html = alice_client.get("/").content.decode()

    assert '<h1 class="page-title" id="overview-title">No hosts yet</h1>' in html
    assert '<span id="overview-sub">0 hosts</span>' in html
    assert '<a class="rack-unit card-press rack-unit--example" href="/hosts/example/">' in html
    assert EXAMPLE_NAME in html
    example = html[html.index("rack-unit--example") :]
    example = example[: example.index("</a>")]
    assert "lamp--amber" in example
    assert '<span class="chip chip--neutral">Example</span>' in html
    assert "This is an example host. Add your own and it disappears." in html
    assert 'class="rack__slot rack__slot--empty well"' in html
    assert "Add your first host" in html


def test_example_is_in_no_count(alice_client: Client):
    html = alice_client.get("/").content.decode()
    data = alice_client.get("/api/v1/overview.json").json()

    assert data["hosts"] == []
    assert data["attention_count"] == 0
    assert data["status_counts"] == {"red": 0, "yellow": 0, "green": 0, "offline": 0}
    assert data["hosts_online"] == 0
    assert "Welcome" not in json.dumps(data)
    assert "example" not in json.dumps(data).lower()
    # Desktop tiles: all zero, "Hosts online 0 of 0", no offline note.
    for key in ("red", "yellow", "green"):
        assert f'<span id="tile-{key}">0</span>' in html
    assert '<span id="tile-online">0</span>' in html
    assert '<span class="tile__unit" id="tile-online-unit">of 0</span>' in html
    assert 'id="tile-offline" hidden>' in html


def test_example_disappears_once_a_host_exists(alice_client: Client, alice):
    Host.objects.create(owner=alice, name="nas")

    html = alice_client.get("/").content.decode()

    assert EXAMPLE_NAME not in html
    assert "/hosts/example/" not in html
    assert "rack__slot--empty" not in html
    assert '<span id="overview-sub">1 host · updated never</span>' in html


# --- rack units ----------------------------------------------------------------------


def test_offline_unit_has_an_unlit_lamp_and_no_chip(alice_client: Client, alice):
    from .test_host_views import make_silent_host

    make_silent_host(alice, "diicpi")

    html = alice_client.get("/").content.decode()
    unit = html[html.index('<a class="rack-unit card-press rack-unit--offline"') :]
    unit = unit[: unit.index("</a>")]

    assert "lamp--off" in unit
    assert 'class="chip' not in unit
    assert "<strong>Offline</strong> · last report 28 h ago" in unit
    assert "Nothing needs you" not in unit


def test_waiting_unit_has_an_unlit_lamp_and_the_waiting_chip(alice_client: Client, alice):
    host = Host.objects.create(owner=alice, name="new-box")  # no report yet

    html = alice_client.get("/").content.decode()
    unit = html[html.index('<a class="rack-unit card-press" href="/hosts/') :]
    unit = unit[: unit.index("</a>")]
    detail = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    header = detail[detail.index('<header class="surface host-head">') :]
    header = header[: header.index("</header>")]

    for part in (unit, header):
        assert '<span class="lamp lamp--off" aria-hidden="true"></span>' in part
        assert '<span class="chip chip--neutral">Waiting</span>' in part
    # Link text: "new-box, Waiting, Waiting for the first report"
    assert re.search(
        r'<span class="rack-unit__name">new-box</span><span class="visually-hidden">, </span>'
        r'\s*<span class="chip chip--neutral">Waiting</span>',
        unit,
    )
    assert "Waiting for the first report" in unit


def test_rack_css_stands_on_its_own():
    """No bullets and no sideways scrollbar, even without pages.css."""
    css = (STATIC.parent / "css/components.css").read_text()
    rack = css[css.index("\n.rack {") :]
    rack = rack[: rack.index("}")]
    assert "list-style: none" in rack
    assert "overflow: clip" in rack
    assert ".rack > li { list-style: none;" in css


def test_unit_link_text_says_name_and_status_in_words(alice_client: Client, alice):
    from .sample import make_report
    from .test_host_views import make_host

    make_host(alice, "homeserver", "red", make_report(failed_units=["x.service"]))

    html = alice_client.get("/").content.decode()

    assert re.search(
        r'<span class="rack-unit__name">homeserver</span><span class="visually-hidden">, </span>'
        r'\s*<span class="chip chip--red">Needs you</span>',
        html,
    )
    assert (
        '<span class="visually-hidden">, </span><span class="rack-unit__issue-text">'
        "x.service failed</span>"
    ) in html


def test_template_copy_holds_no_host_data(alice_client: Client, alice):
    """The <template> overview.js clones is empty: no names, no links, nothing hidden."""
    Host.objects.create(owner=alice, name="secret-box")

    html = alice_client.get("/").content.decode()
    blank = html[html.index('<template id="rack-unit-template">') :]
    blank = blank[: blank.index("</template>")]

    assert "secret-box" not in blank
    assert "href=" not in blank
    assert "data-" not in blank


# --- shared sample data ----------------------------------------------------------------


def test_preview_and_example_share_one_sample_module():
    from cronrack import dev_views
    from hosts import views

    assert dev_views.PREVIEW_CONTEXT is PREVIEW_CONTEXT
    assert views.EXAMPLE_DETAIL_CONTEXT is EXAMPLE_DETAIL_CONTEXT


@override_settings(DEBUG=True)
def test_preview_xss_state_renders_as_text(alice_client: Client):
    html = alice_client.get("/dev/preview/?state=xss").content.decode()

    assert "<script>alert(1)</script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in html
    page = scan(html)
    assert page.inline_script_text == ""
    assert page.event_handlers == []


@override_settings(DEBUG=False)
def test_preview_xss_state_is_404_without_debug(alice_client: Client):
    assert alice_client.get("/dev/preview/?state=xss").status_code == 404


# --- overview.js / actions.js -----------------------------------------------------------


@pytest.mark.parametrize("name", ["overview.js", "actions.js"])
def test_js_never_uses_inner_html_or_the_console(name: str):
    source = (STATIC / name).read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in source, forbidden
    assert "console." not in source


def test_overview_js_is_loaded_only_on_the_overview(alice_client: Client, alice):
    tag = '<script src="/static/cronrack/js/overview.js" defer></script>'
    assert tag in alice_client.get("/").content.decode()
    assert tag not in alice_client.get("/hosts/example/").content.decode()
    assert tag not in alice_client.get("/activity/").content.decode()
