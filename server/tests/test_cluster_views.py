"""Cluster pages, the JSON endpoint, and the cluster data on the overview and host pages."""

import re
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from activity.models import StatusEvent
from agents.consumers import ingest_report
from clusters.models import Cluster, UpdateCheck
from cronrack.dev_views import XSS_IMG, XSS_SCRIPT
from hosts.models import Host
from hosts.presenters import OwnerData, detail_context, host_summary, overview_context
from prefs.models import Prefs

from .conftest import CLUSTER_UID
from .sample import make_cluster, make_kube_report, make_report

pytestmark = pytest.mark.django_db


def pod(**changes):
    values = {
        "ns": "apps",
        "name": "web-5d9f7c8b6-x2k4q",
        "node": "nas",
        "phase": "Running",
        "ready": [0, 1],
        "restarts": 12,
        "reason": "CrashLoopBackOff",
        "last": "OOMKilled",
        "last_age_s": 120,
        "age_s": 86400,
        "owner": "Deployment/web",
    }
    values.update(changes)
    return values


@pytest.fixture
def live(alice, host):
    """alice's host 'nas' reported a cluster with a crash-looping pod."""
    counts = {"total": 2, "running": 2, "pending": 0, "succeeded": 0, "failed": 0, "problem": 1}
    snapshot = make_cluster(problem_pods=[pod()], pod_counts=counts)
    ingest_report.func(host.pk, make_kube_report(cluster=snapshot), "c")
    return Cluster.objects.get(owner=alice)


# --- the list --------------------------------------------------------------------------------------


def test_list_is_200_without_clusters(alice_client: Client):
    response = alice_client.get("/clusters/")
    assert response.status_code == 200
    assert b"No clusters yet" in response.content


def test_list_shows_only_my_clusters(alice_client: Client, bob, live):
    Cluster.objects.create(
        owner=bob, uid="1" * 8 + "-1111-1111-1111-" + "1" * 12, name="bobs-secret"
    )
    html = alice_client.get("/clusters/").content.decode()
    assert "nas-cluster" in html
    assert "bobs-secret" not in html
    assert "1 problem pod" in html
    assert "keeps crashing" in html


# --- detail page and JSON --------------------------------------------------------------------------------


def test_detail_page_shows_findings_with_why_and_a_copy_only_fix(alice_client: Client, live):
    html = alice_client.get(f"/clusters/{live.pk}/").content.decode()

    assert "keeps crashing" in html
    assert "Kubernetes keeps restarting it" in html
    assert "kubectl -n apps logs web-5d9f7c8b6-x2k4q --previous" in html
    assert "Problem pods" in html
    assert "Healthy pods" in html


def test_detail_page_of_a_waiting_cluster(alice_client: Client, cluster):
    response = alice_client.get(f"/clusters/{cluster.pk}/")
    assert response.status_code == 200
    assert b"Waiting for the first report" in response.content


def test_json_has_the_same_data_as_the_page(alice_client: Client, live):
    data = alice_client.get(f"/api/v1/clusters/{live.pk}/detail.json").json()

    assert data["cluster"]["name"] == "nas-cluster"
    assert data["cluster"]["status"] == "red"
    assert data["cluster"]["url"] == f"/clusters/{live.pk}/"
    assert {i["rule_id"] for i in data["issues"]} == {
        "k8s.pod_crashloop",
        "k8s.pod_oomkilled",
        "k8s.pod_restarts",
    }
    assert data["tiles"]["nodes_total"] == 2
    assert data["nodes"][0]["host"]["name"] == "nas"
    assert data["unlinked_nodes"] == ["worker-1"]
    assert data["problem_pods"][0]["age_human"] == "24 h"
    assert data["versions"]["updates_online"] is False
    assert "Turn on update checks in Settings" in data["versions"]["hint"]


