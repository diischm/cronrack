"""Check now, Check all now and Send test alert work both ways (progressive enhancement):

- fetch from actions.js (Accept: application/json) gets JSON;
- a plain form POST (no JavaScript) is redirected to a FIXED page with a flash message,
  never to the Referer or a URL from the request.
"""

from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.test import Client

from hosts.models import Host
from hosts.services import save_report
from prefs.models import Prefs

from .sample import make_report

pytestmark = pytest.mark.django_db

JSON = "application/json"
GOOD_URL = "https://discord.com/api/webhooks/123456789/abcDEF_-xyz"
EVIL = "https://evil.example/"


def flash(response) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


@pytest.fixture
def online(alice):
    return Host.objects.create(owner=alice, name="homeserver", connected=True)


# --- Check now ----------------------------------------------------------------------


def test_collect_json(alice_client: Client, online):
    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post(f"/hosts/{online.pk}/collect/", HTTP_ACCEPT=JSON)
    assert response.status_code == 202
    assert response.json() == {"ok": True}
    collect.assert_called_once_with(online)


def test_collect_plain_post_redirects_back_to_the_host(alice_client: Client, online):
    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post(f"/hosts/{online.pk}/collect/?next={EVIL}", HTTP_REFERER=EVIL)
    assert response.status_code == 302
    assert response["Location"] == f"/hosts/{online.pk}/"
    assert flash(response) == ["Asked homeserver to report now."]
    collect.assert_called_once_with(online)


def test_collect_offline_host_is_409_and_sends_nothing(alice_client: Client, host):
    assert host.connected is False
    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post(f"/hosts/{host.pk}/collect/", HTTP_ACCEPT=JSON)
    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "error": "Host is offline, so the agent can't be reached.",
    }
    collect.assert_not_called()


def test_collect_offline_host_plain_post(alice_client: Client, host):
    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post(f"/hosts/{host.pk}/collect/")
    assert response.status_code == 302
    assert response["Location"] == f"/hosts/{host.pk}/"
    assert flash(response) == ["Host is offline, so the agent can't be reached."]
    collect.assert_not_called()


def test_collect_other_users_host_is_404_and_sends_nothing(client: Client, bob, online):
    client.force_login(bob)
    with mock.patch("hosts.views.request_collect") as collect:
        response = client.post(f"/hosts/{online.pk}/collect/", HTTP_ACCEPT=JSON)
    assert response.status_code == 404
    collect.assert_not_called()


# --- Check all now ------------------------------------------------------------------


def test_collect_all_json_counts_and_only_asks_my_hosts(alice_client: Client, alice, bob):
    """IDOR: Bob's hosts are never asked and never counted, connected or not."""
    mine = [
        Host.objects.create(owner=alice, name="a", connected=True),
        Host.objects.create(owner=alice, name="b", connected=True),
    ]
    Host.objects.create(owner=alice, name="c", connected=False)
    Host.objects.create(owner=bob, name="bob-on", connected=True)
    Host.objects.create(owner=bob, name="bob-off", connected=False)

    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post("/hosts/collect-all/", HTTP_ACCEPT=JSON)

    assert response.status_code == 202
    assert response.json() == {"ok": True, "asked": 2, "offline": 1}
    asked = [c.args[0] for c in collect.call_args_list]
    assert sorted(h.pk for h in asked) == sorted(h.pk for h in mine)
    assert all(h.owner_id == alice.pk for h in asked)


def test_collect_all_plain_post_redirects_to_overview(alice_client: Client, alice):
    Host.objects.create(owner=alice, name="a", connected=True)
    Host.objects.create(owner=alice, name="b", connected=False)
    with mock.patch("hosts.views.request_collect"):
        response = alice_client.post(f"/hosts/collect-all/?next={EVIL}", HTTP_REFERER=EVIL)
    assert response.status_code == 302
    assert response["Location"] == "/"
    assert flash(response) == ["Asked 1 host · 1 offline."]


def test_collect_all_with_no_hosts(alice_client: Client):
    with mock.patch("hosts.views.request_collect") as collect:
        response = alice_client.post("/hosts/collect-all/", HTTP_ACCEPT=JSON)
    assert response.json() == {"ok": True, "asked": 0, "offline": 0}
    collect.assert_not_called()


