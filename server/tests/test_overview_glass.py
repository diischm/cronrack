"""Glass overview v8 (design-ref/glass/overview-glass-v8.html): the headline in the page
header, no tiles or Status panel in Glass, short attention cards that open in place, the
status counts as filter chips above the Hosts rack, and rack units that carry their status
as a soft gradient instead of an LED. The summary line comes from ONE function
(hosts.presenters.short_summary); overview.js only shows the "summary" field it gets.

Legacy is checked by the tests that already cover it (test_rack.py,
test_status_icons_layout.py); the no-style="" and CSP rules by test_glass_theme.py."""

import re
from pathlib import Path

import pytest
from django.test import Client, override_settings

from activity.models import StatusEvent
from hosts.presenters import short_summary

from .sample import make_report
from .test_cluster_views import live  # noqa: F401 - live is a fixture
from .test_glass_calm import css_rules, glass_css, page, without_templates
from .test_host_views import make_host, make_silent_host

SERVER = Path(__file__).resolve().parent.parent
JS = SERVER / "static" / "cronrack" / "js"


def service(name: str) -> dict:
    return {"text": f"{name} failed", "rule_id": "systemd.unit_failed", "subject": f"unit:{name}"}


def read_js(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


# --- 1. The summary line: every case ------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "issues", "expected"),
    [
        # no issues
        ("green", [], "All good"),
        # offline wins, whatever the stale issues say
        ("offline", [], "Offline"),
        ("offline", [{"text": "3 security updates"}], "Offline"),
        # exactly one issue: its text without the parenthesised technical details
        ("red", [{"text": "immich-server exited (code 137)"}], "immich-server exited"),
        ("yellow", [{"text": "Reboot required"}], "Reboot required"),
        ("red", [{"text": "web (a) exited (137) now"}], "web exited now"),
        ("red", [{"text": "(only details)"}], "(only details)"),  # nothing left: keep it
        ("red", [service("backup.service")], "backup.service failed"),
        # only failed systemd services
        ("red", [service("a.service"), service("b.service")], "2 services failed"),
        # otherwise: N issues
        ("red", [service("a.service"), {"text": "disk / at 94%"}], "2 issues"),
        ("red", [service("a.timer"), service("b.timer")], "2 issues"),  # timers aren't services
        ("yellow", [{"text": "x"}, {"text": "y"}, {"text": "z"}], "3 issues"),
        # the other states the UI knows
        ("waiting", [], "Waiting for the first report"),
        ("maintenance", [{"text": "x"}], "Maintenance"),
    ],
)
def test_short_summary(status, issues, expected):
    assert short_summary(status, issues) == expected


def test_js_has_no_second_summary_implementation():
    # overview.js shows the server's "summary"; it never builds one of its own.
    js = read_js("overview.js")
    for field in ("text(host.summary)", "text(cluster.summary)", "text(row.summary)"):
        assert field in js
    for own in ("services failed", '" issues"', "replace(/\\s*\\("):
        assert own not in js


@pytest.mark.django_db
def test_server_and_json_summaries_agree(alice_client: Client, alice, live):  # noqa: F811
    make_host(alice, "a-red", "red", make_report(failed_units=["x.service", "y.service"]))
    make_host(alice, "b-yellow", "yellow", make_report(reboot_required=True))
    make_host(alice, "c-green", "green", make_report())
    make_silent_host(alice, "d-off")
    html = without_templates(page(alice_client))
    data = alice_client.get("/api/v1/overview.json").json()

    expected = {h["name"]: h["summary"] for h in data["hosts"] + data["clusters"]}
    assert expected["a-red"] == "2 services failed"
    assert expected["b-yellow"] == "Reboot required"
    assert expected["c-green"] == "All good"
    assert expected["d-off"] == "Offline"
    units = re.findall(
        r'<button type="button" class="rack-unit g-unit g-only.*?'
        r'<span class="rack-unit__name">([^<]*)</span>'
        r'.*?<span class="rack-unit__summary-text">([^<]*)</span>',
        html,
        flags=re.DOTALL,
    )
    assert dict(units) == expected  # every host and cluster unit, the same words
    cards = re.findall(
        r'<span class="g-attn__name">([^<]*)</span>'
        r'.*?<span class="g-attn__summary-text">([^<]*)</span>',
        html,
        flags=re.DOTALL,
    )
    assert cards
    assert all(expected[name] == text for name, text in cards)


# --- 2. Header: headline, subline, keys; no tiles in Glass --------------------------------------


