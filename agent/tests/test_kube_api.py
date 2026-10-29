"""The read-only Kubernetes API client, against a fake HTTPSConnection (no network)."""

import base64
import http.client
import json
import os
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from agent import kube_api
from agent.kube_api import (
    ApiError,
    DeadlineExceeded,
    Denied,
    KubeClient,
    NotInstalled,
    ResponseTooLarge,
    TokenError,
    Unauthorized,
)

FIXTURES = Path(__file__).parent / "fixtures" / "k8s"
NOW = 1_800_000_000.0  # a fixed "current time" for certificate and token expiry maths


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def make_jwt(claims: dict) -> str:
    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.c2lnbmF0dXJlLXBhcnQtb2YtdGhlLWp3dA"


CLAIMS = {"sub": "system:serviceaccount:monitoring:cronrack-agent", "exp": int(NOW) + 100 * 86400}
TOKEN = make_jwt(CLAIMS)


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


class FakeResponse:
    def __init__(self, status: int, body: bytes | dict = b"{}") -> None:
        self.status = status
        self.body = json.dumps(body).encode() if isinstance(body, dict) else body
        self.bytes_read = 0

    def read(self, amt: int = -1) -> bytes:
        chunk = self.body[:amt] if amt >= 0 else self.body
        self.body = self.body[len(chunk) :]
        self.bytes_read += len(chunk)
        return chunk


class FakeSocket:
    def __init__(self, cert: dict | None) -> None:
        self.cert = cert

    def getpeercert(self) -> dict | None:
        return self.cert


class FakeConnection:
    def __init__(self, script: "Script", host, port, timeout, context) -> None:
        self.script = script
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.closed = False
        self.sock = FakeSocket(script.cert)

    def request(self, method, url, body=None, headers=None) -> None:
        self.script.requests.append(
            {"method": method, "url": url, "body": body, "headers": headers}
        )

    def getresponse(self) -> FakeResponse:
        url = self.script.requests[-1]["url"]
        answer = self.script.next_answer(urlsplit(url).path)
        if isinstance(answer, BaseException):
            raise answer
        self.script.responses.append(answer)
        return answer

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Script:
    """Hands out answers in order (or by path) and records every request."""

    def __init__(self, *answers: Any, router=None, cert: dict | None = None) -> None:
        self.answers = list(answers)
        self.router = router
        self.cert = cert
        self.requests: list[dict] = []
        self.responses: list[FakeResponse] = []
        self.connections: list[FakeConnection] = []
        self.clock = Clock()

    def __call__(self, host, port=None, timeout=None, context=None) -> FakeConnection:
        connection = FakeConnection(self, host, port, timeout, context)
        self.connections.append(connection)
        return connection

    def next_answer(self, path: str) -> Any:
        if self.router is not None:
            return self.router(path)
        return self.answers.pop(0)

    @property
    def urls(self) -> list[str]:
        return [r["url"] for r in self.requests]

    def query(self, index: int) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.requests[index]["url"]).query)


@pytest.fixture
def token_file(tmp_path: Path) -> str:
    path = tmp_path / "k8s-token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return str(path)


def make_client(script: Script, token_file: str, **kwargs) -> KubeClient:
    return KubeClient(
        "https://127.0.0.1:6443",
        token_file,
        None,
        connection_factory=script,
        context=ssl.create_default_context(),
        clock=script.clock,
        **kwargs,
    )


def ok(body: dict | bytes = b"{}") -> FakeResponse:
    return FakeResponse(200, body)


# --- the request itself ------------------------------------------------------------------------


def test_only_get_with_the_token_in_the_header(token_file):
    script = Script(ok({"gitVersion": "v1.31.2+k3s1"}))
    client = make_client(script, token_file)

    assert client.get_json("/version") == {"gitVersion": "v1.31.2+k3s1"}

    [request] = script.requests
    assert request["method"] == "GET"
    assert request["body"] is None
    assert request["url"] == "/version"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["Accept"] == "application/json"
    assert request["headers"]["User-Agent"].startswith("cronrack-agent/")
    assert TOKEN not in request["url"]


