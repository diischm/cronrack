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
from alerts.discord import ALERT_COOLDOWN_SECONDS
from clusters.models import Cluster
from clusters.services import (
    cluster_offline_threshold,
    effective_cluster_status,
    mark_silent_clusters_offline,
)
from cronrack.dev_views import XSS_IMG, XSS_SCRIPT
from notifications import dispatch
from notifications.text import escape_markdown
from prefs.models import Prefs
from rules.base import Finding, fingerprint
from rules.engine import CLUSTER_OFFLINE_RULE
from rules.models import RuleOverride
from rules.registry import get_rule

from .conftest import CLUSTER_UID
from .sample import make_cluster

pytestmark = pytest.mark.django_db

# The rule's default_threshold: the only source of the cluster offline limit.
CLUSTER_SILENT_SECONDS = get_rule(CLUSTER_OFFLINE_RULE).default_threshold
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


def node_down(cluster, node="node-a") -> Finding:
    rule_id = "k8s.node_not_ready"
    subject = f"node:{node}"
    return Finding(
        rule_id=rule_id,
        subject=subject,
        severity="red",
        text=f"Node {node} not ready",
        why="",
        fix="",
        fingerprint=fingerprint(rule_id, f"c{cluster.pk}", subject),
    )


def evaluate(subject, now, *findings) -> int:
    """One evaluation of `subject` with these findings, then its delivery."""
    dispatch.sync(subject, list(findings), now)
    return dispatch.deliver(subject, now)


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
        record_status_change(evil, "green", "red", [])
        with mock.patch("alerts.discord.send_discord", return_value=True) as send:
            evaluate(evil, NOW, node_down(evil))
        text = send.call_args.args[1]
        # Plain text for Discord: no markdown survives (<, = and quotes mean nothing there).
        assert escape_markdown(name) in text
    items = activity_page(alice, 1)["items"]
    assert {i["text"] for i in items} == {f"{XSS_SCRIPT} went red", f"{XSS_IMG} went red"}


def test_activity_text_for_a_cluster_event(cluster):
    event = StatusEvent.objects.create(
        cluster=cluster, old_status="green", new_status="red", summary="api down"
    )
    assert event.describe() == "lab went red: api down"


# --- Discord: one message per problem, cooldown -----------------------------------------------


def test_a_cluster_problem_sends_one_alert_to_its_owners_webhook(cluster, webhook):
    with mock.patch("alerts.discord.send_discord", return_value=True) as send:
        assert evaluate(cluster, NOW, node_down(cluster)) == 1
        assert evaluate(cluster, NOW + timedelta(minutes=1), node_down(cluster)) == 0

    send.assert_called_once_with(GOOD_URL, "🔴 lab: Node node-a not ready")


def test_no_webhook_no_alert(cluster):
    with mock.patch("alerts.discord.send_discord") as send:
        assert evaluate(cluster, NOW, node_down(cluster)) == 0
    send.assert_not_called()


def test_another_users_webhook_is_never_used(alice, bob, cluster):
    Prefs.objects.create(owner=bob, discord_webhook_url=GOOD_URL)
    with mock.patch("alerts.discord.send_discord") as send:
        assert evaluate(cluster, NOW, node_down(cluster)) == 0
    send.assert_not_called()


def test_cluster_cooldown_holds_back_the_second_alert(cluster, webhook):
    with mock.patch("alerts.discord.send_discord", return_value=True) as send:
        assert evaluate(cluster, NOW, node_down(cluster)) == 1
        later = NOW + timedelta(seconds=10)
        assert evaluate(cluster, later, node_down(cluster), node_down(cluster, "node-b")) == 0
        assert dispatch.deliver_pending(later) == 0
        assert dispatch.deliver_pending(NOW + timedelta(seconds=ALERT_COOLDOWN_SECONDS)) == 1

    texts = [c.args[1] for c in send.call_args_list]
    assert texts == ["🔴 lab: Node node-a not ready", "🔴 lab: Node node-b not ready"]


def test_cooldown_of_a_cluster_and_a_host_are_independent(cluster, webhook, host):
    from hosts.services import save_report

    from .sample import make_report

    with mock.patch("alerts.discord.send_discord", return_value=True) as send:
        save_report(host, make_report(failed_units=["x.service"]), now=NOW)
        assert dispatch.deliver(host, NOW) == 1
        assert evaluate(cluster, NOW, node_down(cluster)) == 1
    assert send.call_count == 2


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
    assert effective_cluster_status(live, NOW, 90, []) == "green"
    assert effective_cluster_status(live, NOW + timedelta(seconds=300), 90, []) == "offline"
    assert effective_cluster_status(live, NOW + timedelta(seconds=300), 900, []) == "green"


