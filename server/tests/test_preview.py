import copy
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.template.loader import render_to_string
from django.test import Client, override_settings

from cronrack.dev_views import PREVIEW_CONTEXT

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
XSS_NAME = "<script>alert(1)</script>"
XSS_ISSUE = '"><img src=x onerror=alert(1)>'
# An HTML event-handler attribute such as onclick="..." (blocked by a strict CSP).
EVENT_HANDLER_ATTR = re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE)


class PageScanner(HTMLParser):
    """Collects what a strict Content Security Policy would block."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.inline_script_text = ""
        self.inline_styles: list[str] = []
        self.event_handlers: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, _value in attrs:
            if name == "style":
                self.inline_styles.append(tag)
            if name.startswith("on"):
                self.event_handlers.append(f"{tag} {name}")
        if tag == "script":
            self.scripts.append(dict(attrs))
            self._in_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.inline_script_text += data.strip()


def scan(html: str) -> PageScanner:
    scanner = PageScanner()
    scanner.feed(html)
    return scanner


@pytest.fixture
def client(client: Client, alice) -> Client:
    """Logged in: with no account, every page redirects to /setup/ (first run)."""
    client.force_login(alice)
    return client


@override_settings(DEBUG=False)
@pytest.mark.django_db
def test_preview_is_404_without_debug(client: Client) -> None:
    assert client.get("/dev/preview/").status_code == 404


@override_settings(DEBUG=False)
@pytest.mark.django_db
def test_styleguide_is_404_without_debug(client: Client) -> None:
    assert client.get("/dev/styleguide/").status_code == 404


@override_settings(DEBUG=True)
@pytest.mark.django_db
def test_styleguide_renders_with_debug_and_is_csp_clean(client: Client) -> None:
    response = client.get("/dev/styleguide/")

    assert response.status_code == 200
    html = response.content.decode()
    for component in ("btn--primary", "lamp--red", "lamp--amber", "lamp--green", "loader", "code"):
        assert component in html
    page = scan(html)
    assert all(script.get("src") for script in page.scripts)
    assert page.inline_script_text == ""
    assert page.inline_styles == []
    assert page.event_handlers == []


@override_settings(DEBUG=True)
@pytest.mark.django_db
def test_preview_renders_with_debug(client: Client) -> None:
    response = client.get("/dev/preview/")

    assert response.status_code == 200
    html = response.content.decode()
    assert "2 things need you" in html
    for name in ("homeserver", "pi-01", "nas"):
        assert name in html
    assert "While you were away" in html
    assert 'aria-current="page"' in html


@override_settings(DEBUG=True)
@pytest.mark.django_db
def test_preview_empty_state(client: Client) -> None:
    html = client.get("/dev/preview/?state=empty").content.decode()

    assert "No hosts yet" in html
    assert "Add your first host" in html


@override_settings(DEBUG=True)
@pytest.mark.django_db
def test_page_has_no_inline_scripts_styles_or_handlers(client: Client) -> None:
    page = scan(client.get("/dev/preview/").content.decode())

    assert page.scripts, "expected the external theme scripts"
    assert all(script.get("src") for script in page.scripts)
    assert page.inline_script_text == ""
    assert page.inline_styles == []
    assert page.event_handlers == []


def test_hostile_host_data_renders_escaped() -> None:
    context = copy.deepcopy(PREVIEW_CONTEXT)
    context["hosts"][0]["name"] = XSS_NAME
    context["hosts"][0]["issues"][0]["text"] = XSS_ISSUE
    context["activity"][0]["text"] = XSS_NAME

    html = render_to_string("hosts/overview.html", context)

    assert XSS_NAME not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in html
    page = scan(html)
    assert page.inline_script_text == ""
    assert page.event_handlers == []


def test_templates_follow_the_placeholder_and_csp_rules() -> None:
    templates = sorted(TEMPLATES_DIR.rglob("*.html"))
    assert templates
    for template in templates:
        text = template.read_text(encoding="utf-8")
        assert text.startswith("{# PLACEHOLDER — Apo owns the final version #}"), template
        assert "|safe" not in text, template
        assert "style=" not in text, template
        assert not EVENT_HANDLER_ATTR.search(text), template
        # Django's {# #} comments are single-line only; a multi-line one is printed as text.
        for line in text.splitlines():
            assert "{#" not in line or "#}" in line, f"{template}: use {{% comment %}}"


@pytest.mark.parametrize(
    "template", ["components/loader.html", "components/lamp.html", "components/status_icon.html"]
)
def test_components_render(template: str) -> None:
    html = render_to_string(template, {"status": "offline", "label": "Offline"})
    assert "{%" not in html


# --- every dev page, anonymously, and every URL under dev/ ----------------------------------------
#
# The dev pages need no login (they only show sample data), so they are checked as an
# anonymous visitor too. An account must exist, or every page redirects to /setup/.

DEV_PAGES = [
    "/dev/preview/",
    "/dev/preview/?state=xss",
    "/dev/preview/?state=empty",
    "/dev/preview/cluster/",
    *(
        f"/dev/preview/cluster/?state={state}"
        for state in ("broken", "xss", "truncated", "offline", "waiting")
    ),
    "/dev/styleguide/",
]
REAL_NAMES = ("realhost-7f3a", "realcluster-9c2b", "alice-real-user")


def dev_url_patterns() -> list[str]:
    """Every route under dev/ in the URLconf, as a path to request.

    A converter is filled with a value it accepts (1 for <int:...>, x otherwise), so a
    route with arguments is reached too instead of 404ing for the wrong reason.
    """
    from django.urls import URLPattern, get_resolver

    def fill(route: str) -> str:
        return re.sub(r"<(?:(\w+):)?\w+>", lambda m: "1" if m[1] == "int" else "x", route)

    paths = []
    for pattern in get_resolver().url_patterns:
        route = str(pattern.pattern)
        if route.startswith("dev/"):
            assert isinstance(pattern, URLPattern), f"include() under dev/ isn't walked: {route}"
            paths.append("/" + fill(route))
    return paths


@pytest.fixture
def anonymous(client: Client, django_user_model) -> Client:
    from clusters.models import Cluster
    from hosts.models import Host

    owner = django_user_model.objects.create_user(username=REAL_NAMES[2], password="unused-pw-9")
    Host.objects.create(owner=owner, name=REAL_NAMES[0])
    Cluster.objects.create(
        owner=owner, uid="08528ee9-428d-5167-8a8e-df19e635932f", name=REAL_NAMES[1]
    )
    return client


@override_settings(DEBUG=False)
@pytest.mark.django_db
@pytest.mark.parametrize("url", DEV_PAGES)
def test_dev_pages_are_404_for_anonymous_visitors_without_debug(anonymous: Client, url) -> None:
    assert anonymous.get(url).status_code == 404


@override_settings(DEBUG=True)
@pytest.mark.django_db
@pytest.mark.parametrize("url", DEV_PAGES)
def test_dev_pages_show_only_sample_data_to_anonymous_visitors(anonymous: Client, url) -> None:
    response = anonymous.get(url)

    assert response.status_code == 200
    html = response.content.decode()
    for name in REAL_NAMES:
        assert name not in html, name


def test_dev_url_patterns_are_found():
    # Not a vacuous walk: the three known dev pages are among the routes.
    assert {"/dev/preview/", "/dev/preview/cluster/", "/dev/styleguide/"} <= set(dev_url_patterns())


@override_settings(DEBUG=False)
@pytest.mark.django_db
@pytest.mark.parametrize("logged_in", [False, True])
def test_every_url_under_dev_is_404_without_debug(client: Client, alice, logged_in) -> None:
    """Covers dev pages added later automatically: each new route under dev/ must 404."""
    if logged_in:
        client.force_login(alice)
    for path in dev_url_patterns():
        assert client.get(path).status_code == 404, path