def test_connection_uses_host_port_timeout_and_the_tls_context(token_file):
    script = Script(ok())
    context = ssl.create_default_context()
    client = KubeClient(
        "https://k3s.home.lan:6444",
        token_file,
        None,
        connection_factory=script,
        context=context,
        clock=script.clock,
    )

    client.get_json("/version")

    [connection] = script.connections
    assert (connection.host, connection.port) == ("k3s.home.lan", 6444)
    assert connection.timeout == kube_api.REQUEST_TIMEOUT
    assert connection.context is context


def test_query_is_url_encoded(token_file):
    script = Script(ok())
    client = make_client(script, token_file)

    client.get_json("/api/v1/events", {"fieldSelector": "type=Warning", "limit": "1"})

    assert script.query(0) == {"fieldSelector": ["type=Warning"], "limit": ["1"]}
    assert "type%3DWarning" in script.urls[0]


@pytest.mark.parametrize(
    "path", ["api/v1/pods", "/api/v1/pods?x=1", "/api/v1/pods\r\nX: y", "//evil", ""]
)
def test_odd_paths_are_refused_before_anything_is_sent(token_file, path):
    script = Script()
    client = make_client(script, token_file)

    with pytest.raises(ValueError):
        client.get_json(path)

    assert script.requests == []


def test_the_client_has_no_way_to_send_anything_but_get():
    public = {name for name in dir(KubeClient) if not name.startswith("_")}
    assert not public & {"post", "put", "patch", "delete", "request", "send"}


# --- status codes -------------------------------------------------------------------------------


def test_401_is_unauthorized(token_file):
    client = make_client(Script(FakeResponse(401, b"{}")), token_file)
    with pytest.raises(Unauthorized):
        client.get_json("/api/v1/nodes")


def test_404_is_not_installed(token_file):
    client = make_client(Script(FakeResponse(404, fixture("status_404"))), token_file)
    with pytest.raises(NotInstalled):
        client.get_json("/apis/argoproj.io/v1alpha1/applications")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/pods", ("", "pods", "list")),
        ("/api/v1/nodes", ("", "nodes", "list")),
        ("/api/v1/namespaces/kube-system", ("", "namespaces", "get")),
        ("/apis/apps/v1/deployments", ("apps", "deployments", "list")),
        ("/apis/metrics.k8s.io/v1beta1/nodes", ("metrics.k8s.io", "nodes", "list")),
        ("/apis/argoproj.io/v1alpha1/applications", ("argoproj.io", "applications", "list")),
        ("/version", ("", "/version", "get")),
    ],
)
def test_403_records_only_group_resource_and_verb(token_file, path, expected):
    response = FakeResponse(403, fixture("status_403"))
    client = make_client(Script(response), token_file)

    with pytest.raises(Denied) as excinfo:
        client.get_json(path)

    assert (excinfo.value.group, excinfo.value.resource, excinfo.value.verb) == expected
    # The body (which names the service account) is never read, kept or shown.
    assert response.bytes_read == 0
    assert "cronrack-agent" not in str(excinfo.value)
    assert "cronrack-agent" not in repr(excinfo.value)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_errors_and_never_followed(token_file, status):
    script = Script(FakeResponse(status, b""))
    client = make_client(script, token_file)

    with pytest.raises(ApiError) as excinfo:
        client.get_json("/version")

    assert excinfo.value.kind == "bad_response"
    assert len(script.requests) == 1


@pytest.mark.parametrize("status", [400, 429, 500, 502, 503])
def test_other_statuses_are_bad_responses(token_file, status):
    client = make_client(Script(FakeResponse(status, b"{}")), token_file)
    with pytest.raises(ApiError) as excinfo:
        client.get_json("/version")
    assert excinfo.value.kind == "bad_response"


