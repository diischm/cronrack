"""Load and check the agent's local config file (/etc/cronrack/agent.toml).

The file will hold the host's token, so two rules apply everywhere here:
- the token never appears in error messages, logs, repr() or str();
- error messages name the bad *key*, never the value that was in it.
"""

import ipaddress
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

DEFAULT_CONFIG_PATH = "/etc/cronrack/agent.toml"
# B105 reviewed: this is the *name* of an environment variable, not a secret.
TOKEN_ENV_VAR = "CRONRACK_TOKEN"  # nosec B105

MVP_CHECKS = ("updates", "reboot", "containers", "units", "timers", "disks")

# Below 10s the agent would mostly be busy running commands; above a day the
# server's dead-man's switch would mark the host offline long before a report.
MIN_INTERVAL_SECONDS = 10
MAX_INTERVAL_SECONDS = 86_400

# Plain http:// / ws:// is only allowed to these hosts (local development, SSH tunnels).
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
WEBSOCKET_PATH = "/ws/agent/"

# Server tokens come from secrets.token_urlsafe(32): 43 URL-safe characters. Checking
# the shape also keeps anything odd (spaces, newlines) out of the HTTP header.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")

# The Kubernetes cluster view talks to the API server over TLS only; k3s listens on 6443.
DEFAULT_KUBE_API_SERVER = "https://127.0.0.1:6443"
# /etc/rancher/k3s/k3s.yaml is the cluster *admin* kubeconfig. The agent must never use it.
FORBIDDEN_KUBE_DIR = "/etc/rancher"
# [network]: the points between this host and the internet. Every target is an address
# written in this file (or the default route on this host): the server can't add any.
DEFAULT_INTERNET_TARGETS = ("1.1.1.1", "8.8.8.8")
DEFAULT_DNS_NAME = "example.com"
DEFAULT_PUBLIC_IP_URL = "https://api.ipify.org"
MAX_NETWORK_BEFORE = 2  # points between this host and the router, e.g. a LAN switch
MAX_NETWORK_HOPS = 2  # points after the router, e.g. the ISP box
MAX_INTERNET_TARGETS = 4
MAX_POINT_NAME = 32
RESERVED_POINT_NAMES = frozenset({"router", "you", "internet"})
# Local points: private (RFC 1918), link-local or carrier-grade NAT (RFC 6598) IPv4.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)

MAX_TOKEN_FILE_BYTES = 16 * 1024
MAX_CA_FILE_BYTES = 1024 * 1024

PERMISSION_FIX = (
    "Run: sudo chown root:cronrack {path} && sudo chmod 640 {path} "
    "(installed service), or chmod 600 {path} for your own test file."
)


class ConfigError(Exception):
    """The config file is missing something, malformed, or unsafe."""


@dataclass
class KubernetesConfig:
    """The [kubernetes] table. Everything is off until the host owner turns it on here."""

    enabled: bool = False  # report this host's node role (needs no token)
    cluster_view: bool = False  # also read the cluster through the API (needs token + CA)
    api_server: str = DEFAULT_KUBE_API_SERVER
    token_file: str | None = None
    ca_file: str | None = None


@dataclass
class NetworkHop:
    """An extra local point on the path: before the router (a switch) or after it (ISP box)."""

    name: str
    address: str


@dataclass
class NetworkConfig:
    """The [network] table. Off until the host owner turns it on here."""

    enabled: bool = False
    gateway: str = "auto"  # "auto" = the default route in /proc/net/route, or an address
    before: list[NetworkHop] = field(default_factory=list)  # in path order, up to the router
    hops: list[NetworkHop] = field(default_factory=list)  # in path order, after the router
    internet: list[str] = field(default_factory=lambda: list(DEFAULT_INTERNET_TARGETS))
    dns: bool = True
    dns_name: str = DEFAULT_DNS_NAME
    public_ip_check: bool = False  # off: it asks a third party
    public_ip_url: str = DEFAULT_PUBLIC_IP_URL


@dataclass
class AgentConfig:
    server_url: str | None = None
    token: str | None = None
    ca_file: str | None = None
    interval_seconds: int = 60
    enabled_checks: list[str] = field(default_factory=lambda: list(MVP_CHECKS))
    actions: dict[str, Any] = field(default_factory=dict)
    kubernetes: KubernetesConfig = field(default_factory=KubernetesConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)

    def __repr__(self) -> str:
        # Written by hand so the token can't leak into logs or tracebacks.
        # str() falls back to this too.
        token = "<hidden>" if self.token else None
        return (
            f"AgentConfig(server_url={self.server_url!r}, token={token}, "
            f"ca_file={self.ca_file!r}, interval_seconds={self.interval_seconds!r}, "
            f"enabled_checks={self.enabled_checks!r}, actions={self.actions!r}, "
            f"kubernetes={self.kubernetes!r}, network={self.network!r})"
        )


