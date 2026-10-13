import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from accounts.forms import GENERIC_LOGIN_ERROR, RATE_LIMITED_ERROR
from accounts.ratelimit import MAX_FAILURES
from prefs.models import Prefs

pytestmark = pytest.mark.django_db

GOOD_PASSWORD = "correct-horse-battery-9"


# --- first run ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["/", "/login/", "/hosts/add/", "/api/v1/overview.json", "/admin/", "/nope/"]
)
def test_everything_redirects_to_setup_while_no_user_exists(client: Client, url):
    response = client.get(url)
    assert response.status_code == 302
    assert response["Location"] == "/setup/"


def test_static_files_are_not_redirected(client: Client):
    # pytest runs with DEBUG off, so the dev static view may answer 404; the point is
    # that it isn't redirected to /setup/.
    response = client.get("/static/cronrack/css/tokens.css")
    assert response.status_code != 302


def test_setup_creates_admin_and_logs_in(client: Client):
    response = client.post(
        "/setup/",
        {"username": "dimi", "password1": GOOD_PASSWORD, "password2": GOOD_PASSWORD},
    )

    assert response.status_code == 302
    assert response["Location"] == "/"
    user = get_user_model().objects.get()
    assert user.username == "dimi"
    assert user.is_superuser and user.is_staff
    assert user.check_password(GOOD_PASSWORD)
    assert Prefs.objects.filter(owner=user).exists()
    assert client.get("/").status_code == 200  # logged in


def test_setup_rejects_weak_or_mismatched_passwords(client: Client):
    response = client.post("/setup/", {"username": "dimi", "password1": "123", "password2": "123"})
    assert response.status_code == 200
    assert not get_user_model().objects.exists()

    client.post(
        "/setup/", {"username": "dimi", "password1": GOOD_PASSWORD, "password2": "different-9x"}
    )
    assert not get_user_model().objects.exists()


def test_setup_is_404_once_a_user_exists(client: Client, alice):
    assert client.get("/setup/").status_code == 404
    response = client.post(
        "/setup/",
        {"username": "evil", "password1": GOOD_PASSWORD, "password2": GOOD_PASSWORD},
    )
    assert response.status_code == 404
    assert not get_user_model().objects.filter(username="evil").exists()


def test_no_redirect_once_a_user_exists(client: Client, alice):
    assert client.get("/login/").status_code == 200


# --- login / logout -------------------------------------------------------------


def login(client, username="alice", password="unused-pw-1"):
    return client.post("/login/", {"username": username, "password": password})


def test_login_works(client: Client, alice):
    response = login(client)
    assert response.status_code == 302
    assert response["Location"] == "/"


@pytest.mark.parametrize(("username", "password"), [("alice", "wrong"), ("nobody", "unused-pw-1")])
def test_login_error_is_generic(client: Client, alice, username, password):
    """Wrong username and wrong password give exactly the same message."""
    html = login(client, username, password).content.decode()
    assert GENERIC_LOGIN_ERROR in html


def test_inactive_user_gets_the_generic_error(client: Client, alice):
    alice.is_active = False
    alice.save()
    assert GENERIC_LOGIN_ERROR in login(client).content.decode()


def test_login_rate_limit_blocks_after_five_failures(client: Client, alice):
    for _ in range(MAX_FAILURES):
        assert GENERIC_LOGIN_ERROR in login(client, password="wrong").content.decode()

    # Now even the right password is refused.
    response = login(client)
    assert response.status_code == 200
    assert RATE_LIMITED_ERROR in response.content.decode()
    assert "_auth_user_id" not in client.session


def test_rate_limit_is_per_ip_and_username(client: Client, alice, bob):
    for _ in range(MAX_FAILURES):
        login(client, password="wrong")

    # Another username from the same IP still works...
    assert login(client, "bob", "unused-pw-2").status_code == 302
    # ...and so does alice from another IP.
    other_ip = Client(REMOTE_ADDR="10.0.0.99")
    assert login(other_ip).status_code == 302


def test_username_case_does_not_bypass_the_limit(client: Client, alice):
    for _ in range(MAX_FAILURES):
        login(client, "ALICE", "wrong")
    assert RATE_LIMITED_ERROR in login(client).content.decode()


def test_successful_login_resets_the_counter(client: Client, alice):
    for _ in range(MAX_FAILURES - 1):
        login(client, password="wrong")
    assert login(client).status_code == 302
    client.logout()
    for _ in range(MAX_FAILURES - 1):
        login(client, password="wrong")
    assert login(client).status_code == 302


def test_login_redirect_rejects_other_sites(client: Client, alice):
    response = client.post(
        "/login/?next=https://evil.example/",
        {"username": "alice", "password": "unused-pw-1", "next": "https://evil.example/"},
    )
    assert response["Location"] == "/"


def test_logout_is_post_only(alice_client: Client):
    assert alice_client.get("/logout/").status_code == 405
    response = alice_client.post("/logout/")
    assert response.status_code == 302
    assert response["Location"] == "/login/"
    assert alice_client.get("/").status_code == 302