@pytest.mark.django_db
def test_header_has_the_headline_subline_and_both_keys(alice_client: Client, alice, live):  # noqa: F811
    make_host(alice, "a-red", "red", make_report(reboot_required=True))
    html = without_templates(page(alice_client))
    head = html[
        html.index('<div class="page-head overview-head">') : html.index("summary desk-only")
    ]
    # alice has "nas" (the live fixture's healthy reporter), a-red, and the cluster with a
    # crash-looping pod: a-red + the cluster need her.
    assert '<h1 class="page-title" id="overview-title">2 things need you</h1>' in head
    assert (
        '<span id="overview-sub">2 hosts<span class="g-only" id="overview-sub-clusters">'
        " · 1 cluster</span> · updated just now</span>"
    ) in head
    assert "Check all now" in head and "Add host" in head
    assert '<section class="summary desk-only legacy-only" aria-label="Summary">' in html
    assert "tile--offline" not in html  # Glass's own tile is gone
    assert 'id="g-duo"' not in html


def test_glass_css_has_no_tile_rules_and_a_24px_headline():
    css = glass_css()
    assert not [sel for sel, _ in css_rules(css) if ".tile" in sel or ".summary" in sel]
    assert '[data-skin="glass"] .overview-head .page-title { font-size: 24px;' in css
    # Phones: the keys sit under the headline; the top bar's copies are hidden.
    assert '[data-skin="glass"] .overview-head__actions.desk-only' in css
    assert '[data-skin="glass"] .app:has(.overview-head__actions) .add-key' in css


# --- 3. Needs attention: short cards, details on click ------------------------------------------


def card_html(html: str, kind: str, pk: int) -> str:
    start = html.index(f'<li class="g-attention__slot" data-card="{kind}" data-id="{pk}">')
    return html[start : html.index("  </div>\n</li>", start)]


def header_button(card: str) -> str:
    return card[card.index("<button") : card.index("</button>")]


@pytest.mark.django_db
def test_closed_card_shows_only_name_dot_summary_and_type(alice_client: Client, alice):
    host = make_host(alice, "nas-01", "red", make_report(failed_units=["x.service"]))
    button = header_button(card_html(without_templates(page(alice_client)), "host", host.pk))

    assert f'aria-expanded="false" aria-controls="attn-more-host-{host.pk}"' in button
    pieces = re.findall(r'<span class="(g-attn__[a-z-]+)"', button)
    assert pieces == [
        "g-attn__head",
        "g-attn__name",
        "g-attn__summary",
        "g-attn__summary-text",
        "g-attn__meta",
        "g-attn__type",
    ]
    assert "status-icon--bad" in button  # the dot, with its words
    assert '<span class="g-attn__summary-text">x.service failed</span>' in button
    assert '<span class="g-attn__type">host</span>' in button
    # Nothing else when closed: no facts, no keys, no issue list.
    for absent in ("<a ", "<form", "Disk", "Last report", "issue-line"):
        assert absent not in button


@pytest.mark.django_db
def test_card_region_has_issues_facts_and_separate_actions(alice_client: Client, alice):
    report = make_report(failed_units=["x.service"], reboot_required=True)
    host = make_host(alice, "nas-01", "red", report)
    card = card_html(without_templates(page(alice_client)), "host", host.pk)
    region = card[card.index(f'<div class="g-attn-more" id="attn-more-host-{host.pk}">') :]

    assert region.count('<li class="issue-line') == 2  # the full list
    for fact in ("<dt>Disk</dt>", "<dt>Updates</dt>", "<dt>Last report</dt>"):
        assert fact in region
    assert f'action="/hosts/{host.pk}/collect/"' in region
    assert ">Check now</button>" in region
    assert f'href="/hosts/{host.pk}/">Open host</a>' in region
    # No nested interactive elements: the header button holds no link or button.
    button = header_button(card)
    assert "<a " not in button and "<button" not in button[1:]


@pytest.mark.django_db
def test_cluster_and_offline_cards(alice_client: Client, alice, live):  # noqa: F811
    off = make_silent_host(alice, "edge-pi")
    html = without_templates(page(alice_client))

    cluster = card_html(html, "cluster", live.pk)
    assert '<span class="g-attn__type">cluster</span>' in cluster
    for fact in ("<dt>Nodes</dt>", "<dt>Pods</dt>", "<dt>Version</dt>"):
        assert fact in cluster
    assert "<form" not in cluster
    assert f'href="/clusters/{live.pk}/">Open cluster</a>' in cluster

    offline = card_html(html, "host", off.pk)
    assert '<span class="g-attn__summary-text">Offline</span>' in offline
    assert f'aria-describedby="attn-off-host-{off.pk}">Check now</button>' in offline
    assert "<form" not in offline


