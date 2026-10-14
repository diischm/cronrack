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
    "template", ["components/loader.html", "components/lamp.html", "components/chip.html"]
)
def test_components_render(template: str) -> None:
    html = render_to_string(template, {"status": "offline", "label": "Offline"})
    assert "{%" not in html
