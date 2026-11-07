"""Settings → Notifications: the page, the no-JS form, the JSON autosave, presets and the
test alert. Every write is scoped to the logged-in user."""

import json
import re
from pathlib import Path
from unittest import mock

import pytest
from django.test import Client
from django.utils.html import escape

from clusters.models import Cluster
from hosts.models import Host
from notifications.models import NotificationRuleOverride
from notifications.rules import SPECS, specs_by_group
from prefs.models import Prefs
from rules.models import RuleOverride

from .test_preview import EVENT_HANDLER_ATTR, scan

pytestmark = pytest.mark.django_db

URL = "/settings/notifications/"
PRESET_URL = "/settings/notifications/preset/"
JSON = "application/json"
GOOD_URL = "https://discord.com/api/webhooks/123456789/abcDEF_-xyz"
STATIC = Path(__file__).resolve().parents[1] / "static" / "cronrack"


def defaults_form(preset: str = "balanced", **changes) -> dict:
    """The whole form as the page renders it for `preset` with no overrides."""
    data = {
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "08:00",
        "digest_time": "09:00",
        "sound_volume": "40",
    }
    for spec in SPECS.values():
        data[f"level:{spec.id}"] = spec.defaults[preset]
        data[f"counts:{spec.id}"] = spec.counts_as
        if spec.limit:
            data[f"limit:{spec.id}"] = spec.limit.default
        if spec.applies_to:
            data[f"applies:{spec.id}"] = "all"
    data.update(changes)
    return data


# --- access --------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("url", [URL, PRESET_URL, "/settings/appearance/"])
def test_login_required(client: Client, alice, method, url):
    # alice exists, so the server is set up: anonymous visitors go to the login page.
    response = getattr(client, method)(url)
    assert response.status_code == 302
    assert response["Location"].startswith("/login/")


def test_csrf_is_enforced(alice):
    client = Client(enforce_csrf_checks=True)
    client.force_login(alice)
    assert client.post(URL, defaults_form()).status_code == 403
    assert client.post(PRESET_URL, {"preset": "quiet"}).status_code == 403
    assert client.post("/settings/test-alert/").status_code == 403


# --- the page ------------------------------------------------------------------------------


def test_page_lists_every_rule_with_a_three_way_radio_group(alice_client: Client):
    html = alice_client.get(URL).content.decode()
    for spec in SPECS.values():
        assert escape(spec.label) in html, spec.id
        for level in ("alert", "show", "off"):
            assert f'type="radio" name="level:{spec.id}" value="{level}"' in html, spec.id
    assert html.count('class="notify-rule"') == len(SPECS)


def test_groups_kubernetes_starts_collapsed_with_its_count(alice_client: Client):
    html = alice_client.get(URL).content.decode()
    groups = dict(specs_by_group())
    for name in ("Hosts", "Services &amp; containers", "Network"):
        assert re.search(
            rf'<details class="notify-group"[^>]* open>\s*<summary[^>]*>\s*{name}', html
        )
    k8s = re.search(r'<details class="notify-group"([^>]*)>\s*<summary[^>]*>\s*Kubernetes', html)
    assert k8s and " open" not in k8s.group(1)
    count = len(groups["Kubernetes"])
    assert f'Kubernetes <span class="notify-group__count">· {count}</span>' in html


def test_page_shows_the_settings_tabs_with_notifications_current(alice_client: Client):
    html = alice_client.get(URL).content.decode()
    nav = re.search(
        r'<nav class="settings-tabs" aria-label="Settings sections">(.*?)</nav>', html, re.DOTALL
    )
    assert nav
    assert re.search(
        r'href="/settings/notifications/" aria-current="page">Notifications<', nav.group(1)
    )
    assert 'href="/settings/">Account<' in nav.group(1)
    assert 'href="/settings/appearance/">Appearance<' in nav.group(1)


def test_templates_keep_the_placeholder_marker():
    templates = Path(__file__).resolve().parents[1] / "templates" / "prefs"
    for name in ("index.html", "notifications.html", "appearance.html", "_tabs.html"):
        assert "PLACEHOLDER — Apo owns the final version" in (templates / name).read_text(), name


