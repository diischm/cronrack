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

from .icons import shown, status_icon
from .rotate import post_rotate
from .sample import make_report

pytestmark = pytest.mark.django_db

# The token's code well (components/agent_setup.html): the only place it appears.
TOKEN_RE = re.compile(r'<pre [^>]*id="agent-token"[^>]*>([A-Za-z0-9_-]{40,})</pre>')


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
    # The one-line install command for this server (the token is NOT part of it) ...
    assert (
        "curl -fsSL http://testserver/agent/install.sh | sudo bash -s -- "
        "--server http://testserver" in html
    )
    # ... the token to paste at the prompt, and the manual agent.toml fallback (which
    # leaves the token out: it is shown exactly once).
    assert html.count(token) == 1
    assert 'server_url = "http://testserver"\ntoken = ""' in html
    assert "sudo install -m 640 -o root -g cronrack /dev/null /etc/cronrack/agent.toml" in html
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
        response = post_rotate(alice_client, host.pk)
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
    Host.objects.filter(pk=host.pk).update(connected=True)
    host.refresh_from_db()
    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post(f"/hosts/{host.pk}/collect/", HTTP_ACCEPT="application/json")
    assert response.status_code == 202
    assert response.json() == {"ok": True}
    collect.assert_called_once_with(host)


def test_collect_all_only_asks_my_connected_hosts(alice_client: Client, alice, bob):
    mine = Host.objects.create(owner=alice, name="on", connected=True)
    Host.objects.create(owner=alice, name="off", connected=False)
    Host.objects.create(owner=bob, name="bobs", connected=True)

    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post("/hosts/collect-all/", HTTP_ACCEPT="application/json")

    assert response.status_code == 202
    assert response.json() == {"ok": True, "asked": 1, "offline": 1, "just_reported": 0}
    assert [c.args[0] for c in collect.call_args_list] == [mine]


# --- overview, detail and the JSON shapes (handoff 6.2 / 6.3) ---------------------

HOST_KEYS = {
    "id", "name", "status", "status_label", "issues", "last_seen_human", "url",
    # Phase 6b: the desktop card's detail strip.
    "updates", "containers", "disk_max_percent",
}  # fmt: skip
OVERVIEW_KEYS = {
    "attention_count", "updated_human", "hosts", "activity",
    # Phase 6b: the desktop summary tiles.
    "status_counts", "hosts_online",
}  # fmt: skip


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

    assert set(data) == OVERVIEW_KEYS
    assert data["attention_count"] == 3
    assert data["status_counts"] == {"red": 2, "yellow": 1, "green": 1, "offline": 0}
    assert data["hosts_online"] == 4  # bob's host is not counted
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
    """Marked offline by the scheduler: the time is in last_seen_human, not an issue line."""
    host = make_host(alice, "pi-01", "offline", make_report())
    Host.objects.filter(pk=host.pk).update(
        connected=False, last_seen_at=timezone.now() - timedelta(minutes=14)
    )
    card = alice_client.get("/api/v1/overview.json").json()["hosts"][0]
    assert card["status_label"] == "Offline"
    assert card["last_seen_human"] == "14 min ago"
    assert card["issues"] == []


def test_overview_card_numbers(alice_client: Client, alice):
    """The desktop card strip: updates, container counts and the fullest disk."""
    containers = [
        {"name": "nginx", "state": "restarting", "status": "Restarting (1) 5s ago",
         "health": None, "exit_code": None},
        {"name": "db", "state": "running", "status": "Up 3 days", "health": "healthy",
         "exit_code": None},
        {"name": "web", "state": "running", "status": "Up 3 days", "health": None,
         "exit_code": None},
    ]  # fmt: skip
    report = make_report(
        updates={"total": 12, "security": 3},
        containers=containers,
        disks=[{"mount": "/", "percent": 62}, {"mount": "/srv", "percent": 91}],
    )
    make_host(alice, "homeserver", "red", report)

    card = alice_client.get("/api/v1/overview.json").json()["hosts"][0]

    assert card["updates"] == {"total": 12, "security": 3}
    assert card["containers"] == {"total": 3, "running": 2, "failing": 1}
    assert card["disk_max_percent"] == 91


def test_overview_card_numbers_can_be_missing(alice_client: Client, alice):
    make_host(alice, "tiny", "green", make_report(updates=None, containers=None, disks=None))
    make_host(alice, "waiting", "waiting")  # no report yet

    for card in alice_client.get("/api/v1/overview.json").json()["hosts"]:
        assert card["updates"] is None
        assert card["containers"] is None
        assert card["disk_max_percent"] is None


