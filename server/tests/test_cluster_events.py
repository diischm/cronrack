"""Cluster status changes: activity list, Discord alerts (same cooldown as hosts), dead-man's switch."""

import io
from datetime import timedelta
from unittest import mock

import pytest
from django.core.management import call_command
from django.utils import timezone

from activity.events import record_status_change
from activity.models import StatusEvent
from activity.views import activity_page
from alerts.discord import (
    ALERT_COOLDOWN_SECONDS,
    format_alert,
    format_current,
    notify,
    send_pending_alerts,
)
from clusters.models import Cluster
from clusters.services import (
    CLUSTER_SILENT_SECONDS,
    effective_cluster_status,
    mark_silent_clusters_offline,
)
from cronrack.dev_views import XSS_IMG, XSS_SCRIPT
from hosts.models import Host
from prefs.models import Prefs

from .conftest import CLUSTER_UID
from .sample import make_cluster

pytestmark = pytest.mark.django_db

GOOD_URL = "https://discord.com/api/webhooks/123456789/abcDEF_-xyz"
NOW = timezone.now()


@pytest.fixture
def webhook(alice):
    return Prefs.objects.create(owner=alice, discord_webhook_url=GOOD_URL)


@pytest.fixture
def live(cluster):
    """A cluster that reported 10 seconds ago and is green."""
    Cluster.objects.filter(pk=cluster.pk).update(
        status="green",
        latest_snapshot=make_cluster(),
        snapshot_received_at=NOW - timedelta(seconds=10),
        last_seen_at=NOW - timedelta(seconds=10),
    )
    cluster.refresh_from_db()
    return cluster


