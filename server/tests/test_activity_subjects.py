"""Activity entries: the sentence (pinned), and the subject fields the page groups by."""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from activity.models import StatusEvent
from clusters.models import Cluster
from hosts.models import Host
from network.models import Network

from .conftest import CLUSTER_UID

pytestmark = pytest.mark.django_db

SUMMARY = "2 security updates"
XSS = "<script>alert(1)</script>"


@pytest.fixture
def pi(alice):
    return Host.objects.create(owner=alice, name="pi-01")


@pytest.fixture
def lab(alice):
    return Cluster.objects.create(owner=alice, uid=CLUSTER_UID, name="lab")


@pytest.fixture
def network(alice):
    return Network.objects.create(owner=alice)


def event(subject, old, new, summary="", **fields):
    kind = {Host: "host", Cluster: "cluster", Network: "network"}[type(subject)]
    return StatusEvent.objects.create(
        **{kind: subject}, old_status=old, new_status=new, summary=summary, **fields
    )


# --- Step A: describe() output, pinned before the refactor --------------------------------------


@pytest.mark.parametrize("kind", ["host", "cluster"])
@pytest.mark.parametrize(
    ("old", "new", "summary", "expected"),
    [
        ("green", "red", SUMMARY, "{n} went red: 2 security updates"),
        ("green", "red", "", "{n} went red"),
        ("green", "offline", "", "{n} went offline"),
        ("green", "offline", "no check-in", "{n} went offline: no check-in"),
        # offline as the new status wins over every old-status branch
        ("waiting", "offline", "", "{n} went offline"),
        ("offline", "offline", "", "{n} went offline"),
        ("offline", "green", "", "{n} is back online (green)"),
        ("offline", "yellow", "1 update", "{n} is back online (yellow): 1 update"),
        ("waiting", "green", "", "{n} sent its first report (green)"),
        ("waiting", "red", "disk full", "{n} sent its first report (red): disk full"),
    ],
)
def test_describe_host_and_cluster(pi, lab, kind, old, new, summary, expected):
    subject = pi if kind == "host" else lab
    assert event(subject, old, new, summary).describe() == expected.format(n=subject.name)


@pytest.mark.parametrize(
    ("old", "new", "summary", "expected"),
    [
        ("green", "red", "Internet down", "Network: Internet down"),
        ("red", "red", "Router rebooted", "Network: Router rebooted"),
        ("red", "green", "", "Network OK again"),
        ("green", "green", "", "Network OK again"),
        ("green", "red", "", "Network went red"),
        ("green", "yellow", "", "Network went yellow"),
        ("green", "offline", "", "Network went offline"),
    ],
)
def test_describe_network(network, old, new, summary, expected):
    assert event(network, old, new, summary).describe() == expected


# --- Step B: the new subject fields -------------------------------------------------------------

FIELDS = {
    "text",
    "time_human",
    "status",
    "day",
    "subject",
    "subject_kind",
    "subject_key",
    "text_short",
}


def both_views(client):
    """The /activity/ context items and the JSON items, which must agree."""
    page = client.get("/activity/").context["items"]
    api = client.get("/api/v1/activity.json").json()["items"]
    return page, api


def test_host_entry_fields(alice_client, pi):
    event(pi, "green", "red", SUMMARY)
    for items in both_views(alice_client):
        assert len(items) == 1
        item = items[0]
        assert set(item) == FIELDS
        assert item["subject"] == "pi-01"
        assert item["subject_kind"] == "host"
        assert item["subject_key"] == f"host-{pi.pk}"
        assert item["text_short"] == "went red: 2 security updates"
        assert item["text"] == "pi-01 went red: 2 security updates"


def test_cluster_entry_fields(alice_client, lab):
    event(lab, "waiting", "green")
    for items in both_views(alice_client):
        item = items[0]
        assert item["subject"] == "lab"
        assert item["subject_kind"] == "cluster"
        assert item["subject_key"] == f"cluster-{lab.pk}"
        assert item["text_short"] == "sent its first report (green)"