def test_overview_summary_counts(alice_client: Client, alice):
    """Offline has its own count (unlit lamp); "online" means the agent is connected."""
    make_host(alice, "a", "red", make_report())
    make_host(alice, "b", "offline", make_report())
    Host.objects.filter(name="b").update(connected=False)
    make_host(alice, "c", "green", make_report())
    make_host(alice, "d", "waiting")

    data = alice_client.get("/api/v1/overview.json").json()

    assert data["status_counts"] == {"red": 1, "yellow": 0, "green": 1, "offline": 1}
    assert data["hosts_online"] == 2


# --- offline worked out at read time (the scheduler may not be running) ------------


def make_silent_host(owner, name="pi-01", hours=28, status="green"):
    """A host whose stored status is still `status`, but that went quiet `hours` ago."""
    host = make_host(owner, name, status, make_report())
    Host.objects.filter(pk=host.pk).update(
        connected=False, last_seen_at=timezone.now() - timedelta(hours=hours)
    )
    return Host.objects.get(pk=host.pk)


def test_silent_host_is_offline_everywhere(alice_client: Client, alice):
    host = make_silent_host(alice)
    assert host.status == "green"  # nothing rewrote the stored status

    card = alice_client.get("/api/v1/overview.json").json()["hosts"][0]
    status = alice_client.get(f"/api/v1/hosts/{host.pk}/status.json").json()
    detail = alice_client.get(f"/hosts/{host.pk}/").content.decode()
    overview = alice_client.get("/").content.decode()

    assert card["status"] == status["status"] == "offline"
    assert card["status_label"] == "Offline"
    assert card["issues"] == []  # "Offline · last report …" says it, no issue line repeats it
    for html in (overview, detail):
        assert "lamp--off" in html
        assert "<strong>Offline</strong> · last report 28 h ago" in html
        # No status icon (issue rows on the detail page keep their severity icons).
        assert not re.search(
            r'<span class="visually-hidden">(All good|Needs you|Heads up|Offline)</span>',
            shown(html),
        )
    assert Host.objects.get(pk=host.pk).status == "green"  # reading never writes


def test_offline_host_lists_no_offline_issue_but_keeps_the_others(alice_client: Client, alice):
    from hosts.presenters import detail_context, overview_context

    report = make_report(reboot_required=True, failed_units=["backup.service"])
    host = make_host(alice, "pi-01", "red", report)
    Host.objects.filter(pk=host.pk).update(
        connected=False, last_seen_at=timezone.now() - timedelta(hours=28)
    )
    host = Host.objects.get(pk=host.pk)
    now = timezone.now()
    others = {"backup.service failed", "Reboot required"}

    card = overview_context(alice, now)["hosts"][0]
    assert card["status"] == "offline"
    assert {i["text"] for i in card["issues"]} == others

    json_card = alice_client.get("/api/v1/overview.json").json()["hosts"][0]
    assert {i["text"] for i in json_card["issues"]} == others

    detail = detail_context(host, now)
    assert detail["host"]["status"] == "offline"
    assert {i["text"] for i in detail["issues"]} == others
    assert all(i["rule_id"] != "connectivity.offline" for i in detail["issues"])

    for html in (
        alice_client.get("/").content.decode(),
        alice_client.get(f"/hosts/{host.pk}/").content.decode(),
    ):
        assert "Last seen" not in html
        assert "backup.service failed" in html


def test_online_host_issues_are_unchanged(alice_client: Client, alice):
    from hosts.presenters import detail_context, overview_context

    report = make_report(reboot_required=True, failed_units=["backup.service"])
    host = make_host(alice, "pi-01", "red", report)
    now = timezone.now()
    expected = [
        {"text": "backup.service failed", "severity": "red"},
        {"text": "Reboot required", "severity": "yellow"},
    ]

    assert overview_context(alice, now)["hosts"][0]["issues"] == expected
    assert alice_client.get("/api/v1/overview.json").json()["hosts"][0]["issues"] == expected
    detail_issues = detail_context(host, now)["issues"]
    assert [{"text": i["text"], "severity": i["severity"]} for i in detail_issues] == expected


