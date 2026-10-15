"""GET /api/v1/hosts/<id>/summary.json: the data behind the overview's host preview."""

import pytest
from django.test import Client
from django.utils import timezone

from hosts.models import AgentToken
from hosts.presenters import detail_context

from .sample import make_report
from .test_host_views import make_host, make_silent_host

pytestmark = pytest.mark.django_db

KEYS = {
    "id",
    "name",
    "status",
    "status_label",
    "last_seen_human",
    "report_received_at",
    "url",
    "issues",
    "updates",
    "containers",
    "failed_units",
    "disks",
}


def url(host) -> str:
    return f"/api/v1/hosts/{host.pk}/summary.json"


def busy_report():
    return make_report(
        updates={"total": 12, "security": 3},
        reboot_required=True,
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
                "health": None,
                "exit_code": None,
            },
        ],
        failed_units=["backup.service"],
        disks=[{"mount": "/", "percent": 96}, {"mount": "/srv", "percent": 40}],
    )


def test_owner_gets_the_summary(alice_client: Client, alice):
    host = make_host(alice, "homeserver", "red", busy_report())

    response = alice_client.get(url(host))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    data = response.json()
    assert set(data) == KEYS
    assert data["id"] == host.pk
    assert data["name"] == "homeserver"
    assert data["status"] == "red"
    assert data["status_label"] == "Needs you"
    assert data["last_seen_human"] == "just now"
    assert data["report_received_at"] == host.report_received_at.isoformat()
    assert data["url"] == f"/hosts/{host.pk}/"
    assert data["updates"] == {"total": 12, "security": 3, "reboot_required": True}
    assert data["containers"] == {
        "running": 1,
        "problems": [{"name": "nginx", "state": "restarting"}],
    }
    assert data["failed_units"] == ["backup.service"]
    assert data["disks"] == [
        {"mount": "/", "percent": 96, "level": "red"},
        {"mount": "/srv", "percent": 40, "level": "green"},
    ]
    # Every issue is {text, severity} only: no why/fix/fingerprint in the preview.
    assert all(set(i) == {"text", "severity"} for i in data["issues"])
    texts = {i["text"] for i in data["issues"]}
    assert {"3 security updates", "backup.service failed", "Reboot required"} <= texts


def test_issues_match_the_detail_page(alice_client: Client, alice):
    host = make_host(alice, "homeserver", "red", busy_report())

    data = alice_client.get(url(host)).json()
    detail = detail_context(host, timezone.now())

    # Same issues, same order, same filtering.
    assert data["issues"] == [
        {"text": i["text"], "severity": i["severity"]} for i in detail["issues"]
    ]
    assert data["status"] == detail["host"]["status"]
    assert data["disks"] == detail["disks"]


def test_another_users_host_is_404(client: Client, alice, bob):
    host = make_host(alice, "homeserver", "red", busy_report())
    client.force_login(bob)

    response = client.get(url(host))

    assert response.status_code == 404
    assert b"homeserver" not in response.content


def test_missing_host_is_404(alice_client: Client):
    assert alice_client.get("/api/v1/hosts/999999/summary.json").status_code == 404


def test_anonymous_is_sent_to_login(client: Client, alice):
    host = make_host(alice, "homeserver", "red", busy_report())

    response = client.get(url(host))

    assert response.status_code == 302
    assert response["Location"].startswith("/login/")
    assert b"homeserver" not in response.content


def test_only_get(alice_client: Client, alice):
    host = make_host(alice, "homeserver", "red", busy_report())
    assert alice_client.post(url(host)).status_code == 405


def test_offline_host_says_offline_without_a_last_seen_issue(alice_client: Client, alice):
    host = make_silent_host(alice, status="yellow")
    host.latest_report = make_report(reboot_required=True)
    host.save(update_fields=["latest_report"])

    data = alice_client.get(url(host)).json()

    assert data["status"] == "offline"
    assert data["status_label"] == "Offline"
    assert data["last_seen_human"] == "28 h ago"
    # The offline finding is never an issue line; the other issues stay.
    assert data["issues"] == [{"text": "Reboot required", "severity": "yellow"}]
    assert "Last seen" not in str(data)


def test_waiting_host_has_null_sections(alice_client: Client, host):
    data = alice_client.get(url(host)).json()

    assert data["status"] == "waiting"
    assert data["status_label"] == "Waiting"
    assert data["last_seen_human"] == "never"
    assert data["report_received_at"] is None
    assert data["issues"] == []
    for key in ("updates", "containers", "failed_units", "disks"):
        assert data[key] is None, key


def test_sections_the_host_did_not_report_stay_null(alice_client: Client, alice):
    report = make_report(updates=None, containers=None, failed_units=None, disks=None)
    host = make_host(alice, "bare", "green", report)

    data = alice_client.get(url(host)).json()

    assert data["status"] == "green"
    for key in ("updates", "containers", "failed_units", "disks"):
        assert data[key] is None, key


def test_empty_sections_stay_empty_not_null(alice_client: Client, alice):
    host = make_host(alice, "quiet", "green", make_report(containers=[], failed_units=[]))

    data = alice_client.get(url(host)).json()

    assert data["containers"] == {"running": 0, "problems": []}
    assert data["failed_units"] == []


def test_no_secrets_or_raw_status_strings(alice_client: Client, alice):
    host = make_host(alice, "homeserver", "red", busy_report())
    token = AgentToken.issue(host)

    body = alice_client.get(url(host)).content.decode()

    assert token not in body
    assert "token" not in body
    assert "Restarting (1) 5s ago" not in body  # container status text stays on the host page
    assert '"why"' not in body and '"fix"' not in body


def test_hostile_names_come_back_as_plain_json_strings(alice_client: Client, alice):
    xss = "<script>alert(1)</script>"
    report = make_report(
        containers=[{"name": xss, "state": "exited", "status": "", "health": None, "exit_code": 1}],
        failed_units=[xss],
        disks=[{"mount": xss, "percent": 99}],
    )
    host = make_host(alice, "homeserver", "red", report)

    response = alice_client.get(url(host))

    # JSON, never HTML: the browser can't sniff it into a page (nosniff), and the
    # preview puts these strings in with textContent.
    assert response["Content-Type"] == "application/json"
    assert response["X-Content-Type-Options"] == "nosniff"
    data = response.json()
    assert data["containers"]["problems"][0]["name"] == xss
    assert data["failed_units"] == [xss]
    assert data["disks"][0]["mount"] == xss
