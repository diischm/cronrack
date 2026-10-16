"""Host settings and Settings pages (handoff 5.6, 5.7).

Rotate and revoke need an explicit confirmation (confirm=yes: the ticked box without JS, the
confirm dialog with JS); delete needs the typed host name. Without it nothing happens and the
page shows an inline error on that field. Plus: the webhook URL is never repeated in a toast
or message, no inline script/style/handlers, IDOR, and the title's status.
"""

import re
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone

from hosts.models import AgentToken, Host
from hosts.views import CONFIRM_ERROR, ROTATE_AGAIN
from prefs.models import Prefs

from .rotate import rotate_key
from .sample import make_report
from .test_host_views import TOKEN_RE, token_from
from .test_preview import EVENT_HANDLER_ATTR, scan

pytestmark = pytest.mark.django_db

WEBHOOK = "https://discord.com/api/webhooks/123456789/s3cret-T0KEN_value"
JS_DIR = Path(__file__).resolve().parents[1] / "static" / "cronrack" / "js"


@pytest.fixture
def close():
    """close_agent, wherever it's called from (rotate: on commit; revoke: in the view)."""
    with (
        mock.patch("hosts.services.close_agent") as on_commit,
        mock.patch("hosts.views.close_agent") as direct,
    ):
        yield mock.Mock(on_commit=on_commit, direct=direct)


def token_hash(host: Host) -> str:
    return AgentToken.objects.get(host=host).token_hash


def toasts(html: str) -> str:
    """The toast region: every flash message the page shows."""
    match = re.search(
        r'<div class="toasts" id="toasts"[^>]*>(.*?)</div>\s*<template', html, re.DOTALL
    )
    assert match, "the page has the toast region"
    return match.group(1)


def assert_checkbox_error(html: str, box_id: str) -> None:
    """The confirm box is marked invalid and tied to its error text, right under it."""
    box = re.search(rf'<input [^>]*id="{box_id}"[^>]*>', html)
    assert box, box_id
    assert 'aria-invalid="true"' in box.group(0)
    assert f'aria-describedby="{box_id}-error"' in box.group(0)
    assert f'<p class="field__error" id="{box_id}-error">{CONFIRM_ERROR}</p>' in html


# --- rotate ---------------------------------------------------------------------------


@pytest.mark.parametrize("confirm", [None, "", "no", "on", "YES"])
def test_rotate_without_confirm_does_nothing(alice_client: Client, host, close, confirm):
    AgentToken.issue(host)
    key = rotate_key(alice_client, host.pk)
    before = token_hash(host)
    data = {"rotate_key": key} if confirm is None else {"rotate_key": key, "confirm": confirm}

    response = alice_client.post(f"/hosts/{host.pk}/rotate/", data)

    assert response.status_code == 400
    assert token_hash(host) == before
    close.on_commit.assert_not_called()
    html = response.content.decode()
    assert not TOKEN_RE.search(html), "no token is shown"
    assert_checkbox_error(html, "rotate-confirm")
    assert 'id="revoke-confirm-error"' not in html  # only the rotate field has the error


def test_unconfirmed_rotate_does_not_use_up_the_key(alice_client: Client, host, close):
    """The confirm check comes first: the same form, ticked this time, still rotates."""
    AgentToken.issue(host)
    key = rotate_key(alice_client, host.pk)
    alice_client.post(f"/hosts/{host.pk}/rotate/", {"rotate_key": key})

    response = alice_client.post(f"/hosts/{host.pk}/rotate/", {"rotate_key": key, "confirm": "yes"})

    assert response.status_code == 200
    assert AgentToken.verify(token_from(response)) == host


def test_confirmed_rotate_still_enforces_the_one_time_key(alice_client: Client, host, close):
    AgentToken.issue(host)
    key = rotate_key(alice_client, host.pk)
    first = alice_client.post(f"/hosts/{host.pk}/rotate/", {"rotate_key": key, "confirm": "yes"})
    assert first.status_code == 200
    rotated = token_hash(host)

    # The same POST again (Back / reload), confirmed: refused, no second rotation.
    again = alice_client.post(f"/hosts/{host.pk}/rotate/", {"rotate_key": key, "confirm": "yes"})

    assert again.status_code == 409
    assert ROTATE_AGAIN in again.content.decode()
    assert token_hash(host) == rotated
    # And a confirmed POST without any key is refused too.
    keyless = alice_client.post(f"/hosts/{host.pk}/rotate/", {"confirm": "yes"})
    assert keyless.status_code == 409
    assert token_hash(host) == rotated


# --- revoke ---------------------------------------------------------------------------


@pytest.mark.parametrize("confirm", [None, "", "no", "on"])
def test_revoke_without_confirm_does_nothing(alice_client: Client, host, close, confirm):
    token = AgentToken.issue(host)
    data = {} if confirm is None else {"confirm": confirm}

    response = alice_client.post(f"/hosts/{host.pk}/revoke/", data)

    assert response.status_code == 400
    assert AgentToken.verify(token) == host
    assert AgentToken.objects.get(host=host).revoked is False
    close.direct.assert_not_called()
    html = response.content.decode()
    assert_checkbox_error(html, "revoke-confirm")
    assert 'id="rotate-confirm-error"' not in html