@pytest.mark.parametrize(
    "body", [b"not json", b"[1, 2]", b'"text"', b"", b"[" * 100_000, b"\xff\xfe"]
)
def test_bad_bodies_are_bad_responses(token_file, body):
    client = make_client(Script(ok(body)), token_file)
    with pytest.raises(ApiError) as excinfo:
        client.get_json("/version")
    assert excinfo.value.kind == "bad_response"


def test_error_connection_is_closed_and_a_new_one_is_opened(token_file):
    script = Script(FakeResponse(403, fixture("status_403")), ok())
    client = make_client(script, token_file)

    with pytest.raises(Denied):
        client.get_json("/api/v1/pods")
    client.get_json("/api/v1/nodes")

    assert len(script.connections) == 2
    assert script.connections[0].closed is True


def test_the_connection_is_reused_between_requests(token_file):
    script = Script(ok(), ok())
    client = make_client(script, token_file)

    client.get_json("/api/v1/nodes")
    client.get_json("/api/v1/pods")

    assert len(script.connections) == 1


# --- size cap -------------------------------------------------------------------------------------


def test_response_over_the_cap_is_rejected_after_reading_at_most_cap_plus_one(
    token_file, monkeypatch
):
    monkeypatch.setattr(kube_api, "MAX_RESPONSE_BYTES", 100)
    response = ok(b"[" + b" " * 10_000 + b"]")
    script = Script(response)
    client = make_client(script, token_file)

    with pytest.raises(ResponseTooLarge):
        client.get_json("/api/v1/pods")

    assert response.bytes_read == 101
    assert script.connections[0].closed is True


def test_response_exactly_at_the_cap_is_fine(token_file, monkeypatch):
    body = b'{"a": "' + b"x" * 20 + b'"}'
    monkeypatch.setattr(kube_api, "MAX_RESPONSE_BYTES", len(body))
    client = make_client(Script(ok(body)), token_file)
    assert client.get_json("/version") == {"a": "x" * 20}


# --- network failures -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError("timed out"), "timeout"),
        (ssl.SSLCertVerificationError("bad certificate for 127.0.0.1"), "tls"),
        (ssl.SSLError("handshake failure"), "tls"),
        (ConnectionRefusedError(111, "Connection refused"), "refused"),
        (ConnectionResetError(104, "reset"), "refused"),
        (OSError(113, "No route to host"), "refused"),
        (http.client.RemoteDisconnected("closed"), "bad_response"),
        (http.client.IncompleteRead(b"x"), "bad_response"),
    ],
)
def test_network_errors_are_mapped_to_a_kind(token_file, error, kind):
    script = Script(error)
    client = make_client(script, token_file)

    with pytest.raises(ApiError) as excinfo:
        client.get_json("/version")

    assert excinfo.value.kind == kind
    assert script.connections[0].closed is True
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in repr(excinfo.value)


def test_a_missing_ca_file_is_a_tls_error(token_file, tmp_path):
    script = Script(ok())
    client = KubeClient(
        "https://127.0.0.1:6443",
        token_file,
        str(tmp_path / "nope.crt"),
        connection_factory=script,
        clock=script.clock,
    )
    with pytest.raises(ApiError) as excinfo:
        client.get_json("/version")
    assert excinfo.value.kind == "tls"


def test_deadline_stops_further_requests(token_file):
    script = Script(ok(), ok())
    client = make_client(script, token_file)

    client.get_json("/api/v1/nodes")
    script.clock.advance(kube_api.CLUSTER_DEADLINE + 1)
    with pytest.raises(DeadlineExceeded):
        client.get_json("/api/v1/pods")

    assert len(script.requests) == 1


def test_deadline_applies_while_reading_a_slow_body(token_file):
    class SlowResponse(FakeResponse):
        def __init__(self, clock):
            super().__init__(200, b" " * 1_000_000)
            self.clock = clock

        def read(self, amt=-1):
            self.clock.advance(kube_api.CLUSTER_DEADLINE)  # every read takes "forever"
            return super().read(amt)

    script = Script()
    script.answers = [SlowResponse(script.clock)]
    client = make_client(script, token_file)

    with pytest.raises(DeadlineExceeded):
        client.get_json("/api/v1/pods")