def test_network_entry_fields(alice_client, network):
    event(network, "green", "red", "Internet down")
    for items in both_views(alice_client):
        item = items[0]
        assert item["subject"] == "Network"
        assert item["subject_kind"] == "network"
        assert item["subject_key"] == f"network-{network.pk}"
        assert item["text_short"] == "Internet down"
        assert item["text"] == "Network: Internet down"


def test_renaming_a_host_keeps_the_subject_key(alice_client, pi):
    event(pi, "green", "red")
    Host.objects.filter(pk=pi.pk).update(name="pi-renamed")
    event(pi, "red", "green")
    for items in both_views(alice_client):
        assert {i["subject"] for i in items} == {"pi-renamed"}
        assert len({i["subject_key"] for i in items}) == 1


def test_host_and_cluster_with_the_same_name_have_different_keys(alice, alice_client):
    same = Host.objects.create(owner=alice, name="lab")
    cluster = Cluster.objects.create(owner=alice, uid=CLUSTER_UID, name="lab")
    event(same, "green", "red")
    event(cluster, "green", "red")
    for items in both_views(alice_client):
        assert len({i["subject_key"] for i in items}) == 2


@pytest.mark.parametrize("victim", ["host", "cluster", "network"])
def test_deleting_a_subject_removes_only_its_group(alice_client, pi, lab, network, victim):
    event(pi, "green", "red")
    event(lab, "green", "red")
    event(network, "green", "red", "Internet down")
    before = {i["subject_key"] for i in alice_client.get("/api/v1/activity.json").json()["items"]}
    gone = {"host": pi, "cluster": lab, "network": network}[victim]
    gone_key = f"{victim}-{gone.pk}"
    assert gone_key in before
    gone.delete()
    for items in both_views(alice_client):
        keys = {i["subject_key"] for i in items}
        assert keys == before - {gone_key}


# Every event type, with the joiner and the short form written out by hand (never computed).
ALL_EVENTS = [
    ("host", "green", "red", SUMMARY, " ", "went red: 2 security updates"),
    ("host", "green", "red", "", " ", "went red"),
    ("host", "green", "offline", "", " ", "went offline"),
    ("host", "green", "offline", "no check-in", " ", "went offline: no check-in"),
    ("host", "offline", "green", "", " ", "is back online (green)"),
    ("host", "offline", "yellow", "1 update", " ", "is back online (yellow): 1 update"),
    ("host", "waiting", "green", "", " ", "sent its first report (green)"),
    ("host", "waiting", "red", "disk full", " ", "sent its first report (red): disk full"),
    ("cluster", "green", "red", "Node a not ready", " ", "went red: Node a not ready"),
    ("cluster", "green", "offline", "", " ", "went offline"),
    ("cluster", "offline", "green", "", " ", "is back online (green)"),
    ("cluster", "waiting", "green", "", " ", "sent its first report (green)"),
    ("network", "green", "red", "Internet down", ": ", "Internet down"),
    ("network", "red", "red", "Router rebooted", ": ", "Router rebooted"),
    ("network", "red", "green", "", " ", "OK again"),
    ("network", "green", "red", "", " ", "went red"),
]
NAMES = {"host": "pi-01", "cluster": "lab", "network": "Network"}


@pytest.mark.parametrize(("kind", "old", "new", "summary", "joiner", "short"), ALL_EVENTS)
def test_describe_short_and_the_sentence_it_is_part_of(
    pi, lab, network, kind, old, new, summary, joiner, short
):
    made = event({"host": pi, "cluster": lab, "network": network}[kind], old, new, summary)
    assert made.describe_short() == short
    assert made.describe() == NAMES[kind] + joiner + short


