"""Access control on every URL: login, owner-only hosts (IDOR), CSRF, CSP, escaping."""

import re
from html.parser import HTMLParser

import pytest
from django.test import Client
from django.utils import timezone

from clusters.models import Cluster
from hosts.models import AgentToken, Host

from .sample import make_cluster, make_kube_report, make_report

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

# The same for URLs that take a cluster ID.
CLUSTER_URLS = [
    ("get", "/clusters/{id}/"),
    ("get", "/clusters/{id}/settings/"),
    ("post", "/clusters/{id}/settings/"),
    ("post", "/clusters/{id}/delete/"),
    ("get", "/api/v1/clusters/{id}/detail.json"),
]

# Every page that needs a login (host URLs are filled in with a real host).
LOGIN_URLS = [
    ("get", "/"),
    ("get", "/api/v1/overview.json"),
    ("get", "/hosts/add/"),
    ("post", "/hosts/collect-all/"),
    ("get", "/hosts/example/"),
    ("get", "/activity/"),
    ("get", "/api/v1/activity.json"),
    ("get", "/settings/"),
    ("post", "/settings/test-alert/"),
    ("get", "/clusters/"),
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
    "/clusters/{id}/settings/",
    "/clusters/{id}/delete/",
    "/logout/",
]


@pytest.mark.parametrize(("method", "url"), HOST_URLS)
def test_other_users_host_is_404(client: Client, bob, host, method, url):
    """IDOR: bob is logged in, but the host belongs to alice."""
    AgentToken.issue(host)
    token_hash = AgentToken.objects.get(host=host).token_hash
    client.force_login(bob)

    # Every confirmation is sent, so the 404 can't come from a missing confirm field.
    data = {"name": "stolen", "confirm_name": host.name, "confirm": "yes"}
    response = getattr(client, method)(url.format(id=host.pk), data if method == "post" else None)

    assert response.status_code == 404
    host.refresh_from_db()
    assert host.name == "nas"  # not renamed, not deleted
    token = AgentToken.objects.get(host=host)
    assert token.token_hash == token_hash  # not rotated
    assert token.revoked is False


@pytest.mark.parametrize(("method", "url"), CLUSTER_URLS)
def test_other_users_cluster_is_404(client: Client, bob, cluster, method, url):
    """IDOR: bob is logged in, but the cluster belongs to alice."""
    client.force_login(bob)
    data = {"name": "stolen", "confirm_name": cluster.name}

    response = getattr(client, method)(
        url.format(id=cluster.pk), data if method == "post" else None
    )

    assert response.status_code == 404
    cluster.refresh_from_db()  # not renamed, not deleted
    assert cluster.name == "lab"


@pytest.mark.parametrize(("method", "url"), CLUSTER_URLS)
def test_missing_cluster_is_404(alice_client: Client, method, url):
    assert getattr(alice_client, method)(url.format(id=999_999)).status_code == 404


@pytest.mark.parametrize(("method", "url"), CLUSTER_URLS)
def test_cluster_urls_need_a_login(client: Client, cluster, method, url):
    response = getattr(client, method)(url.format(id=cluster.pk))
    assert response.status_code == 302
    assert response["Location"].startswith("/login/")


@pytest.mark.parametrize(("method", "url"), CLUSTER_URLS)
def test_cluster_urls_carry_the_csp_header(alice_client: Client, cluster, method, url):
    if method == "post":
        pytest.skip("covered by the GET of the same page")
    assert alice_client.get(url.format(id=cluster.pk))["Content-Security-Policy"] == EXPECTED_CSP


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


