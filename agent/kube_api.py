"""A small, read-only client for the Kubernetes API (standard library only).

What it can do is deliberately narrow:
- GET requests only. There is no method to send anything else.
- TLS is always verified against the cluster CA from the config (nothing else is trusted).
- Redirects are errors, never followed, so the token can't be sent anywhere else.
- Every request has a timeout, the whole collection has a deadline, and a response
  is never read past MAX_RESPONSE_BYTES.
- A 403 records only which group/resource/verb was refused. The body, which names
  the service account, is never read.
- The service account token is read from its file at the start of each collection
  (so a rotated token just works) and lives only in memory. It is never part of an
  exception message, repr() or log line; exceptions here carry fixed text only.
"""

import base64
import http.client
import json
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from agent import AGENT_VERSION
from agent.config import MAX_TOKEN_FILE_BYTES, ConfigError, read_secret_file

REQUEST_TIMEOUT = 10  # seconds for connecting and for each socket read
CLUSTER_DEADLINE = 30  # seconds for one whole collection
PAGE_LIMIT = 250  # items per page (the API's `limit` parameter)
MAX_PAGES = 10
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # per page
_READ_CHUNK = 64 * 1024

# What a service account token looks like (a JWT). Checking the shape keeps anything
# odd out of the Authorization header.
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{10,12288}")
_SUBJECT = re.compile(r"[A-Za-z0-9:_.@-]{1,253}")
_PATH = re.compile(r"/[A-Za-z0-9._/-]*")


@dataclass(frozen=True)
class Resource:
    """One API path the agent reads, and the RBAC rule that allows it."""

    path: str
    group: str
    resource: str
    verb: str
    query: dict[str, str] | None = None
    optional: bool = False  # the API may be missing from the cluster (404 is fine)


VERSION = Resource("/version", "", "/version", "get")
NAMESPACE = Resource("/api/v1/namespaces/kube-system", "", "namespaces", "get")
NODES = Resource("/api/v1/nodes", "", "nodes", "list")
PODS = Resource("/api/v1/pods", "", "pods", "list")
PVCS = Resource("/api/v1/persistentvolumeclaims", "", "persistentvolumeclaims", "list")
EVENTS = Resource("/api/v1/events", "", "events", "list", {"fieldSelector": "type=Warning"})
DEPLOYMENTS = Resource("/apis/apps/v1/deployments", "apps", "deployments", "list")
STATEFULSETS = Resource("/apis/apps/v1/statefulsets", "apps", "statefulsets", "list")
DAEMONSETS = Resource("/apis/apps/v1/daemonsets", "apps", "daemonsets", "list")
NODE_METRICS = Resource(
    "/apis/metrics.k8s.io/v1beta1/nodes", "metrics.k8s.io", "nodes", "list", optional=True
)
HELMCHARTS = Resource(
    "/apis/helm.cattle.io/v1/helmcharts", "helm.cattle.io", "helmcharts", "list", optional=True
)
APPLICATIONS = Resource(
    "/apis/argoproj.io/v1alpha1/applications", "argoproj.io", "applications", "list", optional=True
)
ALL_RESOURCES = (
    VERSION,
    NAMESPACE,
    NODES,
    PODS,
    PVCS,
    EVENTS,
    DEPLOYMENTS,
    STATEFULSETS,
    DAEMONSETS,
    NODE_METRICS,
    HELMCHARTS,
    APPLICATIONS,
)


class KubeError(Exception):
    """Base class. Messages are fixed text: never the token, a response body or a URL."""

    kind = "bad_response"


class ApiError(KubeError):
    """The API could not be used. kind: refused, tls, timeout or bad_response."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class DeadlineExceeded(ApiError):
    def __init__(self) -> None:
        super().__init__("timeout")


class ResponseTooLarge(ApiError):
    def __init__(self) -> None:
        super().__init__("bad_response")


class Expired(ApiError):
    """410: the continue token of a paged list is too old."""

    def __init__(self) -> None:
        super().__init__("bad_response")


class Unauthorized(KubeError):
    """401: the cluster doesn't accept the token."""

    kind = "unauthorized"


class TokenError(Unauthorized):
    """The token file is missing, unsafe or not a token. The message names the file only."""


class Denied(KubeError):
    """403: RBAC doesn't allow this. Only what was refused is recorded."""

    def __init__(self, group: str, resource: str, verb: str) -> None:
        super().__init__(f"forbidden: {verb} {group or 'core'}/{resource}")
        self.group, self.resource, self.verb = group, resource, verb


class NotInstalled(KubeError):
    """404: the API (e.g. Argo CD's CRD) isn't installed."""


