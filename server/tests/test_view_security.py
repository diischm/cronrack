"""Access control on every URL: login, owner-only hosts (IDOR), CSRF, CSP, escaping."""

import pytest
from django.test import Client

from hosts.models import AgentToken, Host

from .sample import make_report

pytestmark = pytest.mark.django_db

# Every URL that takes a host ID, with the methods it accepts.
HOST_URLS = [
    ("get", "/hosts/{id}/"),
    ("get", "/hosts/{id}/settings/"),
    ("post", "/hosts/{id}/settings/"),
    ("get", "/api/v1/hosts/{id}/status.json"),
    ("post", "/hosts/{id}/collect/"),
    ("post", "/hosts/{id}/rotate/"),
    ("post", "/hosts/{id}/revoke/"),
    ("post", "/hosts/{id}/delete/"),
]

# Every page that needs a login (host URLs are filled in with a real host).
LOGIN_URLS = [
    ("get", "/"),
    ("get", "/api/v1/overview.json"),
    ("get", "/hosts/add/"),
    ("post", "/hosts/collect-all/"),
    ("get", "/activity/"),
    ("get", "/api/v1/activity.json"),
    ("get", "/settings/"),
    ("post", "/settings/test-alert/"),
    *HOST_URLS,
]

POST_URLS = [
    "/hosts/add/",
    "/hosts/collect-all/",
    "/settings/",
    "/settings/test-alert/",
    "/hosts/{id}/settings/",
    "/hosts/{id}/collect/",
    "/hosts/{id}/rotate/",
    "/hosts/{id}/revoke/",
    "/hosts/{id}/delete/",
    "/logout/",
]


@pytest.mark.parametrize(("method", "url"), HOST_URLS)
def test_other_users_host_is_404(client: Client, bob, host, method, url):
    """IDOR: bob is logged in, but the host belongs to alice."""
    AgentToken.issue(host)
    token_hash = AgentToken.objects.get(host=host).token_hash
    client.force_login(bob)

    data = {"name": "stolen", "confirm_name": host.name}
    response = getattr(client, method)(url.format(id=host.pk), data if method == "post" else None)

    assert response.status_code == 404
    host.refresh_from_db()
    assert host.name == "nas"  # not renamed, not deleted
    token = AgentToken.objects.get(host=host)
    assert token.token_hash == token_hash  # not rotated
    assert token.revoked is False


@pytest.mark.parametrize(("method", "url"), HOST_URLS)
def test_missing_host_is_404(alice_client: Client, method, url):
    response = getattr(alice_client, method)(url.format(id=999_999))
    assert response.status_code == 404


@pytest.mark.parametrize(("method", "url"), LOGIN_URLS)
def test_login_required(client: Client, host, method, url):
    response = getattr(client, method)(url.format(id=host.pk))
    assert response.status_code == 302
    assert response["Location"].startswith("/login/")


@pytest.mark.parametrize("url", POST_URLS)
def test_csrf_required_on_every_post(alice, host, url):
    client = Client(enforce_csrf_checks=True)
    client.force_login(alice)

    response = client.post(url.format(id=host.pk), {"confirm_name": "nas", "name": "x"})

    assert response.status_code == 403
    assert Host.objects.filter(pk=host.pk, name="nas").exists()


def test_login_post_requires_csrf(alice):
    client = Client(enforce_csrf_checks=True)
    response = client.post("/login/", {"username": "alice", "password": "unused-pw-1"})
    assert response.status_code == 403


def test_csrf_failure_page_renders(alice):
    client = Client(enforce_csrf_checks=True)
    response = client.post("/login/", {"username": "alice", "password": "x"})
    assert b"This form expired" in response.content


EXPECTED_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@pytest.mark.parametrize("url", ["/", "/login/", "/api/v1/overview.json", "/settings/"])
def test_csp_header_present(alice_client: Client, url):
    assert alice_client.get(url)["Content-Security-Policy"] == EXPECTED_CSP


def test_csp_header_on_anonymous_and_error_pages(client: Client, alice):
    assert client.get("/login/")["Content-Security-Policy"] == EXPECTED_CSP
    client.force_login(alice)
    assert client.get("/hosts/999999/")["Content-Security-Policy"] == EXPECTED_CSP


XSS = "<script>alert(1)</script>"
XSS_ATTR = '"><img src=x onerror=alert(1)>'


def test_hostile_report_data_renders_escaped(alice_client: Client, host):
    """Container and unit names come from the host and are attacker-controlled."""
    host.latest_report = make_report(
        containers=[
            {
                "name": XSS,
                "state": "restarting",
                "status": XSS_ATTR,
                "health": None,
                "exit_code": None,
            }
        ],
        failed_units=[XSS_ATTR],
        # The mount also lands in the disk meter's aria-label attribute.
        disks=[{"mount": XSS_ATTR, "percent": 90}],
    )
    host.status = "red"
    host.save()

    for url in ("/", f"/hosts/{host.pk}/"):
        html = alice_client.get(url).content.decode()
        assert XSS not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_host_name_in_activity_and_settings_is_escaped(alice_client: Client, host):
    # The form only allows [A-Za-z0-9_-], but the template must not rely on that.
    Host.objects.filter(pk=host.pk).update(name=XSS)
    from activity.models import StatusEvent

    StatusEvent.objects.create(host_id=host.pk, old_status="green", new_status="red")
    for url in ("/", "/activity/", f"/hosts/{host.pk}/settings/", f"/hosts/{host.pk}/"):
        html = alice_client.get(url).content.decode()
        assert XSS not in html, url
        assert "&lt;script&gt;" in html, url


def test_json_escapes_nothing_but_is_json(alice_client: Client, host):
    """JSON is data, not HTML: it must be served as application/json, never text/html."""
    response = alice_client.get("/api/v1/overview.json")
    assert response["Content-Type"] == "application/json"
