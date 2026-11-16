"""`manage.py phone_qr` and the first-start QR hint printed by `manage.py startup`."""

import io

import pytest
from django.core.management import call_command
from django.test import override_settings

from pwa.qr import HEADING

from .test_install_qr import matrix_of, read_terminal

BLOCK_CHARS = set("█▀▄")
ALLOWED = ["cronrack.home.lan", "localhost"]


def run(command: str = "phone_qr") -> str:
    out = io.StringIO()
    call_command(command, stdout=out)
    return out.getvalue()


def qr_part(output: str, url: str) -> str:
    """The drawing between the heading and the address line."""
    lines = output.split("\n")
    start = lines.index(HEADING) + 2
    end = lines.index(url) - 1
    return "\n".join(lines[start:end])


# --- phone_qr ----------------------------------------------------------------------------


@override_settings(CRONRACK_PUBLIC_URL="", CRONRACK_DOMAIN="")
def test_nothing_set_prints_one_line_and_no_qr():
    output = run()
    lines = [line for line in output.split("\n") if line.strip()]
    assert len(lines) == 1
    assert "CRONRACK_PUBLIC_URL" in lines[0]
    assert not (set(output) & BLOCK_CHARS)


@override_settings(CRONRACK_PUBLIC_URL="", CRONRACK_DOMAIN="localhost")
def test_loopback_domain_prints_the_message():
    output = run()
    assert "CRONRACK_PUBLIC_URL" in output
    assert not (set(output) & BLOCK_CHARS)


@override_settings(
    CRONRACK_PUBLIC_URL="http://cronrack.home.lan", CRONRACK_DOMAIN="cronrack.home.lan"
)
def test_http_value_is_refused_and_not_echoed():
    output = run()
    assert "CRONRACK_PUBLIC_URL" in output
    assert not (set(output) & BLOCK_CHARS)
    assert "http://cronrack.home.lan" not in output
    assert "cronrack.home.lan" not in output


@override_settings(CRONRACK_PUBLIC_URL="https://user:pw-secret@h.example/x?y=1", CRONRACK_DOMAIN="")
def test_a_bad_value_is_never_echoed():
    output = run()
    assert "pw-secret" not in output and "h.example" not in output
    assert not (set(output) & BLOCK_CHARS)


@override_settings(
    CRONRACK_PUBLIC_URL="https://cronrack.home.lan", CRONRACK_DOMAIN="", ALLOWED_HOSTS=ALLOWED
)
def test_valid_https_prints_heading_qr_and_address():
    url = "https://cronrack.home.lan/install"
    output = run()
    lines = output.rstrip("\n").split("\n")
    assert lines[0] == HEADING
    assert lines[-1] == url
    assert read_terminal(qr_part(output, url)) == matrix_of(url)
    assert "ALLOWED_HOSTS" not in output  # allowed: no warning


@override_settings(
    CRONRACK_PUBLIC_URL="", CRONRACK_DOMAIN="cronrack.home.lan", ALLOWED_HOSTS=ALLOWED
)
def test_falls_back_to_the_domain():
    url = "https://cronrack.home.lan/install"
    output = run()
    assert read_terminal(qr_part(output, url)) == matrix_of(url)


@override_settings(
    CRONRACK_PUBLIC_URL="https://cronrack.home.lan:8443", CRONRACK_DOMAIN="", ALLOWED_HOSTS=ALLOWED
)
def test_a_port_is_kept_and_the_host_part_is_what_gets_checked():
    url = "https://cronrack.home.lan:8443/install"
    output = run()
    assert output.rstrip("\n").split("\n")[-1] == url
    assert "ALLOWED_HOSTS" not in output


@override_settings(
    CRONRACK_PUBLIC_URL="https://other.example.com", CRONRACK_DOMAIN="", ALLOWED_HOSTS=ALLOWED
)
def test_host_not_in_allowed_hosts_adds_a_warning_line():
    output = run()
    warnings = [line for line in output.split("\n") if "ALLOWED_HOSTS" in line]
    assert len(warnings) == 1
    assert "CSRF_TRUSTED_ORIGINS" in warnings[0]
    assert "https://other.example.com/install" in output  # the QR is still printed


@pytest.mark.parametrize(
    ("public", "warned"),
    [("https://app.localhost", False), ("https://cronrack.home.lan", True)],
)
def test_debug_default_allowed_hosts(public, warned):
    # DEBUG=1 with ALLOWED_HOSTS empty: Django itself answers .localhost, 127.0.0.1, [::1].
    with override_settings(
        DEBUG=True, ALLOWED_HOSTS=[], CRONRACK_PUBLIC_URL=public, CRONRACK_DOMAIN=""
    ):
        output = run()
    assert ("ALLOWED_HOSTS" in output) is warned


@override_settings(
    ALLOWED_HOSTS=["*"], CRONRACK_PUBLIC_URL="https://anything.example", CRONRACK_DOMAIN=""
)
def test_wildcard_allowed_hosts_gives_no_warning():
    assert "ALLOWED_HOSTS" not in run()


# --- startup hint ------------------------------------------------------------------------


@pytest.mark.django_db
@override_settings(CRONRACK_PUBLIC_URL="https://cronrack.home.lan", CRONRACK_DOMAIN="")
def test_startup_prints_the_code_then_the_qr():
    url = "https://cronrack.home.lan/install"
    output = run("startup")
    code = output.split("setup code: ", 1)[1].split()[0]
    assert output.index("setup code") < output.index(HEADING)
    block = output[output.index(HEADING) :].rstrip("\n")
    assert code not in block
    assert read_terminal(qr_part(block, url)) == matrix_of(url)
    assert block.split("\n")[-1] == url
    assert output.count(code) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("public", "domain"),
    [("", ""), ("http://cronrack.home.lan", ""), ("", "localhost"), ("https://x.example/p", "")],
)
def test_startup_is_silent_without_a_usable_address(public, domain):
    with override_settings(CRONRACK_PUBLIC_URL=public, CRONRACK_DOMAIN=domain):
        output = run("startup")
    assert "setup code" in output
    assert HEADING not in output
    assert "CRONRACK_PUBLIC_URL" not in output
    assert not (set(output) & BLOCK_CHARS)


@pytest.mark.django_db
@override_settings(CRONRACK_PUBLIC_URL="https://cronrack.home.lan", CRONRACK_DOMAIN="")
def test_startup_prints_no_qr_once_an_account_exists(alice):
    output = run("startup")
    assert output == ""


@pytest.mark.django_db
@override_settings(CRONRACK_PUBLIC_URL="https://cronrack.home.lan", CRONRACK_DOMAIN="")
def test_startup_survives_a_qr_failure(monkeypatch):
    # The web container runs `startup` before it serves anything: a QR problem must never
    # stop it. The setup code is still printed.
    def broken(_base):
        raise RuntimeError("encoder broke")

    monkeypatch.setattr("hosts.management.commands.startup.make_install_qr", broken)
    output = run("startup")
    assert "setup code" in output
    assert HEADING not in output