def test_preset_cards_mark_the_current_one(alice_client: Client, alice):
    Prefs.objects.create(owner=alice, notify_preset="quiet")
    html = alice_client.get(URL).content.decode()
    assert re.search(r'name="preset" value="quiet" aria-pressed="true"', html)
    assert re.search(r'name="preset" value="balanced" aria-pressed="false"', html)


def test_changed_label_only_on_rules_that_differ(alice_client: Client, alice):
    NotificationRuleOverride.objects.create(owner=alice, rule_id="disk.usage", level="show")
    html = alice_client.get(URL).content.decode()
    row = rule_html(html, "disk.usage")
    assert 'class="notify-rule__changed">changed<' in row
    assert 'value="show" checked' in row
    assert 'class="notify-rule__changed" hidden' in rule_html(html, "updates.pending")


def rule_html(html: str, rule_id: str) -> str:
    """One rule's markup, from its row up to the end of its 3-way control."""
    pattern = rf'<div class="notify-rule" data-rule="{re.escape(rule_id)}".*?</fieldset>'
    return re.search(pattern, html, re.DOTALL).group(0)


def test_applies_to_lists_only_the_owners_hosts_and_clusters(
    alice_client: Client, alice, bob, host
):
    Host.objects.create(owner=bob, name="bobs-box")
    Cluster.objects.create(owner=alice, uid="11111111-1111-1111-1111-111111111111", name="lab")
    html = alice_client.get(URL).content.decode()
    assert "bobs-box" not in html
    assert f'name="hosts:disk.usage" value="{host.pk}"' in html
    assert re.search(r'name="clusters:k8s.node_not_ready" value="\d+"', html)
    assert 'name="applies:network.dns_failing"' not in html


def test_hostile_host_names_are_escaped(alice_client: Client, alice):
    Host.objects.create(owner=alice, name="<img src=x onerror=alert(1)>")
    html = alice_client.get(URL).content.decode()
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_page_has_no_inline_script_style_or_handlers(alice_client: Client, host):
    for url in (URL, "/settings/", "/settings/appearance/"):
        raw = alice_client.get(url).content.decode()
        found = scan(raw)
        assert found.inline_script_text == "", url
        assert all(s.get("src") for s in found.scripts), url
        assert found.inline_styles == [], url
        assert found.event_handlers == [], url
        assert not EVENT_HANDLER_ATTR.search(raw), url
        assert "<style" not in raw, url


@pytest.mark.parametrize("name", ["notifications.js", "sound.js"])
def test_notifications_js_follows_the_rules(name):
    source = (STATIC / "js" / name).read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in source, banned
    assert "console." not in source
    assert "dataset" not in source
    assert ".style" not in source