def test_silent_host_under_threshold_keeps_its_status(alice_client: Client, alice):
    host = make_host(alice, "pi-01", "yellow", make_report(reboot_required=True))
    Host.objects.filter(pk=host.pk).update(
        connected=False, last_seen_at=timezone.now() - timedelta(seconds=30)
    )

    assert alice_client.get("/api/v1/overview.json").json()["hosts"][0]["status"] == "yellow"
    assert alice_client.get(f"/api/v1/hosts/{host.pk}/status.json").json()["status"] == "yellow"


def test_silent_host_threshold_comes_from_prefs(alice_client: Client, alice):
    from prefs.models import Prefs

    Prefs.objects.create(owner=alice, silent_after_seconds=2 * 24 * 3600)
    host = make_silent_host(alice)

    assert alice_client.get("/api/v1/overview.json").json()["hosts"][0]["status"] == "green"
    assert alice_client.get(f"/api/v1/hosts/{host.pk}/status.json").json()["status"] == "green"


def test_attention_count_includes_offline_hosts(alice_client: Client, alice):
    make_silent_host(alice, "diicpi")
    make_host(alice, "nas", "green", make_report())

    data = alice_client.get("/api/v1/overview.json").json()
    html = alice_client.get("/").content.decode()

    assert data["attention_count"] == 1
    assert data["status_counts"] == {"red": 0, "yellow": 0, "green": 1, "offline": 1}
    assert data["hosts_online"] == 1
    assert [h["name"] for h in data["hosts"]] == ["diicpi", "nas"]  # offline sorts first
    assert "1 thing needs you" in html


def test_overview_html_and_json_agree_on_offline(alice_client: Client, alice):
    make_silent_host(alice, "diicpi")
    make_host(alice, "nas", "green", make_report())

    data = alice_client.get("/api/v1/overview.json").json()
    html = alice_client.get("/").content.decode()

    statuses = {h["name"]: h["status"] for h in data["hosts"]}
    assert statuses == {"diicpi": "offline", "nas": "green"}
    # diicpi's card only: the "Hosts online" tile is a summary and uses an icon, not a lamp.
    assert html.count("lamp--off") == 1
    assert "<strong>Offline</strong> · last report 28 h ago" in html
    assert "<strong>Offline</strong> · 1 host" in html
    assert len(re.findall(status_icon("good", "All good"), shown(html))) == 1  # nas keeps its icon


def test_waiting_and_maintenance_win_over_offline(alice_client: Client, alice):
    waiting = make_host(alice, "new", "waiting")
    Host.objects.filter(pk=waiting.pk).update(last_seen_at=timezone.now() - timedelta(days=2))
    mnt = make_silent_host(alice, "mnt", status="maintenance")
    Host.objects.filter(pk=mnt.pk).update(maintenance_until=timezone.now() + timedelta(hours=1))

    statuses = {
        h["name"]: h["status"] for h in alice_client.get("/api/v1/overview.json").json()["hosts"]
    }
    assert statuses == {"new": "waiting", "mnt": "maintenance"}
    for h in (waiting, mnt):
        status = alice_client.get(f"/api/v1/hosts/{h.pk}/status.json").json()["status"]
        assert status == statuses[h.name]


# --- both layouts (CSS switches at 900px; the HTML is the same) --------------------
# The server sends one page for phones and desktops, so "renders at both layouts"
# means: every piece both layouts need is in the HTML, and the template has no errors.


def test_overview_has_phone_and_desktop_pieces(alice_client: Client, alice):
    make_host(alice, "homeserver", "red", make_report(failed_units=["x.service"]))
    make_host(alice, "pi-01", "yellow", make_report(reboot_required=True))
    Host.objects.create(owner=alice, name="new-box")  # waiting: no report, no numbers
    host = Host.objects.get(name="homeserver")
    StatusEvent.objects.create(host=host, old_status="green", new_status="red", summary="x")

    response = alice_client.get("/")
    html = response.content.decode()

    assert response.status_code == 200
    # Shared by both layouts
    assert "2 things need you" in html
    assert 'class="bottom-nav seg-track well"' in html
    assert "While you were away" in html
    # The rack: one unit per host, and the <template> overview.js clones
    assert '<ul class="rack well" id="rack" aria-label="Hosts">' in html
    assert html.count('<a class="rack-unit card-press') == 3 + 1  # + the blank template copy
    assert '<template id="rack-unit-template">' in html
    # Desktop: header actions, tiles, the units' front-panel readouts
    assert "Check all now" in html
    assert 'action="/hosts/collect-all/"' in html
    assert "csrfmiddlewaretoken" in html
    assert 'aria-label="Summary"' in html
    assert "Hosts online" in html
    assert 'class="rack-unit__panel well desk-only"' in html
    assert "Waiting for the first report" in html


