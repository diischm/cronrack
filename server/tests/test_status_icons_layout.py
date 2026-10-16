"""Status icons instead of text chips, collapsing panels, the wide layout hooks."""

import re
from pathlib import Path

import pytest
from django.template.loader import render_to_string
from django.test import Client

from activity.models import StatusEvent

from .icons import shown, status_icon
from .sample import make_report
from .test_host_views import make_host, make_silent_host

pytestmark = pytest.mark.django_db

STATIC = Path(__file__).resolve().parent.parent / "static/cronrack"
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# Every way a text chip used to be written.
CHIP_RE = re.compile(r'class="[^"]*\bchip\b|chip--|chip-pick')


# --- the component -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind", "words", "shape"),
    [
        ("green", "good", "All good", "<circle"),
        ("yellow", "warn", "Heads up", "<circle"),
        ("red", "bad", "Needs you", "M10.3 3.9"),  # the triangle
        ("info", "info", "Info", "<circle"),
        ("waiting", "waiting", "Waiting", "M6 2.5h12"),  # the hourglass
        ("example", "info", "Example", "<circle"),
        ("maintenance", "info", "Info", "<circle"),
    ],
)
def test_status_icon_has_shape_hidden_words_and_title(status, kind, words, shape):
    html = render_to_string("components/status_icon.html", {"status": status})
    assert re.fullmatch(status_icon(kind, words), html), html
    assert shape in html
    assert 'aria-hidden="true"' in html
    assert "{" not in html and not html.startswith((" ", "\n")) and not html.endswith("\n")


def test_status_icon_shapes_differ_per_status():
    """Never colour-only: good, heads up and needs you are three different drawings."""
    drawings = set()
    for status in ("green", "yellow", "red", "info", "waiting"):
        html = render_to_string("components/status_icon.html", {"status": status})
        drawings.add(re.search(r"<svg[^>]*>(.*)</svg>", html).group(1))
    assert len(drawings) == 5


def test_status_icon_label_overrides_the_words():
    html = render_to_string("components/status_icon.html", {"status": "red", "label": "Failed"})
    assert re.fullmatch(status_icon("bad", "Failed"), html)


@pytest.mark.parametrize("status", ["offline", "", None, "nonsense"])
def test_offline_and_unknown_render_no_icon(status):
    assert render_to_string("components/status_icon.html", {"status": status}) == ""


def test_status_icon_escapes_its_words():
    html = render_to_string(
        "components/status_icon.html", {"status": "red", "label": '"><img src=x onerror=1>'}
    )
    assert "<img" not in html
    assert "&quot;&gt;&lt;img" in html


# --- no text chips left ------------------------------------------------------------------


def test_chip_components_and_css_are_gone():
    assert not (TEMPLATES / "components/chip.html").exists()
    assert not (TEMPLATES / "components/severity_chip.html").exists()
    for css in ("components.css", "pages.css"):
        text = (STATIC / "css" / css).read_text()
        assert not re.search(r"\.chip\b|chip-pick", text), css
    for js in (STATIC / "js").glob("*.js"):
        assert "chip" not in js.read_text(), js


def test_no_text_chips_on_any_page(alice_client: Client, alice):
    red = make_host(
        alice,
        "homeserver",
        "red",
        make_report(
            failed_units=["backup.service"],
            containers=[
                {
                    "name": "nginx",
                    "state": "restarting",
                    "status": "Restarting (1) 5s ago",
                    "health": None,
                    "exit_code": None,
                }
            ],
        ),
    )
    make_host(alice, "new-box", "waiting")
    make_silent_host(alice, "diicpi")
    pages = ["/", f"/hosts/{red.pk}/", "/activity/", "/settings/", f"/hosts/{red.pk}/settings/"]
    for url in pages:
        html = alice_client.get(url).content.decode()
        assert not CHIP_RE.search(html), url
    detail = alice_client.get(f"/hosts/{red.pk}/").content.decode()
    # Header, issue rows, panel headers and the container state all use icons.
    assert re.search(status_icon("bad", "Needs you"), shown(detail))
    assert re.search(status_icon("bad", "Failed"), detail)  # the failed unit
    assert '<span class="item-row__state-text">restarting</span>' in detail


def test_example_overview_and_page_have_no_chips(client: Client, django_user_model):
    user = django_user_model.objects.create_user(username="fresh", password="unused-pw-3")
    client.force_login(user)
    for url in ("/", "/hosts/example/"):
        assert not CHIP_RE.search(client.get(url).content.decode()), url