def test_css_respects_reduced_motion_and_uses_no_new_colours():
    css = (STATIC / "css" / "notifications.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css
    assert "grid-template-rows" in css
    # Only existing tokens: no hex colours or rgb() of its own.
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", css)
    assert "rgb(" not in css
    for banned in ("box-shadow: 0 0", "glow", "::before"):
        assert banned not in css


def test_the_sound_settings_reach_every_page_only_when_on(alice_client: Client, alice):
    Prefs.objects.create(owner=alice, sounds_enabled=False)
    assert "data-sound-volume" not in alice_client.get("/").content.decode()
    Prefs.objects.filter(owner=alice).update(sounds_enabled=True, sound_volume=55)
    assert 'data-sound-volume="55"' in alice_client.get("/").content.decode()


# --- saving without JavaScript ------------------------------------------------------------


def test_no_js_save_creates_only_the_changed_override(alice_client: Client, alice):
    response = alice_client.post(URL, defaults_form(**{"level:disk.usage": "show"}))
    assert response.status_code == 302
    assert response["Location"] == URL
    (override,) = NotificationRuleOverride.objects.filter(owner=alice)
    assert (override.rule_id, override.level) == ("disk.usage", "show")


def test_saving_the_defaults_again_removes_the_override(alice_client: Client, alice):
    NotificationRuleOverride.objects.create(owner=alice, rule_id="disk.usage", level="show")
    alice_client.post(URL, defaults_form())
    assert not NotificationRuleOverride.objects.filter(owner=alice).exists()


def test_general_settings_are_saved_to_prefs(alice_client: Client, alice):
    alice_client.post(
        URL,
        defaults_form(
            quiet_hours_enabled="on",
            quiet_hours_start="22:30",
            quiet_hours_end="07:00",
            digest_enabled="on",
            digest_time="18:00",
            sounds_enabled="on",
            sound_volume="70",
        ),
    )
    prefs = Prefs.objects.get(owner=alice)
    assert prefs.quiet_hours_enabled and prefs.digest_enabled and prefs.sounds_enabled
    assert (prefs.quiet_hours_start.hour, prefs.quiet_hours_start.minute) == (22, 30)
    assert prefs.digest_time.hour == 18
    assert prefs.sound_volume == 70


def test_summary_turned_on_after_its_time_starts_tomorrow(alice_client: Client, alice):
    from datetime import UTC, datetime, time

    Prefs.objects.create(owner=alice, timezone="UTC", digest_enabled=False, digest_time=time(8, 0))
    evening = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
    with mock.patch("notifications.views.timezone.now", return_value=evening):
        alice_client.post(URL, defaults_form(digest_enabled="on", digest_time="08:00"))
    prefs = Prefs.objects.get(owner=alice)
    assert prefs.digest_enabled
    assert prefs.digest_sent_on == evening.date()  # no summary right away


def test_summary_turned_on_before_its_time_sends_today(alice_client: Client, alice):
    from datetime import UTC, datetime, time

    Prefs.objects.create(owner=alice, timezone="UTC", digest_enabled=False, digest_time=time(20, 0))
    evening = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
    with mock.patch("notifications.views.timezone.now", return_value=evening):
        alice_client.post(URL, defaults_form(digest_enabled="on", digest_time="20:00"))
    assert Prefs.objects.get(owner=alice).digest_sent_on is None


def test_unchecked_switches_turn_off(alice_client: Client, alice):
    Prefs.objects.create(owner=alice, quiet_hours_enabled=True, sounds_enabled=True)
    alice_client.post(URL, defaults_form())
    prefs = Prefs.objects.get(owner=alice)
    assert not prefs.quiet_hours_enabled and not prefs.sounds_enabled


@pytest.mark.parametrize(
    "bad",
    [
        {"level:disk.usage": "loud"},
        {"counts:disk.usage": "maybe"},
        {"limit:disk.usage": "99"},
        {"applies:disk.usage": "everyone"},
        {"sound_volume": "101"},
        {"quiet_hours_start": "25:00"},
        {"quiet_hours_start": "08:00", "quiet_hours_end": "08:00"},
    ],
)
def test_invalid_values_are_refused_and_nothing_is_saved(alice_client: Client, alice, bad):
    response = alice_client.post(URL, defaults_form(**bad))
    assert response.status_code == 400
    assert not NotificationRuleOverride.objects.exists()
    fetched = alice_client.post(URL, defaults_form(**bad), HTTP_ACCEPT=JSON)
    assert fetched.status_code == 400
    assert json.loads(fetched.content)["ok"] is False


def test_unknown_rule_fields_are_ignored(alice_client: Client, alice):
    response = alice_client.post(URL, defaults_form(**{"level:evil.rule": "alert"}))
    assert response.status_code == 302
    assert not NotificationRuleOverride.objects.exists()


def test_another_users_host_cannot_be_selected(alice_client: Client, alice, bob):
    theirs = Host.objects.create(owner=bob, name="theirs")
    response = alice_client.post(
        URL,
        defaults_form(**{"applies:disk.usage": "selected", "hosts:disk.usage": [str(theirs.pk)]}),
    )
    assert response.status_code == 400
    assert not NotificationRuleOverride.objects.exists()


def test_selected_hosts_are_saved(alice_client: Client, alice, host):
    alice_client.post(
        URL,
        defaults_form(**{"applies:disk.usage": "selected", "hosts:disk.usage": [str(host.pk)]}),
    )
    override = NotificationRuleOverride.objects.get(owner=alice, rule_id="disk.usage")
    assert override.applies_to == "selected"
    assert list(override.hosts.all()) == [host]


def test_saves_are_per_user(alice_client: Client, alice, bob):
    NotificationRuleOverride.objects.create(owner=bob, rule_id="disk.usage", level="off")
    alice_client.post(URL, defaults_form())
    assert NotificationRuleOverride.objects.filter(owner=bob).count() == 1


# --- autosave (fetch) ----------------------------------------------------------------------


def test_json_autosave_answers_ok(alice_client: Client, alice):
    response = alice_client.post(
        URL, defaults_form(**{"level:disk.usage": "off"}), HTTP_ACCEPT=JSON
    )
    assert response.status_code == 200
    assert json.loads(response.content) == {"ok": True, "changed": ["disk.usage"]}


# --- presets -------------------------------------------------------------------------------


def test_preset_post_sets_the_preset_and_clears_overrides(alice_client: Client, alice):
    NotificationRuleOverride.objects.create(owner=alice, rule_id="disk.usage", level="off")
    admin = RuleOverride.objects.create(owner=alice, rule_id="disk.usage", threshold=70)
    response = alice_client.post(PRESET_URL, {"preset": "quiet"})
    assert response.status_code == 302
    assert Prefs.objects.get(owner=alice).notify_preset == "quiet"
    assert not NotificationRuleOverride.objects.filter(owner=alice).exists()
    assert RuleOverride.objects.filter(pk=admin.pk).exists()

    fetched = alice_client.post(PRESET_URL, {"preset": "everything"}, HTTP_ACCEPT=JSON)
    assert json.loads(fetched.content) == {"ok": True, "preset": "everything"}


def test_unknown_preset_is_refused(alice_client: Client, alice):
    assert alice_client.post(PRESET_URL, {"preset": "loud"}).status_code == 400
    assert not Prefs.objects.filter(owner=alice).exclude(notify_preset="balanced").exists()


# --- the test alert ------------------------------------------------------------------------


def test_test_alert_is_labelled_and_rate_limited(alice_client: Client, alice):
    Prefs.objects.create(owner=alice, discord_webhook_url=GOOD_URL)
    with mock.patch("prefs.views.send_discord", return_value=True) as send:
        first = alice_client.post("/settings/test-alert/", HTTP_ACCEPT=JSON)
        second = alice_client.post("/settings/test-alert/", HTTP_ACCEPT=JSON)
    assert json.loads(first.content) == {"ok": True, "error": ""}
    assert second.status_code == 429
    assert "30 seconds" in json.loads(second.content)["error"]
    send.assert_called_once()
    assert "test" in send.call_args.args[1].lower()


def test_the_rate_limit_is_per_user(alice_client: Client, alice, bob):
    Prefs.objects.create(owner=alice, discord_webhook_url=GOOD_URL)
    Prefs.objects.create(owner=bob, discord_webhook_url=GOOD_URL)
    other = Client()
    other.force_login(bob)
    with mock.patch("prefs.views.send_discord", return_value=True) as send:
        alice_client.post("/settings/test-alert/", HTTP_ACCEPT=JSON)
        other.post("/settings/test-alert/", HTTP_ACCEPT=JSON)
    assert send.call_count == 2


# --- the other tabs ------------------------------------------------------------------------


def test_appearance_tab_has_the_theme_pickers(alice_client: Client):
    html = alice_client.get("/settings/appearance/").content.decode()
    assert 'type="radio" name="skin" value="glass"' in html
    assert ">Reduce animations<" in html


def test_account_tab_keeps_webhook_and_password(alice_client: Client):
    html = alice_client.get("/settings/").content.decode()
    assert 'name="discord_webhook_url"' in html
    assert 'autocomplete="current-password"' in html
    assert 'name="skin"' not in html
