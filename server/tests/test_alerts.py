import json
import urllib.error
from unittest import mock

import pytest
from django.test import Client

from activity.events import record_status_change
from alerts.discord import format_alert, is_allowed_webhook, notify, send_discord
from hosts.services import save_report
from prefs.models import Prefs

from .sample import make_report

pytestmark = pytest.mark.django_db

GOOD_URL = "https://discord.com/api/webhooks/123456789/abcDEF_-xyz"
JSON = "application/json"


# --- the SSRF allowlist ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        GOOD_URL,
        "https://discordapp.com/api/webhooks/123456789/abcDEF_-xyz",
    ],
)
def test_discord_webhook_urls_are_allowed(url):
    assert is_allowed_webhook(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123/abc",  # not https
        "https://discord.com.evil.net/api/webhooks/123/abc",  # lookalike host
        "https://evil.net/discord.com/api/webhooks/123/abc",
        "https://discord.com@evil.net/api/webhooks/123/abc",  # userinfo trick
        "https://evil.net@discord.com/api/webhooks/123/abc",
        "https://discord.com:8443/api/webhooks/123/abc",  # other port
        "https://DISCORD.COM/api/webhooks/123/abc",
        "https://canary.discord.com/api/webhooks/123/abc",
        "https://xdiscord.com/api/webhooks/123/abc",
        "https://discord.com/api/webhooks/123/abc?wait=true",
        "https://discord.com/api/webhooks/123/abc#x",
        "https://discord.com/api/webhooks/../../internal",
        "https://discord.com/api/other/123/abc",
        "https://discord.com/api/webhooks/123/abc\n",
        "https://discord.com/api/webhooks/123/abc/../x",
        "http://127.0.0.1/api/webhooks/123/abc",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "",
        None,
        123,
    ],
)
def test_other_urls_are_rejected(url):
    assert not is_allowed_webhook(url)


def test_send_discord_refuses_a_bad_url_without_any_request():
    with mock.patch("alerts.discord._opener") as opener:
        assert send_discord("http://192.168.1.1/admin", "hi") is False
    opener.open.assert_not_called()


# --- send_discord ------------------------------------------------------------------


def fake_response(status=204):
    response = mock.MagicMock()
    response.status = status
    response.__enter__.return_value = response
    return response


def test_send_discord_posts_json_without_mentions():
    with mock.patch("alerts.discord._opener") as opener:
        opener.open.return_value = fake_response()
        assert send_discord(GOOD_URL, "@everyone hello") is True

    request = opener.open.call_args.args[0]
    assert opener.open.call_args.kwargs["timeout"] == 5
    assert request.full_url == GOOD_URL
    assert request.get_method() == "POST"
    body = json.loads(request.data)
    assert body == {"content": "@everyone hello", "allowed_mentions": {"parse": []}}


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(GOOD_URL, 404, "Not Found", {}, None),
        urllib.error.URLError("dns"),
        TimeoutError(),
        ConnectionResetError(),
    ],
)
def test_send_discord_never_raises(error, caplog):
    with mock.patch("alerts.discord._opener") as opener:
        opener.open.side_effect = error
        assert send_discord(GOOD_URL, "hi") is False
    assert "abcDEF" not in caplog.text  # the webhook token is never logged


# --- notify: once per status change ------------------------------------------------


@pytest.fixture
def webhook(alice):
    return Prefs.objects.create(owner=alice, discord_webhook_url=GOOD_URL)


def test_notify_fires_once_per_status_change(host, webhook, django_capture_on_commit_callbacks):
    sent = []
    with (
        mock.patch("alerts.discord.send_discord", side_effect=lambda u, t: sent.append(t)),
        django_capture_on_commit_callbacks(execute=True),
    ):
        for report in [
            make_report(failed_units=["x.service"]),  # waiting -> red: alert
            make_report(failed_units=["x.service"]),  # still red: nothing
            make_report(failed_units=["x.service", "y.service"]),  # still red: nothing
            make_report(),  # red -> green: alert
            make_report(),  # still green: nothing
        ]:
            result = save_report(host, report)
            if result.old_status != result.new_status:
                record_status_change(host, result.old_status, result.new_status, result.findings)

    assert sent == [
        "🔴 nas sent its first report (red): x.service failed",
        "🟢 nas went green",
    ]


