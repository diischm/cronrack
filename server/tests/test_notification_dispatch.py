"""Per-finding Discord alerts (notifications/dispatch.py).

One FindingState row per problem (stable fingerprint: scope + subject + rule + a stable key
such as the mount point, never the value or the text). One message when it opens, one
"now red" message when it escalates, one "resolved" message when it has stayed away for
2 evaluations. Quiet hours, the flap guard, the cooldown, maintenance, the first report
after the upgrade, retention and the daily summary are all here.
"""

from datetime import UTC, datetime, time, timedelta
from unittest import mock

import pytest

from hosts.models import Host
from hosts.services import mark_silent_hosts_offline, save_report
from notifications import dispatch
from notifications.models import BaselinePending, FindingState, NotificationRuleOverride
from notifications.rules import SHOW
from notifications.summary import send_daily_summaries
from prefs.models import Prefs

from .sample import make_report

pytestmark = pytest.mark.django_db

GOOD_URL = "https://discord.com/api/webhooks/123456789/abcDEF_-xyz"
NOON = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def prefs(alice):
    return Prefs.objects.create(
        owner=alice, discord_webhook_url=GOOD_URL, timezone="UTC", quiet_hours_enabled=False
    )


@pytest.fixture
def sent(monkeypatch):
    """Every Discord message, in order. The cooldown is off unless a test turns it on."""
    out: list[str] = []

    def fake(url, text):
        assert url == GOOD_URL
        out.append(text)
        return True

    monkeypatch.setattr("alerts.discord.send_discord", fake)
    monkeypatch.setattr("alerts.discord.ALERT_COOLDOWN_SECONDS", 0)
    return out


@pytest.fixture
def push(host, prefs, django_capture_on_commit_callbacks):
    """Save one report at `now`, with the after-commit delivery run."""

    def push(now, **changes):
        with django_capture_on_commit_callbacks(execute=True):
            save_report(host, make_report(**changes), now=now)

    return push


def disk(percent):
    return {"disks": [{"mount": "/", "percent": percent}]}


def minutes(n):
    return NOON + timedelta(minutes=n)


# --- one row per problem, text updated in place ---------------------------------------------


def test_disk_rising_keeps_one_row_and_one_message(push, sent):
    push(minutes(0), **disk(91))
    push(minutes(1), **disk(93))

    assert FindingState.objects.count() == 1
    row = FindingState.objects.get()
    assert row.text == "Disk / at 93%"
    assert len(sent) == 1
    assert sent[0] == "🟡 nas: Disk / at 91%"


def test_crossing_into_red_escalates_the_same_row_once(push, sent):
    for minute, percent in enumerate((91, 93, 96, 97)):
        push(minutes(minute), **disk(percent))

    row = FindingState.objects.get()
    assert (row.severity, row.text, row.resolved_at) == ("red", "Disk / at 97%", None)
    assert sent == ["🟡 nas: Disk / at 91%", "🔴 nas: Disk / at 96% (now red)"]
    assert not any("Resolved" in text for text in sent)


def test_the_fingerprint_never_contains_the_value(push):
    push(minutes(0), **disk(91))
    first = FindingState.objects.get().fingerprint
    push(minutes(1), **disk(96))
    assert FindingState.objects.get().fingerprint == first


def test_two_mounts_are_two_rows(push, sent):
    push(minutes(0), disks=[{"mount": "/", "percent": 91}, {"mount": "/srv", "percent": 90}])
    assert FindingState.objects.count() == 2
    assert sent == ["nas:\n🟡 Disk / at 91%\n🟡 Disk /srv at 90%"]


# --- resolving and the flap guard -----------------------------------------------------------


def test_resolved_only_after_two_clean_evaluations(push, sent):
    push(minutes(0), **disk(91))
    push(minutes(1), **disk(42))
    assert FindingState.objects.get().resolved_at is None
    assert len(sent) == 1

    push(minutes(2), **disk(42))
    assert FindingState.objects.get().resolved_at == minutes(2)
    assert sent[1] == "🟢 nas: Resolved: Disk / at 91%"


def test_flapping_disk_sends_one_message(push, sent):
    for minute, percent in enumerate((86, 84, 86, 84)):
        push(minutes(minute), **disk(percent))

    assert sent == ["🟡 nas: Disk / at 86%"]
    row = FindingState.objects.get()
    assert row.resolved_at is None