class Clock:
    def __init__(self):
        self.now = timezone.now()

    def tick(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr("alerts.discord.timezone.now", lambda: fake.now)
    return fake


def change(cluster, old, new):
    Cluster.objects.filter(pk=cluster.pk).update(status=new)
    cluster.refresh_from_db()
    return notify(record_status_change(cluster, old, new, []))


# --- the activity list --------------------------------------------------------------------------


def test_cluster_event_shows_in_the_owners_activity_only(alice, bob, cluster, host):
    record_status_change(cluster, "green", "red", [])
    record_status_change(host, "green", "red", [])

    mine = activity_page(alice, 1)["items"]
    assert sorted(i["text"] for i in mine) == ["lab went red", "nas went red"]
    assert activity_page(bob, 1)["items"] == []


def test_hostile_cluster_names_are_plain_text_in_activity_and_discord(alice, webhook):
    # Cluster names are validated, but an old or edited row may hold anything: it must stay text.
    for number, name in enumerate((XSS_SCRIPT, XSS_IMG)):
        evil = Cluster.objects.create(
            owner=alice, uid=f"{number}" * 8 + "-0000-0000-0000-" + f"{number}" * 12, name=name
        )
        event = record_status_change(evil, "green", "red", [])
        assert name in format_alert(event)
        assert name in format_current(evil)
    items = activity_page(alice, 1)["items"]
    assert {i["text"] for i in items} == {f"{XSS_SCRIPT} went red", f"{XSS_IMG} went red"}


def test_alert_text_for_a_cluster_event(cluster):
    event = StatusEvent.objects.create(
        cluster=cluster, old_status="green", new_status="red", summary="api down"
    )
    assert format_alert(event) == "🔴 lab went red: api down"


# --- Discord: cooldown and coalescing -------------------------------------------------------------


def test_a_cluster_change_sends_one_alert_to_its_owners_webhook(cluster, webhook):
    with mock.patch("alerts.discord.send_discord", return_value=True) as send:
        assert change(cluster, "green", "red") is True

    send.assert_called_once_with(GOOD_URL, "🔴 lab went red")


def test_no_webhook_no_alert(cluster):
    with mock.patch("alerts.discord.send_discord") as send:
        assert change(cluster, "green", "red") is False
    send.assert_not_called()


def test_another_users_webhook_is_never_used(alice, bob, cluster):
    Prefs.objects.create(owner=bob, discord_webhook_url=GOOD_URL)
    with mock.patch("alerts.discord.send_discord") as send:
        assert change(cluster, "green", "red") is False
    send.assert_not_called()


def test_cluster_cooldown_holds_back_the_second_alert(cluster, webhook):
    with mock.patch("alerts.discord.send_discord", return_value=True) as send:
        assert change(cluster, "green", "red") is True
        assert change(cluster, "red", "green") is False
    assert send.call_count == 1
    cluster.refresh_from_db()
    assert cluster.last_alert_status == "red"


def test_cooldown_of_a_cluster_and_a_host_are_independent(cluster, webhook, host):
    with mock.patch("alerts.discord.send_discord", return_value=True) as send:
        notify(record_status_change(host, "green", "red", []))
        assert change(cluster, "green", "red") is True
    assert send.call_count == 2


def test_held_back_cluster_change_is_sent_after_the_cooldown(cluster, webhook, clock):
    with mock.patch("alerts.discord.send_discord", return_value=True) as send:
        assert change(cluster, "green", "red") is True
        clock.tick(20)
        assert change(cluster, "red", "green") is False
        assert send_pending_alerts(clock.now) == []
        clock.tick(ALERT_COOLDOWN_SECONDS)
        assert [c.pk for c in send_pending_alerts(clock.now)] == [cluster.pk]
        assert send_pending_alerts(clock.now) == []  # once

    texts = [c.args[1] for c in send.call_args_list]
    assert texts == ["🔴 lab went red", "🟢 lab is green now (after several quick changes)"]


def test_pending_alerts_cover_hosts_and_clusters_together(cluster, host, webhook, clock):
    with mock.patch("alerts.discord.send_discord", return_value=True):
        notify(record_status_change(host, "green", "red", []))
        change(cluster, "green", "red")
        Host.objects.filter(pk=host.pk).update(status="green")
        Cluster.objects.filter(pk=cluster.pk).update(status="green")
        clock.tick(ALERT_COOLDOWN_SECONDS + 1)
        sent = send_pending_alerts(clock.now)
    assert {type(s).__name__ for s in sent} == {"Host", "Cluster"}


# --- the dead-man's switch ----------------------------------------------------------------------------


def test_silent_cluster_goes_offline_exactly_once(
    alice, live, webhook, django_capture_on_commit_callbacks
):
    later = NOW + timedelta(seconds=CLUSTER_SILENT_SECONDS + 11)
    with (
        mock.patch("alerts.discord.send_discord", return_value=True) as send,
        django_capture_on_commit_callbacks(execute=True),
    ):
        marked = mark_silent_clusters_offline(later)
        again = mark_silent_clusters_offline(later + timedelta(seconds=30))

    assert marked == [live]
    assert again == []
    live.refresh_from_db()
    assert live.status == "offline"
    event = StatusEvent.objects.get(cluster=live)
    assert (event.old_status, event.new_status) == ("green", "offline")
    assert event.summary.startswith("No cluster data for")
    assert send.call_count == 1


def test_a_cluster_within_the_limit_stays(live):
    assert mark_silent_clusters_offline(NOW + timedelta(seconds=CLUSTER_SILENT_SECONDS - 10)) == []
    live.refresh_from_db()
    assert live.status == "green"


def test_the_owners_longer_silence_setting_wins(alice, live):
    Prefs.objects.create(owner=alice, silent_after_seconds=600)
    assert mark_silent_clusters_offline(NOW + timedelta(seconds=400)) == []
    assert mark_silent_clusters_offline(NOW + timedelta(seconds=611)) == [live]


def test_waiting_cluster_is_never_offline(cluster):
    assert mark_silent_clusters_offline(NOW + timedelta(days=3)) == []


def test_a_new_snapshot_brings_an_offline_cluster_back(alice, live, host):
    from agents.consumers import ingest_report

    from .sample import make_kube_report

    mark_silent_clusters_offline(NOW + timedelta(seconds=300))
    live.refresh_from_db()
    assert live.status == "offline"
    ingest_report.func(host.pk, make_kube_report(), "c")
    live.refresh_from_db()
    assert live.status == "green"
    assert [e.new_status for e in StatusEvent.objects.filter(cluster=live).order_by("pk")] == [
        "offline",
        "green",
    ]


def test_effective_status_is_worked_out_at_read_time(live):
    assert effective_cluster_status(live, NOW, 90) == "green"
    assert effective_cluster_status(live, NOW + timedelta(seconds=300), 90) == "offline"
    assert effective_cluster_status(live, NOW + timedelta(seconds=300), 900) == "green"


def test_effective_status_without_data_is_waiting(cluster):
    assert effective_cluster_status(cluster, NOW, 90) == "waiting"


def test_check_hosts_marks_a_silent_cluster_offline(live):
    Cluster.objects.filter(pk=live.pk).update(
        last_seen_at=timezone.now() - timedelta(seconds=600),
        snapshot_received_at=timezone.now() - timedelta(seconds=600),
    )
    out = io.StringIO()
    call_command("check_hosts", "--once", stdout=out)
    live.refresh_from_db()
    assert live.status == "offline"
    assert "lab: cluster offline" in out.getvalue()


def test_check_hosts_survives_a_failing_update_check(live):
    with mock.patch(
        "hosts.management.commands.check_hosts.maybe_check_updates", side_effect=RuntimeError("x")
    ):
        call_command("check_hosts", "--once", stdout=io.StringIO())


def test_uid_constant_is_what_the_fixtures_use():
    assert CLUSTER_UID == "08528ee9-428d-5167-8a8e-df19e635932f"