# --- Send test alert ----------------------------------------------------------------


def test_test_alert_json(alice_client: Client, alice):
    Prefs.objects.create(owner=alice, discord_webhook_url=GOOD_URL)
    with mock.patch("prefs.views.send_discord", return_value=True):
        response = alice_client.post("/settings/test-alert/", HTTP_ACCEPT=JSON)
    assert response.status_code == 200
    assert response["Content-Type"] == JSON
    assert response.json() == {"ok": True, "error": ""}


def test_test_alert_plain_post_redirects_to_settings(alice_client: Client, alice):
    Prefs.objects.create(owner=alice, discord_webhook_url=GOOD_URL)
    with mock.patch("prefs.views.send_discord", return_value=True):
        response = alice_client.post(f"/settings/test-alert/?next={EVIL}", HTTP_REFERER=EVIL)
    assert response.status_code == 302
    assert response["Location"] == "/settings/"
    assert flash(response) == ["Test alert sent."]


def test_test_alert_plain_post_shows_the_error(alice_client: Client):
    with mock.patch("prefs.views.send_discord") as send:
        response = alice_client.post("/settings/test-alert/")
    assert response.status_code == 302
    assert response["Location"] == "/settings/"
    assert flash(response) == ["Save a Discord webhook URL first."]
    send.assert_not_called()


# --- CSRF on all three, also for fetch ------------------------------------------------


@pytest.mark.parametrize(
    "url", ["/hosts/{id}/collect/", "/hosts/collect-all/", "/settings/test-alert/"]
)
def test_csrf_required_for_fetch_too(alice, online, url):
    client = Client(enforce_csrf_checks=True)
    client.force_login(alice)
    with (
        mock.patch("hosts.views.request_collect") as collect,
        mock.patch("prefs.views.send_discord") as send,
    ):
        response = client.post(url.format(id=online.pk), HTTP_ACCEPT=JSON)
    assert response.status_code == 403
    collect.assert_not_called()
    send.assert_not_called()


# --- status.json: a stable "new report" signal for Check now --------------------------


def status(client: Client, host: Host):
    return client.get(f"/api/v1/hosts/{host.pk}/status.json")


def test_status_json_report_received_at_is_null_while_waiting(alice_client: Client, host):
    data = status(alice_client, host).json()
    assert data["report_received_at"] is None
    assert data["status"] == "waiting"
    assert data["last_seen_human"] == "never"


def test_status_json_report_received_at_changes_after_a_new_report(alice_client: Client, host):
    first = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    save_report(host, make_report(), now=first)
    before = status(alice_client, host).json()["report_received_at"]
    assert before == "2026-10-15T12:00:00+00:00"

    save_report(host, make_report(), now=first + timedelta(seconds=3))
    after = status(alice_client, host).json()["report_received_at"]
    assert after == "2026-10-15T12:00:03+00:00"
    assert after != before


def test_status_json_other_users_host_is_404(client: Client, bob, host):
    save_report(host, make_report())
    client.force_login(bob)
    assert status(client, host).status_code == 404


# --- flash messages are escaped (host names are untrusted text) -----------------------

XSS = "<script>alert(1)</script>"


def assert_escaped(html: str) -> None:
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert XSS not in html


def test_collect_flash_message_is_escaped(alice_client: Client, alice):
    # The name form only allows [A-Za-z0-9_-]; the template must not rely on that.
    hostile = Host.objects.create(owner=alice, name=XSS, connected=True)
    with mock.patch("hosts.views.request_collect"):
        response = alice_client.post(f"/hosts/{hostile.pk}/collect/", follow=True)
    assert response.redirect_chain == [(f"/hosts/{hostile.pk}/", 302)]
    html = response.content.decode()
    assert "Asked &lt;script&gt;alert(1)&lt;/script&gt; to report now." in html
    assert_escaped(html)


def test_collect_all_flash_message_is_escaped(alice_client: Client, alice):
    Host.objects.create(owner=alice, name=XSS, connected=True)
    with mock.patch("hosts.views.request_collect"):
        response = alice_client.post("/hosts/collect-all/", follow=True)
    assert response.redirect_chain == [("/", 302)]
    html = response.content.decode()
    assert "Asked 1 host · 0 offline." in html
    # The host name also appears on the overview card: escaped there too.
    assert_escaped(html)
