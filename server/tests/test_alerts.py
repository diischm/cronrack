"""Discord transport (alerts.discord) and the alerts it sends through notifications.dispatch.

Per-problem behaviour (open, escalate, resolve, flap guard, quiet hours, summary) is tested
in test_notification_dispatch.py; this file keeps the transport's safety rules: the SSRF
allowlist, never raising, no mentions, the cooldown, and checking the URL again before use.
"""

import json
import urllib.error
from datetime import timedelta
from itertools import pairwise
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone

from alerts.discord import (
    ALERT_COOLDOWN_SECONDS,
    claim_cooldown,
    is_allowed_webhook,
    send_discord,
)
from hosts.models import Host
from hosts.services import save_report
from notifications import dispatch
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
        assert send_discord("http://192.0.2.1/admin", "hi") is False
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


def test_a_long_message_is_cut_to_discords_limit():
    with mock.patch("alerts.discord._opener") as opener:
        opener.open.return_value = fake_response()
        send_discord(GOOD_URL, "x" * 5000)
    assert len(json.loads(opener.open.call_args.args[0].data)["content"]) == 2000


# --- the cooldown ------------------------------------------------------------------


def test_cooldown_is_claimed_once_per_window(host):
    now = timezone.now()
    assert claim_cooldown(Host, host.pk, now) is True
    assert claim_cooldown(Host, host.pk, now + timedelta(seconds=10)) is False
    later = now + timedelta(seconds=ALERT_COOLDOWN_SECONDS)
    assert claim_cooldown(Host, host.pk, later) is True


def test_cooldown_is_per_host(host, alice):
    other = Host.objects.create(owner=alice, name="edge-pi")
    now = timezone.now()
    assert claim_cooldown(Host, host.pk, now)
    assert claim_cooldown(Host, other.pk, now)


@pytest.fixture
def webhook(alice):
    return Prefs.objects.create(
        owner=alice, discord_webhook_url=GOOD_URL, timezone="UTC", quiet_hours_enabled=False
    )


def test_a_problem_alerts_once_while_it_stays(
    host, webhook, django_capture_on_commit_callbacks, monkeypatch
):
    monkeypatch.setattr("alerts.discord.ALERT_COOLDOWN_SECONDS", 0)
    sent = []
    start = timezone.now()
    with mock.patch("alerts.discord.send_discord", side_effect=lambda u, t: sent.append(t) or True):
        for minute, report in enumerate(
            [
                make_report(failed_units=["x.service"]),  # opens: alert
                make_report(failed_units=["x.service"]),  # still there: nothing
                make_report(failed_units=["x.service", "y.service"]),  # a second problem
                make_report(),  # 1st clean evaluation: nothing yet
                make_report(),  # 2nd: both resolved
                make_report(),  # still fine: nothing
            ]
        ):
            # Each report commits on its own, so its delivery runs right after it.
            with django_capture_on_commit_callbacks(execute=True):
                save_report(host, report, now=start + timedelta(minutes=minute))

    assert sent == [
        "🔴 nas: x.service failed",
        "🔴 nas: y.service failed",
        "nas:\n🟢 Resolved: x.service failed\n🟢 Resolved: y.service failed",
    ]


def test_alerts_go_out_only_after_commit(host, webhook, django_capture_on_commit_callbacks):
    with mock.patch("alerts.discord.send_discord") as send:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            save_report(host, make_report(failed_units=["x.service"]))
        send.assert_not_called()
        for callback in callbacks:
            callback()
    send.assert_called_once()


def test_without_a_webhook_nothing_is_sent_and_no_cooldown_is_used(
    host, alice, django_capture_on_commit_callbacks
):
    with (
        mock.patch("alerts.discord.send_discord") as send,
        django_capture_on_commit_callbacks(execute=True),
    ):
        save_report(host, make_report(failed_units=["x.service"]))
    send.assert_not_called()
    host.refresh_from_db()
    assert host.last_alert_at is None


def test_the_url_is_checked_again_before_sending(host, alice, django_capture_on_commit_callbacks):
    # A bad URL that got into the database some other way (old data, admin edit).
    Prefs.objects.create(owner=alice, discord_webhook_url="http://192.0.2.1/hook")
    with (
        mock.patch("alerts.discord._opener") as opener,
        django_capture_on_commit_callbacks(execute=True),
    ):
        save_report(host, make_report(failed_units=["x.service"]))
    opener.open.assert_not_called()


def test_another_users_webhook_is_never_used(host, bob, django_capture_on_commit_callbacks):
    Prefs.objects.create(owner=bob, discord_webhook_url=GOOD_URL)
    with (
        mock.patch("alerts.discord.send_discord") as send,
        django_capture_on_commit_callbacks(execute=True),
    ):
        save_report(host, make_report(failed_units=["x.service"]))
    send.assert_not_called()


def test_a_flapping_host_sends_at_most_one_message_per_cooldown(host, webhook):
    """A new problem every 10 s for ten minutes: never more than one message a minute."""
    sent_at = []
    start = timezone.now()
    clock = {"now": start}

    def record(url, text):
        sent_at.append(clock["now"])
        return True

    with mock.patch("alerts.discord.send_discord", side_effect=record):
        for second in range(0, 600, 10):
            clock["now"] = now = start + timedelta(seconds=second)
            save_report(host, make_report(failed_units=[f"u{second}.service"]), now=now)
            dispatch.deliver(host, now)
            if second % 30 == 0:
                dispatch.deliver_pending(now)

    gaps = [(b - a).total_seconds() for a, b in pairwise(sent_at)]
    assert sent_at
    assert all(gap >= ALERT_COOLDOWN_SECONDS for gap in gaps)
    assert len(sent_at) <= 600 // ALERT_COOLDOWN_SECONDS + 1


def test_scheduler_pass_sends_held_back_alerts(host, webhook, capsys):
    from django.core.management import call_command

    now = timezone.now()
    Host.objects.filter(pk=host.pk).update(last_alert_at=now)  # cooling down
    save_report(host, make_report(failed_units=["x.service"]), now=now)
    Host.objects.filter(pk=host.pk).update(
        last_alert_at=now - timedelta(seconds=ALERT_COOLDOWN_SECONDS + 1)
    )
    with mock.patch("alerts.discord.send_discord", return_value=True) as send:
        call_command("check_hosts", "--once")

    send.assert_called_once()
    assert "1 held-back alerts sent" in capsys.readouterr().out


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
            "/settings/test-alert/", {"discord_webhook_url": "http://192.0.2.1/"}, HTTP_ACCEPT=JSON
        ).json()
    assert data == {"ok": True, "error": ""}
    assert send.call_args.args[0] == GOOD_URL


def test_test_alert_reports_failure(alice_client: Client, webhook):
    with mock.patch("prefs.views.send_discord", return_value=False):
        data = alice_client.post("/settings/test-alert/", HTTP_ACCEPT=JSON).json()
    assert data["ok"] is False
    assert data["error"]