def test_cards_open_by_delegation_and_tilt_only_while_closed():
    js = read_js("glass.js")
    assert 'closest("button.g-unit, button.g-attn__btn")' in js
    assert '"#g-attention-list .g-attention__slot:not(.is-open) > .g-attn' in js
    assert ".g-attention__slot.is-open[data-card]" in read_js("overview.js")  # kept open
    css = glass_css()
    assert "grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr))" in css
    assert (
        '[data-skin="glass"] .g-attention__slot.is-open .g-attn-more { grid-template-rows: 1fr;'
        in css
    )


@pytest.mark.django_db
def test_headline_count_matches_the_cards(alice_client: Client, alice, live):  # noqa: F811
    make_host(alice, "a-red", "red", make_report(reboot_required=True))
    make_silent_host(alice, "b-off")
    html = without_templates(page(alice_client))
    cards = re.findall(r'<li class="g-attention__slot" data-card="(?:host|cluster)"', html)
    count = alice_client.get("/api/v1/overview.json").json()["attention_count"]
    assert len(cards) == count
    assert f">{count} things need you</h1>" in html


# --- 4. Filter chips ----------------------------------------------------------------------------


def chip_attrs(html: str) -> dict[str, str]:
    box = html[html.index('<div class="g-chips g-only" id="host-filter"') :]
    box = box[: box.index("</div>")]
    pattern = r'<button type="button" class="g-chip" data-filter="(\w+)"([^>]*)>'
    return dict(re.findall(pattern, box))


@pytest.mark.django_db
def test_chips_render_counts_disabled_zeroes_and_all_pressed(alice_client: Client, alice):
    make_host(alice, "a", "red", make_report(failed_units=["x.service"]))
    make_host(alice, "b", "green", make_report())
    make_host(alice, "c", "green", make_report())
    html = without_templates(page(alice_client))

    assert (
        '<div class="g-chips g-only" id="host-filter" role="group"'
        ' aria-label="Filter hosts by status">'
    ) in html
    attrs = chip_attrs(html)
    assert list(attrs) == ["all", "red", "yellow", "green", "offline"]
    assert attrs["all"] == ' aria-pressed="true"'  # the server-rendered default is All
    assert attrs["red"] == attrs["green"] == ' aria-pressed="false"'
    disabled = ' aria-pressed="false" aria-disabled="true"'
    assert attrs["yellow"] == attrs["offline"] == disabled
    for key, n in (("all", 3), ("red", 1), ("yellow", 0), ("green", 2), ("offline", 0)):
        assert f'<span class="g-flow" id="chip-{key}">{n}</span>' in html  # readable at 0 too
    assert (
        '<p class="visually-hidden g-only" id="host-filter-status" role="status"'
        ' aria-live="polite">'
    ) in html
    # The heading count stays the total.
    assert '<span class="section-count g-only" id="hosts-count"> 3</span>' in html


@pytest.mark.django_db
def test_no_chips_without_hosts(alice_client: Client):
    assert 'id="host-filter"' not in without_templates(page(alice_client))


def test_filter_js_session_only_fallback_and_announcement():
    js = read_js("overview.js")
    assert 'const FILTER_KEY = "cronrack-host-filter";' in js
    assert re.search(r"try \{\s*const value = window\.sessionStorage\.getItem\(FILTER_KEY\)", js)
    assert "localStorage" not in js  # never across devices or sessions
    # A filter whose count dropped to 0 goes back to All, and says so.
    settle = js[js.index("function settleFilter") :]
    settle = settle[: settle.index("\n  }\n")]
    assert 'if (filter !== "all" && !counts[filter])' in settle
    assert 'filter = "all";' in settle
    assert '"Showing " + shown + " of " + plural(units.length, "host")' in js
    assert 'chip.getAttribute("aria-disabled") === "true"' in js  # empty chips do nothing
    css = glass_css()
    # Hidden in Glass only: Legacy never hides a unit.
    assert '[data-skin="glass"] .rack > .rack__slot.is-filtered-out { display: none; }' in css
    assert '[data-skin="glass"] .g-chip { min-height: 44px; }' in css  # phones


# --- 5. Rack units ------------------------------------------------------------------------------


def slot_html(html: str, status: str, pk: int) -> str:
    start = html.index(
        f'<li class="rack__slot rack__slot--st-{status}" data-unit="host" data-id="{pk}">'
    )
    return html[start : html.index("  </div>\n</li>", start)]


