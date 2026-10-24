import io
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
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
SERVER_DIR = Path(__file__).resolve().parents[1]


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
    host.refresh_from_db()
    assert host.status == "offline"
    assert host.connected is False


def test_startup_clears_every_connected_flag(alice, bob):
    hosts = [
        reported_host(alice, "a", connected=True, seen_ago=0),
        reported_host(bob, "b", connected=True, seen_ago=0),
        reported_host(alice, "c", connected=False, seen_ago=0),
    ]
    Host.objects.filter(name="a").update(connection_id="specific.old!socket")

    call_command("startup")

    for host in hosts:
        host.refresh_from_db()
        assert host.connected is False
        assert host.connection_id == ""
        assert host.status == "green"  # only the flag; status is the scheduler's job


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


# transaction=True: the loop calls close_old_connections(), which closes the database
# connection, as it should in production. Inside a test transaction that would end it.
@pytest.mark.django_db(transaction=True)
def test_loop_touches_the_heartbeat_file(alice, tmp_path, monkeypatch):
    from hosts.management.commands import check_hosts

    # Stop the endless loop during the first wait, the way `docker stop` would: a real
    # SIGTERM to this process, handled by the command's own handler.
    def stop(seconds):
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(check_hosts.time, "sleep", stop)
    heartbeat = tmp_path / "alive"
    before = signal.getsignal(signal.SIGTERM)
    out = io.StringIO()
    call_command("check_hosts", "--loop", "--heartbeat-file", str(heartbeat), stdout=out)
    assert heartbeat.exists()
    assert out.getvalue().rstrip().endswith("Stopped.")
    assert signal.getsignal(signal.SIGTERM) == before  # handlers put back


@pytest.mark.django_db(transaction=True)
def test_sigterm_during_a_pass_finishes_it_and_does_not_wait(alice, monkeypatch):
    from hosts.management.commands import check_hosts

    passes = []

    def pass_that_gets_sigterm(self):
        os.kill(os.getpid(), signal.SIGTERM)  # arrives while the pass runs
        passes.append("done")  # ...and the pass still finishes

    def must_not_sleep(seconds):
        raise AssertionError("waited after a stop was requested")

    monkeypatch.setattr(check_hosts.Command, "run_pass", pass_that_gets_sigterm)
    monkeypatch.setattr(check_hosts.time, "sleep", must_not_sleep)
    call_command("check_hosts", "--loop", stdout=io.StringIO())
    assert passes == ["done"]


def test_scheduler_process_stops_on_sigterm(tmp_path):
    """The real command in its own process, as PID 1 runs it: SIGTERM -> exit 0, quickly."""
    env = {
        **{k: v for k, v in os.environ.items() if k not in {"DJANGO_SETTINGS_MODULE"}},
        "DJANGO_SETTINGS_MODULE": "cronrack.settings",
        "DEBUG": "1",
        "SQLITE_PATH": str(tmp_path / "db.sqlite3"),
    }
    manage = [sys.executable, "manage.py"]
    subprocess.run(
        [*manage, "migrate", "--noinput"],
        cwd=SERVER_DIR,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )
    heartbeat = tmp_path / "alive"
    process = subprocess.Popen(
        [*manage, "check_hosts", "--loop", "--every", "30", "--heartbeat-file", str(heartbeat)],
        cwd=SERVER_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not heartbeat.exists():  # the first pass is done; now it waits 30 s
            assert process.poll() is None, process.communicate()[0]
            assert time.monotonic() < deadline, "no pass within 60 s"
            time.sleep(0.1)
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0, output
    assert time.monotonic() - started < 5  # not the 30 s wait, not a kill after a timeout
    assert "Stopped." in output


def test_back_online_after_next_report(alice):
    from hosts.services import save_report

    host = reported_host(alice, connected=False, seen_ago=500)
    mark_silent_hosts_offline(NOW)
    Host.objects.filter(pk=host.pk).update(connected=True)
    host.refresh_from_db()

    result = save_report(host, make_report())

    assert (result.old_status, result.new_status) == ("offline", "green")