def test_start_gives_a_fresh_deadline(token_file):
    script = Script(ok(), ok())
    client = make_client(script, token_file)

    client.get_json("/api/v1/nodes")
    script.clock.advance(kube_api.CLUSTER_DEADLINE + 1)
    with pytest.raises(DeadlineExceeded):
        client.get_json("/api/v1/pods")
    client.start()
    client.get_json("/api/v1/pods")


# --- list_all: paging ---------------------------------------------------------------------------------


def page(items: list, token: str | None = None) -> FakeResponse:
    metadata = {"continue": token} if token else {}
    return ok({"items": items, "metadata": metadata})


def test_list_all_single_page(token_file):
    script = Script(page([{"a": 1}, {"a": 2}]))
    client = make_client(script, token_file)

    items, truncated = client.list_all("/api/v1/nodes")

    assert items == [{"a": 1}, {"a": 2}]
    assert truncated is False
    assert script.query(0) == {"limit": [str(kube_api.PAGE_LIMIT)]}


def test_list_all_follows_continue_tokens(token_file):
    script = Script(
        FakeResponse(200, fixture("list_page1")),
        FakeResponse(200, fixture("list_page2")),
    )
    client = make_client(script, token_file)

    items, truncated = client.list_all("/api/v1/pods", {"fieldSelector": "a=b"})

    assert len(items) == 4
    assert truncated is False
    assert script.query(0) == {"fieldSelector": ["a=b"], "limit": ["250"]}
    assert script.query(1) == {
        "fieldSelector": ["a=b"],
        "limit": ["250"],
        "continue": ["ENCODED-CONTINUE-TOKEN"],
    }
    assert len(script.connections) == 1


def test_continue_token_is_url_encoded(token_file):
    script = Script(page([{"n": 1}], "a b&c=d/e+f"), page([{"n": 2}]))
    make_client(script, token_file).list_all("/api/v1/pods")
    assert script.query(1)["continue"] == ["a b&c=d/e+f"]
    assert "&c=d" not in script.urls[1].split("continue=")[1]


def test_list_all_stops_at_max_pages_and_says_truncated(token_file, monkeypatch):
    monkeypatch.setattr(kube_api, "MAX_PAGES", 3)
    script = Script(
        page([{"n": 1}], "t1"),
        page([{"n": 2}], "t2"),
        page([{"n": 3}], "t3"),
        page([{"n": 4}], "t4"),
    )
    client = make_client(script, token_file)

    items, truncated = client.list_all("/api/v1/pods")

    assert items == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert truncated is True
    assert len(script.requests) == 3


def test_410_restarts_the_listing_once(token_file):
    script = Script(
        page([{"n": 1}], "t1"),
        FakeResponse(410, fixture("status_410")),
        page([{"n": 10}], "t2"),
        page([{"n": 20}]),
    )
    client = make_client(script, token_file)

    items, truncated = client.list_all("/api/v1/pods")

    assert items == [{"n": 10}, {"n": 20}]  # nothing from the abandoned first attempt
    assert truncated is False
    assert "continue" not in script.query(2)  # the restart starts from page one


def test_second_410_gives_up_with_what_it_has(token_file):
    script = Script(
        page([{"n": 1}], "t1"),
        FakeResponse(410, fixture("status_410")),
        page([{"n": 10}], "t2"),
        FakeResponse(410, fixture("status_410")),
    )
    client = make_client(script, token_file)

    items, truncated = client.list_all("/api/v1/pods")

    assert items == [{"n": 10}]
    assert truncated is True
    assert len(script.requests) == 4


