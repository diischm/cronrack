"""Rotate token can't happen twice from one form: the one-time rotate key.

Pressing Back on the "New token" page can make the browser re-send the Rotate POST.
Without a one-time key the server would rotate again: the token the user just
installed would stop working and a token they never saw would be issued.
"""

from unittest import mock

import pytest
from django.test import Client

from hosts.models import AgentToken, Host
from hosts.views import MAX_ROTATE_KEYS, ROTATE_AGAIN, ROTATE_KEYS_SESSION

from .rotate import ROTATE_KEY_RE, post_rotate, rotate_key
from .test_host_views import TOKEN_RE, all_database_text, token_from

pytestmark = pytest.mark.django_db


def token_hash(host: Host) -> str:
    return AgentToken.objects.get(host=host).token_hash


def assert_refused(response, host: Host, hash_before: str, close) -> None:
    """Not rotated, agent not disconnected, settings page with a fresh form."""
    assert response.status_code == 409
    assert token_hash(host) == hash_before
    close.assert_not_called()
    html = response.content.decode()
    assert ROTATE_AGAIN in html
    assert ROTATE_KEY_RE.search(html), "a fresh Rotate form comes with a new key"
    assert not TOKEN_RE.search(html), "no token is shown"
    assert "no-store" in response["Cache-Control"]


@pytest.fixture
def close():
    # close_agent runs on commit: capture it so a refused rotation provably never calls it.
    with mock.patch("hosts.services.close_agent") as close_agent:
        yield close_agent


def test_same_key_twice_rotates_once(
    alice_client: Client, host, close, django_capture_on_commit_callbacks
):
    AgentToken.issue(host)
    key = rotate_key(alice_client, host.pk)

    with django_capture_on_commit_callbacks(execute=True):
        first = post_rotate(alice_client, host.pk, key)
    installed = token_from(first)
    hash_after_first = token_hash(host)
    close.assert_called_once()
    close.reset_mock()

    # The browser re-sends the same POST (Back on the "New token" page).
    with django_capture_on_commit_callbacks(execute=True):
        again = post_rotate(alice_client, host.pk, key)

    assert_refused(again, host, hash_after_first, close)
    assert AgentToken.verify(installed) == host  # the token just installed still works


def test_fresh_key_rotates_again(alice_client: Client, host, close):
    AgentToken.issue(host)
    first = token_from(post_rotate(alice_client, host.pk))
    # "Rotate again?": the refused page's fresh form, or a new visit to settings, works.
    second = token_from(post_rotate(alice_client, host.pk))
    assert second != first
    assert AgentToken.verify(first) is None
    assert AgentToken.verify(second) == host


def test_refused_page_offers_a_working_fresh_form(alice_client: Client, host, close):
    AgentToken.issue(host)
    key = rotate_key(alice_client, host.pk)
    post_rotate(alice_client, host.pk, key)
    refused = post_rotate(alice_client, host.pk, key)
    fresh = ROTATE_KEY_RE.search(refused.content.decode()).group(1)

    assert fresh != key
    assert post_rotate(alice_client, host.pk, fresh).status_code == 200


@pytest.mark.parametrize("data", [{}, {"rotate_key": ""}, {"rotate_key": "made-up-key-123456"}])
def test_missing_or_unknown_key_is_refused(alice_client: Client, host, close, data):
    AgentToken.issue(host)
    rotate_key(alice_client, host.pk)  # the session has a real key, but the POST doesn't send it
    before = token_hash(host)

    response = alice_client.post(f"/hosts/{host.pk}/rotate/", data)

    assert_refused(response, host, before, close)


def test_key_for_host_a_cannot_rotate_host_b(alice_client: Client, alice, host, close):
    other = Host.objects.create(owner=alice, name="pi")
    AgentToken.issue(host)
    AgentToken.issue(other)
    key_for_host = rotate_key(alice_client, host.pk)
    before = token_hash(other)

    response = post_rotate(alice_client, other.pk, key_for_host)

    assert_refused(response, other, before, close)
    # The key still belongs to its own host and works there.
    assert post_rotate(alice_client, host.pk, key_for_host).status_code == 200


def test_another_session_cannot_use_the_key(alice, host, close):
    """The key lives in one browser session: a second login (same user) can't replay it."""
    AgentToken.issue(host)
    first_browser, second_browser = Client(), Client()
    first_browser.force_login(alice)
    second_browser.force_login(alice)
    key = rotate_key(first_browser, host.pk)
    before = token_hash(host)

    response = post_rotate(second_browser, host.pk, key)

    assert_refused(response, host, before, close)
    # Unused, so the browser that was shown the form can still rotate.
    assert post_rotate(first_browser, host.pk, key).status_code == 200


def test_another_users_session_cannot_use_the_key(alice, bob, host, close):
    """Bob can't use alice's key: her host is 404 for him before the key is even looked at."""
    AgentToken.issue(host)
    alice_browser, bob_browser = Client(), Client()
    alice_browser.force_login(alice)
    bob_browser.force_login(bob)
    key = rotate_key(alice_browser, host.pk)
    before = token_hash(host)

    assert post_rotate(bob_browser, host.pk, key).status_code == 404
    assert token_hash(host) == before
    close.assert_not_called()


def test_csrf_is_still_required(alice, host, close):
    client = Client(enforce_csrf_checks=True)
    client.force_login(alice)
    AgentToken.issue(host)
    key = rotate_key(client, host.pk)
    before = token_hash(host)

    response = client.post(f"/hosts/{host.pk}/rotate/", {"rotate_key": key})

    assert response.status_code == 403
    assert token_hash(host) == before


def test_used_keys_are_dropped_and_no_token_is_stored(alice_client: Client, host, close):
    AgentToken.issue(host)
    key = rotate_key(alice_client, host.pk)
    token = token_from(post_rotate(alice_client, host.pk, key))

    keys = alice_client.session.get(ROTATE_KEYS_SESSION, {})
    assert key not in keys
    assert token not in str(dict(alice_client.session))
    assert token not in all_database_text()  # the session table included


def test_session_keeps_only_the_newest_keys(alice_client: Client, host):
    first = rotate_key(alice_client, host.pk)
    for _ in range(MAX_ROTATE_KEYS):
        rotate_key(alice_client, host.pk)
    keys = alice_client.session[ROTATE_KEYS_SESSION]
    assert len(keys) == MAX_ROTATE_KEYS
    assert first not in keys


def test_rotate_page_is_never_cached(alice_client: Client, host, close):
    AgentToken.issue(host)
    assert "no-store" in post_rotate(alice_client, host.pk)["Cache-Control"]


# --- Create host: a re-sent Create is refused as a duplicate name -----------------------


def test_resent_create_makes_no_second_host_and_shows_no_token(alice_client: Client, alice):
    first = alice_client.post("/hosts/add/", {"name": "pi-01"})
    assert TOKEN_RE.search(first.content.decode())

    again = alice_client.post("/hosts/add/", {"name": "pi-01"})

    assert Host.objects.filter(owner=alice, name="pi-01").count() == 1
    assert AgentToken.objects.count() == 1
    html = again.content.decode()
    assert not TOKEN_RE.search(html)
    assert "You already have a host with this name." in html
