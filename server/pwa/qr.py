"""The phone-install QR code: which address it holds, and how it is drawn.

The code only ever holds ONE thing: the public https address of this server ending in
/install (for example https://cronrack.example.com/install). That page is public and shows
the "Install app" button, so the code carries no login, token, session or setup code.

- is_loopback_host / validate_install_url: strict checks on the address. An address that
  fails is rejected, never "cleaned up", so nothing surprising can end up in the code.
- public_install_url: the address the terminal QR uses, from CRONRACK_PUBLIC_URL (or, when
  that is not set, https://<CRONRACK_DOMAIN>).
- make_install_qr: builds the code once and gives it as an SVG (the Settings page) and as
  text blocks (the terminal and the start-up log).
"""

import base64
import io
import ipaddress
import re
from typing import NamedTuple
from urllib.parse import urlsplit

import segno
from django.conf import settings

INSTALL_PATH = "/install"
MAX_URL_LENGTH = 200
# Border (quiet zone) around the code, in modules. QR readers need at least 2 to be safe.
BORDER = 2
HEADING = "Get CronRack on your phone: scan this with your phone camera"

# Letters, digits, dots and dashes, or a bracketed IPv6 address; then an optional :port.
_NETLOC = re.compile(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?")
# One part of a name between dots: no empty parts, no dash at either end.
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _split_host(host: str) -> str:
    """The host name without its :port (an IPv6 address keeps its brackets)."""
    if host.startswith("["):
        return host[: host.find("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def is_loopback_host(host: str) -> bool:
    """True for names that only mean "this machine": localhost, *.localhost, 127.x, [::1]."""
    # A trailing dot ("localhost.") is the same name; Django accepts it in the Host header.
    name = _split_host(host.strip().lower()).strip("[]").removesuffix(".")
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _is_plain_netloc(netloc: str) -> bool:
    """A real host name or IP address, and a port from 1 to 65535 if there is one."""
    if not _NETLOC.fullmatch(netloc):
        return False
    host = _split_host(netloc)
    port = netloc[len(host) + 1 :]
    if port and not 1 <= int(port) <= 65535:
        return False
    if host.startswith("["):
        try:
            return ipaddress.ip_address(host[1:-1]).version == 6
        except ValueError:
            return False
    return all(_LABEL.fullmatch(label) for label in host.lower().split("."))


def validate_install_url(url: str) -> str:
    """Return `url` unchanged if it is a plain https .../install address, else ValueError."""
    if not isinstance(url, str) or not 0 < len(url) <= MAX_URL_LENGTH:
        raise ValueError("install URL has a bad length")
    # Printable ASCII only: no spaces, control characters or look-alike letters.
    if not all(0x21 <= ord(ch) <= 0x7E for ch in url):
        raise ValueError("install URL has unusual characters")
    if "?" in url or "#" in url:
        raise ValueError("install URL must have no query or fragment")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("install URL must be https")
    if not parts.netloc or "@" in parts.netloc or not _is_plain_netloc(parts.netloc):
        raise ValueError("install URL needs a plain host (and optional port)")
    if parts.path != INSTALL_PATH:
        raise ValueError("install URL must end in /install")
    return url


def public_install_url() -> str | None:
    """The https .../install address from the settings, or None. Never raises.

    CRONRACK_PUBLIC_URL wins. Only when it is not set at all, https://<CRONRACK_DOMAIN> is
    used (unless that name only means this machine). A value that is set but invalid gives
    None: it is not quietly replaced by the domain.
    """
    explicit = (getattr(settings, "CRONRACK_PUBLIC_URL", "") or "").strip()
    if explicit:
        base = explicit
    else:
        domain = (getattr(settings, "CRONRACK_DOMAIN", "") or "").strip()
        if not domain or is_loopback_host(domain):
            return None
        base = f"https://{domain}"
    try:
        return validate_install_url(base.rstrip("/") + INSTALL_PATH)
    except ValueError:
        return None


def render_terminal(matrix: list[list[bool]], border: int = BORDER) -> str:
    """Draw modules as text, two module rows per line, with no colour codes.

    `matrix` holds True for dark modules. LIGHT modules (and the border) are drawn as
    blocks and dark ones as spaces, so the code scans on a dark terminal and in
    `docker compose logs`. A light terminal shows it inverted (see docs/install-app.md).
    """
    width = len(matrix[0]) if matrix else 0
    light = [True] * (width + 2 * border)
    rows = [light] * border
    rows += [[True] * border + [not cell for cell in row] + [True] * border for row in matrix]
    rows += [light] * border
    # In `rows`, True means light. An odd count gets one more light row at the bottom.
    if len(rows) % 2:
        rows.append(light)
    glyph = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}
    return "\n".join(
        "".join(glyph[(top, bottom)] for top, bottom in zip(upper, lower, strict=True))
        for upper, lower in zip(rows[0::2], rows[1::2], strict=True)
    )


class InstallQR(NamedTuple):
    """One finished code: the address, an SVG and a terminal drawing of it."""

    url: str
    svg: str
    terminal: str
    version: int  # the QR size step (1 to 40)
    error: str  # the error correction level; always "M"

    @property
    def svg_data_uri(self) -> str:
        """The SVG as a data: address an <img> can show (the CSP allows img-src data:)."""
        encoded = base64.b64encode(self.svg.encode("ascii")).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"


def _build(text: str) -> segno.QRCode:
    # A regular QR (never Micro) at error correction M, exactly: boost_error=False stops
    # segno from quietly raising the level when the data leaves room.
    return segno.make(text, error="m", micro=False, boost_error=False)


def make_install_qr(base_url: str) -> InstallQR:
    """The QR for `<base_url>/install`. `base_url` is the site address without a path.

    Anything that is not a plain https address raises ValueError (it is rejected, not fixed).
    """
    url = validate_install_url(base_url.rstrip("/") + INSTALL_PATH)
    code = _build(url)
    # The SVG is only the squares: no title or link inside, so the address is not in it.
    buffer = io.BytesIO()
    code.save(buffer, kind="svg", xmldecl=False, border=BORDER, scale=4, dark="#000", light="#fff")
    matrix = [[bool(cell) for cell in row] for row in code.matrix]
    return InstallQR(
        url=url,
        svg=buffer.getvalue().decode("ascii"),
        terminal=render_terminal(matrix),
        version=code.version,
        error=code.error,
    )


def install_qr_block(qr: InstallQR) -> str:
    """Heading, the code, and the address: what the command and the start-up log print."""
    return f"{HEADING}\n\n{qr.terminal}\n\n{qr.url}"
