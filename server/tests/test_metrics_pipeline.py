"""Metrics end to end: report -> samples -> sustained finding -> status -> one Discord row.

Uses the same path as a real report (metrics.ingest.save_metrics, then hosts.services.
save_report), with the clock under test control. Plus the agent socket and the scheduler.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command

from hosts.management.commands import check_hosts
from hosts.services import save_report
from metrics.ingest import save_metrics
from metrics.models import HostSample, Resolution
from notifications.models import FindingState
from prefs.models import Prefs

from .sample import SAMPLE_METRICS, make_metrics, make_metrics_report

pytestmark = pytest.mark.django_db

GOOD_URL = "https://discord.com/api/webhooks/123456789/abcDEF_-xyz"
NOON = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def sent(monkeypatch, alice):
    Prefs.objects.create(
        owner=alice, discord_webhook_url=GOOD_URL, timezone="UTC", quiet_hours_enabled=False
    )
    out: list[str] = []

    def fake(url, text):
        out.append(text)
        return True

    monkeypatch.setattr("alerts.discord.send_discord", fake)
    monkeypatch.setattr("alerts.discord.ALERT_COOLDOWN_SECONDS", 0)
    return out


@pytest.fixture
def report(host, django_capture_on_commit_callbacks):
    """One report at minute n: the samples first, then the report (like the consumer)."""

    def push(n: int, cpu: float):
        now = NOON + timedelta(minutes=n)
        data = make_metrics(cpu={**SAMPLE_METRICS["cpu"], "busy_pct": cpu})
        with django_capture_on_commit_callbacks(execute=True):
            save_metrics(host, data, now)
            return save_report(host, make_metrics_report(data), now=now)

    return push


def cpu_rows(host):
    return FindingState.objects.filter(host=host, rule_id="host.cpu_high")


def test_busy_for_ten_minutes_turns_the_host_yellow_once(host, report, sent):
    for n in range(10):
        assert report(n, 97).new_status == "green"  # not yet ten minutes
    result = report(10, 97)
    assert result.new_status == "yellow"
    assert [f.text for f in result.findings] == ["CPU at 97% for 10+ min"]
    assert cpu_rows(host).count() == 1
    assert len([m for m in sent if "CPU" in m]) == 1

    for n in range(11, 20):  # still busy: the same row, no new message
        report(n, 98)
    assert cpu_rows(host).count() == 1
    assert len([m for m in sent if "CPU" in m]) == 1


def test_a_short_spike_never_alerts(host, report, sent):
    for n in range(15):
        report(n, 99 if n in (7, 8, 9) else 20)
    assert not cpu_rows(host).exists()
    assert sent == []


def test_one_calm_minute_does_not_resolve_it(host, report, sent):
    for n in range(11):
        report(n, 97)
    report(11, 30)  # one quiet minute: 10 of 11 samples still over the limit
    report(12, 97)
    assert cpu_rows(host).count() == 1
    assert not [m for m in sent if "esolved" in m]


def test_old_agent_reports_write_no_samples_and_no_resource_findings(host):
    from .sample import make_report

    result = save_report(host, make_report(), now=NOON)
    assert not HostSample.objects.exists()
    assert not [f for f in result.findings if f.rule_id.startswith("host.")]


# --- the scheduler -----------------------------------------------------------------------


def test_the_scheduler_rolls_up_and_prunes(host, monkeypatch):
    for i in range(6):
        save_metrics(host, SAMPLE_METRICS, NOON + timedelta(minutes=i))
    HostSample.objects.create(host=host, res=Resolution.RAW, at=NOON - timedelta(days=2))
    monkeypatch.setattr(check_hosts.timezone, "now", lambda: NOON + timedelta(minutes=6))

    call_command("check_hosts", "--once")

    # The six minutes since noon: one closed 5-minute bucket (12:05 is still open).
    assert HostSample.objects.filter(res=Resolution.FIVE_MIN, at__gte=NOON).count() == 1
    # The two-day-old raw row was averaged first, then deleted (raw keeps 24 h).
    assert not HostSample.objects.filter(res=Resolution.RAW, at__lt=NOON).exists()
    assert HostSample.objects.filter(res=Resolution.FIVE_MIN, at__lt=NOON).count() == 1


def test_a_failing_metrics_job_never_stops_the_others(host, monkeypatch, caplog):
    from metrics import rollup

    def boom(now):
        raise RuntimeError("database locked")

    pruned = []
    monkeypatch.setattr(rollup, "rollup", boom)
    monkeypatch.setattr(rollup, "prune", lambda now: pruned.append(now) or 0)

    call_command("check_hosts", "--once")

    assert pruned  # prune still ran
    assert "metrics job failed: metric averages written" in caplog.text
