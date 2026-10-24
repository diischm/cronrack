import importlib
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.urls import clear_url_caches

import cronrack.urls
from accounts.forms import (
    GENERIC_LOGIN_ERROR,
    RATE_LIMITED_ERROR,
    WRONG_SETUP_CODE_ERROR,
    SetupForm,
)
from accounts.models import SetupCode
from accounts.ratelimit import (
    MAX_FAILURES,
    MAX_FAILURES_PER_IP,
    MAX_SETUP_CODE_FAILURES,
    check_login_rate,
    record_login_failure,
)
from accounts.setup_code import new_setup_code, setup_code_matches
from accounts.views import create_first_admin
from prefs.models import Prefs

pytestmark = pytest.mark.django_db

GOOD_PASSWORD = "correct-horse-battery-9"
SERVER_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def setup_code():
    """The code `manage.py startup` prints on a fresh install."""
    return new_setup_code()


def setup_data(code, username="dimi"):
    return {
        "setup_code": code,
        "username": username,
        "password1": GOOD_PASSWORD,
        "password2": GOOD_PASSWORD,
    }


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


def test_setup_creates_admin_and_logs_in(client: Client, setup_code):
    response = client.post("/setup/", setup_data(setup_code))

    assert response.status_code == 302
    assert response["Location"] == "/"
    user = get_user_model().objects.get()
    assert user.username == "dimi"
    assert user.is_superuser and user.is_staff
    assert user.check_password(GOOD_PASSWORD)
    assert Prefs.objects.filter(owner=user).exists()
    assert client.get("/").status_code == 200  # logged in
    # The code is used up: only its hash was stored, and that is gone now.
    assert not SetupCode.objects.exists()


def test_setup_rejects_weak_or_mismatched_passwords(client: Client, setup_code):
    data = setup_data(setup_code) | {"password1": "123", "password2": "123"}
    response = client.post("/setup/", data)
    assert response.status_code == 200
    assert not get_user_model().objects.exists()

    client.post("/setup/", setup_data(setup_code) | {"password2": "different-9x"})
    assert not get_user_model().objects.exists()


# --- setup code -------------------------------------------------------------------


def test_setup_code_format_and_only_hash_stored(setup_code):
    assert len(setup_code) == 19  # XXXX-XXXX-XXXX-XXXX
    stored = SetupCode.objects.get()
    assert setup_code not in stored.code_hash
    assert setup_code.replace("-", "") not in stored.code_hash
    assert len(stored.code_hash) == 64


def test_setup_without_the_code_fails(client: Client, setup_code):
    data = setup_data(setup_code)
    del data["setup_code"]

    response = client.post("/setup/", data)

    assert response.status_code == 200
    assert not get_user_model().objects.exists()


def test_setup_with_a_wrong_code_fails(client: Client, setup_code):
    response = client.post("/setup/", setup_data("AAAA-BBBB-CCCC-DDDD"))

    assert response.status_code == 200
    assert WRONG_SETUP_CODE_ERROR in response.content.decode()
    assert not get_user_model().objects.exists()


def test_setup_code_ignores_case_dashes_and_spaces(client: Client, setup_code):
    typed = " " + setup_code.lower().replace("-", " ") + " "
    assert client.post("/setup/", setup_data(typed)).status_code == 302


def test_wrong_setup_codes_are_rate_limited(client: Client, setup_code):
    for _ in range(MAX_SETUP_CODE_FAILURES):
        client.post("/setup/", setup_data("AAAA-BBBB-CCCC-DDDD"))

    # Now even the right code is refused...
    response = client.post("/setup/", setup_data(setup_code))
    assert response.status_code == 200
    assert RATE_LIMITED_ERROR in response.content.decode()
    assert not get_user_model().objects.exists()
    # ...but only for that IP.
    other_ip = Client(REMOTE_ADDR="10.0.0.99")
    assert other_ip.post("/setup/", setup_data(setup_code)).status_code == 302


def test_a_new_start_replaces_the_code(setup_code):
    newer = new_setup_code()
    assert newer != setup_code
    assert setup_code_matches(newer)
    assert not setup_code_matches(setup_code)
    assert SetupCode.objects.count() == 1


def test_no_setup_code_once_a_user_exists(alice):
    assert new_setup_code() is None
    assert not SetupCode.objects.exists()


def test_startup_command_prints_the_code(capsys):
    call_command("startup")
    out = capsys.readouterr().out
    code = out.split("setup code: ", 1)[1].split()[0]
    assert setup_code_matches(code)


def test_startup_command_prints_nothing_once_a_user_exists(capsys, alice):
    call_command("startup")
    assert "setup code" not in capsys.readouterr().out


def test_setup_page_makes_a_code_if_startup_never_ran(client: Client):
    """Running without docker-entrypoint.sh (e.g. uvicorn on a dev machine)."""
    assert client.get("/setup/").status_code == 200
    assert SetupCode.objects.count() == 1


