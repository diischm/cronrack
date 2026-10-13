from datetime import timedelta
from unittest import mock

import pytest
from django.core.management import call_command
from django.utils import timezone

from activity.models import StatusEvent
from hosts.models import Host
from hosts.services import REPORT_INTERVAL_SECONDS, mark_silent_hosts_offline
from prefs.models import Prefs

from .sample import make_report

pytestmark = pytest.mark.django_db

NOW = timezone.now()


def reported_host(owner, name="nas", *, connected, seen_ago, report_ago=None, status="green"):
    seen = NOW - timedelta(seconds=seen_ago)
    report_at = NOW - timedelta(seconds=report_ago if report_ago is not None else seen_ago)
    return Host.objects.create(
        owner=owner,
        name=name,
        status=status,
        connected=connected,
        last_seen_at=seen,
        latest_report=make_report(),
        report_received_at=report_at,
    )


def test_disconnected_and_silent_too_long_goes_offline(alice):
    host = reported_host(alice, connected=False, seen_ago=91)

    marked = mark_silent_hosts_offline(NOW)

    assert marked == [host]
    host.refresh_from_db()
    assert host.status == "offline"
    event = StatusEvent.objects.get(host=host)
    assert (event.old_status, event.new_status) == ("green", "offline")
    assert event.summary == "Last seen 1 min ago"


def test_disconnected_but_within_threshold_stays(alice):
    host = reported_host(alice, connected=False, seen_ago=89)
    assert mark_silent_hosts_offline(NOW) == []
    host.refresh_from_db()
    assert host.status == "green"


def test_uses_the_owners_silent_after_setting(alice, bob):
    Prefs.objects.create(owner=alice, silent_after_seconds=600)
    patient = reported_host(alice, "patient", connected=False, seen_ago=300)
    default = reported_host(bob, "default", connected=False, seen_ago=300)

    assert mark_silent_hosts_offline(NOW) == [default]
    patient.refresh_from_db()
    assert patient.status == "green"


def test_connected_but_stuck_goes_offline(alice):
    stuck_for = 3 * REPORT_INTERVAL_SECONDS + 1
    host = reported_host(alice, connected=True, seen_ago=0, report_ago=stuck_for)

    assert mark_silent_hosts_offline(NOW) == [host]
    assert StatusEvent.objects.get(host=host).summary.startswith("Connected, but no report")


def test_connected_and_reporting_stays(alice):
    host = reported_host(alice, connected=True, seen_ago=0, report_ago=3 * REPORT_INTERVAL_SECONDS)
    assert mark_silent_hosts_offline(NOW) == []
    host.refresh_from_db()
    assert host.status == "green"


@pytest.mark.parametrize("status", ["waiting", "offline", "maintenance"])
def test_skipped_statuses(alice, status):
    reported_host(alice, connected=False, seen_ago=10_000, status=status)
    assert mark_silent_hosts_offline(NOW) == []
    assert not StatusEvent.objects.exists()


def test_never_reported_is_not_offline(alice):
    Host.objects.create(owner=alice, name="new", status="green")
    assert mark_silent_hosts_offline(NOW) == []


def test_only_one_event_and_alert_per_outage(alice, django_capture_on_commit_callbacks):
    Prefs.objects.create(owner=alice, discord_webhook_url="https://discord.com/api/webhooks/1/abc")
    reported_host(alice, connected=False, seen_ago=500)

    with (
        mock.patch("alerts.discord.send_discord") as send,
        django_capture_on_commit_callbacks(execute=True),
    ):
        mark_silent_hosts_offline(NOW)
        mark_silent_hosts_offline(NOW + timedelta(minutes=5))

    assert StatusEvent.objects.count() == 1
    send.assert_called_once()
    assert send.call_args.args[1] == "🔴 nas went offline: Last seen 8 min ago"


def test_command_once(alice, capsys):
    reported_host(alice, connected=False, seen_ago=500)
    call_command("check_hosts", "--once")
    assert "nas: offline" in capsys.readouterr().out
    assert Host.objects.get().status == "offline"


def test_command_rejects_tiny_interval():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("check_hosts", "--loop", "--every", "1")


def test_loop_touches_the_heartbeat_file(alice, tmp_path, monkeypatch):
    from hosts.management.commands import check_hosts

    # Stop the endless loop after the first pass, the way Ctrl+C would.
    def stop(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(check_hosts.time, "sleep", stop)
    heartbeat = tmp_path / "alive"
    call_command("check_hosts", "--loop", "--heartbeat-file", str(heartbeat))
    assert heartbeat.exists()


def test_back_online_after_next_report(alice):
    from hosts.services import save_report

    host = reported_host(alice, connected=False, seen_ago=500)
    mark_silent_hosts_offline(NOW)
    Host.objects.filter(pk=host.pk).update(connected=True)
    host.refresh_from_db()

    result = save_report(host, make_report())

    assert (result.old_status, result.new_status) == ("offline", "green")