def _process_groups() -> set[int]:
    return {os.getgid(), *os.getgroups()}


def _check_stat(st: os.stat_result, path: str, uid: int, groups: set[int]) -> None:
    """Refuse a config file that anyone but us (and root) could read or change.

    Allowed: root:cronrack 0640 (the installed service: the agent can read its token
    but can't rewrite its own config), and <me>:<me> 0600 (a local test file).
    """
    mode = stat.S_IMODE(st.st_mode)
    fix = PERMISSION_FIX.format(path=path)
    if mode & stat.S_IRWXO:
        raise ConfigError(
            f"{path} is accessible by others (mode {mode:03o}); it holds the token. {fix}"
        )
    if mode & (stat.S_IWGRP | stat.S_IXGRP):
        raise ConfigError(f"{path} is writable or executable by its group (mode {mode:03o}). {fix}")
    if st.st_uid not in (0, uid):
        raise ConfigError(f"{path} is owned by another user (uid {st.st_uid}). {fix}")
    if mode & stat.S_IRGRP and st.st_gid not in groups:
        raise ConfigError(
            f"{path} is readable by group {st.st_gid}, which this agent isn't in. {fix}"
        )


def check_permissions(path: str) -> None:
    """Raise ConfigError if the file's owner or mode would expose or allow changing the token."""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    _check_stat(st, path, os.getuid(), _process_groups())


def check_server_url(url: str) -> str:
    """Validate the server's base URL and return it. Raises ConfigError.

    https:// (or wss://) is required. Plain http:// / ws:// is only accepted for
    localhost, 127.0.0.1 and ::1, so the token never crosses a network unencrypted.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise ConfigError("'server_url' is not a valid URL") from None
    if parts.scheme not in {"https", "wss", "http", "ws"}:
        raise ConfigError("'server_url' must start with https://")
    if not host:
        raise ConfigError("'server_url' has no host name")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("'server_url' must not contain a user name, password, ? or #")
    if parts.scheme in {"http", "ws"} and host not in LOCAL_HOSTS:
        raise ConfigError(
            "'server_url' must use https:// (plain http:// is only allowed for "
            "localhost, 127.0.0.1 and ::1)"
        )
    return url


def websocket_url(server_url: str) -> str:
    """The agent endpoint for a base URL: https://x -> wss://x/ws/agent/, http -> ws.

    A URL that already ends in /ws/agent/ is kept (older config files used that form).
    """
    parts = urlsplit(check_server_url(server_url))
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/")
    if not path.endswith(WEBSOCKET_PATH.rstrip("/")):
        path += WEBSOCKET_PATH.rstrip("/")
    return f"{scheme}://{parts.netloc}{path}/"


def check_token(token: str) -> str:
    if not _TOKEN.fullmatch(token):
        # Never include the value: it may be a real (if mangled) secret.
        raise ConfigError(
            "'token' doesn't look like a CronRack token (copy it again from the server)"
        )
    return token


def check_api_server(url: str) -> str:
    """Validate the Kubernetes API server's base URL and return it. Raises ConfigError.

    https:// only, even for localhost: the service account token is sent to it.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise ConfigError("'api_server' is not a valid URL") from None
    if parts.scheme != "https":
        raise ConfigError("'api_server' must start with https://")
    if not host:
        raise ConfigError("'api_server' has no host name")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("'api_server' must not contain a user name, password, ? or #")
    if parts.path not in ("", "/"):
        raise ConfigError("'api_server' must be just the address, without a path")
    return url.rstrip("/")


def _check_kube_path(path: str, key: str) -> str:
    if not os.path.isabs(path):
        raise ConfigError(f"'{key}' must be an absolute path")
    resolved = os.path.realpath(path)  # also follows symlinks and ..
    if resolved == FORBIDDEN_KUBE_DIR or resolved.startswith(FORBIDDEN_KUBE_DIR + "/"):
        raise ConfigError(
            f"'{key}' must not point into {FORBIDDEN_KUBE_DIR} (that holds cluster admin "
            "credentials; create a read-only service account token instead)"
        )
    return path


def read_secret_file(path: str, key: str, limit: int) -> bytes:
    """Read a secret file after checking the file we actually opened (same rules as agent.toml)."""
    try:
        with open(path, "rb") as f:
            _check_stat(os.fstat(f.fileno()), path, os.getuid(), _process_groups())
            data = f.read(limit + 1)
    except OSError as exc:
        raise ConfigError(f"cannot read '{key}' {path}: {exc.strerror}") from None
    if len(data) > limit:
        raise ConfigError(f"'{key}' {path} is too large")
    return data