def build_ssl_context(ca_file: str | None) -> ssl.SSLContext:
    """TLS settings for the API server. Certificate checking is always on.

    With a ca_file only that CA is trusted (cafile replaces the system CAs), which is
    what we want: the API server's certificate comes from the cluster's own CA.
    """
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # create_default_context() already sets both; stated so a refactor can't drop them.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def rbac_for_path(path: str) -> tuple[str, str, str]:
    """(group, resource, verb) a request to `path` needs, worked out from the path alone."""
    parts = [p for p in path.split("/") if p]
    if parts[:1] == ["api"]:
        group, rest = "", parts[2:]
    elif parts[:1] == ["apis"]:
        group, rest = (parts[1] if len(parts) > 1 else ""), parts[3:]
    else:
        return "", path, "get"  # a non-resource URL such as /version
    if rest[:1] == ["namespaces"] and len(rest) > 2:
        rest = rest[2:]  # /namespaces/<ns>/<resource>: the resource is what counts
        return group, rest[0], "get" if len(rest) > 1 else "list"
    if not rest:
        return group, "unknown", "list"
    return group, rest[0], "get" if len(rest) > 1 else "list"


def token_claims(token: str) -> dict[str, Any]:
    """The `sub` and `exp` claims of a JWT, without checking its signature.

    We only want to show who the token is and when it expires; the cluster decides
    whether to trust it. Everything else in the token is ignored.
    """
    result: dict[str, Any] = {"sub": None, "exp": None}
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return result
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (ValueError, RecursionError, TypeError):
        return result
    if not isinstance(claims, dict):
        return result
    sub, exp = claims.get("sub"), claims.get("exp")
    if isinstance(sub, str) and _SUBJECT.fullmatch(sub):
        result["sub"] = sub
    if isinstance(exp, int | float) and not isinstance(exp, bool) and 0 < exp < 4e9:
        result["exp"] = int(exp)
    return result


