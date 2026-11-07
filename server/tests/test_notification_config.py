"""The effective notification settings and how they reach the rule engine.

Effective rule = the user's NotificationRuleOverride if there is one, otherwise the
preset's default. "Off" drops the finding (not shown, not sent), "Counts as" is the
finding's severity, and a "Limit" choice is the rule's threshold.
"""

from datetime import UTC, datetime

import pytest

from hosts.models import Host
from hosts.services import save_report
from notifications.config import DEFAULT_CONFIG, apply_preset, load_config
from notifications.models import NotificationRuleOverride
from notifications.rules import ALERT, HEADS_UP, NEEDS_YOU, OFF, SHOW
from prefs.models import Prefs
from rules.engine import evaluate
from rules.models import RuleOverride

from .sample import make_report

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def findings_for(host, **changes):
    host.latest_report = make_report(**changes)
    return evaluate(host, host.latest_report, NOW, notify=load_config(host.owner_id))


def rule_ids(findings):
    return {f.rule_id for f in findings}


# --- presets ---------------------------------------------------------------------------


def test_no_prefs_means_balanced_with_no_changes(alice):
    config = load_config(alice.pk)
    assert config.preset == "balanced"
    assert config.level("updates.pending") == ALERT
    assert config.level("network.public_ip_changed") == SHOW
    assert config.counts_as("systemd.unit_failed") == NEEDS_YOU
    assert config.counts_as("updates.pending") == HEADS_UP
    assert not config.is_changed("disk.usage")
    assert DEFAULT_CONFIG.level("updates.pending") == ALERT


def test_quiet_turns_noise_off_and_everything_alerts_on_all(alice):
    Prefs.objects.create(owner=alice, notify_preset="quiet")
    quiet = load_config(alice.pk)
    assert quiet.level("updates.pending") == OFF
    assert quiet.level("systemd.unit_failed") == ALERT

    Prefs.objects.filter(owner=alice).update(notify_preset="everything")
    everything = load_config(alice.pk)
    assert everything.level("network.public_ip_changed") == ALERT


def test_off_drops_the_finding_so_it_is_not_shown(host):
    Prefs.objects.create(owner=host.owner, notify_preset="quiet")
    found = findings_for(host, updates={"total": 4, "security": 0})
    assert "updates.pending" not in rule_ids(found)


def test_an_override_wins_over_the_preset(host):
    Prefs.objects.create(owner=host.owner, notify_preset="quiet")
    NotificationRuleOverride.objects.create(owner=host.owner, rule_id="updates.pending", level=SHOW)
    config = load_config(host.owner_id)
    assert config.level("updates.pending") == SHOW
    assert config.is_changed("updates.pending")
    assert "updates.pending" in rule_ids(findings_for(host, updates={"total": 4, "security": 0}))


def test_overrides_of_another_user_never_apply(host, bob):
    NotificationRuleOverride.objects.create(owner=bob, rule_id="systemd.unit_failed", level=OFF)
    assert load_config(host.owner_id).level("systemd.unit_failed") == ALERT
    assert "systemd.unit_failed" in rule_ids(findings_for(host, failed_units=["x.service"]))


# --- counts as = severity ---------------------------------------------------------------


def test_counts_as_heads_up_makes_a_red_rule_yellow(host):
    NotificationRuleOverride.objects.create(
        owner=host.owner, rule_id="systemd.unit_failed", level=ALERT, counts_as=HEADS_UP
    )
    (finding,) = [
        f
        for f in findings_for(host, failed_units=["x.service"])
        if f.rule_id == "systemd.unit_failed"
    ]
    assert finding.severity == "yellow"
    save_report(host, make_report(failed_units=["x.service"]), now=NOW)
    host.refresh_from_db()
    assert host.status == "yellow"


def test_counts_as_left_at_its_default_keeps_the_rules_own_severity(host):
    # Disk is "heads up" by default but turns red from 95%: that must stay.
    NotificationRuleOverride.objects.create(
        owner=host.owner, rule_id="disk.usage", level=ALERT, counts_as=HEADS_UP
    )
    (disk,) = findings_for(host, disks=[{"mount": "/", "percent": 97}])
    assert disk.severity == "red"


# --- limit = threshold ------------------------------------------------------------------