def test_reopening_within_15_minutes_reuses_the_row_without_a_new_alert(push, sent):
    push(minutes(0), **disk(91))
    push(minutes(1), **disk(42))
    push(minutes(2), **disk(42))  # resolved message
    row = FindingState.objects.get()

    push(minutes(10), **disk(92))
    assert FindingState.objects.get().pk == row.pk
    assert FindingState.objects.get().resolved_at is None
    assert len(sent) == 2


def test_reopening_later_is_a_new_alert_on_the_same_row(push, sent):
    push(minutes(0), **disk(91))
    push(minutes(1), **disk(42))
    push(minutes(2), **disk(42))

    push(minutes(30), **disk(92))
    assert FindingState.objects.count() == 1
    assert sent[-1] == "🟡 nas: Disk / at 92%"


def test_a_problem_gone_before_it_was_sent_needs_no_resolved_message(push, sent, monkeypatch):
    monkeypatch.setattr("alerts.discord.ALERT_COOLDOWN_SECONDS", 3600)
    push(minutes(0), failed_units=["a.service"])  # sent, starts the cooldown
    push(minutes(1), failed_units=["a.service", "b.service"])  # held: cooldown
    push(minutes(2), failed_units=["a.service"])
    push(minutes(3), failed_units=["a.service"])  # b resolved before anyone heard of it
    monkeypatch.setattr("alerts.discord.ALERT_COOLDOWN_SECONDS", 0)
    dispatch.deliver_pending(minutes(4))
    assert sent == ["🔴 nas: a.service failed"]


def test_a_quiet_flap_reopen_stays_quiet_on_later_reports(push, sent):
    push(minutes(0), **disk(91))
    push(minutes(1), **disk(42))
    push(minutes(2), **disk(42))  # resolved message
    for minute in (5, 6, 7):  # back within 15 minutes, and it stays
        push(minutes(minute), **disk(92))
    assert len(sent) == 2


def test_a_failed_send_is_kept_and_retried(push, sent, monkeypatch):
    monkeypatch.setattr("alerts.discord.send_discord", lambda url, text: False)
    push(minutes(0), failed_units=["x.service"])
    row = FindingState.objects.get()
    assert row.announce == "open"  # still in the outbox
    assert row.announced_severity == ""

    monkeypatch.setattr("alerts.discord.send_discord", lambda url, text: sent.append(text) or True)
    assert dispatch.deliver_pending(minutes(2)) == 1
    assert sent == ["🔴 nas: x.service failed"]


def test_snoozing_an_alerting_problem_ends_it_quietly(push, sent, host, alice):
    from rules.models import Suppression

    push(minutes(0), failed_units=["x.service"])
    Suppression.objects.create(
        owner=alice,
        host=host,
        rule_id="systemd.unit_failed",
        subject="unit:x.service",
        kind="expected",
    )
    push(minutes(1), failed_units=["x.service"])
    push(minutes(2), failed_units=["x.service"])
    assert sent == ["🔴 nas: x.service failed"]  # no "resolved": it isn't
    assert FindingState.objects.get().resolved_at is not None


def test_adding_a_webhook_later_sends_no_resolved_for_unannounced_problems(
    host, alice, sent, django_capture_on_commit_callbacks
):
    prefs = Prefs.objects.create(owner=alice, timezone="UTC", quiet_hours_enabled=False)
    with django_capture_on_commit_callbacks(execute=True):
        save_report(host, make_report(failed_units=["x.service"]), now=minutes(0))
    prefs.discord_webhook_url = GOOD_URL
    prefs.save()
    for minute in (1, 2):
        with django_capture_on_commit_callbacks(execute=True):
            save_report(host, make_report(), now=minutes(minute))
    assert sent == []


def test_the_morning_bundle_waits_for_maintenance_to_end(push, sent, quiet, host):
    push(NIGHT, updates={"total": 3, "security": 0})
    Host.objects.filter(pk=host.pk).update(maintenance_until=MORNING + timedelta(hours=1))
    assert dispatch.send_quiet_bundles(MORNING) == 0
    assert dispatch.send_quiet_bundles(MORNING + timedelta(hours=2)) == 1


# --- levels: off, show, alert ----------------------------------------------------------------


def test_off_is_neither_shown_nor_sent(push, sent, alice):
    NotificationRuleOverride.objects.create(owner=alice, rule_id="disk.usage", level="off")
    push(minutes(0), **disk(91))
    assert sent == []
    assert not FindingState.objects.exists()
    assert Host.objects.get().status == "green"