def days_until(timestamp: float | None, now: float | None = None) -> int | None:
    """Whole days from `now` until the Unix `timestamp` (negative once it has passed)."""
    if timestamp is None:
        return None
    return int((timestamp - (time.time() if now is None else now)) // 86_400)


class KubeClient:
    """Read-only API access. One keep-alive connection, re-opened after any error."""

    def __init__(
        self,
        api_server: str,
        token_file: str,
        ca_file: str | None,
        *,
        connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
        context: ssl.SSLContext | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parts = urlsplit(api_server)
        self._api_server = api_server
        self._host = parts.hostname or ""
        self._port = parts.port or 443
        self._token_file = token_file
        self._ca_file = ca_file
        self._factory = connection_factory
        self._context = context
        self._clock = clock
        self._token: str | None = None
        self._conn: Any = None
        self._deadline = 0.0
        self._cert_expiry: float | None = None

    def __repr__(self) -> str:
        # Written by hand so the token can't leak into logs or tracebacks.
        return f"KubeClient(api_server={self._api_server!r}, token_file={self._token_file!r})"

    # --- one collection ----------------------------------------------------------------

    def start(self) -> None:
        """Begin a collection: read the token file again and start the deadline clock."""
        self._close()
        self._token = self._read_token()
        self._deadline = self._clock() + CLUSTER_DEADLINE
        self._cert_expiry = None

    def _read_token(self) -> str:
        try:
            raw = read_secret_file(self._token_file, "token_file", MAX_TOKEN_FILE_BYTES)
            token = raw.decode("ascii").strip()
        except ConfigError as exc:
            raise TokenError(str(exc)) from None
        except UnicodeDecodeError:
            raise TokenError("the token file is not a service account token") from None
        if not _TOKEN.fullmatch(token):
            raise TokenError("the token file doesn't contain a service account token")
        return token

    def token_details(self) -> dict[str, Any]:
        """{"sub": service account name or None, "exp": Unix time or None}."""
        return token_claims(self._token or "")

    def api_cert_days(self, now: float | None = None) -> int | None:
        """Days until the API server's certificate expires (from the TLS handshake)."""
        return days_until(self._cert_expiry, now)

    # --- requests ----------------------------------------------------------------------------

    def get_json(self, path: str, query: dict[str, str] | None = None) -> dict:
        """GET one path and return the decoded JSON object. Raises KubeError subclasses."""
        if not _PATH.fullmatch(path) or "//" in path:
            raise ValueError("not an API path")
        if self._token is None:
            self.start()
        target = path + (f"?{urlencode(query)}" if query else "")
        if self._clock() >= self._deadline:
            raise DeadlineExceeded()
        try:
            return self._get(target, path)
        except KubeError:
            self._close()
            raise

    def list_all(self, path: str, query: dict[str, str] | None = None) -> tuple[list[dict], bool]:
        """Every item of a list, page by page. Returns (items, truncated).

        truncated is true if the pages ran out (MAX_PAGES), one was too large, or the
        listing expired twice (410: the continue token got too old; we restart once).
        """
        items: list[dict] = []
        for _ in range(2):
            items = []
            try:
                return items, self._pages(path, query or {}, items)
            except Expired:
                continue
        return items, True

    def _pages(self, path: str, query: dict[str, str], items: list[dict]) -> bool:
        token: str | None = None
        for _ in range(MAX_PAGES):
            page_query = {**query, "limit": str(PAGE_LIMIT)}
            if token:
                page_query["continue"] = token
            try:
                body = self.get_json(path, page_query)
            except ResponseTooLarge:
                return True
            page = body.get("items")
            if not isinstance(page, list):
                raise ApiError("bad_response")
            items.extend(item for item in page if isinstance(item, dict))
            metadata = body.get("metadata")
            token = metadata.get("continue") if isinstance(metadata, dict) else None
            if not isinstance(token, str) or not token:
                return False
        return True

    def _get(self, target: str, path: str) -> dict:
        conn = self._conn or self._open()
        try:
            conn.request("GET", target, headers=self._headers())
            response = conn.getresponse()
            self._note_certificate(conn)
            status = response.status
            if status == 200:
                return self._decode(self._read(response))
        except KubeError:
            raise
        except TimeoutError:
            raise ApiError("timeout") from None
        except ssl.SSLError:
            raise ApiError("tls") from None
        except http.client.HTTPException:
            raise ApiError("bad_response") from None
        except OSError:
            raise ApiError("refused") from None
        # Anything but 200 is decided from the status line alone; the body stays unread.
        if status == 401:
            raise Unauthorized()
        if status == 403:
            raise Denied(*rbac_for_path(path))
        if status == 404:
            raise NotInstalled()
        if status == 410:
            raise Expired()
        raise ApiError("bad_response")  # includes every 3xx: redirects are never followed

    def _open(self) -> Any:
        if self._context is None:
            try:
                self._context = build_ssl_context(self._ca_file)
            except (OSError, ssl.SSLError):
                raise ApiError("tls") from None
        self._conn = self._factory(
            self._host, self._port, timeout=REQUEST_TIMEOUT, context=self._context
        )
        return self._conn

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": f"cronrack-agent/{AGENT_VERSION}",
        }

    def _note_certificate(self, conn: Any) -> None:
        """Remember when the API server's certificate expires (only the first time)."""
        sock = getattr(conn, "sock", None)
        if self._cert_expiry is not None or sock is None:
            return
        try:
            self._cert_expiry = ssl.cert_time_to_seconds(sock.getpeercert()["notAfter"])
        except (KeyError, ValueError, TypeError, AttributeError):
            self._cert_expiry = None

    def _read(self, response: Any) -> bytes:
        """The body, read in chunks: stops at the size cap and at the overall deadline."""
        chunks: list[bytes] = []
        total = 0
        while True:
            if self._clock() >= self._deadline:
                raise DeadlineExceeded()
            chunk = response.read(min(_READ_CHUNK, MAX_RESPONSE_BYTES + 1 - total))
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ResponseTooLarge()
            chunks.append(chunk)

    @staticmethod
    def _decode(raw: bytes) -> dict:
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            raise ApiError("bad_response") from None
        if not isinstance(data, dict):
            raise ApiError("bad_response")
        return data


def permission_report(client: Any, now: float | None = None) -> dict[str, Any]:
    """Probe every API the agent reads, with limit=1, and record only the outcome.

    {"reachable", "error", "sa", "token_days", "cert_days",
     "rows": [{"group", "resource", "verb", "result", "code"}]}
    result is ok / denied / missing / unauthorized / error. No object data is kept.
    """
    now = time.time() if now is None else now
    report: dict[str, Any] = {
        "reachable": False,
        "error": None,
        "sa": None,
        "token_days": None,  # nosec B105 - days left, not a password
        "cert_days": None,
        "rows": [],
    }
    try:
        client.start()
    except Unauthorized:
        report["error"] = "unauthorized"
        return report
    claims = client.token_details()
    report["sa"] = claims.get("sub")
    report["token_days"] = days_until(claims.get("exp"), now)

    for resource in ALL_RESOURCES:
        query = resource.query
        if resource.verb == "list":
            query = {**(query or {}), "limit": "1"}
        result, code = "ok", 200
        try:
            client.get_json(resource.path, query)
            report["reachable"] = True
        except Denied:
            report["reachable"] = True
            result, code = "denied", 403
        except NotInstalled:
            report["reachable"] = True
            result, code = "missing", 404
        except Unauthorized:
            report["reachable"] = True
            report["error"] = "unauthorized"
            result, code = "unauthorized", 401
        except ApiError as exc:
            if not report["reachable"] or exc.kind in ("refused", "tls"):
                report["error"] = exc.kind
                break
            result, code = "error", None
        report["rows"].append(
            {
                "group": resource.group,
                "resource": resource.resource,
                "verb": resource.verb,
                "result": result,
                "code": code,
            }
        )
        if report["error"]:
            break
    report["cert_days"] = client.api_cert_days(now)
    return report