def test_notify_is_sent_only_after_commit(host, webhook, django_capture_on_commit_callbacks):
    with mock.patch("alerts.discord.send_discord") as send:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            record_status_change(host, "green", "red", [])
        send.assert_not_called()
        callbacks[0]()
    send.assert_called_once()


def test_notify_without_webhook_sends_nothing(host):
    event = record_status_change(host, "green", "red", [])
    with mock.patch("alerts.discord.send_discord") as send:
        assert notify(event) is False
    send.assert_not_called()


def test_notify_rechecks_the_url_before_sending(host, alice):
    # A bad URL that got into the database some other way (old data, admin edit).
    Prefs.objects.create(owner=alice, discord_webhook_url="http://10.0.0.1/hook")
    event = record_status_change(host, "green", "red", [])
    with mock.patch("alerts.discord._opener") as opener:
        assert notify(event) is False
    opener.open.assert_not_called()


def test_alert_text(host):
    event = record_status_change(host, "green", "offline", [])
    assert format_alert(event) == "🔴 nas went offline"


# --- settings page and test alert ---------------------------------------------------


def test_prefs_reject_non_discord_webhooks(alice_client: Client, alice):
    response = alice_client.post(
        "/settings/",
        {"form": "prefs", "discord_webhook_url": "https://evil.net/x", "silent_after_seconds": 90},
    )
    assert response.status_code == 400
    assert Prefs.objects.get(owner=alice).discord_webhook_url == ""


def test_prefs_save(alice_client: Client, alice):
    response = alice_client.post(
        "/settings/",
        {"form": "prefs", "discord_webhook_url": GOOD_URL, "silent_after_seconds": 300},
    )
    assert response.status_code == 302
    prefs = Prefs.objects.get(owner=alice)
    assert (prefs.discord_webhook_url, prefs.silent_after_seconds) == (GOOD_URL, 300)


def test_password_change(alice_client: Client, alice):
    response = alice_client.post(
        "/settings/",
        {
            "form": "password",
            "old_password": "unused-pw-1",
            "new_password1": "brand-new-secret-77",
            "new_password2": "brand-new-secret-77",
        },
    )
    assert response.status_code == 302
    alice.refresh_from_db()
    assert alice.check_password("brand-new-secret-77")
    assert alice_client.get("/settings/").status_code == 200  # still logged in


def test_password_change_needs_the_old_password(alice_client: Client, alice):
    alice_client.post(
        "/settings/",
        {
            "form": "password",
            "old_password": "wrong",
            "new_password1": "brand-new-secret-77",
            "new_password2": "brand-new-secret-77",
        },
    )
    alice.refresh_from_db()
    assert alice.check_password("unused-pw-1")


def test_test_alert_without_webhook(alice_client: Client):
    data = alice_client.post("/settings/test-alert/", HTTP_ACCEPT=JSON).json()
    assert data["ok"] is False
    assert data["error"]


def test_test_alert_sends_to_the_saved_url_only(alice_client: Client, webhook):
    with mock.patch("prefs.views.send_discord", return_value=True) as send:
        data = alice_client.post(
            "/settings/test-alert/", {"discord_webhook_url": "http://10.0.0.1/"}, HTTP_ACCEPT=JSON
        ).json()
    assert data == {"ok": True, "error": ""}
    assert send.call_args.args[0] == GOOD_URL


def test_test_alert_reports_failure(alice_client: Client, webhook):
    with mock.patch("prefs.views.send_discord", return_value=False):
        data = alice_client.post("/settings/test-alert/", HTTP_ACCEPT=JSON).json()
    assert data["ok"] is False
    assert data["error"]