def test_effective_status_without_data_is_waiting(cluster):
    assert effective_cluster_status(cluster, NOW, 90, []) == "waiting"


# --- the k8s.cluster_offline override ------------------------------------------------------------
#
# cluster_offline_threshold() is the one place the limit is decided; the page
# (effective_cluster_status) and the scheduler (mark_silent_clusters_offline) both use it.


def offline_override(owner, cluster=None, **fields):
    return RuleOverride.objects.create(
        owner=owner, rule_id=CLUSTER_OFFLINE_RULE, cluster=cluster, **fields
    )


def page_status(cluster, seconds, silent_after=90):
    overrides = list(RuleOverride.objects.filter(owner=cluster.owner))
    return effective_cluster_status(
        cluster, NOW + timedelta(seconds=seconds), silent_after, overrides
    )


def test_a_longer_cluster_override_keeps_the_cluster_online(alice, live):
    assert page_status(live, 300) == "offline"  # without an override: past the default

    offline_override(alice, live, threshold=600)

    assert page_status(live, 300) == "green"
    assert mark_silent_clusters_offline(NOW + timedelta(seconds=300)) == []
    assert mark_silent_clusters_offline(NOW + timedelta(seconds=621)) == [live]


def test_a_shorter_override_wins_over_a_longer_silence_setting(alice, live):
    Prefs.objects.create(owner=alice, silent_after_seconds=900)
    offline_override(alice, live, threshold=60)

    assert page_status(live, 120, silent_after=900) == "offline"
    assert mark_silent_clusters_offline(NOW + timedelta(seconds=120)) == [live]


def test_a_global_override_applies_to_the_cluster(alice, live):
    offline_override(alice, None, threshold=600)

    assert page_status(live, 300) == "green"
    assert mark_silent_clusters_offline(NOW + timedelta(seconds=300)) == []


def test_another_owners_or_a_hosts_override_is_ignored(alice, bob, live, host):
    offline_override(bob, None, threshold=600)
    RuleOverride.objects.create(owner=alice, rule_id=CLUSTER_OFFLINE_RULE, host=host, threshold=600)
    every = list(RuleOverride.objects.all())  # even if a caller passed them in by mistake

    assert cluster_offline_threshold(live, 90, every) == CLUSTER_SILENT_SECONDS
    assert page_status(live, 300) == "offline"
    assert mark_silent_clusters_offline(NOW + timedelta(seconds=300)) == [live]


@pytest.mark.parametrize("seconds", [100, 300, 700, 3600])
@pytest.mark.parametrize("threshold", [None, 60, 600])
def test_the_page_and_the_scheduler_agree(alice, live, seconds, threshold):
    if threshold is not None:
        offline_override(alice, live, threshold=threshold)
    shown = page_status(live, seconds)
    marked = mark_silent_clusters_offline(NOW + timedelta(seconds=seconds))
    assert (shown == "offline") == (marked == [live])


def test_the_scheduler_loads_overrides_once_not_per_cluster(alice, bob, live):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for n, owner in enumerate([alice, alice, bob, bob]):
        Cluster.objects.create(
            owner=owner,
            uid=f"{n:08d}-0000-0000-0000-000000000000",
            name=f"c{n}",
            status="green",
            latest_snapshot=make_cluster(),
            last_seen_at=NOW,
        )
    offline_override(alice, None, threshold=600)
    offline_override(bob, None, threshold=600)

    with CaptureQueriesContext(connection) as ctx:
        mark_silent_clusters_offline(NOW + timedelta(seconds=60))

    reads = [q["sql"] for q in ctx.captured_queries if "rules_ruleoverride" in q["sql"]]
    assert len(reads) == 1, reads


def test_a_disabled_rule_never_marks_the_cluster_offline(alice, live, webhook):
    offline_override(alice, live, enabled=False)

    assert cluster_offline_threshold(live, 90, list(RuleOverride.objects.all())) is None
    assert page_status(live, 86_400 * 30) == "green"
    with mock.patch("alerts.discord.send_discord", return_value=True) as send:
        assert mark_silent_clusters_offline(NOW + timedelta(days=30)) == []
    live.refresh_from_db()
    assert live.status == "green"
    assert not StatusEvent.objects.filter(cluster=live).exists()
    send.assert_not_called()


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