def test_oversized_page_keeps_earlier_pages_and_says_truncated(token_file, monkeypatch):
    first = page([{"n": 1}, {"n": 2}], "t1")
    monkeypatch.setattr(kube_api, "MAX_RESPONSE_BYTES", len(first.body) + 5)
    script = Script(first, ok(b'{"items": [' + b'{"n": 0},' * 100 + b'{"n": 0}]}'))
    client = make_client(script, token_file)

    items, truncated = client.list_all("/api/v1/pods")

    assert items == [{"n": 1}, {"n": 2}]
    assert truncated is True


def test_list_without_items_is_a_bad_response(token_file):
    client = make_client(Script(ok({"kind": "Status"})), token_file)
    with pytest.raises(ApiError) as excinfo:
        client.list_all("/api/v1/pods")
    assert excinfo.value.kind == "bad_response"


def test_list_all_propagates_denied_and_not_installed(token_file):
    with pytest.raises(Denied):
        make_client(Script(FakeResponse(403, b"{}")), token_file).list_all("/api/v1/pods")
    with pytest.raises(NotInstalled):
        make_client(Script(FakeResponse(404, b"{}")), token_file).list_all("/api/v1/pods")


def test_list_all_ignores_non_object_items(token_file):
    client = make_client(Script(page([{"a": 1}, 5, "x", None])), token_file)
    assert client.list_all("/api/v1/pods")[0] == [{"a": 1}]


# --- the token ---------------------------------------------------------------------------------------


def test_token_is_read_again_on_start(token_file):
    script = Script(ok(), ok())
    client = make_client(script, token_file)
    client.get_json("/version")

    new_token = make_jwt({**CLAIMS, "sub": "system:serviceaccount:monitoring:rotated"})
    Path(token_file).write_text(new_token, encoding="utf-8")
    client.start()
    client.get_json("/version")

    assert script.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert script.requests[1]["headers"]["Authorization"] == f"Bearer {new_token}"
    assert client.token_details()["sub"].endswith(":rotated")


def test_token_surrounding_whitespace_is_stripped(token_file):
    Path(token_file).write_text(f"\n  {TOKEN} \n\n", encoding="utf-8")
    script = Script(ok())
    make_client(script, token_file).get_json("/version")
    assert script.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_world_readable_token_file_is_refused(token_file):
    os.chmod(token_file, 0o644)
    script = Script(ok())
    client = make_client(script, token_file)

    with pytest.raises(TokenError) as excinfo:
        client.get_json("/version")

    assert script.requests == []
    assert TOKEN not in str(excinfo.value)
    assert isinstance(excinfo.value, Unauthorized)


def test_missing_token_file(tmp_path):
    script = Script(ok())
    client = make_client(script, str(tmp_path / "nope"))
    with pytest.raises(TokenError):
        client.start()


@pytest.mark.parametrize(
    "content", ["", "short", "has a space in the token value", "tok\x00en" * 5, "é" * 30]
)
def test_malformed_token_file_is_refused(token_file, content):
    Path(token_file).write_text(content, encoding="utf-8")
    with pytest.raises(TokenError) as excinfo:
        make_client(Script(ok()), token_file).start()
    assert content.strip() not in str(excinfo.value) or not content.strip()


def test_binary_token_file_is_refused(token_file):
    Path(token_file).write_bytes(b"\xff\xfe\x00" * 50)
    with pytest.raises(TokenError):
        make_client(Script(ok()), token_file).start()


def test_oversized_token_file_is_refused(token_file):
    Path(token_file).write_text("a" * 100_000, encoding="utf-8")
    with pytest.raises(TokenError):
        make_client(Script(ok()), token_file).start()


def test_token_is_not_in_repr_or_str_of_the_client(token_file):
    client = make_client(Script(ok()), token_file)
    client.start()
    for text in (repr(client), str(client)):
        assert TOKEN not in text
        assert TOKEN.split(".")[1] not in text