def test_show_is_kept_for_the_summary_but_not_sent(push, sent, alice):
    NotificationRuleOverride.objects.create(owner=alice, rule_id="disk.usage", level=SHOW)
    push(minutes(0), **disk(91))
    assert sent == []
    row = FindingState.objects.get()
    assert row.level == SHOW
    assert Host.objects.get().status == "yellow"  # still on the dashboard


def test_turning_a_rule_off_resolves_it_silently(push, sent, alice):
    push(minutes(0), **disk(91))
    NotificationRuleOverride.objects.create(owner=alice, rule_id="disk.usage", level="off")
    push(minutes(1), **disk(91))
    push(minutes(2), **disk(91))
    assert len(sent) == 1
    assert FindingState.objects.get().resolved_at is not None


def test_no_webhook_sends_nothing_and_leaves_nothing_pending(host, alice, sent):
    Prefs.objects.create(owner=alice, timezone="UTC", quiet_hours_enabled=False)
    save_report(host, make_report(failed_units=["x.service"]), now=NOON)
    dispatch.deliver_pending(NOON)
    assert sent == []
    assert not FindingState.objects.exclude(announce="").exists()


# --- quiet hours -----------------------------------------------------------------------------


@pytest.fixture
def quiet(prefs):
    prefs.quiet_hours_enabled = True
    prefs.quiet_hours_start = time(23, 0)
    prefs.quiet_hours_end = time(8, 0)
    prefs.save()
    return prefs


NIGHT = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)
MORNING = datetime(2026, 11, 3, 8, 5, tzinfo=UTC)


def test_quiet_hours_window_crosses_midnight(quiet):
    assert dispatch.in_quiet_hours(quiet, NIGHT)
    assert dispatch.in_quiet_hours(quiet, datetime(2026, 11, 3, 7, 59, tzinfo=UTC))
    assert not dispatch.in_quiet_hours(quiet, MORNING)
    assert not dispatch.in_quiet_hours(quiet, NOON)


def test_quiet_hours_use_the_users_time_zone(quiet):
    quiet.timezone = "Europe/Skopje"  # UTC+1 in November
    quiet.save()
    assert dispatch.in_quiet_hours(quiet, datetime(2026, 11, 2, 22, 30, tzinfo=UTC))
    assert not dispatch.in_quiet_hours(quiet, datetime(2026, 11, 3, 7, 30, tzinfo=UTC))


def test_needs_you_gets_through_and_heads_up_waits_for_the_morning(push, sent, quiet):
    push(NIGHT, failed_units=["x.service"], updates={"total": 3, "security": 0})
    assert sent == ["🔴 nas: x.service failed"]

    assert dispatch.send_quiet_bundles(NIGHT + timedelta(hours=1)) == 0  # still night
    assert dispatch.send_quiet_bundles(MORNING) == 1
    assert sent[1] == "🌙 While you were asleep\nnas:\n🟡 3 updates pending"
    assert dispatch.send_quiet_bundles(MORNING + timedelta(minutes=1)) == 0  # once


def test_the_bundle_is_skipped_when_everything_resolved(push, sent, quiet):
    push(NIGHT, updates={"total": 3, "security": 0})
    push(NIGHT + timedelta(minutes=1))
    push(NIGHT + timedelta(minutes=2))
    assert dispatch.send_quiet_bundles(MORNING) == 0
    assert sent == []


def test_a_held_row_that_resolved_is_not_counted_as_told(push, sent, quiet):
    before_eight = datetime(2026, 11, 3, 7, 50, tzinfo=UTC)
    push(before_eight, updates={"total": 3, "security": 0})  # held for the morning
    push(before_eight + timedelta(minutes=1))
    push(before_eight + timedelta(minutes=2))  # resolved before anyone heard of it
    assert dispatch.send_quiet_bundles(MORNING) == 0  # 08:05: nothing left to say
    row = FindingState.objects.get()
    assert (row.announced_severity, row.announce, row.held_since) == ("", "", None)
    # Back within 15 minutes of resolving (a quiet reopen), then gone again: nothing to
    # take back, so no "Resolved" for a problem nobody was told about.
    for minute, total in ((1, 3), (2, 0), (3, 0)):  # 08:06 is 14 min after 07:52
        push(MORNING + timedelta(minutes=minute), updates={"total": total, "security": 0})
    dispatch.deliver_pending(MORNING + timedelta(minutes=4))
    assert sent == []


