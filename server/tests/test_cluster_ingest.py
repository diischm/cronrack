"""A cluster view arrives inside a host's report: who owns it, who reports it, who is a node."""

import logging
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from activity.models import StatusEvent
from agents.consumers import ingest_report
from clusters.models import Cluster
from clusters.services import (
    STALE_AFTER_SECONDS,
    link_nodes,
    save_cluster_snapshot,
    split_report,
)
from hosts.models import Host
from hosts.services import save_report
from rules.engine import load_cluster_overrides  # noqa: F401 - imported by the service too
from rules.models import Suppression

from .conftest import CLUSTER_UID
from .sample import make_cluster, make_kube_report

pytestmark = pytest.mark.django_db


def ingest(host, report):
    """What the agent socket does with a validated report (the same function, minus the thread hop)."""
    ingest_report.func(host.pk, report, "conn-1")


def pod(**changes):
    values = {
        "ns": "apps",
        "name": "web-5d9f7c8b6-x2k4q",
        "node": "worker-1",
        "phase": "Running",
        "ready": [0, 1],
        "restarts": 0,
        "reason": "CrashLoopBackOff",
        "last": None,
        "last_age_s": None,
        "age_s": 86400,
        "owner": "Deployment/web",
    }
    values.update(changes)
    return values


# --- splitting the report ----------------------------------------------------------------------


def test_split_keeps_the_node_part_and_a_small_stub_on_the_host():
    report = make_kube_report()
    host_part, cluster = split_report(report)

    assert host_part["kubernetes"]["node"] == report["kubernetes"]["node"]
    assert host_part["kubernetes"]["cluster"] == {"uid": CLUSTER_UID, "truncated": False}
    assert cluster == report["kubernetes"]["cluster"]
    assert host_part["hostname"] == report["hostname"]
    # the original is untouched
    assert "nodes" in report["kubernetes"]["cluster"]


def test_split_without_kubernetes_returns_the_same_report():
    from .sample import make_report

    report = make_report()
    assert split_report(report) == (report, None)
    assert split_report(make_report(kubernetes=None)) == (make_report(kubernetes=None), None)


def test_split_node_only_report_has_no_cluster():
    host_part, cluster = split_report(make_kube_report(cluster=None))
    assert cluster is None
    assert host_part["kubernetes"]["cluster"] is None


def test_split_stub_for_a_cluster_without_uid():
    report = make_kube_report(cluster=make_cluster(uid=None, truncated=True, cut=["pods"]))
    host_part, _ = split_report(report)
    assert host_part["kubernetes"]["cluster"] == {"uid": None, "truncated": True}


# --- the consumer's database step ---------------------------------------------------------------------


def test_ingest_stores_the_cluster_and_keeps_the_bulk_off_the_host(alice, host):
    ingest(host, make_kube_report())

    host.refresh_from_db()
    assert host.latest_report["kubernetes"]["cluster"] == {"uid": CLUSTER_UID, "truncated": False}
    assert "nodes" not in str(host.latest_report["kubernetes"])
    cluster = Cluster.objects.get()
    assert cluster.owner == alice
    assert cluster.uid == CLUSTER_UID
    assert cluster.name == "nas-cluster"
    assert cluster.reporter == host
    assert cluster.latest_snapshot["nodes"][0]["name"] == "nas"
    assert cluster.snapshot_received_at is not None
    assert cluster.last_seen_at == cluster.snapshot_received_at
    assert cluster.status == "green"


def test_ingest_records_the_first_report_as_an_event(alice, host):
    ingest(host, make_kube_report())
    event = StatusEvent.objects.get(cluster=Cluster.objects.get())
    assert (event.old_status, event.new_status) == ("waiting", "green")
    assert event.host is None


def test_second_report_updates_the_same_cluster(alice, host):
    ingest(host, make_kube_report())
    ingest(host, make_kube_report(cluster=make_cluster(version="v1.31.3+k3s1")))

    assert Cluster.objects.count() == 1
    assert Cluster.objects.get().latest_snapshot["version"] == "v1.31.3+k3s1"
    assert StatusEvent.objects.filter(cluster__isnull=False).count() == 1  # green stays green


def test_a_problem_changes_the_status_once_and_records_it(alice, host):
    ingest(host, make_kube_report())
    broken = make_cluster(problem_pods=[pod()])
    ingest(host, make_kube_report(cluster=broken))
    ingest(host, make_kube_report(cluster=broken))

    cluster = Cluster.objects.get()
    assert cluster.status == "red"
    events = list(StatusEvent.objects.filter(cluster=cluster).order_by("created_at", "pk"))
    assert [(e.old_status, e.new_status) for e in events] == [
        ("waiting", "green"),
        ("green", "red"),
    ]
    assert "keeps crashing" in events[1].summary


