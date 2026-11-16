"""The phone-install QR code (pwa/qr.py): the address rules and the code itself.

segno can make QR codes but not read them, so the "does it scan" check is a round trip:
the terminal text is read back into a grid of modules and compared with the grid segno
builds for exactly the address we meant to encode.
"""

import base64

import pytest
import segno
from django.test import override_settings

from pwa.qr import (
    install_qr_block,
    is_loopback_host,
    make_install_qr,
    public_install_url,
    render_terminal,
    validate_install_url,
)

BASES = (
    "https://cronrack.example.com",
    "https://cronrack.example.com/",
    "https://cronrack.home.lan:8443",
    "https://192.168.1.20",
)


def matrix_of(text: str) -> list[list[bool]]:
    """What segno itself builds for `text` with the parameters we use (True = dark)."""
    code = segno.make(text, error="m", micro=False, boost_error=False)
    return [[bool(cell) for cell in row] for row in code.matrix]


def read_terminal(text: str, border: int = 2) -> list[list[bool]]:
    """Turn the terminal drawing back into modules. Light is drawn as a block, dark as a space."""
    modules: list[list[bool]] = []
    for line in text.split("\n"):
        top = [ch in " ▄" for ch in line]  # top half is dark when it is not filled
        bottom = [ch in " ▀" for ch in line]
        modules.append(top)
        modules.append(bottom)
    size = len(modules[0]) - 2 * border
    rows = modules[border : border + size]
    return [row[border : border + size] for row in rows]


# --- is_loopback_host ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:8000",
        "app.localhost",
        "127.0.0.1",
        "127.5.6.7:80",
        "[::1]",
        "[::1]:8000",
        "localhost.",
        "app.localhost.:8000",
    ],
)
def test_loopback_hosts(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    ["cronrack.home.lan", "192.168.1.20", "10.0.0.1:8000", "[2001:db8::1]", "", "notlocalhost"],
)
def test_other_hosts_are_not_loopback(host):
    assert not is_loopback_host(host)


# --- validate_install_url -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://h/install",
        "https://h:8443/install",
        "https://cronrack.home.lan/install",
        "https://192.168.1.20:443/install",
        "https://[2001:db8::1]:8443/install",
    ],
)
def test_validate_accepts(url):
    assert validate_install_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://h/install",
        "https://user@h/install",
        "https://user:pw@h/install",
        "https://h/install?x=1",
        "https://h/install?",
        "https://h/install#frag",
        "https://h/other",
        "https://h/install/",
        "https://h/",
        "https://h",
        "https:///install",
        "https://h /install",
        "https://h/install\n",
        "https://h/install\x00",
        "https://h/install\t",
        "https://h/inställ",
        "ftp://h/install",
        "",
        "https://" + "a" * 200 + "/install",
        "https://a..b/install",
        "https://.-/install",
        "https://-h/install",
        "https://h:0/install",
        "https://h:99999/install",
        "https://[1:2:3]/install",
        "https://[::]:99999/install",
    ],
)
def test_validate_rejects(url):
    with pytest.raises(ValueError):
        validate_install_url(url)


def test_validate_length_limit_is_200():
    base = "https://"
    # A real name: parts of at most 63 characters between dots.
    room = 200 - len(base) - len("/install")
    host = ".".join(["a" * 60] * 3)
    host += "." + "a" * (room - len(host) - 1)
    ok = base + host + "/install"
    assert len(ok) == 200
    assert validate_install_url(ok) == ok
    with pytest.raises(ValueError):
        validate_install_url(ok.replace("https://", "https://a"))


# --- public_install_url -------------------------------------------------------------------


@override_settings(CRONRACK_PUBLIC_URL="https://cronrack.example.com", CRONRACK_DOMAIN="")
def test_explicit_url():
    assert public_install_url() == "https://cronrack.example.com/install"


@override_settings(CRONRACK_PUBLIC_URL="https://cronrack.example.com/", CRONRACK_DOMAIN="")
def test_explicit_url_with_trailing_slash():
    assert public_install_url() == "https://cronrack.example.com/install"


@override_settings(
    CRONRACK_PUBLIC_URL="http://cronrack.example.com", CRONRACK_DOMAIN="cronrack.home.lan"
)
def test_explicit_http_is_none_and_never_falls_back():
    assert public_install_url() is None


@override_settings(CRONRACK_PUBLIC_URL="", CRONRACK_DOMAIN="cronrack.home.lan")
def test_falls_back_to_the_domain():
    assert public_install_url() == "https://cronrack.home.lan/install"


@override_settings(CRONRACK_PUBLIC_URL="", CRONRACK_DOMAIN="localhost")
def test_loopback_domain_is_none():
    assert public_install_url() is None


