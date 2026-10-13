import html as html_lib
import logging
import re
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client
from django.utils import timezone

from activity.models import StatusEvent
from hosts.models import AgentToken, Host, hash_token

from .sample import make_report

pytestmark = pytest.mark.django_db

TOKEN_RE = re.compile(r'token = "([A-Za-z0-9_-]{40,})"')


def token_from(response) -> str:
    match = TOKEN_RE.search(html_lib.unescape(response.content.decode()))
    assert match, "the response should show the token"
    return match.group(1)


def all_database_text() -> str:
    """Every value in every table, as one string (to prove a secret isn't stored)."""
    chunks = []
    with connection.cursor() as cursor:
        for table in connection.introspection.table_names(cursor):
            cursor.execute(f'SELECT * FROM "{table}"')  # table names come from Django
            chunks.extend(str(row) for row in cursor.fetchall())
    return "\n".join(chunks)


# --- add host / token shown once -------------------------------------------------


def test_add_host_shows_the_token_once(alice_client: Client, alice, caplog):
    caplog.set_level(logging.DEBUG)
    response = alice_client.post("/hosts/add/", {"name": "pi-01"})

    assert response.status_code == 200
    host = Host.objects.get(owner=alice, name="pi-01")
    token = token_from(response)
    html = html_lib.unescape(response.content.decode())
    assert 'server_url = "wss://testserver/ws/agent/"' in html
    assert "chmod 600 /etc/cronrack/agent.toml" in html
    assert "no-store" in response["Cache-Control"]

    # Only the hash is stored, and it matches the token shown.
    assert AgentToken.objects.get(host=host).token_hash == hash_token(token)
    assert AgentToken.verify(token) == host
    assert token not in all_database_text()
    assert token not in caplog.text
    assert token not in response.get("Location", "")

    # Never shown again: not on the host pages.
    for url in (f"/hosts/{host.pk}/", f"/hosts/{host.pk}/settings/", "/"):
        assert token not in alice_client.get(url).content.decode()


@pytest.mark.parametrize("name", ["", "has space", "a" * 65, "semi;colon", "../etc"])
def test_add_host_validates_the_name(alice_client: Client, name):
    response = alice_client.post("/hosts/add/", {"name": name})
    assert response.status_code == 200
    assert not Host.objects.exists()
    assert not TOKEN_RE.search(response.content.decode())


def test_host_names_are_unique_per_owner(alice_client: Client, host, bob):
    alice_client.post("/hosts/add/", {"name": "nas"})
    assert Host.objects.filter(name="nas").count() == 1
    # Bob may use the same name for his own host.
    bob_client = Client()
    bob_client.force_login(bob)
    bob_client.post("/hosts/add/", {"name": "nas"})
    assert Host.objects.filter(name="nas").count() == 2


# --- settings: rename, rotate, revoke, delete ------------------------------------


def test_rename(alice_client: Client, host):
    response = alice_client.post(f"/hosts/{host.pk}/settings/", {"name": "nas-2"})
    assert response.status_code == 302
    host.refresh_from_db()
    assert host.name == "nas-2"