@pytest.mark.parametrize(
    ("url", "data"),
    [
        ("/clusters/{id}/settings/", {"name": "renamed"}),
        ("/clusters/{id}/delete/", {"confirm_name": "lab"}),
    ],
)
def test_cluster_posts_need_csrf(alice, cluster, url, data):
    """On a real cluster of the logged-in owner: no token, no change."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(alice)

    response = client.post(url.format(id=cluster.pk), data)

    assert response.status_code == 403
    assert Cluster.objects.filter(pk=cluster.pk, name="lab").exists()


def test_cluster_rename_with_the_pages_token_works(alice, cluster):
    """The control for the test above: the 403 really comes from the missing token."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(alice)
    page = client.get(f"/clusters/{cluster.pk}/settings/").content.decode()
    tokens = re.findall(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
    assert len(tokens) == 2  # one in each form

    response = client.post(
        f"/clusters/{cluster.pk}/settings/", {"name": "renamed", "csrfmiddlewaretoken": tokens[0]}
    )

    assert response.status_code == 302
    assert Cluster.objects.get(pk=cluster.pk).name == "renamed"


def test_cluster_delete_by_get_is_refused(alice_client: Client, cluster):
    assert alice_client.get(f"/clusters/{cluster.pk}/delete/").status_code == 405
    assert Cluster.objects.filter(pk=cluster.pk).exists()


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


@pytest.mark.parametrize(
    "url", ["/", "/login/", "/api/v1/overview.json", "/settings/", "/clusters/"]
)
def test_csp_header_present(alice_client: Client, url):
    assert alice_client.get(url)["Content-Security-Policy"] == EXPECTED_CSP


def test_csp_header_on_anonymous_and_error_pages(client: Client, alice):
    assert client.get("/login/")["Content-Security-Policy"] == EXPECTED_CSP
    client.force_login(alice)
    assert client.get("/hosts/999999/")["Content-Security-Policy"] == EXPECTED_CSP


EXPECTED_HEADERS = {
    # cronrack.middleware.SecurityHeadersMiddleware
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    # Django's SecurityMiddleware defaults (not set again in our middleware)
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    # XFrameOptionsMiddleware
    "X-Frame-Options": "DENY",
}


@pytest.mark.parametrize("url", ["/", "/login/", "/api/v1/overview.json", "/hosts/999999/"])
def test_security_headers_present(alice_client: Client, url):
    response = alice_client.get(url)
    for name, value in EXPECTED_HEADERS.items():
        assert response[name] == value, name


def test_security_headers_on_anonymous_pages(client: Client, alice):
    response = client.get("/login/")
    for name, value in EXPECTED_HEADERS.items():
        assert response[name] == value, name


@pytest.mark.parametrize("url", ["/", "/api/v1/overview.json", "/activity/", "/hosts/999999/"])
def test_pages_must_be_revalidated_by_default(alice_client: Client, url):
    # Caddy used to add this; the app does it now, behind any proxy.
    assert alice_client.get(url)["Cache-Control"] == "no-cache"


def test_a_views_own_cache_control_is_kept(client: Client):
    # /healthz uses never_cache, which is stricter than the default.
    cache_control = client.get("/healthz")["Cache-Control"]
    assert "no-store" in cache_control
    assert cache_control != "no-cache"


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


# --- links are never built from agent data -------------------------------------------------------
#
# Every href, src and action (and data-*-url) is a fixed path, a static file, or an ID-based
# URL from reverse(). Nothing an agent or a cluster sends may become a link target.

URL_ATTRIBUTES = ("href", "src", "action", "formaction")
ALLOWED_URL = re.compile(
    r"/(clusters|hosts)/\d+/([a-z-]+/)?"  # detail, settings/, delete/, collect/ ...
    r"|/api/v1/(clusters|hosts)/\d+/[a-z]+\.json"
    r"|/static/cronrack/[a-z0-9_/-]+\.(css|js|png|svg)"
    r"|/|/activity/|/clusters/|/settings/|/hosts/add/|/hosts/collect-all/"
    r"|/api/v1/overview\.json"
)
EVIL_URLS = [
    "javascript:alert(1)",
    "https://evil.example/a",
    "//evil.example/b",
    "data:text/html,<script>alert(1)</script>",
    '"><a href="https://evil.example/c">x</a>',
    "/clusters/1/../../evil",
]


class _UrlCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in URL_ATTRIBUTES or (name.startswith("data-") and name.endswith("url")):
                self.urls.append((tag, name, value or ""))


def _urls(html: str) -> list[tuple[str, str, str]]:
    collector = _UrlCollector()
    collector.feed(html)
    return collector.urls


def _evil(i: int) -> str:
    return EVIL_URLS[i % len(EVIL_URLS)]


def hostile_cluster_snapshot() -> dict:
    """Every string the agent sends about the cluster, set to something link-like."""
    base = make_cluster()
    node = {**base["nodes"][0], "name": _evil(0)}
    return {
        **base,
        "sa": _evil(1),
        "nodes": [base["nodes"][0], node],
        "problem_pods": [
            {
                "ns": _evil(2),
                "name": _evil(3),
                "node": _evil(4),
                "phase": "Running",
                "ready": [0, 1],
                "restarts": 9,
                "reason": "CrashLoopBackOff",
                "last": "OOMKilled",
                "last_age_s": 60,
                "age_s": 600,
                "owner": _evil(5),
            }
        ],
        "ok_pods": {_evil(0): [[_evil(1), _evil(2), 0, 5]]},
        "workloads": [
            {
                **base["workloads"][0],
                "ns": _evil(3),
                "name": _evil(4),
                "images": [_evil(5), "evil.example/latest"],
                "helm": _evil(0),
                "available": 0,
            }
        ],
        "helm": [
            {
                "name": _evil(1),
                "ns": _evil(2),
                "chart": _evil(3),
                "app_version": _evil(4),
                "source": "labels",
            }
        ],
        "argo": [
            {
                "name": _evil(5),
                "ns": _evil(0),
                "project": _evil(1),
                "sync": "OutOfSync",
                "health": "Degraded",
                "rev": "abc1234",
                "op": "Failed",
                "synced_at": None,
            }
        ],
        "pvcs": [
            {
                "ns": _evil(2),
                "name": _evil(3),
                "phase": "Pending",
                "class": _evil(4),
                "size": "1Gi",
                "age_s": 9000,
            }
        ],
        "events": [
            {
                "ns": _evil(5),
                "obj": _evil(0),
                "reason": "BackOff",
                "msg": _evil(1),
                "count": 2,
                "age_s": 5,
            }
        ],
        "denied": [{"g": _evil(2), "r": _evil(3), "v": "list"}],
        "cut": [],
        "incomplete": ["pods"],
    }


def test_no_agent_string_ever_becomes_a_link(alice_client: Client, alice, host, cluster):
    # Straight into the database, past the serializers: the templates must hold on their own.
    now = timezone.now()
    Cluster.objects.filter(pk=cluster.pk).update(
        name=_evil(2),
        status="red",
        latest_snapshot=hostile_cluster_snapshot(),
        snapshot_received_at=now,
        last_seen_at=now,
        reporter=host,
    )
    host.name = _evil(3)
    host.cluster = cluster
    host.status = "red"
    host.latest_report = make_kube_report(
        cluster={"uid": None, "truncated": False},
        name=_evil(4),
        role="server",
    )
    host.latest_report["hostname"] = _evil(5)
    host.latest_report["containers"] = [
        {
            "name": _evil(0),
            "state": "restarting",
            "status": _evil(1),
            "health": None,
            "exit_code": None,
        }
    ]
    host.save()

    pages = [
        "/",
        "/clusters/",
        f"/clusters/{cluster.pk}/",
        f"/clusters/{cluster.pk}/settings/",
        f"/hosts/{host.pk}/",
        f"/hosts/{host.pk}/settings/",
        "/activity/",
    ]
    for page in pages:
        response = alice_client.get(page)
        assert response.status_code == 200, page
        html = response.content.decode()
        urls = _urls(html)
        assert urls, page
        for tag, attribute, value in urls:
            assert ALLOWED_URL.fullmatch(value), f"{page}: <{tag} {attribute}={value!r}>"
        assert "evil.example" not in " ".join(v for _, _, v in urls), page

    # Not a vacuous pass: the hostile strings did reach the pages, as text.
    detail = alice_client.get(f"/clusters/{cluster.pk}/").content.decode()
    assert "javascript:alert(1)" in detail
    assert "https://evil.example/a" in detail
    assert "&quot;&gt;&lt;a href=&quot;https://evil.example/c&quot;&gt;" in detail