def test_json_content_type_and_scoping(alice_client: Client, bob, live):
    response = alice_client.get(f"/api/v1/clusters/{live.pk}/detail.json")
    assert response["Content-Type"] == "application/json"
    other = Client()
    other.force_login(bob)
    assert other.get(f"/api/v1/clusters/{live.pk}/detail.json").status_code == 404


def test_json_never_lists_another_users_host_as_a_node(alice_client: Client, bob, live):
    Host.objects.create(owner=bob, name="worker-1", latest_report=make_report(hostname="worker-1"))
    data = alice_client.get(f"/api/v1/clusters/{live.pk}/detail.json").json()
    assert data["unlinked_nodes"] == ["worker-1"]
    assert all(n["host"] is None or n["host"]["name"] == "nas" for n in data["nodes"])


def test_version_hint_goes_away_with_update_checks(alice, alice_client: Client, live):
    Prefs.objects.create(owner=alice, check_updates_online=True)
    UpdateCheck.objects.create(source="k3s", data={"stable": "v1.33.1+k3s1"})
    data = alice_client.get(f"/api/v1/clusters/{live.pk}/detail.json").json()
    assert data["versions"]["hint"] is None
    assert data["versions"]["k3s_stable"] == "v1.33.1+k3s1"


# --- hostile data ------------------------------------------------------------------------------------------


def hostile_snapshot():
    evil = pod(name=XSS_SCRIPT, ns=XSS_IMG, owner=f"Deployment/{XSS_IMG}", node=XSS_SCRIPT)
    return make_cluster(
        problem_pods=[evil],
        ok_pods={XSS_SCRIPT: [[XSS_IMG, XSS_SCRIPT, 0, 5]]},
        workloads=[
            {
                **make_cluster()["workloads"][0],
                "name": XSS_SCRIPT,
                "ns": XSS_IMG,
                "images": [],
                "desired": 2,
                "available": 0,
            }
        ],
        events=[
            {
                "ns": XSS_IMG,
                "obj": XSS_SCRIPT,
                "reason": "BackOff",
                "msg": XSS_IMG,
                "count": 1,
                "age_s": 5,
            }
        ],
    )


def test_hostile_agent_data_is_escaped_on_the_cluster_page(alice_client: Client, alice, cluster):
    Cluster.objects.filter(pk=cluster.pk).update(
        status="red",
        latest_snapshot=hostile_snapshot(),
        snapshot_received_at=timezone.now(),
        last_seen_at=timezone.now(),
    )
    html = alice_client.get(f"/clusters/{cluster.pk}/").content.decode()

    assert XSS_SCRIPT not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_hostile_cluster_name_is_escaped_in_every_page(alice_client: Client, alice, cluster):
    Cluster.objects.filter(pk=cluster.pk).update(
        name=XSS_SCRIPT, latest_snapshot=make_cluster(), last_seen_at=timezone.now()
    )
    StatusEvent.objects.create(cluster_id=cluster.pk, old_status="green", new_status="red")
    for url in (
        "/",
        "/clusters/",
        "/activity/",
        f"/clusters/{cluster.pk}/",
        f"/clusters/{cluster.pk}/settings/",
    ):
        html = alice_client.get(url).content.decode()
        assert XSS_SCRIPT not in html, url
        assert "&lt;script&gt;" in html, url


# --- settings: rename and delete ---------------------------------------------------------------------------------


def test_rename(alice_client: Client, cluster):
    response = alice_client.post(f"/clusters/{cluster.pk}/settings/", {"name": "home-lab"})
    assert response.status_code == 302
    cluster.refresh_from_db()
    assert cluster.name == "home-lab"


@pytest.mark.parametrize("name", ["", "bad name", "x" * 65, "a;b"])
def test_rename_rejects_bad_names(alice_client: Client, cluster, name):
    response = alice_client.post(f"/clusters/{cluster.pk}/settings/", {"name": name})
    assert response.status_code == 400
    cluster.refresh_from_db()
    assert cluster.name == "lab"


