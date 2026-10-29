"""Cluster tables: constraints, owner scoping and the migrations that created them."""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from activity.models import StatusEvent
from clusters.access import get_owned_cluster
from clusters.models import Cluster, UpdateCheck
from hosts.models import Host
from rules.engine import load_cluster_overrides, load_cluster_suppressions, load_overrides
from rules.models import RuleOverride, Suppression, fingerprint_for

from .conftest import CLUSTER_UID

pytestmark = pytest.mark.django_db


def test_migrations_match_the_models():
    out = io.StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out)


def test_uid_is_unique_per_owner_but_not_across_owners(alice, bob, cluster):
    with pytest.raises(IntegrityError), transaction.atomic():
        Cluster.objects.create(owner=alice, uid=CLUSTER_UID, name="other")
    Cluster.objects.create(owner=bob, uid=CLUSTER_UID, name="lab")  # same name, same uid: fine


def test_name_is_unique_per_owner(alice, cluster):
    with pytest.raises(IntegrityError), transaction.atomic():
        Cluster.objects.create(owner=alice, uid="0" * 8 + "-0000-0000-0000-" + "0" * 12, name="lab")


def test_bad_uid_and_name_fail_validation(alice):
    with pytest.raises(ValidationError):
        Cluster(owner=alice, uid="not-a-uuid", name="lab").full_clean()
    with pytest.raises(ValidationError):
        Cluster(owner=alice, uid=CLUSTER_UID, name="bad name!").full_clean()


def test_new_cluster_waits_for_data(cluster):
    assert cluster.status == "waiting"
    assert cluster.latest_snapshot is None
    assert cluster.latest_report is None  # the name the rule engine uses


def test_deleting_the_reporter_keeps_the_cluster(alice, cluster, host):
    cluster.reporter = host
    cluster.save()
    host.delete()
    cluster.refresh_from_db()
    assert cluster.reporter is None


def test_deleting_a_cluster_unlinks_its_nodes(alice, cluster, host):
    host.cluster = cluster
    host.save()
    cluster.delete()
    host.refresh_from_db()
    assert host.cluster is None


def test_get_owned_cluster(alice, bob, cluster, django_user_model):
    assert get_owned_cluster(alice, cluster.pk) == cluster
    from django.http import Http404

    with pytest.raises(Http404):
        get_owned_cluster(bob, cluster.pk)
    with pytest.raises(Http404):
        get_owned_cluster(alice, 99999)


def test_update_check_source_is_unique():
    UpdateCheck.objects.create(source="k3s", data={"stable": "v1.31.5+k3s1"})
    with pytest.raises(IntegrityError), transaction.atomic():
        UpdateCheck.objects.create(source="k3s")


# --- rule overrides and suppressions ----------------------------------------------------------


def test_override_belongs_to_a_host_or_a_cluster_not_both(alice, host, cluster):
    with pytest.raises(IntegrityError), transaction.atomic():
        RuleOverride.objects.create(
            owner=alice, rule_id="k8s.pod_pending", host=host, cluster=cluster
        )


def test_cluster_override_is_unique_and_independent_of_the_global_one(alice, cluster):
    RuleOverride.objects.create(owner=alice, rule_id="k8s.pod_pending", cluster=cluster)
    RuleOverride.objects.create(owner=alice, rule_id="k8s.pod_pending")  # all hosts and clusters
    RuleOverride.objects.create(
        owner=alice, rule_id="k8s.pod_pending", cluster=cluster, subject="a"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        RuleOverride.objects.create(owner=alice, rule_id="k8s.pod_pending", cluster=cluster)
    with pytest.raises(IntegrityError), transaction.atomic():
        RuleOverride.objects.create(owner=alice, rule_id="k8s.pod_pending")


def test_override_on_another_users_cluster_fails_validation(bob, cluster):
    override = RuleOverride(owner=bob, rule_id="k8s.pod_pending", cluster=cluster)
    with pytest.raises(ValidationError):
        override.full_clean()


def test_host_overrides_never_include_cluster_overrides(alice, host, cluster):
    mine = RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=host)
    RuleOverride.objects.create(owner=alice, rule_id="k8s.pod_pending", cluster=cluster)
    glob = RuleOverride.objects.create(owner=alice, rule_id="disk.usage")

    assert set(load_overrides(host)) == {mine, glob}