@pytest.mark.django_db
def test_units_carry_a_status_class_no_dot_and_the_status_in_words(alice_client: Client, alice):
    red = make_host(alice, "a-red", "red", make_report(failed_units=["x.service"]))
    off = make_silent_host(alice, "b-off")
    html = without_templates(page(alice_client))

    for host, status in ((red, "red"), (off, "offline")):
        slot = slot_html(html, status, host.pk)
        button = slot[slot.index('<button type="button" class="rack-unit g-unit') :]
        button = button[: button.index("</button>")]
        assert "lamp" not in button  # no LED / dot element in Glass
        assert 'class="rack-unit__summary g-only"' in button
        assert "rack-unit__lamp" in slot[: slot.index("<button")]  # Legacy's link keeps it
    red_button = slot_html(html, "red", red.pk)
    red_button = red_button[red_button.index("<button") :]
    assert '<span class="visually-hidden">Needs you</span>' in red_button  # ", Needs you"
    assert '<span class="rack-unit__summary-text">x.service failed</span>' in red_button


def test_unit_gradient_is_registered_and_crossfades():
    css = glass_css()
    assert '@property --g-st { syntax: "<color>"; inherits: false; initial-value: #8e8e93; }' in css
    assert (
        "background: linear-gradient(90deg, color-mix(in srgb, var(--g-st) 11%, var(--g-unit)) 0%,\n"
        "    color-mix(in srgb, var(--g-st) 4%, var(--g-unit)) 14%, var(--g-unit) 30%);"
    ) in css
    assert "transition: --g-st 700ms var(--g-ease)" in css
    for status, colour in (("red", "--g-red"), ("yellow", "--g-yellow"), ("green", "--g-green")):
        assert f'[data-skin="glass"] .rack__slot--st-{status} {{ --g-st: var({colour}); }}' in css
    assert '[data-skin="glass"] .rack-unit__lamp { display: none; }' in css
    # Only the summary line in the Glass button (Legacy's issue line would overlap it).
    assert '[data-skin="glass"] .rack-unit.g-unit .rack-unit__issues { display: none; }' in css
    # The only registered property is the status colour.
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    assert len(re.findall(r"@property\b", plain)) == 1
    js = read_js("overview.js")
    assert "function fadeStatus(before)" in js
    assert "li.classList.replace(now, old);" in js


def test_show_status_labels_still_shows_the_word():
    css = glass_css()
    assert (
        '[data-skin="glass"][data-labels="on"] .status-icon > .visually-hidden,\n'
        '[data-skin="glass"] [data-labels="on"] .status-icon > .visually-hidden {\n'
        "  position: static;"
    ) in css
    # Next to the name: the unit head's second column.
    assert (
        '[data-skin="glass"] .rack-unit__head > .status-icon { grid-column: 2; grid-row: 1;' in css
    )


# --- 6. Layout: empty sections, the Hosts row -------------------------------------------------


@pytest.mark.django_db
def test_empty_sections_render_nothing_and_hosts_span_the_row(alice_client: Client, alice):
    make_host(alice, "nas", "green", make_report())
    html = without_templates(page(alice_client))
    for absent in ('id="g-attention"', 'id="g-away"', 'id="clusters-title"'):
        assert absent not in html
    css = glass_css()
    # Two columns only when the activity box is there; otherwise Hosts takes the row.
    assert (
        '[data-skin="glass"] .overview-hosts:has(> #g-away) {'
        " grid-template-columns: minmax(0, 7fr) minmax(280px, 3fr);"
    ) in css
    assert (
        '[data-skin="glass"] .overview-hosts { display: grid; grid-template-columns: minmax(0, 1fr);'
        in css
    )


@pytest.mark.django_db
def test_dom_order_is_header_attention_hosts_away_clusters(alice_client: Client, alice, live):  # noqa: F811
    host = make_host(alice, "a-red", "red", make_report(reboot_required=True))
    StatusEvent.objects.create(host=host, old_status="green", new_status="red")
    html = without_templates(page(alice_client))
    order = [
        'id="overview-title"',
        'id="g-attention"',
        'id="host-filter"',
        'id="rack"',
        'id="g-away"',
        'id="clusters-head"',
        'id="cluster-rack"',
    ]
    positions = [html.index(part) for part in order]
    assert positions == sorted(positions)
    css = glass_css()
    assert (
        '[data-skin="glass"] .overview-hosts > #g-away > .g-away { position: absolute; inset: 0; }'
        in css
    )
    assert '[data-skin="glass"] .g-away__list > :nth-child(n+4) { display: none; }' in css


# --- 7. Dev preview states ----------------------------------------------------------------------


@override_settings(DEBUG=True)
@pytest.mark.django_db
@pytest.mark.usefixtures("alice")
@pytest.mark.parametrize("state", ["one", "small", "busy", "healthy"])
def test_preview_states_show_chips_and_summaries(client: Client, state):
    html = without_templates(page(client, f"/dev/preview/?state={state}"))
    assert 'id="host-filter"' in html
    assert "rack__slot--st-" in html
    assert 'class="rack-unit__summary-text"' in html
    if state == "small":
        assert '<span class="rack-unit__summary-text">web container exited</span>' in html