def test_revoke_with_confirm_revokes(alice_client: Client, host, close):
    token = AgentToken.issue(host)

    response = alice_client.post(f"/hosts/{host.pk}/revoke/", {"confirm": "yes"}, follow=True)

    assert AgentToken.verify(token) is None
    close.direct.assert_called_once_with(host)
    assert "Token revoked." in toasts(response.content.decode())


# --- delete ---------------------------------------------------------------------------


# Not " nas" / "nas ": Django's CharField strips surrounding spaces before the check (existing
# server behaviour, left as it is); the dialog's own check is exact.
@pytest.mark.parametrize("typed", ["", "NAS", "Nas", "na", "nas2", "n a s"])
def test_delete_with_a_wrong_name_does_nothing(alice_client: Client, host, close, typed):
    AgentToken.issue(host)

    response = alice_client.post(f"/hosts/{host.pk}/delete/", {"confirm_name": typed})

    assert response.status_code == 400
    assert Host.objects.filter(pk=host.pk, name="nas").exists()
    close.direct.assert_not_called()
    field = re.search(r'<input [^>]*name="confirm_name"[^>]*>', response.content.decode())
    assert field and 'aria-invalid="true"' in field.group(0)


def test_delete_with_the_exact_name_redirects_with_a_toast(alice_client: Client, host, close):
    response = alice_client.post(f"/hosts/{host.pk}/delete/", {"confirm_name": "nas"}, follow=True)

    assert response.redirect_chain == [("/", 302)]
    assert not Host.objects.filter(pk=host.pk).exists()
    assert "Deleted nas." in toasts(response.content.decode())


# --- the page itself ------------------------------------------------------------------


def test_host_settings_page_has_the_no_js_confirmations(alice_client: Client, host):
    html = alice_client.get(f"/hosts/{host.pk}/settings/").content.decode()

    for action, label in (("rotate", "Yes, rotate the token"), ("revoke", "Yes, revoke the token")):
        assert re.search(
            rf'<input class="check__input visually-hidden" type="checkbox" name="confirm" value="yes" id="{action}-confirm">',
            html,
        ), action
        assert f'<span class="check__label">{label}</span>' in html
        assert f'<label class="check" for="{action}-confirm">' in html
    assert html.count('class="surface panel form js-confirm"') == 2
    assert 'class="surface panel form danger-zone js-confirm"' in html
    assert '<dialog class="confirm-dialog surface" id="confirm-dialog"' in html
    assert "cronrack/js/dialog.js" in html
    # The one-time key of the last PR is still there.
    assert re.search(r'<input type="hidden" name="rotate_key" value="[A-Za-z0-9_-]{16,}">', html)
    # Back key to the host page.
    assert f'<a class="btn btn--icon" href="/hosts/{host.pk}/" aria-label="Back">' in html


def test_title_shows_offline_not_the_stale_stored_status(alice_client: Client, alice, host):
    """Stored status still green, but silent past the owner's threshold: Offline."""
    Prefs.objects.create(owner=alice, silent_after_seconds=90)
    Host.objects.filter(pk=host.pk).update(
        status="green",
        connected=False,
        latest_report=make_report(),
        last_seen_at=timezone.now() - timedelta(minutes=10),
    )

    html = alice_client.get(f"/hosts/{host.pk}/settings/").content.decode()
    head = re.search(r'<div class="settings-head__title">(.*?)</div>', html, re.DOTALL).group(1)

    assert '<span class="lamp lamp--off" aria-hidden="true"></span>' in head
    assert "<strong>Offline</strong>" in head
    assert "All good" not in head
    assert "status-icon" not in head


def test_title_shows_the_status_icon_when_online(alice_client: Client, host):
    Host.objects.filter(pk=host.pk).update(
        status="yellow", connected=True, latest_report=make_report(), last_seen_at=timezone.now()
    )

    html = alice_client.get(f"/hosts/{host.pk}/settings/").content.decode()
    head = re.search(r'<div class="settings-head__title">(.*?)</div>', html, re.DOTALL).group(1)

    assert "status-icon--warn" in head
    assert "Heads up" in head
    assert "lamp--off" not in head


# --- Settings: webhook ----------------------------------------------------------------


def test_webhook_url_is_never_in_a_toast_or_message(alice_client: Client, alice):
    saved = alice_client.post(
        "/settings/",
        {"form": "prefs", "discord_webhook_url": WEBHOOK, "silent_after_seconds": "120"},
        follow=True,
    )
    assert Prefs.objects.get(owner=alice).discord_webhook_url == WEBHOOK
    region = toasts(saved.content.decode())
    assert "Settings saved." in region
    assert WEBHOOK not in region
    assert "s3cret" not in region

    for ok in (True, False):
        with mock.patch("prefs.views.send_discord", return_value=ok):
            fetched = alice_client.post("/settings/test-alert/", HTTP_ACCEPT="application/json")
            plain = alice_client.post("/settings/test-alert/", follow=True)
        assert "s3cret" not in fetched.content.decode()
        assert "s3cret" not in toasts(plain.content.decode())