@pytest.mark.parametrize(
    "answer",
    [
        FakeResponse(401),
        FakeResponse(403, fixture("status_403")),
        FakeResponse(404),
        FakeResponse(500),
        FakeResponse(302),
        TimeoutError("x"),
        ssl.SSLError("x"),
        ConnectionRefusedError("x"),
        FakeResponse(200, b"not json"),
    ],
)
def test_token_is_in_no_exception(token_file, answer):
    client = make_client(Script(answer), token_file)
    with pytest.raises(kube_api.KubeError) as excinfo:
        client.get_json("/api/v1/pods")
    error = excinfo.value
    for text in (
        str(error),
        repr(error),
        repr(error.args),
        repr(error.__cause__),
        repr(error.__context__),
    ):
        assert TOKEN not in text
        assert "Bearer" not in text


def test_token_is_not_logged(token_file, caplog):
    caplog.set_level("DEBUG")
    script = Script(FakeResponse(401))
    client = make_client(script, token_file)
    with pytest.raises(Unauthorized):
        client.get_json("/api/v1/pods")
    assert TOKEN not in caplog.text


# --- reading the token's claims (no signature check) --------------------------------------------------


def test_token_details_has_only_sub_and_exp(token_file):
    client = make_client(Script(), token_file)
    client.start()
    assert client.token_details() == {"sub": CLAIMS["sub"], "exp": CLAIMS["exp"]}


@pytest.mark.parametrize(
    "token",
    [
        "no-dots-at-all-in-this-token",
        "a.b",
        "a.!!!.c",
        "a." + b64({"sub": 5, "exp": "soon"}) + ".c",
        "a." + base64.urlsafe_b64encode(b"[1,2]").decode() + ".c",
        "a." + base64.urlsafe_b64encode(b"not json").decode() + ".c",
        "a." + b64({"sub": "bad sub with spaces\n", "exp": True}) + ".c",
        "a." + b64({"sub": "x" * 1000}) + ".c",
    ],
)
def test_token_details_never_raises(token):
    details = kube_api.token_claims(token)
    assert details == {"sub": None, "exp": None}


def test_token_claims_ignore_everything_else():
    token = make_jwt({**CLAIMS, "kubernetes.io": {"secret": {"name": "s3cret"}}})
    assert kube_api.token_claims(token) == {"sub": CLAIMS["sub"], "exp": CLAIMS["exp"]}


def test_token_days(token_file):
    client = make_client(Script(), token_file)
    client.start()
    assert kube_api.days_until(client.token_details()["exp"], now=NOW) == 100
    assert kube_api.days_until(None, now=NOW) is None
    assert kube_api.days_until(int(NOW) - 86400 * 3 - 1, now=NOW) == -4


# --- certificate days left ----------------------------------------------------------------------------


def test_cert_days_from_the_peer_certificate(token_file):
    expires = ssl.cert_time_to_seconds("Jan  1 00:00:00 2030 GMT")
    script = Script(ok(), cert={"notAfter": "Jan  1 00:00:00 2030 GMT"})
    client = make_client(script, token_file)

    client.get_json("/version")
    assert client.api_cert_days(now=expires - 290 * 86400 - 5) == 290
    assert client.api_cert_days(now=expires + 3 * 86400) == -3


def test_cert_days_is_none_without_a_certificate(token_file):
    script = Script(ok(), cert=None)
    client = make_client(script, token_file)
    client.get_json("/version")
    assert client.api_cert_days() is None


@pytest.mark.parametrize("cert", [{}, {"notAfter": 5}, {"notAfter": "garbage"}, {"subject": []}])
def test_cert_days_survives_odd_certificates(token_file, cert):
    script = Script(ok(), cert=cert)
    client = make_client(script, token_file)
    client.get_json("/version")
    assert client.api_cert_days() is None


def test_cert_days_before_any_request_is_none(token_file):
    assert make_client(Script(), token_file).api_cert_days() is None


# --- TLS settings ---------------------------------------------------------------------------------------