def test_rotate_shows_a_new_token_and_kills_the_old_one(
    alice_client: Client, host, django_capture_on_commit_callbacks
):
    old = AgentToken.issue(host)
    with (
        mock.patch("hosts.services.close_agent") as close,
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = alice_client.post(f"/hosts/{host.pk}/rotate/")
    new = token_from(response)

    assert new != old
    assert AgentToken.verify(old) is None
    assert AgentToken.verify(new) == host
    assert new not in all_database_text()
    assert "no-store" in response["Cache-Control"]
    close.assert_called_once()


def test_rotate_requires_post(alice_client: Client, host):
    assert alice_client.get(f"/hosts/{host.pk}/rotate/").status_code == 405


def test_revoke(alice_client: Client, host):
    token = AgentToken.issue(host)
    with mock.patch("hosts.views.close_agent") as close:
        response = alice_client.post(f"/hosts/{host.pk}/revoke/")

    assert response.status_code == 302
    assert AgentToken.verify(token) is None
    close.assert_called_once_with(host)


def test_delete_requires_the_typed_name(alice_client: Client, host):
    response = alice_client.post(f"/hosts/{host.pk}/delete/", {"confirm_name": "NAS"})
    assert response.status_code == 400
    assert Host.objects.filter(pk=host.pk).exists()

    with mock.patch("hosts.views.close_agent") as close:
        response = alice_client.post(f"/hosts/{host.pk}/delete/", {"confirm_name": "nas"})
    assert response.status_code == 302
    assert response["Location"] == "/"
    assert not Host.objects.filter(pk=host.pk).exists()
    close.assert_called_once()


# --- collect ---------------------------------------------------------------------


def test_collect_returns_202(alice_client: Client, host):
    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post(f"/hosts/{host.pk}/collect/")
    assert response.status_code == 202
    assert response.json() == {"ok": True}
    collect.assert_called_once_with(host)


def test_collect_all_only_asks_my_connected_hosts(alice_client: Client, alice, bob):
    mine = Host.objects.create(owner=alice, name="on", connected=True)
    Host.objects.create(owner=alice, name="off", connected=False)
    Host.objects.create(owner=bob, name="bobs", connected=True)

    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post("/hosts/collect-all/")

    assert response.status_code == 202
    assert [c.args[0] for c in collect.call_args_list] == [mine]


# --- overview, detail and the JSON shapes (handoff 6.2 / 6.3) ---------------------

HOST_KEYS = {"id", "name", "status", "status_label", "issues", "last_seen_human", "url"}


def make_host(owner, name, status, report=None, **fields):
    now = timezone.now()
    return Host.objects.create(
        owner=owner,
        name=name,
        status=status,
        latest_report=report,
        report_received_at=now if report else None,
        last_seen_at=now if report else None,
        connected=report is not None,
        **fields,
    )


def test_overview_json_shape_and_order(alice_client: Client, alice, bob):
    make_host(alice, "zz-green", "green", make_report())
    red = make_host(alice, "b-red", "red", make_report(failed_units=["x.service"]))
    make_host(alice, "a-yellow", "yellow", make_report(reboot_required=True))
    make_host(alice, "a-red", "red", make_report(failed_units=["y.service"]))
    make_host(bob, "bobs-host", "red", make_report())
    StatusEvent.objects.create(host=red, old_status="green", new_status="red", summary="x")

    data = alice_client.get("/api/v1/overview.json").json()

    assert set(data) == {"attention_count", "updated_human", "hosts", "activity"}
    assert data["attention_count"] == 3
    assert [h["name"] for h in data["hosts"]] == ["a-red", "b-red", "a-yellow", "zz-green"]
    for h in data["hosts"]:
        assert set(h) == HOST_KEYS
        assert h["url"] == f"/hosts/{h['id']}/"
        for issue in h["issues"]:
            assert set(issue) == {"text", "severity"}
    assert data["hosts"][1]["issues"] == [{"text": "x.service failed", "severity": "red"}]
    assert data["hosts"][1]["status_label"] == "Needs you"
    assert data["activity"] == [
        {"text": "b-red went red: x", "time_human": mock.ANY, "status": "red"}
    ]


def test_overview_page_and_json_use_the_same_data(alice_client: Client, alice):
    make_host(alice, "pi-01", "yellow", make_report(reboot_required=True))
    html = alice_client.get("/").content.decode()
    assert "1 thing needs you" in html
    assert "Reboot required" in html


def test_overview_empty_state(alice_client: Client):
    html = alice_client.get("/").content.decode()
    assert "No hosts yet" in html
    assert "Add your first host" in html
    assert alice_client.get("/api/v1/overview.json").json()["hosts"] == []


def test_offline_host_shows_last_seen(alice_client: Client, alice):
    host = make_host(alice, "pi-01", "offline", make_report())
    Host.objects.filter(pk=host.pk).update(
        connected=False, last_seen_at=timezone.now() - timedelta(minutes=14)
    )
    card = alice_client.get("/api/v1/overview.json").json()["hosts"][0]
    assert card["status_label"] == "Offline"
    assert card["issues"][0] == {"text": "Last seen 14 min ago", "severity": "red"}


def test_status_json_shape(alice_client: Client, host):
    data = alice_client.get(f"/api/v1/hosts/{host.pk}/status.json").json()
    assert data == {"status": "waiting", "last_seen_human": "never"}


def test_detail_page_sections(alice_client: Client, alice):
    report = make_report(
        updates={"total": 12, "security": 3},
        reboot_required=True,
        disks=[{"mount": "/", "percent": 87}, {"mount": "/srv", "percent": 10}],
        containers=[
            {
                "name": "nginx",
                "state": "restarting",
                "status": "Restarting (1) 5s ago",
                "health": None,
                "exit_code": None,
            },
            {
                "name": "postgres",
                "state": "running",
                "status": "Up 3 days",
                "health": "healthy",
                "exit_code": None,
            },
        ],
    )
    host = make_host(alice, "homeserver", "red", report)

    response = alice_client.get(f"/hosts/{host.pk}/")
    ctx = response.context

    assert set(ctx["host"]) == {
        "id", "name", "status", "status_label", "last_seen_human", "has_report",
    }  # fmt: skip
    assert ctx["updates"] == {"total": 12, "security": 3, "reboot_required": True}
    assert ctx["containers"]["problems"] == [
        {"name": "nginx", "state": "restarting", "status": "Restarting (1) 5s ago"}
    ]
    assert ctx["containers"]["healthy"] == [{"name": "postgres", "status": "Up 3 days"}]
    assert ctx["disks"] == [
        {"mount": "/", "percent": 87, "level": "yellow"},
        {"mount": "/srv", "percent": 10, "level": "green"},
    ]
    assert ctx["timers"][0]["name"] == "apt-daily.timer"
    assert ctx["issues"][0]["severity"] == "red"
    assert {"text", "severity", "why", "fix", "fingerprint"} <= set(ctx["issues"][0])


def test_detail_sections_can_be_unavailable(alice_client: Client, alice):
    report = make_report(updates=None, containers=None, failed_units=None, timers=None, disks=None)
    host = make_host(alice, "tiny", "green", report)
    response = alice_client.get(f"/hosts/{host.pk}/")
    for key in ("updates", "containers", "failed_units", "timers", "disks"):
        assert response.context[key] is None
    assert "Not available on this host" in response.content.decode()


def test_detail_waiting_state(alice_client: Client, host):
    response = alice_client.get(f"/hosts/{host.pk}/")
    assert response.context["host"]["has_report"] is False
    assert "Waiting for nas to check in" in response.content.decode()


# --- activity ----------------------------------------------------------------------


def test_activity_json_pages_of_25(alice_client: Client, host, bob):
    for i in range(30):
        StatusEvent.objects.create(host=host, old_status="green", new_status="red", summary=str(i))
    bobs = Host.objects.create(owner=bob, name="bobs")
    StatusEvent.objects.create(host=bobs, old_status="green", new_status="red")

    first = alice_client.get("/api/v1/activity.json").json()
    second = alice_client.get("/api/v1/activity.json?page=2").json()
    beyond = alice_client.get("/api/v1/activity.json?page=99").json()
    junk = alice_client.get("/api/v1/activity.json?page=abc").json()

    assert set(first) == {"items", "next_page"}
    assert len(first["items"]) == 25
    assert first["next_page"] == 2
    assert len(second["items"]) == 5
    assert second["next_page"] is None
    assert beyond == {"items": [], "next_page": None}
    assert junk == first
    assert set(first["items"][0]) == {"text", "time_human", "status", "day"}
    assert first["items"][0]["day"] == "Today"
    assert all("bobs" not in item["text"] for item in first["items"] + second["items"])


def test_activity_page_empty(alice_client: Client):
    assert "Nothing happened yet" in alice_client.get("/activity/").content.decode()


# --- error pages -------------------------------------------------------------------


def test_404_page(alice_client: Client):
    response = alice_client.get("/hosts/424242/")
    assert response.status_code == 404
    assert "Back to Overview" in response.content.decode()


def test_500_page_is_fully_static():
    path = Path(__file__).resolve().parent.parent / "templates" / "500.html"
    body = path.read_text().split("\n", 2)[2]  # skip the placeholder header comments
    assert "{{" not in body
    assert "{%" not in body
    assert "Back to Overview" in render_to_string("500.html")