def test_invalid_webhook_error_is_inline_and_does_not_repeat_the_url(alice_client: Client):
    bad = "https://evil.example/api/webhooks/1/s3cret"
    response = alice_client.post(
        "/settings/", {"form": "prefs", "discord_webhook_url": bad, "silent_after_seconds": "120"}
    )

    assert response.status_code == 400
    html = response.content.decode()
    field = re.search(r'<input type="url"[^>]*>', html, re.DOTALL).group(0)
    assert 'aria-invalid="true"' in field
    assert (
        'aria-describedby="id_discord_webhook_url_helptext id_discord_webhook_url_error"' in field
    )
    assert 'autocomplete="off"' in field
    errors = re.search(
        r'<div class="field__errors" id="id_discord_webhook_url_error">(.*?)</div>', html, re.DOTALL
    )
    assert errors and "https://discord.com/api/webhooks/" in errors.group(1)
    assert "s3cret" not in errors.group(1)
    assert "s3cret" not in toasts(html)


def test_settings_page_fields(alice_client: Client):
    html = alice_client.get("/settings/").content.decode()

    assert re.search(
        r'<input type="number" class="well unit-row__input" name="silent_after_seconds"', html
    )
    assert 'min="60" max="86400"' in html
    assert '<span class="unit-row__unit" id="id_silent_after_seconds_unit">seconds</span>' in html
    assert 'autocomplete="current-password"' in html
    assert html.count('autocomplete="new-password"') == 2
    assert html.count('aria-label="Show password" aria-pressed="false"') == 3
    for value in ("light", "dark", "system"):
        assert f'type="radio" name="theme" value="{value}"' in html
    # "Send test alert" is its own form, linked from the Alerts panel.
    assert (
        '<button type="submit" class="btn" form="test-alert-form">Send test alert</button>' in html
    )
    assert re.search(
        r'<form id="test-alert-form" class="js-fetch-action" method="post" action="/settings/test-alert/"',
        html,
    )
    assert "cronrack/js/password.js" in html


# --- CSP: no inline script, style or handlers -----------------------------------------


def pages(client: Client, host: Host) -> dict[str, str]:
    AgentToken.issue(host)
    out = {
        "host settings": client.get(f"/hosts/{host.pk}/settings/"),
        "host settings, rotate error": client.post(f"/hosts/{host.pk}/rotate/", {}),
        "host settings, revoke error": client.post(f"/hosts/{host.pk}/revoke/", {}),
        "host settings, delete error": client.post(
            f"/hosts/{host.pk}/delete/", {"confirm_name": "x"}
        ),
        "settings": client.get("/settings/"),
        "settings, errors": client.post(
            "/settings/", {"form": "prefs", "discord_webhook_url": "x", "silent_after_seconds": "1"}
        ),
        "settings, password errors": client.post("/settings/", {"form": "password"}),
    }
    return {name: response.content.decode() for name, response in out.items()}


def test_settings_pages_have_no_inline_script_style_or_handlers(alice_client: Client, host):
    for name, raw in pages(alice_client, host).items():
        found = scan(raw)
        assert found.inline_script_text == "", name
        assert all(s.get("src") for s in found.scripts), name
        assert found.inline_styles == [], name
        assert found.event_handlers == [], name
        assert not EVENT_HANDLER_ATTR.search(raw), name
        assert "<style" not in raw, name
        assert "javascript:" not in raw, name


@pytest.mark.parametrize("name", ["dialog.js", "password.js", "theme.js", "actions.js"])
def test_settings_js_follows_the_rules(name):
    source = (JS_DIR / name).read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert banned not in source, banned
    assert "console." not in source
    assert "dataset" not in source
    assert ".style" not in source


def test_xss_host_name_is_text_on_host_settings(alice_client: Client, host):
    xss = '<script>alert(1)</script>"><img src=x onerror=alert(1)>'
    Host.objects.filter(pk=host.pk).update(name=xss)

    html = alice_client.get(f"/hosts/{host.pk}/settings/").content.decode()

    assert xss not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# --- IDOR -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("get", "settings/", None),
        ("post", "settings/", {"name": "stolen"}),
        ("post", "rotate/", {"confirm": "yes", "rotate_key": "x" * 22}),
        ("post", "revoke/", {"confirm": "yes"}),
        ("post", "delete/", {"confirm_name": "nas"}),
    ],
)
def test_other_users_host_settings_are_404(client: Client, bob, host, close, method, path, data):
    AgentToken.issue(host)
    before = token_hash(host)
    client.force_login(bob)

    response = getattr(client, method)(f"/hosts/{host.pk}/{path}", data)

    assert response.status_code == 404
    host.refresh_from_db()
    assert host.name == "nas"
    assert token_hash(host) == before
    assert AgentToken.objects.get(host=host).revoked is False
    close.direct.assert_not_called()
    close.on_commit.assert_not_called()