@pytest.mark.parametrize(("kind", "old", "new", "summary", "joiner", "short"), ALL_EVENTS)
def test_text_is_subject_plus_joiner_plus_short(
    alice_client, pi, lab, network, kind, old, new, summary, joiner, short
):
    event({"host": pi, "cluster": lab, "network": network}[kind], old, new, summary)
    for items in both_views(alice_client):
        item = items[0]
        assert item["subject"] == NAMES[kind]
        assert item["text_short"] == short
        assert item["text"] == item["subject"] + joiner + item["text_short"]
        assert not item["text_short"].startswith(item["subject"])


def test_overview_entries_keep_their_old_shape(alice_client, pi, lab, network):
    now = timezone.now()
    event(pi, "green", "red", SUMMARY, created_at=now - timedelta(minutes=3))
    event(lab, "waiting", "green", created_at=now - timedelta(minutes=2))
    event(network, "green", "red", "Internet down", created_at=now - timedelta(minutes=1))
    context_items = alice_client.get("/").context["activity"]
    json_items = alice_client.get("/api/v1/overview.json").json()["activity"]
    for items in (context_items, json_items):
        assert all(set(item) == {"text", "time_human", "status"} for item in items)
        assert [(item["text"], item["status"]) for item in items] == [
            ("Network: Internet down", "red"),
            ("lab sent its first report (green)", "green"),
            ("pi-01 went red: 2 security updates", "red"),
        ]


def test_more_events_do_not_cost_more_queries(
    alice_client, pi, lab, network, django_assert_max_num_queries
):
    def queries():
        with django_assert_max_num_queries(50) as captured:
            alice_client.get("/api/v1/activity.json")
        return len(captured)

    for subject in (pi, lab, network):
        event(subject, "green", "red", "x")
    few = queries()
    for _ in range(5):
        for subject in (pi, lab, network):
            event(subject, "red", "green")
    assert queries() == few


def test_json_pages_of_25_newest_first(alice_client, pi):
    now = timezone.now()
    for i in range(30):
        event(pi, "green", "red", str(i), created_at=now - timedelta(minutes=30 - i))
    first = alice_client.get("/api/v1/activity.json").json()
    second = alice_client.get("/api/v1/activity.json?page=2").json()
    assert set(first) == {"items", "next_page"}
    assert len(first["items"]) == 25
    assert first["next_page"] == 2
    assert len(second["items"]) == 5
    assert second["next_page"] is None
    assert first["items"][0]["text_short"] == "went red: 29"
    assert second["items"][-1]["text_short"] == "went red: 0"
    assert all(i["subject_key"] == f"host-{pi.pk}" for i in first["items"] + second["items"])


@pytest.mark.parametrize("url", ["/activity/", "/api/v1/activity.json"])
def test_anonymous_is_sent_to_login(client, alice, url):
    response = client.get(url)
    assert response.status_code == 302
    assert response["Location"].startswith("/login/")


def test_hostile_host_name_is_escaped_in_html_and_raw_in_json(alice_client, pi):
    Host.objects.filter(pk=pi.pk).update(name=XSS)
    event(pi, "green", "red")
    html = alice_client.get("/activity/").content.decode()
    assert XSS not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    response = alice_client.get("/api/v1/activity.json")
    assert response["Content-Type"] == "application/json"
    item = json.loads(response.content)["items"][0]
    assert item["subject"] == XSS
    assert item["text"] == f"{XSS} went red"
    assert item["text_short"] == "went red"


def test_other_users_events_never_appear(alice_client, pi, bob):
    event(pi, "green", "red")
    bobs_host = Host.objects.create(owner=bob, name="bobs-box")
    bobs_cluster = Cluster.objects.create(
        owner=bob, uid="aaaaaaaa-0000-5000-8000-000000000001", name="bobs-lab"
    )
    bobs_net = Network.objects.create(owner=bob)
    for subject in (bobs_host, bobs_cluster, bobs_net):
        event(subject, "green", "red", "x")
    for items in both_views(alice_client):
        assert [i["subject_key"] for i in items] == [f"host-{pi.pk}"]