def test_icon_template_has_one_icon_per_js_status(alice_client: Client):
    html = alice_client.get("/").content.decode()
    template = re.search(r'<template id="status-icons">(.*?)</template>', html, re.DOTALL).group(1)
    for kind, words in (
        ("bad", "Needs you"),
        ("warn", "Heads up"),
        ("good", "All good"),
        ("info", "Info"),
        ("waiting", "Waiting"),
    ):
        assert re.search(status_icon(kind, words), template), kind


def test_icon_template_is_only_for_logged_in_pages(client: Client):
    assert 'id="status-icons"' not in client.get("/login/").content.decode()


def test_panel_status_icon_follows_the_worst_issue(alice_client: Client, alice):
    host = make_host(alice, "nas", "red", make_report(failed_units=["x.service"]))
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    systemd = html[html.index('id="panel-systemd"') :]
    systemd = systemd[: systemd.index("</div>")]
    # Every match first, then the "All good" fallback; CSS shows only the first.
    assert '<span class="app-panel__status icon-pick">' in systemd
    icons = re.findall(r'status-icon--(\w+)" title="([^"]+)"', systemd)
    assert icons[0] == ("bad", "Needs you")
    assert icons[-1] == ("good", "All good")
    css = (STATIC / "css/components.css").read_text()
    assert ".icon-pick > .status-icon ~ .status-icon { display: none; }" in css


# --- panels collapse after 6 rows ----------------------------------------------------------


def timers(n):
    return [
        {
            "timer": f"job-{i:02d}.timer",
            "activates": f"job-{i:02d}.service",
            "last_run": "Sun 2026-10-11 03:00:00 UTC",
            "next_run": "Mon 2026-10-12 03:00:00 UTC",
        }
        for i in range(n)
    ]


def test_long_lists_collapse_after_six_rows(alice_client: Client, alice):
    disks = [{"mount": f"/mnt/d{i}", "percent": 10} for i in range(8)]
    host = make_host(alice, "nas", "green", make_report(disks=disks, timers=timers(10)))
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()

    timers_html = html[html.index('class="timers"') :]
    first, more = timers_html.split('<tbody id="timers-more">', 1)
    assert first.count("<tr>") == 1 + 6  # header row + 6 timers
    assert more[: more.index("</tbody>")].count("<tr>") == 4
    assert re.search(r"Hide</span> 4 more", html)
    assert 'aria-controls="timers-more"' in html

    disks_more = html[html.index('id="disks-more"') :]
    assert disks_more[: disks_more.index("</ul>")].count('class="disk-line"') == 2
    assert re.search(r'aria-controls="disks-more" aria-expanded="true" hidden>', html)


def containers(problems, healthy):
    bad = [
        {
            "name": f"bad-{i}",
            "state": "exited",
            "status": "Exited (1)",
            "health": None,
            "exit_code": 1,
        }
        for i in range(problems)
    ]
    good = [
        {
            "name": f"ok-{i}",
            "state": "running",
            "status": "Up 3 days",
            "health": None,
            "exit_code": None,
        }
        for i in range(healthy)
    ]
    return bad + good


def docker_panel(html):
    panel = html[html.index('id="panel-docker"') :]
    return panel[: panel.index("</section>")]


def names(html):
    return re.findall(r'<span class="item-row__name mono">([^<]+)</span>', html)


def test_docker_lists_problems_then_healthy_and_collapses_after_six(alice_client, alice):
    host = make_host(alice, "nas", "red", make_report(containers=containers(2, 7)))
    panel = docker_panel(alice_client.get(f"/hosts/{host.pk}/").content.decode())

    first, more = panel.split('id="docker-more"', 1)
    assert names(first) == ["bad-0", "bad-1", "ok-0", "ok-1", "ok-2", "ok-3"]
    assert names(more) == ["ok-4", "ok-5", "ok-6"]
    assert re.search(r'aria-controls="docker-more" aria-expanded="true" hidden>', panel)
    assert re.search(r"Hide</span> 3 more", panel)
    assert "healthy" not in panel.replace("(healthy)", "")
    assert "9 containers · 2 failing" in panel


def test_docker_more_than_six_problems_pushes_healthy_into_more(alice_client, alice):
    host = make_host(alice, "nas", "red", make_report(containers=containers(7, 2)))
    panel = docker_panel(alice_client.get(f"/hosts/{host.pk}/").content.decode())
    first, more = panel.split('id="docker-more"', 1)
    assert names(first) == [f"bad-{i}" for i in range(6)]
    assert names(more) == ["bad-6", "ok-0", "ok-1"]
    assert re.search(r"Hide</span> 3 more", panel)


@pytest.mark.parametrize(("problems", "healthy"), [(0, 6), (1, 5), (0, 1)])
def test_docker_six_or_fewer_containers_have_no_key(alice_client, alice, problems, healthy):
    host = make_host(alice, "nas", "green", make_report(containers=containers(problems, healthy)))
    panel = docker_panel(alice_client.get(f"/hosts/{host.pk}/").content.decode())
    assert len(names(panel)) == problems + healthy
    assert "show-more" not in panel and "docker-more" not in panel
    assert ("No failing containers." in panel) == (problems == 0)