def test_a_limit_choice_is_the_rules_threshold(host):
    NotificationRuleOverride.objects.create(
        owner=host.owner, rule_id="disk.usage", level=ALERT, limit="90"
    )
    assert findings_for(host, disks=[{"mount": "/", "percent": 88}]) == []
    (disk,) = findings_for(host, disks=[{"mount": "/", "percent": 91}])
    assert disk.severity == "yellow"


def test_an_admin_rule_override_still_wins_over_the_notification_layer(host):
    NotificationRuleOverride.objects.create(
        owner=host.owner, rule_id="disk.usage", level=ALERT, limit="95"
    )
    RuleOverride.objects.create(owner=host.owner, rule_id="disk.usage", threshold=80)
    assert rule_ids(findings_for(host, disks=[{"mount": "/", "percent": 82}])) == {"disk.usage"}


def test_a_stored_limit_that_is_not_a_choice_is_ignored(host):
    NotificationRuleOverride.objects.create(
        owner=host.owner, rule_id="disk.usage", level=ALERT, limit="7"
    )
    assert load_config(host.owner_id).limit_value("disk.usage") is None
    assert rule_ids(findings_for(host, disks=[{"mount": "/", "percent": 86}])) == {"disk.usage"}


# --- applies to -------------------------------------------------------------------------


def test_applies_to_selected_hosts_turns_the_rule_off_elsewhere(host, alice):
    other = Host.objects.create(owner=alice, name="edge-pi")
    override = NotificationRuleOverride.objects.create(
        owner=alice, rule_id="systemd.unit_failed", level=ALERT, applies_to="selected"
    )
    override.hosts.add(host)

    assert "systemd.unit_failed" in rule_ids(findings_for(host, failed_units=["x.service"]))
    assert "systemd.unit_failed" not in rule_ids(findings_for(other, failed_units=["x.service"]))


def test_applies_to_selected_with_nobody_selected_is_off_everywhere(host, alice):
    NotificationRuleOverride.objects.create(
        owner=alice, rule_id="systemd.unit_failed", level=ALERT, applies_to="selected"
    )
    assert "systemd.unit_failed" not in rule_ids(findings_for(host, failed_units=["x.service"]))


# --- preset changes ---------------------------------------------------------------------


def test_changing_the_preset_clears_only_the_notification_overrides(alice, bob):
    NotificationRuleOverride.objects.create(owner=alice, rule_id="disk.usage", level=OFF)
    NotificationRuleOverride.objects.create(owner=bob, rule_id="disk.usage", level=OFF)
    admin = RuleOverride.objects.create(owner=alice, rule_id="disk.usage", threshold=70)

    apply_preset(alice, "quiet")

    assert Prefs.objects.get(owner=alice).notify_preset == "quiet"
    assert not NotificationRuleOverride.objects.filter(owner=alice).exists()
    assert NotificationRuleOverride.objects.filter(owner=bob).count() == 1
    assert RuleOverride.objects.filter(pk=admin.pk).exists()


def test_an_unknown_preset_is_refused(alice):
    with pytest.raises(ValueError):
        apply_preset(alice, "loud")


# --- the stored defaults ----------------------------------------------------------------


def test_new_prefs_defaults(alice):
    prefs = Prefs.objects.create(owner=alice)
    assert prefs.notify_preset == "balanced"
    assert prefs.quiet_hours_enabled is True
    assert (prefs.quiet_hours_start.hour, prefs.quiet_hours_end.hour) == (23, 8)
    assert prefs.digest_enabled is False
    assert prefs.digest_time.hour == 9
    assert prefs.sounds_enabled is False
    assert prefs.sound_volume == 40


def test_override_validation_rejects_unknown_rules_and_bad_choices(alice):
    from django.core.exceptions import ValidationError

    for bad in (
        {"rule_id": "evil.rule", "level": ALERT},
        {"rule_id": "disk.usage", "level": "loud"},
        {"rule_id": "disk.usage", "level": ALERT, "counts_as": "maybe"},
        {"rule_id": "disk.usage", "level": ALERT, "limit": "99"},
        {"rule_id": "updates.pending", "level": ALERT, "limit": "85"},  # no limit there
    ):
        with pytest.raises(ValidationError):
            NotificationRuleOverride(owner=alice, **bad).full_clean()