def test_disk_meter_colors(alice_client: Client, alice):
    for name, percent in (("calm", 84), ("amber", 85), ("red", 95)):
        make_host(alice, name, "green", make_report(disks=[{"mount": "/", "percent": percent}]))

    html = alice_client.get("/").content.decode()

    assert 'class="meter well" min="0" max="100" value="84"' in html
    assert 'class="meter well meter--amber" min="0" max="100" value="85"' in html
    assert 'class="meter well meter--red" min="0" max="100" value="95"' in html


def test_detail_page_renders_in_the_shell(alice_client: Client, alice):
    report = make_report(disks=[{"mount": "/", "percent": 96}])
    host = make_host(alice, "homeserver", "green", report)

    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()

    assert 'class="bottom-nav seg-track well"' in html
    assert 'class="host-rack rack-chassis surface"' in html
    assert '<div class="host-unit">' in html
    assert 'class="meter well meter--red"' in html


def test_shell_counts_only_my_hosts(alice_client: Client, alice, bob):
    """The shell's host counts (context processor) must be owner-scoped: Bob's hosts never count."""
    make_host(alice, "mine", "green", make_report())
    Host.objects.create(owner=alice, name="mine-offline")
    make_host(bob, "bobs-1", "green", make_report())
    make_host(bob, "bobs-2", "green", make_report())

    response = alice_client.get("/activity/")

    assert response.context["shell_hosts"]["connected"] == 1
    assert response.context["shell_hosts"]["total"] == 2


def test_layout_breakpoint_is_in_the_css():
    css = (Path(__file__).resolve().parent.parent / "static/cronrack/css/pages.css").read_text()
    assert "@media (min-width: 900px)" in css
    assert "@media (max-width: 899.98px)" in css
    assert "auto-fill" not in css  # the host rack is one column at every width
    assert "border:" not in css.replace("border: 0", "")


# --- check all now ----------------------------------------------------------------


def test_collect_all_requires_post(alice_client: Client, alice):
    Host.objects.create(owner=alice, name="on", connected=True)
    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.get("/hosts/collect-all/")
    assert response.status_code == 405
    collect.assert_not_called()


def test_collect_all_requires_csrf(alice):
    Host.objects.create(owner=alice, name="on", connected=True)
    client = Client(enforce_csrf_checks=True)
    client.force_login(alice)

    with mock.patch("hosts.views.request_collect") as collect:
        response = client.post("/hosts/collect-all/")

    assert response.status_code == 403
    collect.assert_not_called()


def test_collect_all_works_with_the_csrf_header(alice):
    """What actions.js sends: the form's token in an X-CSRFToken header."""
    Host.objects.create(owner=alice, name="on", connected=True)
    client = Client(enforce_csrf_checks=True)
    client.force_login(alice)
    page = client.get("/").content.decode()
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)

    with mock.patch("hosts.views.request_collect") as collect:
        response = client.post(
            "/hosts/collect-all/", HTTP_X_CSRFTOKEN=token, HTTP_ACCEPT="application/json"
        )

    assert response.status_code == 202
    assert collect.call_count == 1


def test_status_json_shape(alice_client: Client, host):
    data = alice_client.get(f"/api/v1/hosts/{host.pk}/status.json").json()
    assert data == {"status": "waiting", "last_seen_human": "never", "report_received_at": None}


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
        "agent_version",
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


def headings(html: str, level: int = 2) -> list[str]:
    """The visible text of every <hN> on the page, in order."""
    found = re.findall(rf"<h{level}\b[^>]*>(.*?)</h{level}>", html, flags=re.DOTALL)
    return [visible_text(h) for h in found]


def visible_text(fragment: str) -> str:
    """Markup removed, entities decoded, whitespace collapsed."""
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def item_text(html: str, needle: str) -> str:
    """The visible text of the list item that contains `needle`."""
    for item in re.findall(r"<li\b.*?</li>", html, flags=re.DOTALL):
        text = visible_text(item)
        if needle in text:
            return text
    raise AssertionError(f"no list item contains {needle!r}")


APP_PANELS = ("System", "Docker", "Systemd", "Disks")