def check_ca_file(path: str) -> None:
    """The cluster CA certificate must exist and must not be (or contain) a private key."""
    if path.endswith(".key"):
        raise ConfigError("'ca_file' must be a CA certificate, not a .key file")
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_CA_FILE_BYTES + 1)
    except OSError as exc:
        raise ConfigError(f"cannot read 'ca_file' {path}: {exc.strerror}") from None
    if len(data) > MAX_CA_FILE_BYTES:
        raise ConfigError(f"'ca_file' {path} is too large")
    if b"PRIVATE KEY" in data:
        raise ConfigError(f"'ca_file' {path} contains a private key; use the public .crt only")


def _parse_kubernetes(data: Any) -> KubernetesConfig:
    if not isinstance(data, dict):
        raise ConfigError("'kubernetes' must be a table")
    unknown = sorted(set(data) - {"enabled", "cluster_view", "api_server", "token_file", "ca_file"})
    if unknown:
        raise ConfigError(f"unknown [kubernetes] setting(s): {', '.join(unknown)}")
    for key in ("enabled", "cluster_view"):
        if not isinstance(data.get(key, False), bool):
            raise ConfigError(f"'{key}' must be true or false")

    config = KubernetesConfig(
        enabled=data.get("enabled", False),
        cluster_view=data.get("cluster_view", False),
        token_file=_optional_str(data, "token_file"),
        ca_file=_optional_str(data, "ca_file"),
    )
    if "api_server" in data:
        server = _optional_str(data, "api_server")
        config.api_server = check_api_server(server) if server else DEFAULT_KUBE_API_SERVER
    if config.token_file is not None:
        _check_kube_path(config.token_file, "token_file")
    if config.ca_file is not None:
        _check_kube_path(config.ca_file, "ca_file")
    if config.cluster_view:
        if config.token_file is None:
            raise ConfigError("'cluster_view' needs 'token_file' in [kubernetes]")
        if config.ca_file is None:
            raise ConfigError("'cluster_view' needs 'ca_file' in [kubernetes]")
        read_secret_file(config.token_file, "token_file", MAX_TOKEN_FILE_BYTES)
        check_ca_file(config.ca_file)
    return config


def is_local_address(value: str) -> bool:
    """Private, link-local or CGNAT IPv4: a point inside the home network."""
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    return ip.is_private or ip.is_link_local or ip in _CGNAT


def is_internet_address(value: str) -> bool:
    """A public unicast IPv4 address (Python counts 224.0.0.0/4 as global: not here)."""
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


def _point_name(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a string")
    name = value.strip()
    if not 1 <= len(name) <= MAX_POINT_NAME or not name.isprintable():
        raise ConfigError(f"'{key}' must be 1-{MAX_POINT_NAME} printable characters")
    return name


def _hop_name(value: Any, key: str, others: list[NetworkHop]) -> str:
    """Point names are unique in a report: the strip's own words and addresses are taken."""
    name = _point_name(value, f"{key} name")
    if name.casefold() in RESERVED_POINT_NAMES or any(h.name == name for h in others):
        raise ConfigError(f"each of '{key}' needs its own name (not Router, You or Internet)")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name
    raise ConfigError(f"a name in '{key}' must not be an address")


def _parse_points(
    value: Any, key: str, limit: int, taken: list[NetworkHop] | None = None
) -> list[NetworkHop]:
    """The `before` or `hops` list; `taken` holds names already used by the other list."""
    if not isinstance(value, list) or len(value) > limit:
        raise ConfigError(f"'{key}' must be a list of at most {limit} points")
    points: list[NetworkHop] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "address"}:
            raise ConfigError(f'each of \'{key}\' must be {{ name = "...", address = "..." }}')
        if not isinstance(item["address"], str) or not is_local_address(item["address"]):
            raise ConfigError(f"'{key}' addresses must be private IPv4 addresses")
        name = _hop_name(item["name"], key, [*(taken or []), *points])
        points.append(NetworkHop(name, item["address"]))
    return points


def check_public_ip_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise ConfigError("'public_ip_url' is not a valid URL") from None
    if parts.scheme != "https" or not host:
        raise ConfigError("'public_ip_url' must be an https:// URL")
    if parts.username or parts.password or parts.fragment:
        raise ConfigError("'public_ip_url' must not contain a user name, password or #")
    return url