def test_a_held_heads_up_that_turns_red_is_sent_at_once(push, sent, quiet):
    push(NIGHT, **disk(91))
    assert sent == []
    push(NIGHT + timedelta(minutes=1), **disk(97))
    assert sent == ["🔴 nas: Disk / at 97%"]
    assert dispatch.send_quiet_bundles(MORNING) == 0


def test_the_bundle_is_one_message_for_all_hosts(
    push, sent, quiet, alice, django_capture_on_commit_callbacks
):
    other = Host.objects.create(owner=alice, name="edge-pi")
    push(NIGHT, updates={"total": 3, "security": 0})
    with django_capture_on_commit_callbacks(execute=True):
        save_report(other, make_report(updates={"total": 1, "security": 0}), now=NIGHT)
    assert dispatch.send_quiet_bundles(MORNING) == 1
    assert len(sent) == 1
    assert "nas:\n🟡 3 updates pending" in sent[0]
    assert "edge-pi:\n🟡 1 update pending" in sent[0]


# --- cooldown, maintenance, first report after the upgrade --------------------------------


def test_the_cooldown_holds_messages_and_the_scheduler_sends_them(push, sent, monkeypatch):
    monkeypatch.setattr("alerts.discord.ALERT_COOLDOWN_SECONDS", 60)
    push(minutes(0), failed_units=["a.service"])
    push(minutes(0) + timedelta(seconds=10), failed_units=["a.service", "b.service"])
    assert sent == ["🔴 nas: a.service failed"]

    assert dispatch.deliver_pending(minutes(0) + timedelta(seconds=30)) == 0
    assert dispatch.deliver_pending(minutes(2)) == 1
    assert sent[1] == "🔴 nas: b.service failed"
    assert dispatch.deliver_pending(minutes(3)) == 0


def test_maintenance_holds_everything_until_it_ends(push, sent, host):
    Host.objects.filter(pk=host.pk).update(maintenance_until=minutes(30))
    push(minutes(0), failed_units=["x.service"])
    assert sent == []
    assert dispatch.deliver_pending(minutes(31)) == 1
    assert sent == ["🔴 nas: x.service failed"]


def test_first_report_after_the_upgrade_is_recorded_silently(push, sent, host):
    BaselinePending.objects.create(host=host)
    push(minutes(0), failed_units=["x.service"])
    assert sent == []
    assert FindingState.objects.get().resolved_at is None
    assert not BaselinePending.objects.exists()

    push(minutes(1))
    push(minutes(2))
    assert sent == ["🟢 nas: Resolved: x.service failed"]  # it was known before the upgrade


def test_a_brand_new_host_alerts_on_its_first_report(push, sent):
    push(minutes(0), failed_units=["x.service"])
    assert sent == ["🔴 nas: x.service failed"]


def test_a_healthy_first_report_sends_nothing(push, sent):
    push(minutes(0))
    assert sent == []


# --- names are escaped -----------------------------------------------------------------------


def test_discord_markdown_in_names_is_escaped(push, sent, host):
    Host.objects.filter(pk=host.pk).update(name="*nas*_`x`")
    push(minutes(0), failed_units=["evil_*unit*.service"])
    assert sent == [r"🔴 \*nas\*\_\`x\`: evil\_\*unit\*.service failed"]


def test_escape_markdown_covers_links_quotes_and_headings():
    from notifications.text import ZERO_WIDTH_SPACE, escape_markdown

    unlinked = f"https:{ZERO_WIDTH_SPACE}//evil.example"
    assert escape_markdown("[x](https://evil.example)") == rf"\[x\]\({unlinked}\)"
    assert escape_markdown("> # ~~s~~ ||spoiler|| @everyone") == (
        r"\> \# \~\~s\~\~ \|\|spoiler\|\| @everyone"
    )


def test_a_name_cannot_forge_extra_message_lines():
    from notifications.text import escape_markdown

    assert escape_markdown("x\n🟢 Resolved: backup\r\n\tok") == "x 🟢 Resolved: backup ok"


def test_mention_and_channel_syntax_stays_text():
    from notifications.text import escape_markdown

    assert escape_markdown("<@123> <#456>") == r"\<@123\> \<\#456\>"