def test_docker_without_containers_says_so(alice_client, alice):
    host = make_host(alice, "nas", "green", make_report(containers=[]))
    panel = docker_panel(alice_client.get(f"/hosts/{host.pk}/").content.decode())
    assert "No containers on this host." in panel
    assert "item-list" not in panel


def test_few_timers_get_a_show_last_run_key(alice_client: Client, alice):
    host = make_host(alice, "nas", "green", make_report(timers=timers(3)))
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()

    # The key opens the (empty) extra tbody; with it open, CSS shows the Last run column.
    assert '<tbody id="timers-more">' in html
    assert 'class="btn btn--sm show-more js-disclosure timers__last-key"' in html
    assert re.search(r"Hide</span> last run", html)
    assert html.count('class="timers__last"') == 1 + 3  # header + one per timer


def test_timers_css_hides_last_run_only_in_narrow_collapsed_panels():
    css = (STATIC / "css/components.css").read_text()
    narrow = css[css.index("@container app-panel (max-width: 559.98px)") :]
    narrow = narrow[: narrow.index("\n}\n")]
    assert '.timers-box:not(:has(.show-more[aria-expanded="true"])) .timers__last' in narrow
    assert ".timers__last-key { display: inline-flex; }" in narrow


# --- layout hooks --------------------------------------------------------------------------


def test_overview_splits_into_two_columns_only_with_activity(alice_client: Client, alice):
    host = make_host(alice, "nas", "green", make_report())
    assert 'class="overview-body"' in alice_client.get("/").content.decode()

    StatusEvent.objects.create(host=host, old_status="yellow", new_status="green")
    html = alice_client.get("/").content.decode()
    assert 'class="overview-body overview-body--split"' in html
    # DOM order = visual order: the rack comes before "While you were away".
    assert html.index('id="rack"') < html.index('id="away-title"')


def test_overview_hosts_heading_is_for_screen_readers_only(alice_client: Client, alice):
    make_host(alice, "nas", "green", make_report())
    html = alice_client.get("/").content.decode()
    hosts = html[html.index('<section class="overview-hosts">') :]
    # The first thing in the section: an <h2> nobody sees, then the chassis.
    assert re.match(
        r'<section class="overview-hosts">\s*<h2 class="visually-hidden">Hosts</h2>\s*'
        r'<div class="rack-chassis surface">',
        hosts,
    )
    assert '<h2 class="section-title desk-only">Hosts</h2>' not in html


def test_layout_css_uses_one_page_width_and_no_order_trick():
    pages = (STATIC / "css/pages.css").read_text()
    tokens = (STATIC / "css/tokens.css").read_text()
    assert "--page-max: 1600px;" in tokens
    assert "--page-pad: var(--space-8);" in tokens
    assert "max-width: calc(var(--page-max) + 2 * var(--page-pad))" in pages
    assert ":root { --page-pad: 48px; }" in pages
    assert not re.search(r"(?<![\w-])order:", pages)
    assert "grid-template-columns: minmax(0, 2fr) minmax(0, 1fr)" in pages
    components = (STATIC / "css/components.css").read_text()
    assert "repeat(4, minmax(0, 1fr))" in components
    assert "align-items: stretch" in components


def test_detail_header_is_a_server_unit(alice_client: Client, alice):
    host = make_host(alice, "nas", "green", make_report())
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    header = html[html.index('<header class="host-rack') :]
    header = header[: header.index("</header>")]

    assert header.count('class="rack-ear"') == 2
    assert header.count('class="rack-screw"') == 4
    unit = header[header.index('<div class="host-unit">') :]
    # Front panel order: back key, lamp, name + icon, meta, keys, vents.
    order = [
        'aria-label="Back"',
        "lamp--green",
        'class="page-title host-unit__name">nas</h1>',
        "status-icon--good",
        "Last report",
        "Check now",
        "Host settings",
        "host-unit__vents",
    ]
    positions = [unit.index(part) for part in order]
    assert positions == sorted(positions)


def test_offline_header_keeps_the_disabled_check_now_and_no_icon(alice_client: Client, alice):
    host = make_silent_host(alice, "diicpi")
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    header = html[html.index('<header class="host-rack') :]
    header = header[: header.index("</header>")]

    assert "status-icon" not in header
    assert "lamp--off" in header
    assert 'aria-disabled="true" aria-describedby="check-offline-note"' in header
    assert 'id="check-offline-note">Check now needs the agent online</p>' in header