def test_cluster_overrides_are_owner_scoped(alice, bob, cluster):
    mine = RuleOverride.objects.create(owner=alice, rule_id="k8s.pod_pending", cluster=cluster)
    RuleOverride.objects.create(owner=bob, rule_id="k8s.pod_pending")  # bob's global one
    assert load_cluster_overrides(cluster) == [mine]


def test_suppression_has_exactly_one_scope(alice, host, cluster):
    with pytest.raises(IntegrityError), transaction.atomic():
        Suppression.objects.create(owner=alice, rule_id="disk.usage", kind="expected")
    with pytest.raises(IntegrityError), transaction.atomic():
        Suppression.objects.create(
            owner=alice, rule_id="disk.usage", kind="expected", host=host, cluster=cluster
        )


def test_cluster_suppression_fingerprint_uses_the_cluster_scope(alice, host, cluster):
    on_cluster = Suppression.objects.create(
        owner=alice, cluster=cluster, rule_id="k8s.pod_pending", kind="expected", subject="x"
    )
    assert on_cluster.fingerprint == fingerprint_for("k8s.pod_pending", f"c{cluster.pk}", "x")
    on_host = Suppression.objects.create(
        owner=alice, host=host, rule_id="k8s.pod_pending", kind="expected", subject="x"
    )
    assert on_host.fingerprint == fingerprint_for("k8s.pod_pending", host.pk, "x")
    assert on_cluster.fingerprint != on_host.fingerprint
    assert load_cluster_suppressions(cluster) == [on_cluster]


def test_suppression_on_another_users_cluster_fails_validation(bob, cluster):
    suppression = Suppression(
        owner=bob, cluster=cluster, rule_id="k8s.pod_pending", kind="expected"
    )
    with pytest.raises(ValidationError):
        suppression.full_clean()


# --- status events ----------------------------------------------------------------------------------


def test_status_event_has_exactly_one_subject(alice, host, cluster):
    with pytest.raises(IntegrityError), transaction.atomic():
        StatusEvent.objects.create(old_status="green", new_status="red")
    with pytest.raises(IntegrityError), transaction.atomic():
        StatusEvent.objects.create(host=host, cluster=cluster, old_status="green", new_status="red")


def test_owned_by_returns_the_users_host_and_cluster_events_only(alice, bob, host, cluster):
    bobs_host = Host.objects.create(owner=bob, name="bobs")
    bobs_cluster = Cluster.objects.create(
        owner=bob, uid="1" * 8 + "-1111-1111-1111-" + "1" * 12, name="b"
    )
    mine = [
        StatusEvent.objects.create(host=host, old_status="green", new_status="red"),
        StatusEvent.objects.create(cluster=cluster, old_status="green", new_status="red"),
    ]
    StatusEvent.objects.create(host=bobs_host, old_status="green", new_status="red")
    StatusEvent.objects.create(cluster=bobs_cluster, old_status="green", new_status="red")

    assert set(StatusEvent.objects.owned_by(alice)) == set(mine)
    assert StatusEvent.objects.owned_by(alice).count() == 2


def test_cluster_event_text_and_owner(alice, cluster):
    event = StatusEvent.objects.create(
        cluster=cluster, old_status="green", new_status="red", summary="api down"
    )
    assert event.subject_name == "lab"
    assert event.owner_id == alice.pk
    assert event.describe() == "lab went red: api down"
    assert str(event) == "lab: green -> red"


def test_deleting_a_cluster_deletes_its_events(cluster):
    StatusEvent.objects.create(cluster=cluster, old_status="green", new_status="red")
    cluster.delete()
    assert StatusEvent.objects.count() == 0


def test_prefs_check_updates_online_is_off_by_default(alice):
    from prefs.models import Prefs

    assert Prefs.objects.create(owner=alice).check_updates_online is False
    assert timezone.now()
