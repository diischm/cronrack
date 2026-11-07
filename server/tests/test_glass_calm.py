"""Glass "calm": no glows, the rack in Glass with units that expand inline, the overview's
section rules (empty sections not rendered, "See all" in the header, the hero count equal to
the attention cards, Legacy 3 / Glass 10 activity), "Reduce animations" and the preview
states. The CSP and the no-style="" rule are covered by test_glass_theme.py."""

import re
from pathlib import Path

import pytest
from django.test import Client, override_settings

from activity.models import StatusEvent

from .sample import make_report
from .test_cluster_views import live  # noqa: F401 - live is a fixture
from .test_deploy import NEWEST_VERSION_RE
from .test_host_views import make_host, make_silent_host

pytestmark = pytest.mark.django_db

SERVER = Path(__file__).resolve().parent.parent
GLASS_CSS = SERVER / "static" / "cronrack" / "css" / "glass.css"
JS = SERVER / "static" / "cronrack" / "js"
TEMPLATES = SERVER / "templates"
ORANGE = ("--g-accent", "#f26b2e", "--accent", "242, 107, 46")


def no_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def css_rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) for every plain rule; @-blocks are flattened."""
    return [
        (sel.strip(), body) for sel, body in re.findall(r"([^{}@;]+)\{([^{}]*)\}", no_comments(css))
    ]


def glass_css() -> str:
    return GLASS_CSS.read_text(encoding="utf-8")


def page(client: Client, url: str = "/") -> str:
    response = client.get(url)
    assert response.status_code == 200, url
    return response.content.decode()


def without_templates(html: str) -> str:
    """The rendered page, without the inert <template> copies."""
    return re.sub(r"<template\b.*?</template>", "", html, flags=re.DOTALL)


def add_events(host, n: int) -> None:
    for i in range(n):
        StatusEvent.objects.create(
            host=host, old_status="green", new_status="red" if i % 2 else "green"
        )


# --- 1. No glows ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "removed",
    [
        "g-glow",
        "g-backdrop",
        "g-beam",
        "g-orb",
        "g-attn--red",
        "g-attn--yellow",
        "--g-gx",
    ],
)
def test_no_glow_beam_orb_or_glare_remains(removed):
    token = re.compile(r"(?<![\w-])" + re.escape(removed) + r"(?![\w-])")
    assert not token.search(no_comments(glass_css()))
    assert not token.search((JS / "glass.js").read_text(encoding="utf-8"))
    assert not token.search((TEMPLATES / "base.html").read_text(encoding="utf-8"))


def test_no_glare_layer_on_cards_or_tiles():
    for rule, _ in css_rules(glass_css()):
        for selector in rule.split(","):
            if ".g-attn" in selector or ".tile" in selector:
                assert "::after" not in selector, selector
                assert "::before" not in selector.replace(".tile__label::before", ""), selector


def test_no_coloured_shadow_in_glass():
    for shadow in re.findall(r"box-shadow:[^;}]*", no_comments(glass_css())):
        for r, g, b in re.findall(r"rgba\((\d+),\s*(\d+),\s*(\d+)", shadow):
            assert r == g == b, shadow  # black or white only: no orange, red, … shadows


def test_old_moving_background_key_is_never_read():
    for name in ("theme-init.js", "theme.js"):
        code = re.sub(r"^\s*//.*$", "", (JS / name).read_text(encoding="utf-8"), flags=re.MULTILINE)
        assert "cronrack-motion" not in code, name
        assert "cronrack-reduce-motion" in code, name


# --- 2. No orange on the current nav item -----------------------------------------------------


def test_no_orange_accent_on_the_active_nav_item():
    nav = [
        (sel, body)
        for sel, body in css_rules(glass_css())
        if 'aria-current="page"' in sel or ".g-nav-pill" in sel or ".g-nav-hl" in sel
    ]
    assert nav, "no nav rules found"
    for selector, body in nav:
        for orange in ORANGE:
            assert orange not in body, selector
        assert "::after" not in selector and "::before" not in selector, selector  # no dot


def test_the_tabs_indicator_is_grey_too():
    for selector, body in css_rules(glass_css()):
        if ".g-tabs__ind" in selector or 'aria-selected="true"' in selector:
            for orange in ORANGE:
                assert orange not in body, selector


def test_nav_has_the_pill_and_highlight(alice_client: Client):
    html = page(alice_client)
    assert '<span class="g-nav-hl g-only" aria-hidden="true"></span>' in html
    assert '<span class="g-nav-pill g-only" aria-hidden="true"></span>' in html


# --- 3. Settings: Reduce animations -----------------------------------------------------------


def test_settings_show_reduce_animations_not_moving_background(alice_client: Client):
    html = page(alice_client, "/settings/appearance/")
    assert ">Reduce animations<" in html
    assert 'id="pref-reduce-motion"' in html
    assert "Moving background" not in html
    assert 'id="pref-motion"' not in html


def test_motion_off_rules_exist_for_the_setting_and_the_device():
    css = glass_css()
    assert '[data-skin="glass"][data-motion="off"] *' in css
    assert "@media (prefers-reduced-motion: reduce)" in css


# --- 4. The rack: two controls, the Glass one expands -----------------------------------------


def test_units_have_a_legacy_link_and_a_glass_button_with_its_region(alice_client, alice, live):  # noqa: F811
    host = make_host(alice, "box-1", "green", make_report())
    html = without_templates(page(alice_client))

    for kind, pk in (("host", host.pk), ("cluster", live.pk)):
        start = re.search(
            rf'<li class="rack__slot rack__slot--st-\w+" data-unit="{kind}" data-id="{pk}">', html
        ).start()
        slot = html[start : html.index("  </div>\n</li>", start)]  # the li ends after its region
        # Exactly one control per skin: the link is Legacy-only, the button Glass-only.
        assert slot.count('<a class="rack-unit card-press legacy-only') == 1
        assert slot.count('<button type="button" class="rack-unit g-unit g-only') == 1
        assert f'href="/{kind}s/{pk}/"' in slot
        button = re.search(
            r'<button type="button" class="rack-unit g-unit g-only[^>]*>', slot
        ).group(0)
        assert 'aria-expanded="false"' in button
        assert f'aria-controls="unit-more-{kind}-{pk}"' in button
        assert f'<div class="g-unit-more g-only" id="unit-more-{kind}-{pk}">' in slot
        assert f">Open {kind}</a>" in slot


def test_one_control_per_skin_is_hidden_by_css():
    css = glass_css()
    assert '[data-skin="glass"] .legacy-only { display: none !important; }' in css
    assert (
        ':root:not([data-skin="glass"]) .g-only:not([data-skin="glass"] *) { display: none !important; }'
        in css
    )


def test_online_unit_has_check_now_offline_unit_a_disabled_stand_in(alice_client, alice):
    online = make_host(alice, "nas", "green", make_report())
    offline = make_silent_host(alice, "edge-pi")
    html = without_templates(page(alice_client))
    assert (
        f'action="/hosts/{online.pk}/collect/" data-status-url="/api/v1/hosts/{online.pk}/status.json"'
        in html
    )
    assert (
        f'aria-disabled="true" aria-describedby="unit-off-host-{offline.pk}">Check now</button>'
        in html
    )
    assert f'action="/hosts/{offline.pk}/collect/"' not in html


def test_glass_js_expands_units_by_delegation():
    js = (JS / "glass.js").read_text(encoding="utf-8")
    assert 'closest("button.g-unit, button.g-attn__btn")' in js
    assert 'setAttribute("aria-expanded"' in js


# --- 5. Layout: empty sections, "See all", hero count = cards ----------------------------------


def test_empty_sections_render_nothing(alice_client, alice):
    make_host(alice, "nas", "green", make_report())  # all good, no activity, no cluster
    html = without_templates(page(alice_client))
    for absent in (
        'id="g-attention"',
        'id="g-away"',
        'id="away"',
        'id="clusters-title"',
        "Needs attention",
        "While you were away",
        "overview-body--split",
    ):
        assert absent not in html, absent


def test_activity_alone_takes_the_row_without_needs_attention(alice_client, alice):
    host = make_host(alice, "nas", "green", make_report())
    add_events(host, 1)
    html = without_templates(page(alice_client))
    assert 'id="g-away"' in html
    assert 'id="g-attention"' not in html


def test_see_all_links_have_a_fixed_place(alice_client, alice, live):  # noqa: F811
    host = make_host(alice, "box-1", "red", make_report())
    add_events(host, 2)
    html = without_templates(page(alice_client))
    # Legacy: in the section headers (its away box, and the Clusters header, Legacy only).
    # Both skins: each link says what it opens (a links list would show two "See all").
    assert (
        html.count(
            '<a class="section-link" href="/activity/" aria-label="See all activity">See all</a>'
        )
        == 1
    )
    assert (
        '<a class="section-link legacy-only" href="/clusters/" aria-label="See all clusters">See all</a>'
        in html
    )
    # Glass: at the bottom of the activity box and of the Clusters section.
    away = html[html.index('id="g-away-list"') :]
    assert away.index("</ol>") < away.index(
        '<a class="section-link g-see-all" href="/activity/" aria-label="See all activity">'
    )
    assert html.index('id="clusters-rack-chassis"') < html.index(
        '<a class="section-link g-see-all g-only" href="/clusters/" aria-label="See all clusters">'
        "See all →</a>"
    )


def test_every_see_all_sits_inside_its_box(alice_client, alice, live):  # noqa: F811
    host = make_host(alice, "box-1", "red", make_report())
    add_events(host, 2)
    html = page(alice_client)
    chassis = html[
        html.index('id="clusters-rack-chassis"') : html.index(
            '<template id="cluster-unit-template"'
        )
    ]
    link = chassis.index('class="section-link g-see-all g-only" href="/clusters/"')
    assert chassis.rindex("</ul>") < link < chassis.rindex("</div>")  # under the bay, in the box
    box = html[html.index('<div class="g-away">') :]
    assert box.index('href="/activity/"') < box.index("</div>")


def test_hero_count_equals_the_attention_cards(alice_client, alice, live):  # noqa: F811
    make_host(alice, "a-red", "red", make_report())
    make_host(alice, "b-yellow", "yellow", make_report())
    make_host(alice, "c-green", "green", make_report())
    make_silent_host(alice, "d-off")
    html = without_templates(page(alice_client))
    data = alice_client.get("/api/v1/overview.json").json()

    cards = re.findall(r'<li class="g-attention__slot" data-card="(host|cluster)"', html)
    assert data["attention_count"] == len(cards) == 4  # red, yellow, offline host + red cluster
    assert cards.count("cluster") == 1
    assert '<h1 class="page-title" id="overview-title">4 things need you</h1>' in html
    assert '<span id="g-attention-count"> 4</span>' in html


def test_legacy_shows_3_activity_entries_and_glass_10(alice_client, alice):
    host = make_host(alice, "nas", "green", make_report())
    add_events(host, 12)
    html = without_templates(page(alice_client))
    data = alice_client.get("/api/v1/overview.json").json()

    legacy = html[html.index('<ul class="surface activity rows" id="away-list">') :]
    glass = html[html.index('<ol class="g-away__list activity" id="g-away-list">') :]
    assert legacy[: legacy.index("</ul>")].count('<li class="activity__row">') == 3
    assert glass[: glass.index("</ol>")].count('<li class="activity__row">') == 10
    assert len(data["activity"]) == 10
    js = (JS / "overview.js").read_text(encoding="utf-8")
    assert "const GLASS_ACTIVITY = 10;" in js and "const LEGACY_ACTIVITY = 3;" in js


# --- 6. Dev preview states ---------------------------------------------------------------------


@override_settings(DEBUG=True)
@pytest.mark.parametrize(
    ("state", "hosts", "cards"), [("one", 1, 0), ("small", 3, 2), ("busy", 7, 4), ("healthy", 4, 0)]
)
@pytest.mark.usefixtures("alice")  # a user exists, so the preview isn't sent to /setup/
def test_preview_states_render_and_agree(client: Client, state, hosts, cards):
    html = without_templates(page(client, f"/dev/preview/?state={state}"))
    assert (
        len(re.findall(r'<li class="rack__slot rack__slot--st-\w+" data-unit="host"', html))
        == hosts
    )
    assert len(re.findall(r'<li class="g-attention__slot"', html)) == cards
    if cards:
        assert f">{cards} things need you</h1>" in html
    else:
        assert ">All good</h1>" in html and 'id="g-attention"' not in html


@override_settings(DEBUG=True)
@pytest.mark.usefixtures("alice")
def test_busy_preview_has_a_long_activity_list(client: Client):
    html = without_templates(page(client, "/dev/preview/?state=busy"))
    glass = html[html.index('id="g-away-list"') :]
    assert glass[: glass.index("</ol>")].count('<li class="activity__row">') == 10


# --- 7. CHANGELOG: an "Unreleased" heading is skipped by the version check --------------------


def test_unreleased_heading_is_not_taken_for_the_newest_version():
    sample = "# Changelog\n\n## [Unreleased]\n\n- x\n\n## [1.2.3] - 2026-01-01\n"
    assert NEWEST_VERSION_RE.search(sample).group(1) == "1.2.3"  # the real check's pattern


# --- 8. No status dots on the host detail and Clusters pages, except findings and the header ---


NO_DOTS_RULE = '[data-skin="glass"] .g-no-dots .status-icon::before'
KEEP_DOTS_RULE = (
    '[data-skin="glass"] .g-no-dots :is(.issue-line, .host-unit__title) .status-icon::before'
)


def test_the_no_dots_scope_hides_the_dot_and_only_that():
    bodies = [body for selector, body in css_rules(glass_css()) if NO_DOTS_RULE in selector]
    assert bodies and "display: none" in bodies[0]
    # Nothing hides a lamp in that scope: an activity row keeps its dot wherever it appears.
    assert not [s for s, _ in css_rules(glass_css()) if ".g-no-dots" in s and ".lamp" in s]


def test_findings_and_the_header_keep_their_dot():
    """Without it, red and yellow findings look the same to sighted users (words are hidden)."""
    rules = dict(css_rules(glass_css()))
    assert KEEP_DOTS_RULE in rules and "display: block" in rules[KEEP_DOTS_RULE]
    css = no_comments(glass_css())
    # Declared after the rule that hides the dots, and more specific than it.
    assert css.index(NO_DOTS_RULE) < css.index(KEEP_DOTS_RULE)
    # The gap next to a finding's dot stays; only state words lose theirs.
    assert not [
        s for s, _ in css_rules(glass_css()) if ".g-no-dots .issue-line" in s and "gap" in s
    ]


def main_tag(html):
    return re.search(r'<main class="([^"]*)"', html).group(1)


def test_host_detail_has_no_dots_and_keeps_the_words(alice_client, alice, live):  # noqa: F811
    host = make_host(alice, "box-1", "red", make_report())
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    assert "g-no-dots" in main_tag(html)
    assert '<span class="visually-hidden">Needs you</span>' in html


def test_cluster_pages_have_no_dots_and_keep_the_words(alice_client, alice, live):  # noqa: F811
    listing = alice_client.get("/clusters/").content.decode()
    assert "g-no-dots" in main_tag(listing)
    assert 'class="visually-hidden">' in listing
    html = alice_client.get(f"/clusters/{live.pk}/").content.decode()
    assert "g-no-dots" in main_tag(html)
    assert 'class="visually-hidden">' in html


def test_the_overview_and_other_pages_are_outside_the_scope(alice_client, alice, live):  # noqa: F811
    make_host(alice, "box-1", "red", make_report())
    assert "g-no-dots" not in main_tag(page(alice_client))
    assert "g-no-dots" not in main_tag(alice_client.get("/activity/").content.decode())


def test_labels_setting_still_shows_the_words_in_the_scope():
    """The switch's rule isn't scoped, so it reaches the hidden words on these pages too."""
    selectors = [s for s, _ in css_rules(glass_css()) if "> .visually-hidden" in s]
    assert any('[data-labels="on"] .status-icon > .visually-hidden' in s for s in selectors)