def test_report_without_the_key_does_not_touch_clusters(alice, host):
    from .sample import make_report

    ingest(host, make_report())
    assert Cluster.objects.count() == 0
    host.refresh_from_db()
    assert host.latest_report == make_report()


def test_node_only_report_creates_no_cluster(alice, host):
    ingest(host, make_kube_report(cluster=None, role="agent"))
    assert Cluster.objects.count() == 0
    host.refresh_from_db()
    assert host.latest_report["kubernetes"]["node"]["role"] == "agent"


def test_a_broken_cluster_part_never_costs_the_host_its_report(alice, host, monkeypatch, caplog):
    """The cluster part runs in its own savepoint: if it fails, only it is rolled back. The
    host's report is saved and the socket lives on (no exception reaches the consumer)."""
    from agents import consumers

    def boom(host, view):
        Cluster.objects.create(owner=host.owner, uid=CLUSTER_UID, name="half-done")
        raise RuntimeError("cluster trouble")

    monkeypatch.setattr(consumers, "save_cluster_snapshot", boom)

    ingest(host, make_kube_report())  # does not raise

    host.refresh_from_db()
    assert host.latest_report is not None
    assert host.latest_report["kubernetes"]["node"]["role"] == "server"
    assert not Cluster.objects.exists()  # the half-done cluster part was rolled back
    assert "RuntimeError" in caplog.text
    assert "cluster trouble" not in caplog.text  # class name only, never data


def test_a_name_taken_in_a_race_gets_the_next_free_name(alice, host, monkeypatch):
    """Another report took the default name between choosing it and saving: try again."""
    from clusters import services

    Cluster.objects.create(owner=alice, uid="1" * 8 + "-1111-1111-1111-" + "1" * 12, name="taken")
    real = services._default_name
    picks = iter(["taken"])  # the first pick is already gone

    def racing(owner_id, host):
        return next(picks, None) or real(owner_id, host)

    monkeypatch.setattr(services, "_default_name", racing)

    ingest(host, make_kube_report())

    cluster = Cluster.objects.get(uid=CLUSTER_UID)
    assert cluster.name == "nas-cluster"
    host.refresh_from_db()
    assert host.latest_report is not None


def test_the_default_name_always_fits(alice):
    from clusters.services import NAME_MAX, _default_name

    long_host = Host.objects.create(owner=alice, name="h" * 64)
    full = f"{long_host.name}-cluster"

    def nth(n):  # the name _default_name picks for try n, shortened to fit
        return full[:NAME_MAX] if n == 1 else full[: NAME_MAX - len(f"-{n}")] + f"-{n}"

    Cluster.objects.bulk_create(
        Cluster(owner=alice, uid=f"{n:08x}-2222-2222-2222-222222222222", name=nth(n))
        for n in range(1, 1000)
    )

    name = _default_name(alice.pk, long_host)

    assert len(name) <= NAME_MAX
    assert name.endswith("-1000")
    assert not Cluster.objects.filter(owner=alice, name=name).exists()


def test_cluster_name_gets_a_suffix_when_taken(alice, host):
    Cluster.objects.create(
        owner=alice, uid="1" * 8 + "-1111-1111-1111-" + "1" * 12, name="nas-cluster"
    )
    ingest(host, make_kube_report())
    assert Cluster.objects.get(uid=CLUSTER_UID).name == "nas-cluster-2"


# --- without a cluster ID --------------------------------------------------------------------------------


def test_no_uid_and_no_cluster_stores_nothing_cluster_side(alice, host):
    ingest(host, make_kube_report(cluster=make_cluster(uid=None)))

    assert Cluster.objects.count() == 0
    host.refresh_from_db()
    assert host.latest_report is not None  # the host itself is still saved


def test_no_uid_updates_the_cluster_this_host_already_reports(alice, host):
    ingest(host, make_kube_report())
    unreachable = make_cluster(
        uid=None, api={"error": "refused", "cert_days": None}, nodes=[], problem_pods=[], ok_pods={}
    )
    ingest(host, make_kube_report(cluster=unreachable))

    cluster = Cluster.objects.get()
    assert cluster.status == "red"
    assert cluster.latest_snapshot["api"]["error"] == "refused"
    event = StatusEvent.objects.filter(cluster=cluster).latest("created_at")
    assert event.new_status == "red"
    assert "can't reach" in event.summary