def test_rename_to_a_taken_name_is_refused_but_another_users_name_is_fine(
    alice_client, alice, bob, cluster
):
    Cluster.objects.create(owner=alice, uid="2" * 8 + "-2222-2222-2222-" + "2" * 12, name="other")
    Cluster.objects.create(owner=bob, uid="3" * 8 + "-3333-3333-3333-" + "3" * 12, name="bobs")
    assert (
        alice_client.post(f"/clusters/{cluster.pk}/settings/", {"name": "other"}).status_code == 400
    )
    assert (
        alice_client.post(f"/clusters/{cluster.pk}/settings/", {"name": "bobs"}).status_code == 302
    )


def test_delete_needs_the_typed_name(alice_client: Client, cluster):
    wrong = alice_client.post(f"/clusters/{cluster.pk}/delete/", {"confirm_name": "nope"})
    assert wrong.status_code == 400
    assert Cluster.objects.filter(pk=cluster.pk).exists()

    done = alice_client.post(f"/clusters/{cluster.pk}/delete/", {"confirm_name": "lab"})
    assert done.status_code == 302
    assert done["Location"] == "/clusters/"
    assert not Cluster.objects.filter(pk=cluster.pk).exists()


def test_delete_only_unlinks_the_hosts(alice_client: Client, live, host):
    host.refresh_from_db()
    assert host.cluster == live
    alice_client.post(f"/clusters/{live.pk}/delete/", {"confirm_name": live.name})
    host.refresh_from_db()
    assert host.cluster is None
    assert Host.objects.filter(pk=host.pk).exists()


def test_settings_page_renders_both_forms(alice_client: Client, cluster):
    html = alice_client.get(f"/clusters/{cluster.pk}/settings/").content.decode()
    assert f'action="/clusters/{cluster.pk}/settings/"' in html
    assert f'action="/clusters/{cluster.pk}/delete/"' in html
    assert html.count("csrfmiddlewaretoken") >= 2


def test_get_is_not_allowed_on_delete(alice_client: Client, cluster):
    assert alice_client.get(f"/clusters/{cluster.pk}/delete/").status_code == 405


# --- navigation --------------------------------------------------------------------------------------------------


def nav_links(client: Client) -> list[str]:
    html = client.get("/").content.decode()
    navs = re.findall(r"<nav\b.*?</nav>", html, flags=re.DOTALL)
    return [href for nav in navs for href in re.findall(r'href="([^"]+)"', nav)]


def test_nav_has_no_clusters_item_without_a_cluster_and_every_link_works(alice_client: Client):
    links = nav_links(alice_client)
    assert "/clusters/" not in links
    assert all(alice_client.get(h).status_code == 200 for h in links)


def test_nav_has_a_working_clusters_item_with_a_cluster(alice_client: Client, cluster):
    links = nav_links(alice_client)
    assert "/clusters/" in links
    assert all(alice_client.get(h).status_code == 200 for h in links)


def test_nav_never_shows_another_users_cluster(alice_client: Client, bob):
    Cluster.objects.create(owner=bob, uid=CLUSTER_UID, name="bobs")
    assert "/clusters/" not in nav_links(alice_client)


# --- overview, host page ----------------------------------------------------------------------------------------


def test_overview_lists_my_clusters_and_counts_them(alice, bob, live):
    Cluster.objects.create(
        owner=bob, uid="4" * 8 + "-4444-4444-4444-" + "4" * 12, name="bobs", status="red"
    )
    data = overview_context(alice, timezone.now())

    assert [c["name"] for c in data["clusters"]] == ["nas-cluster"]
    assert data["clusters"][0]["status"] == "red"
    assert data["clusters"][0]["nodes"] == {"ready": 2, "total": 2}
    assert data["attention_count"] == 1  # the cluster; the host itself is green


def test_overview_without_clusters_has_an_empty_list(alice, host):
    assert overview_context(alice, timezone.now())["clusters"] == []