def test_ssl_context_always_verifies():
    context = kube_api.build_ssl_context(None)

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_ssl_context_with_a_ca_file_still_verifies():
    system_bundle = ssl.get_default_verify_paths().cafile or "/etc/ssl/certs/ca-certificates.crt"
    if not Path(system_bundle).is_file():
        pytest.skip("no CA bundle on this machine to use as a stand-in cluster CA")

    context = kube_api.build_ssl_context(system_bundle)

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_the_default_connection_uses_an_https_connection_class():
    assert (
        kube_api.KubeClient.__init__.__kwdefaults__["connection_factory"]
        is http.client.HTTPSConnection
    )


# --- the permission report (--check-k8s) ---------------------------------------------------------------


def all_ok_router(path: str) -> FakeResponse:
    return ok({"items": [], "metadata": {}})


def test_permission_report_all_ok(token_file):
    script = Script(router=all_ok_router, cert={"notAfter": "Jan  1 00:00:00 2030 GMT"})
    client = make_client(script, token_file)

    report = kube_api.permission_report(client, now=NOW)

    assert report["reachable"] is True
    assert report["error"] is None
    assert report["sa"] == CLAIMS["sub"]
    assert report["token_days"] == 100
    assert isinstance(report["cert_days"], int)
    assert [(r["group"], r["resource"], r["verb"]) for r in report["rows"]] == [
        (r.group, r.resource, r.verb) for r in kube_api.ALL_RESOURCES
    ]
    assert {r["result"] for r in report["rows"]} == {"ok"}
    assert {r["code"] for r in report["rows"]} == {200}


def test_permission_report_probes_with_limit_one_and_get_only(token_file):
    script = Script(router=all_ok_router)
    kube_api.permission_report(make_client(script, token_file), now=NOW)

    assert {r["method"] for r in script.requests} == {"GET"}
    list_urls = [u for u in script.urls if u not in ("/version",) and "namespaces" not in u]
    assert list_urls
    for url in list_urls:
        assert parse_qs(urlsplit(url).query)["limit"] == ["1"]
        assert "continue" not in url


def test_permission_report_records_denied_and_missing(token_file):
    def router(path: str) -> FakeResponse:
        if path == "/api/v1/pods":
            return FakeResponse(403, fixture("status_403"))
        if path == "/apis/argoproj.io/v1alpha1/applications":
            return FakeResponse(404, fixture("status_404"))
        return ok({"items": []})

    script = Script(router=router)
    report = kube_api.permission_report(make_client(script, token_file), now=NOW)

    results = {(r["group"], r["resource"]): r["result"] for r in report["rows"]}
    assert results[("", "pods")] == "denied"
    assert results[("argoproj.io", "applications")] == "missing"
    assert results[("", "nodes")] == "ok"
    codes = {(r["group"], r["resource"]): r["code"] for r in report["rows"]}
    assert codes[("", "pods")] == 403
    assert codes[("argoproj.io", "applications")] == 404
    assert "forbidden" not in json.dumps(report)  # the 403 body is not in the report


def test_permission_report_unreachable_api(token_file):
    script = Script(ConnectionRefusedError("refused"))
    report = kube_api.permission_report(make_client(script, token_file), now=NOW)

    assert report["reachable"] is False
    assert report["error"] == "refused"
    assert report["rows"] == []


def test_permission_report_unauthorized_stops_early(token_file):
    script = Script(router=lambda path: FakeResponse(401, b"{}"))
    report = kube_api.permission_report(make_client(script, token_file), now=NOW)

    assert report["reachable"] is True
    assert report["error"] == "unauthorized"
    assert len(script.requests) == 1


def test_permission_report_bad_token_file(tmp_path):
    script = Script(router=all_ok_router)
    report = kube_api.permission_report(make_client(script, str(tmp_path / "missing")), now=NOW)

    assert report["error"] == "unauthorized"
    assert script.requests == []


def test_permission_report_contains_no_token_or_object_data(token_file):
    def router(path: str) -> FakeResponse:
        return ok({"items": [{"metadata": {"name": "SENTINEL-SECRET-1"}}]})

    report = kube_api.permission_report(make_client(Script(router=router), token_file), now=NOW)

    text = json.dumps(report)
    assert TOKEN not in text
    assert "SENTINEL" not in text