def test_no_uid_never_updates_a_cluster_another_host_reports(alice, host):
    other = Host.objects.create(owner=alice, name="pi")
    ingest(other, make_kube_report())
    before = Cluster.objects.get().latest_snapshot

    ingest(
        host,
        make_kube_report(
            cluster=make_cluster(uid=None, api={"error": "refused", "cert_days": None})
        ),
    )

    assert Cluster.objects.get().latest_snapshot == before


# --- who reports a cluster ---------------------------------------------------------------------------


def test_a_second_host_cannot_take_over_a_live_reporter(alice, host):
    other = Host.objects.create(owner=alice, name="pi")
    ingest(host, make_kube_report())
    ingest(other, make_kube_report(cluster=make_cluster(version="v9.9.9")))

    cluster = Cluster.objects.get()
    assert cluster.reporter == host
    assert cluster.latest_snapshot["version"] == "v1.31.2+k3s1"


def test_a_second_host_takes_over_when_the_reporter_is_stale(alice, host):
    other = Host.objects.create(owner=alice, name="pi")
    ingest(host, make_kube_report())
    Cluster.objects.update(
        snapshot_received_at=timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS + 1)
    )

    ingest(other, make_kube_report(cluster=make_cluster(version="v1.31.9+k3s1")))

    cluster = Cluster.objects.get()
    assert cluster.reporter == other
    assert cluster.latest_snapshot["version"] == "v1.31.9+k3s1"


def test_just_inside_the_stale_limit_is_not_stale(alice, host):
    other = Host.objects.create(owner=alice, name="pi")
    ingest(host, make_kube_report())
    Cluster.objects.update(
        snapshot_received_at=timezone.now() - timedelta(seconds=STALE_AFTER_SECONDS - 5)
    )
    ingest(other, make_kube_report(cluster=make_cluster(version="v9.9.9")))
    assert Cluster.objects.get().reporter == host


def test_the_reporter_host_deleted_lets_another_host_report(alice, host):
    other = Host.objects.create(owner=alice, name="pi")
    ingest(host, make_kube_report())
    host.delete()
    ingest(other, make_kube_report())
    assert Cluster.objects.get().reporter == other


# --- IDOR: another user's hosts and clusters -----------------------------------------------------------


def test_bobs_report_never_updates_alices_cluster(alice, bob, host, cluster):
    bobs_host = Host.objects.create(owner=bob, name="nas")  # same name, same cluster UID
    ingest(bobs_host, make_kube_report())

    mine = Cluster.objects.get(pk=cluster.pk)
    assert mine.latest_snapshot is None
    assert mine.reporter is None
    theirs = Cluster.objects.get(owner=bob)
    assert theirs.pk != cluster.pk
    assert theirs.uid == CLUSTER_UID
    assert theirs.name == "nas-cluster"
    assert Cluster.objects.count() == 2


def test_bobs_same_named_host_is_never_linked_to_alices_cluster(alice, bob):
    alices = Host.objects.create(owner=alice, name="nas", latest_report=make_kube_report())
    bobs = Host.objects.create(owner=bob, name="nas", latest_report=make_kube_report())

    ingest(alices, make_kube_report())

    alices.refresh_from_db()
    bobs.refresh_from_db()
    assert alices.cluster == Cluster.objects.get(owner=alice)
    assert bobs.cluster is None


def test_bobs_agent_cannot_use_alices_uid_to_read_or_change_her_cluster(alice, bob, host):
    ingest(host, make_kube_report())
    alices = Cluster.objects.get(owner=alice)
    bobs_host = Host.objects.create(owner=bob, name="evil")

    ingest(bobs_host, make_kube_report(cluster=make_cluster(problem_pods=[pod()])))

    alices.refresh_from_db()
    assert alices.status == "green"
    assert alices.reporter == host
    assert alices.latest_snapshot["problem_pods"] == []
    assert StatusEvent.objects.filter(cluster=alices).count() == 1


def test_suppressions_of_another_user_do_not_hide_findings(alice, bob, host):
    ingest(host, make_kube_report(cluster=make_cluster(problem_pods=[pod()])))
    cluster = Cluster.objects.get()
    assert cluster.status == "red"
    from rules.base import fingerprint

    Suppression.objects.create(
        owner=bob,
        host=Host.objects.create(owner=bob, name="b"),
        rule_id="k8s.pod_crashloop",
        subject="pod:apps/Deployment/web",
        kind="expected",
    )
    ingest(host, make_kube_report(cluster=make_cluster(problem_pods=[pod()])))
    assert Cluster.objects.get().status == "red"
    assert fingerprint("k8s.pod_crashloop", f"c{cluster.pk}", "pod:apps/Deployment/web")