def test_detail_hides_panels_without_data(alice_client: Client, alice):
    report = make_report(updates=None, containers=None, failed_units=None, timers=None, disks=None)
    host = make_host(alice, "tiny", "green", report)
    response = alice_client.get(f"/hosts/{host.pk}/")
    for key in ("updates", "containers", "failed_units", "timers", "disks"):
        assert response.context[key] is None
    html = response.content.decode()
    assert not set(APP_PANELS) & set(headings(html))
    assert "Not available" not in html

    # Panels WITH data are still rendered; only the missing app (Docker) is gone.
    host = make_host(alice, "no-docker", "green", make_report(containers=None))
    shown = headings(alice_client.get(f"/hosts/{host.pk}/").content.decode())
    assert [h for h in shown if h in APP_PANELS] == ["System", "Systemd", "Disks"]


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


# --- navigation ---------------------------------------------------------------------


def test_every_nav_link_works(alice_client: Client):
    """Every link in base.html's navigation must open a page, so a dead tab can't ship."""
    html = alice_client.get("/").content.decode()
    navs = re.findall(r"<nav\b.*?</nav>", html, flags=re.DOTALL)
    links = [href for nav in navs for href in re.findall(r'href="([^"]+)"', nav)]
    assert len(links) >= 3  # Overview, Activity, Settings
    for href in links:
        assert alice_client.get(href).status_code == 200, href


# --- offline never says "Nothing needs you" ----------------------------------------

AGENT_CHECK = (
    "sudo systemctl status cronrack-agent\nsudo journalctl -u cronrack-agent -n 50 --no-pager"
)


def test_offline_host_without_issues_never_says_nothing_needs_you(alice_client: Client, alice):
    host = make_silent_host(alice)

    overview = alice_client.get("/").content.decode()
    detail = alice_client.get(f"/hosts/{host.pk}/").content.decode()

    for html in (overview, detail):
        assert "Nothing needs you" not in html
    assert "This host stopped reporting. Last report 28 h ago." in detail
    assert "Check the agent on the host" in detail
    assert f'<pre class="code well" id="agent-check-{host.pk}">{AGENT_CHECK}</pre>' in detail


def test_offline_host_detail_lists_other_issues_after_the_fixed_row(alice_client: Client, alice):
    host = make_silent_host(alice)
    Host.objects.filter(pk=host.pk).update(
        latest_report=make_report(failed_units=["backup.service"])
    )

    detail = alice_client.get(f"/hosts/{host.pk}/").content.decode()

    assert detail.index("This host stopped reporting") < detail.index("backup.service failed")
    assert "Nothing needs you" not in detail


def test_online_host_without_issues_still_says_nothing_needs_you(alice_client: Client, alice):
    host = make_host(alice, "pi-01", "green", make_report())

    detail = alice_client.get(f"/hosts/{host.pk}/").content.decode()

    assert "Nothing needs you" in alice_client.get("/").content.decode()
    assert "Nothing needs you" in detail
    assert "stopped reporting" not in detail
    assert "cronrack-agent" not in detail


# --- Copy key (static/cronrack/js/copy.js) ------------------------------------------

COPY_JS = Path(__file__).resolve().parent.parent / "static/cronrack/js/copy.js"


def test_offline_detail_copy_key_points_at_the_code_well(alice_client: Client, alice):
    from .test_preview import scan

    host = make_silent_host(alice)
    html = alice_client.get(f"/hosts/{host.pk}/").content.decode()

    target = f"agent-check-{host.pk}"  # from the host id, never the (untrusted) name
    button = re.search(
        r'<button type="button" class="btn btn--sm js-copy" aria-controls="([^"]+)">Copy</button>',
        html,
    )
    assert button and button.group(1) == target
    assert html.count(f'id="{target}"') == 1
    assert f'<pre class="code well" id="{target}">{AGENT_CHECK}</pre>' in html
    # One shared live region; copy.js is an external, deferred script.
    assert html.count('id="copy-status" aria-live="polite"') == 1
    assert '<script src="/static/cronrack/js/copy.js" defer></script>' in html

    page = scan(html)
    assert all(script.get("src") for script in page.scripts)
    assert page.inline_script_text == ""
    assert page.inline_styles == []
    assert page.event_handlers == []


def test_copy_js_never_uses_inner_html_or_the_console():
    source = COPY_JS.read_text()
    assert "innerHTML" not in source
    assert "outerHTML" not in source
    assert "console." not in source
    assert "dataset" not in source  # nothing stored in data- attributes