def test_two_racing_setups_create_only_one_admin(setup_code):
    """Both requests passed validation while no account existed; only one may win."""
    rf = RequestFactory()
    forms = [
        SetupForm(setup_data(setup_code, name), request=rf.post("/setup/"))
        for name in ("first", "second")
    ]
    assert all(form.is_valid() for form in forms)

    assert create_first_admin(forms[0]) is not None
    assert create_first_admin(forms[1]) is None
    assert list(get_user_model().objects.values_list("username", flat=True)) == ["first"]


@pytest.mark.django_db(transaction=True)
def test_concurrent_setup_requests_create_only_one_admin(setup_code):
    """The same race with real threads, each with its own database connection."""
    start = threading.Barrier(4)
    statuses = []

    def submit(name):
        try:
            start.wait()
            statuses.append(Client().post("/setup/", setup_data(setup_code, name)).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=submit, args=(f"user{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert get_user_model().objects.count() == 1
    assert statuses.count(302) == 1


def test_setup_is_404_once_a_user_exists(client: Client, alice):
    assert client.get("/setup/").status_code == 404
    response = client.post("/setup/", setup_data("AAAA-BBBB-CCCC-DDDD", "evil"))
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


# --- the limit lives in the authentication backend ----------------------------------


def test_lockout_survives_hundreds_of_junk_usernames(client: Client, alice):
    """Junk entries must not push alice's lockout out of the cache (MAX_ENTRIES)."""
    for _ in range(MAX_FAILURES):
        login(client, password="wrong")

    rf = RequestFactory()
    # 400 made-up usernames from 400 other IPs: what the backend records for each.
    for i in range(400):
        record_login_failure(rf.post("/login/", REMOTE_ADDR=f"10.9.{i // 250}.{i % 250}"), f"x{i}")

    response = login(client)
    assert RATE_LIMITED_ERROR in response.content.decode()
    assert "_auth_user_id" not in client.session


def test_per_ip_limit_across_usernames(client: Client, alice):
    request = RequestFactory().post("/login/")  # same REMOTE_ADDR as the test client
    for i in range(MAX_FAILURES_PER_IP):
        assert check_login_rate(request, f"junk{i}")
        record_login_failure(request, f"junk{i}")

    # alice never failed from this IP, but the IP itself is blocked now.
    assert not check_login_rate(request, "alice")
    assert RATE_LIMITED_ERROR in login(client).content.decode()
    # Another IP is unaffected.
    assert login(Client(REMOTE_ADDR="10.0.0.99")).status_code == 302


def test_any_authenticate_call_is_rate_limited(alice):
    """Not just LoginForm: authenticate() itself refuses once the limit is hit."""
    from django.contrib.auth import authenticate

    request = RequestFactory().post("/anything/")
    for _ in range(MAX_FAILURES):
        assert authenticate(request, username="alice", password="wrong") is None

    assert authenticate(request, username="alice", password="unused-pw-1") is None


# --- Django admin -----------------------------------------------------------------


def reload_urls():
    importlib.reload(cronrack.urls)
    clear_url_caches()


@contextmanager
def admin_setting(enabled):
    """Rebuild the URL patterns with CRONRACK_ENABLE_ADMIN set (urls.py reads it on import)."""
    try:
        with override_settings(CRONRACK_ENABLE_ADMIN=enabled):
            reload_urls()
            yield
    finally:
        reload_urls()  # back to the patterns for the real setting


def test_admin_is_404_when_disabled(client: Client, alice):
    with admin_setting(False):
        assert client.get("/admin/").status_code == 404
        assert client.get("/admin/login/").status_code == 404


def test_admin_login_is_rate_limited_when_enabled(client: Client, alice):
    alice.is_staff = True
    alice.save()
    with admin_setting(True):
        assert client.get("/admin/login/").status_code == 200
        for _ in range(MAX_FAILURES):
            client.post("/admin/login/", {"username": "alice", "password": "wrong"})

        # Now even the right password is refused there too.
        response = client.post("/admin/login/", {"username": "alice", "password": "unused-pw-1"})
        assert response.status_code == 200
        assert "_auth_user_id" not in client.session


def test_admin_failures_count_for_the_normal_login(client: Client, alice):
    with admin_setting(True):
        for _ in range(MAX_FAILURES):
            client.post("/admin/login/", {"username": "alice", "password": "wrong"})
    assert RATE_LIMITED_ERROR in login(client).content.decode()


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"DEBUG": "1"}, "True"),
        ({"DEBUG": "0"}, "False"),
        ({"DEBUG": "0", "CRONRACK_ENABLE_ADMIN": "1"}, "True"),
        ({"DEBUG": "1", "CRONRACK_ENABLE_ADMIN": "0"}, "False"),
    ],
)
def test_admin_setting_defaults_to_debug(env, expected):
    """Settings are read once per process, so load them fresh in a child process."""
    base = {k: v for k, v in os.environ.items() if k not in ("DEBUG", "CRONRACK_ENABLE_ADMIN")}
    result = subprocess.run(
        [sys.executable, "-c", "import cronrack.settings as s; print(s.CRONRACK_ENABLE_ADMIN)"],
        cwd=SERVER_DIR,
        env=base | {"SECRET_KEY": "test-only"} | env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == expected