def test_a_suppression_on_the_cluster_turns_it_green(alice, host):
    ingest(host, make_kube_report(cluster=make_cluster(problem_pods=[pod()])))
    cluster = Cluster.objects.get()
    Suppression.objects.create(
        owner=alice,
        cluster=cluster,
        rule_id="k8s.pod_crashloop",
        subject="pod:apps/Deployment/web",
        kind="expected",
    )
    ingest(host, make_kube_report(cluster=make_cluster(problem_pods=[pod(name="web-new-1")])))
    assert Cluster.objects.get().status == "green"


# --- linking nodes to hosts ---------------------------------------------------------------------------------


def test_nodes_are_linked_to_hosts_by_first_label_lowercased(alice, host):
    snapshot = make_cluster()
    snapshot["nodes"][0]["name"] = "nas"
    snapshot["nodes"][1]["name"] = "worker-1"
    worker = Host.objects.create(
        owner=alice,
        name="w1",
        latest_report=make_kube_report().copy() | {"hostname": "Worker-1.lan"},
    )
    stranger = Host.objects.create(
        owner=alice, name="x", latest_report=make_kube_report().copy() | {"hostname": "other"}
    )
    host.latest_report = make_kube_report()
    host.save()

    ingest(host, make_kube_report(cluster=snapshot))

    cluster = Cluster.objects.get()
    for h in (host, worker, stranger):
        h.refresh_from_db()
    assert host.cluster == cluster
    assert worker.cluster == cluster
    assert stranger.cluster is None


def test_hosts_that_are_no_longer_nodes_are_unlinked(alice, host):
    ingest(host, make_kube_report())
    host.refresh_from_db()
    assert host.cluster is not None

    shrunk = make_cluster()
    shrunk["nodes"] = shrunk["nodes"][1:]  # "nas" left the cluster
    ingest(host, make_kube_report(cluster=shrunk))

    host.refresh_from_db()
    assert host.cluster is None


def test_hosts_without_a_report_are_not_linked(alice, cluster):
    quiet = Host.objects.create(owner=alice, name="nas")
    link_nodes(cluster, make_cluster())
    quiet.refresh_from_db()
    assert quiet.cluster is None


def test_the_host_name_is_not_used_only_the_reported_hostname(alice, cluster):
    renamed = Host.objects.create(
        owner=alice, name="nas", latest_report=make_kube_report() | {"hostname": "elsewhere"}
    )
    link_nodes(cluster, make_cluster())
    renamed.refresh_from_db()
    assert renamed.cluster is None


# --- direct calls -------------------------------------------------------------------------------------------


def test_save_cluster_snapshot_returns_none_without_a_view(alice, host):
    assert save_cluster_snapshot(host, None) is None


def test_save_report_still_works_with_the_stub(alice, host):
    host_part, _ = split_report(make_kube_report())
    result = save_report(host, host_part)
    assert result.new_status == "green"


# --- caps on new clusters (a stolen agent token must not create clusters without end) -----------


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def report_for(n: int):
    return make_kube_report(cluster=make_cluster(uid=uid(n)))


def fill(owner, count, reporter=None, start=1000):
    """`count` existing clusters of `owner`, reported by `reporter` (or by nobody)."""
    for n in range(start, start + count):
        Cluster.objects.create(owner=owner, uid=uid(n), name=f"c{n}", reporter=reporter)


@pytest.fixture
def webhook(alice):
    from prefs.models import Prefs

    return Prefs.objects.create(
        owner=alice, discord_webhook_url="https://discord.com/api/webhooks/123456789/abcDEF_-xyz"
    )


def test_a_host_may_add_up_to_three_clusters_and_no_fourth(alice, host):
    from clusters.services import MAX_CLUSTERS_PER_HOST

    assert MAX_CLUSTERS_PER_HOST == 3
    for n in range(1, 4):
        assert save_cluster_snapshot(host, make_cluster(uid=uid(n))) is not None
    assert save_cluster_snapshot(host, make_cluster(uid=uid(4))) is None

    assert sorted(Cluster.objects.values_list("uid", flat=True)) == [uid(1), uid(2), uid(3)]