def test_one_huge_line_is_cut_not_dropped():
    from notifications.text import LINE_MAX, subject_message

    message = subject_message("nas", [("🔴", ": ", "x" * 5000)])
    assert message.startswith("🔴 nas: xxx")
    assert len(message) == LINE_MAX
    assert message.endswith("…")


def test_a_hostile_agent_cannot_grow_the_table_without_limit(host, prefs, monkeypatch):
    monkeypatch.setattr("notifications.dispatch.MAX_OPEN_PER_SUBJECT", 3)
    save_report(host, make_report(failed_units=[f"u{i}.service" for i in range(10)]), now=NOON)
    assert FindingState.objects.filter(host=host).count() == 3


# --- offline: resolved after 2 scheduler passes ---------------------------------------------


def test_offline_opens_once_and_resolves_after_two_scheduler_passes(
    host, prefs, sent, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        save_report(host, make_report(), now=NOON)
    Host.objects.filter(pk=host.pk).update(connected=False)
    later = NOON + timedelta(minutes=10)
    with django_capture_on_commit_callbacks(execute=True):
        mark_silent_hosts_offline(later)
        mark_silent_hosts_offline(later + timedelta(seconds=30))
    assert sent == ["🔴 nas went offline: Last seen 10 min ago"]

    back = later + timedelta(minutes=1)
    with django_capture_on_commit_callbacks(execute=True):
        save_report(host, make_report(), now=back)
    assert dispatch.sweep_offline(back) == 0
    assert len(sent) == 1
    assert dispatch.sweep_offline(back + timedelta(seconds=30)) == 1
    dispatch.deliver_pending(back + timedelta(seconds=30))
    assert sent[1] == "🟢 nas is back online"


# --- retention -------------------------------------------------------------------------------


def test_prune_removes_old_delivered_resolved_rows_only(host, alice):
    now = NOON
    common = {"owner": alice, "host": host, "rule_id": "disk.usage", "severity": "yellow"}

    def row(key, resolved_days_ago, announce=""):
        resolved = None if resolved_days_ago is None else now - timedelta(days=resolved_days_ago)
        return FindingState.objects.create(
            fingerprint=key,
            subject=f"mount:/{key}",
            text="x",
            opened_at=now - timedelta(days=60),
            last_seen_at=now - timedelta(days=60),
            resolved_at=resolved,
            announce=announce,
            **common,
        )

    old = row("old", 31)
    recent = row("recent", 29)
    pending = row("pending", 31, announce="resolve")
    still_open = row("open", None)

    assert dispatch.prune(now) == 1
    left = set(FindingState.objects.values_list("pk", flat=True))
    assert left == {recent.pk, pending.pk, still_open.pk}
    assert old.pk not in left


def test_the_scheduler_runs_the_notification_jobs(host, prefs, sent, capsys):
    from django.core.management import call_command

    with mock.patch("notifications.dispatch.prune", return_value=2) as prune:
        call_command("check_hosts", "--once")
    prune.assert_called_once()


# --- the daily summary -----------------------------------------------------------------------


def test_daily_summary_lists_show_only_findings_once_a_day(push, sent, prefs, alice):
    prefs.digest_enabled = True
    prefs.digest_time = time(9, 0)
    prefs.save()
    NotificationRuleOverride.objects.create(owner=alice, rule_id="updates.pending", level=SHOW)
    push(NOON, updates={"total": 3, "security": 0}, failed_units=["x.service"])
    assert sent == ["🔴 nas: x.service failed"]

    next_morning = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    assert send_daily_summaries(next_morning - timedelta(minutes=1)) == 0  # not yet
    assert send_daily_summaries(next_morning) == 1
    assert sent[1] == "📋 Daily summary\nHosts\n• nas: 3 updates pending"
    assert send_daily_summaries(next_morning + timedelta(minutes=5)) == 0  # once a day


def test_daily_summary_is_skipped_when_there_is_nothing(prefs, sent):
    prefs.digest_enabled = True
    prefs.save()
    assert send_daily_summaries(datetime(2026, 11, 3, 9, 30, tzinfo=UTC)) == 0
    assert sent == []


def test_daily_summary_is_off_by_default(push, sent, alice):
    NotificationRuleOverride.objects.create(owner=alice, rule_id="updates.pending", level=SHOW)
    push(NOON, updates={"total": 3, "security": 0})
    assert send_daily_summaries(datetime(2026, 11, 3, 9, 30, tzinfo=UTC)) == 0