def _parse_network(data: Any) -> NetworkConfig:
    """[network]. Errors name the key, never the value (addresses are host details)."""
    if not isinstance(data, dict):
        raise ConfigError("'network' must be a table")
    known = {
        "enabled",
        "gateway",
        "before",
        "hops",
        "internet",
        "dns",
        "dns_name",
        "public_ip_check",
        "public_ip_url",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown [network] setting(s): {', '.join(unknown)}")
    for key in ("enabled", "dns", "public_ip_check"):
        if not isinstance(data.get(key, False), bool):
            raise ConfigError(f"'{key}' must be true or false")
    config = NetworkConfig(
        enabled=data.get("enabled", False),
        dns=data.get("dns", True),
        public_ip_check=data.get("public_ip_check", False),
    )
    if "gateway" in data:
        gateway = data["gateway"]
        if not isinstance(gateway, str) or not (gateway == "auto" or is_local_address(gateway)):
            raise ConfigError("'gateway' must be \"auto\" or a private IPv4 address")
        config.gateway = gateway
    if "before" in data:
        config.before = _parse_points(data["before"], "before", MAX_NETWORK_BEFORE)
        # With gateway = "auto" the router is only known at runtime: agent/network.py drops
        # such a point there.
        if any(point.address == config.gateway for point in config.before):
            raise ConfigError("a point in 'before' can't have the router's address")
    if "hops" in data:
        config.hops = _parse_points(data["hops"], "hops", MAX_NETWORK_HOPS, config.before)
    if "internet" in data:
        targets = data["internet"]
        if (
            not isinstance(targets, list)
            or not 1 <= len(targets) <= MAX_INTERNET_TARGETS
            or not all(isinstance(t, str) and is_internet_address(t) for t in targets)
        ):
            raise ConfigError(
                f"'internet' must be a list of 1-{MAX_INTERNET_TARGETS} public IPv4 addresses"
            )
        config.internet = list(dict.fromkeys(targets))
    if "dns_name" in data:
        name = data["dns_name"]
        if not isinstance(name, str) or not _HOSTNAME.fullmatch(name):
            raise ConfigError("'dns_name' must be a host name such as example.com")
        config.dns_name = name
    if "public_ip_url" in data:
        url = data["public_ip_url"]
        if not isinstance(url, str):
            raise ConfigError("'public_ip_url' must be a string")
        config.public_ip_url = check_public_ip_url(url)
    return config


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a string")
    return value or None


def _parse(data: dict[str, Any]) -> AgentConfig:
    known = {
        "server_url",
        "token",
        "ca_file",
        "interval_seconds",
        "enabled_checks",
        "actions",
        "kubernetes",
        "network",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(unknown)}")

    config = AgentConfig(
        server_url=_optional_str(data, "server_url"),
        token=_optional_str(data, "token"),
        ca_file=_optional_str(data, "ca_file"),
    )
    if config.server_url is not None:
        check_server_url(config.server_url)
    if config.token is not None:
        check_token(config.token)

    if "interval_seconds" in data:
        interval = data["interval_seconds"]
        # bool is a subclass of int in Python, so `true` would otherwise pass.
        if not isinstance(interval, int) or isinstance(interval, bool):
            raise ConfigError("'interval_seconds' must be a whole number")
        if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
            raise ConfigError(
                f"'interval_seconds' must be between {MIN_INTERVAL_SECONDS} "
                f"and {MAX_INTERVAL_SECONDS}"
            )
        config.interval_seconds = interval

    if "enabled_checks" in data:
        checks = data["enabled_checks"]
        if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
            raise ConfigError("'enabled_checks' must be a list of strings")
        unknown_checks = sorted(set(checks) - set(MVP_CHECKS))
        if unknown_checks:
            raise ConfigError(
                f"unknown check(s) in 'enabled_checks': {', '.join(unknown_checks)} "
                f"(known: {', '.join(MVP_CHECKS)})"
            )
        config.enabled_checks = list(dict.fromkeys(checks))  # drop duplicates, keep order

    if "actions" in data:
        if not isinstance(data["actions"], dict):
            raise ConfigError("'actions' must be a table")
        config.actions = data["actions"]

    if "kubernetes" in data:
        config.kubernetes = _parse_kubernetes(data["kubernetes"])

    if "network" in data:
        config.network = _parse_network(data["network"])

    return config


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AgentConfig:
    """Read the TOML config. A missing file gives the defaults.

    The CRONRACK_TOKEN environment variable, if set, replaces the file's token.
    """
    try:
        with open(path, "rb") as f:
            # Check the file we actually opened, so it can't be swapped in between.
            _check_stat(os.fstat(f.fileno()), path, os.getuid(), _process_groups())
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}
    except tomllib.TOMLDecodeError as exc:
        # tomllib's message is a description plus line/column, never file content.
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None

    config = _parse(data)

    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        config.token = check_token(env_token)
    return config