def test_overview_activity_includes_cluster_events(alice, bob, live):
    texts = [e["text"] for e in overview_context(alice, timezone.now())["activity"]]
    assert any(t.startswith("nas-cluster ") for t in texts)
    assert overview_context(bob, timezone.now())["activity"] == []


def test_offline_cluster_counts_as_attention_at_read_time(alice, live):
    Cluster.objects.filter(pk=live.pk).update(status="green")
    later = timezone.now() + timedelta(seconds=3600)
    data = overview_context(alice, later)
    assert data["clusters"][0]["status"] == "offline"
    assert data["attention_count"] >= 1


def test_host_summary_has_the_k8s_role(alice, host):
    data = OwnerData(alice)
    now = timezone.now()
    assert host_summary(host, now, data)["k8s_role"] is None
    Host.objects.filter(pk=host.pk).update(
        latest_report=make_kube_report(cluster=None, role="agent")
    )
    host.refresh_from_db()
    assert host_summary(host, now, data)["k8s_role"] == "agent"
    Host.objects.filter(pk=host.pk).update(
        latest_report=make_kube_report(cluster=None, role="none")
    )
    host.refresh_from_db()
    assert host_summary(host, now, data)["k8s_role"] is None


def test_host_detail_has_the_kubernetes_panel_when_linked(alice, live, host):
    host.refresh_from_db()
    panel = detail_context(host, timezone.now())["kubernetes"]

    assert panel["role"] == "server"
    assert panel["k3s"] == "v1.31.2+k3s1"
    assert panel["cluster"] == {
        "id": live.pk,
        "name": "nas-cluster",
        "url": f"/clusters/{live.pk}/",
    }
    assert panel["conditions"]["ready"] is True
    assert [p["name"] for p in panel["pods"]] == ["web-5d9f7c8b6-x2k4q"]
    assert "Turn on update checks" in panel["hint"]


def test_host_detail_without_kubernetes_has_no_panel(alice, host):
    host.latest_report = make_report()
    host.save()
    assert detail_context(host, timezone.now())["kubernetes"] is None
    assert (
        detail_context(Host.objects.create(owner=alice, name="new"), timezone.now())["kubernetes"]
        is None
    )


def test_host_page_still_renders_with_the_kubernetes_data(alice_client: Client, live, host):
    assert alice_client.get(f"/hosts/{host.pk}/").status_code == 200
    assert alice_client.get("/").status_code == 200


def test_helm_cli_info_shows_on_the_host_only_with_update_checks(alice, alice_client: Client, host):
    Host.objects.filter(pk=host.pk).update(
        latest_report=make_kube_report(cluster=None, role="none", helm="v3.0.0"),
        status="green",
    )
    UpdateCheck.objects.create(source="helm", data={"latest": "v3.19.0"})
    assert "Helm command is v3.0.0" not in alice_client.get(f"/hosts/{host.pk}/").content.decode()
    Prefs.objects.create(owner=alice, check_updates_online=True)
    assert "Helm command is v3.0.0" in alice_client.get(f"/hosts/{host.pk}/").content.decode()


# --- the settings form ---------------------------------------------------------------------------------------------


def test_settings_page_has_the_update_check_toggle_off_by_default(alice_client: Client):
    html = alice_client.get("/settings/").content.decode()
    assert 'name="check_updates_online"' in html
    assert "checked" not in re.search(r'<input[^>]*name="check_updates_online"[^>]*>', html)[0]


def test_toggle_can_be_turned_on_and_off(alice, alice_client: Client):
    base = {"silent_after_seconds": "90", "discord_webhook_url": ""}
    alice_client.post("/settings/", {**base, "check_updates_online": "on"})
    assert Prefs.objects.get(owner=alice).check_updates_online is True
    alice_client.post("/settings/", base)
    assert Prefs.objects.get(owner=alice).check_updates_online is False