@override_settings(CRONRACK_PUBLIC_URL="", CRONRACK_DOMAIN="")
def test_nothing_set_is_none():
    assert public_install_url() is None


@pytest.mark.parametrize(
    "value",
    [
        "https://h.example/path",
        "https://h.example/install",
        "https://h.example?x=1",
        "https://user@h.example",
        "https://h.example#x",
        "cronrack.example.com",
        "https://",
        "https://h .example",
    ],
)
def test_invalid_explicit_values_are_none(value):
    with override_settings(CRONRACK_PUBLIC_URL=value, CRONRACK_DOMAIN="cronrack.home.lan"):
        assert public_install_url() is None


# --- make_install_qr ----------------------------------------------------------------------


@pytest.mark.parametrize("base", BASES)
def test_url_is_the_base_plus_install(base):
    assert make_install_qr(base).url == base.rstrip("/") + "/install"


@pytest.mark.parametrize("base", BASES)
def test_svg_is_geometry_only(base):
    qr = make_install_qr(base)
    assert "<svg" in qr.svg
    for banned in ("<script", "href", "style=", "<?xml"):
        assert banned not in qr.svg
    assert qr.url not in qr.svg
    assert base.split("//")[1].split("/")[0] not in qr.svg
    assert "#fff" in qr.svg.lower()  # a real white ground and dark modules


def test_svg_has_a_quiet_zone():
    qr = make_install_qr(BASES[0])
    size = len(matrix_of(qr.url))
    # width is in pixels: (modules + 2 * border) * scale
    import re

    width = int(re.search(r'width="(\d+)"', qr.svg).group(1))
    assert width > size  # larger than the bare modules: there is a border


@pytest.mark.parametrize("base", BASES)
def test_terminal_shape(base):
    text = make_install_qr(base).terminal
    assert text
    assert "\x1b" not in text
    lines = text.split("\n")
    assert len({len(line) for line in lines}) == 1
    assert set(text) <= set("█▀▄ \n")


@pytest.mark.parametrize("base", BASES)
def test_terminal_round_trip(base):
    qr = make_install_qr(base)
    assert read_terminal(qr.terminal) == matrix_of(base.rstrip("/") + "/install")


def test_terminal_quiet_zone_is_light():
    lines = make_install_qr(BASES[0]).terminal.split("\n")
    assert set(lines[0]) == {"█"}
    assert all(line[:2] == "██" and line[-2:] == "██" for line in lines)


def test_render_terminal_pads_an_odd_number_of_rows_with_light():
    out = render_terminal([[True]], border=1)
    # 3 module rows (light, dark, light) + 1 padding row (light): two lines of 3 columns.
    assert out.split("\n") == ["█▀█", "███"]


def test_qr_is_regular_m_level():
    qr = make_install_qr(BASES[0])
    real = segno.make(qr.url, error="m", micro=False, boost_error=False)
    assert not real.is_micro
    assert real.error == "M"
    assert real.designator.endswith("-M")
    assert qr.version == real.version and qr.error == "M"


def test_data_uri_decodes_to_the_svg():
    qr = make_install_qr(BASES[0])
    prefix = "data:image/svg+xml;base64,"
    assert qr.svg_data_uri.startswith(prefix)
    decoded = base64.b64decode(qr.svg_data_uri[len(prefix) :]).decode("ascii")
    assert decoded == qr.svg
    for banned in ("<script", "href", qr.url):
        assert banned not in decoded


@pytest.mark.parametrize("base", BASES)
def test_payload_holds_no_secret_or_query(base):
    url = make_install_qr(base).url
    for word in ("token", "session", "csrf", "?"):
        assert word not in url


@pytest.mark.parametrize(
    "base",
    [
        "https://h.example?token=1",
        "https://h.example/?x=1",
        "https://h.example/app",
        "https://user@h.example",
        "https://user:pw@h.example",
        "http://h.example",
        "https://h.example#x",
        "https://h.example/install",
        "",
    ],
)
def test_bad_bases_are_rejected(base):
    with pytest.raises(ValueError):
        make_install_qr(base)


def test_install_qr_block_has_heading_qr_and_url():
    qr = make_install_qr(BASES[0])
    lines = install_qr_block(qr).split("\n")
    assert lines[0] == "Get CronRack on your phone: scan this with your phone camera"
    assert lines[1] == ""
    assert "\n".join(lines[2 : 2 + len(qr.terminal.split("\n"))]) == qr.terminal
    assert lines[-2] == ""
    assert lines[-1] == qr.url
    assert len(lines) == 2 + len(qr.terminal.split("\n")) + 2