def test_a_refused_cluster_still_saves_the_hosts_own_report(alice, host):
    fill(alice, 3, reporter=host)

    ingest(host, report_for(4))

    host.refresh_from_db()
    assert host.latest_report["kubernetes"]["cluster"]["uid"] == uid(4)
    assert host.latest_report["hostname"] == make_kube_report()["hostname"]
    assert not Cluster.objects.filter(uid=uid(4)).exists()


def test_a_refused_cluster_records_no_event_and_sends_no_alert(
    alice, host, webhook, django_capture_on_commit_callbacks
):
    fill(alice, 3, reporter=host)
    ingest(host, make_kube_report(cluster=None))  # the host's own first report (and alert)
    events_before = StatusEvent.objects.count()

    with (
        mock.patch("alerts.discord.send_discord", return_value=True) as send,
        django_capture_on_commit_callbacks(execute=True),
    ):
        ingest(host, report_for(4))

    assert not StatusEvent.objects.filter(cluster__isnull=False).exists()
    assert StatusEvent.objects.count() == events_before
    send.assert_not_called()


def test_the_cap_warning_is_logged_once_per_host_with_ids_only(alice, host, caplog):
    fill(alice, 3, reporter=host)

    with caplog.at_level(logging.WARNING, logger="clusters.services"):
        ingest(host, report_for(4))
        ingest(host, report_for(5))

    warnings = [r for r in caplog.records if r.name == "clusters.services"]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        f"Host {host.pk} (owner {alice.pk}): cluster limit reached, new cluster refused"
    )
    assert host.name not in caplog.text
    assert uid(4) not in caplog.text


def test_a_known_cluster_keeps_updating_at_the_cap(alice, host):
    fill(alice, 2, reporter=host)
    ingest(host, report_for(1))
    assert Cluster.objects.filter(reporter=host).count() == 3

    ingest(host, make_kube_report(cluster=make_cluster(uid=uid(1), version="v1.31.3+k3s1")))

    assert Cluster.objects.get(uid=uid(1)).latest_snapshot["version"] == "v1.31.3+k3s1"


def test_deleting_a_cluster_frees_a_slot(alice, host):
    fill(alice, 3, reporter=host)
    ingest(host, report_for(4))
    assert not Cluster.objects.filter(uid=uid(4)).exists()

    Cluster.objects.filter(reporter=host).first().delete()
    ingest(host, report_for(4))

    assert Cluster.objects.get(uid=uid(4)).reporter == host


def test_other_hosts_clusters_do_not_count_toward_a_hosts_cap(alice, bob, host):
    other = Host.objects.create(owner=alice, name="pi-01")
    bobs = Host.objects.create(owner=bob, name="nas")
    fill(alice, 3, reporter=other)
    fill(bob, 3, reporter=bobs, start=2000)

    ingest(host, report_for(1))

    assert Cluster.objects.get(uid=uid(1)).reporter == host


def test_no_uid_still_updates_the_hosts_cluster_at_the_cap(alice, host):
    fill(alice, 3, reporter=host)
    existing = Cluster.objects.filter(reporter=host).first()

    ingest(host, make_kube_report(cluster=make_cluster(uid=None, version="v1.31.3+k3s1")))

    existing.refresh_from_db()
    assert existing.latest_snapshot["version"] == "v1.31.3+k3s1"
    assert Cluster.objects.count() == 3


def test_the_owner_cap_refuses_the_21st_cluster_across_hosts(alice, bob, host):
    from clusters.services import MAX_CLUSTERS_PER_OWNER

    assert MAX_CLUSTERS_PER_OWNER == 20
    # 20 clusters over ten hosts, two each: every host is below its own cap.
    for h in range(10):
        reporter = Host.objects.create(owner=alice, name=f"node-{h}")
        fill(alice, 2, reporter=reporter, start=1000 + 2 * h)

    ingest(host, report_for(1))  # a host with no clusters at all

    assert not Cluster.objects.filter(uid=uid(1)).exists()
    assert Cluster.objects.filter(owner=alice).count() == 20
    host.refresh_from_db()
    assert host.latest_report is not None

    # Bob is not affected by Alice's clusters.
    bobs = Host.objects.create(owner=bob, name="nas")
    ingest(bobs, report_for(1))
    assert Cluster.objects.get(owner=bob).uid == uid(1)


def test_clusters_without_a_reporter_count_toward_the_owner_cap(alice, host):
    fill(alice, 20)  # e.g. their reporting hosts were deleted

    assert save_cluster_snapshot(host, make_cluster(uid=uid(1))) is None
