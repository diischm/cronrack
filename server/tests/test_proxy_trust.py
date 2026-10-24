"""uvicorn --forwarded-allow-ips with a network (CIDR), as docker-entrypoint.sh passes it.

In Kubernetes the reverse proxy (Traefik) runs in a pod whose IP changes, so
FORWARDED_ALLOW_IPS is the pod network (k3s default 10.42.0.0/16), not one address.
These tests run the installed uvicorn's own ProxyHeadersMiddleware, so they fail if a
uvicorn version without network support is ever pinned.
"""

import asyncio

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

TRUSTED = "10.42.0.0/16"


def seen_by_the_app(peer: str, headers: dict[str, str], trusted: str = TRUSTED) -> dict:
    """The scope the app gets for a request from `peer` with these headers."""
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"]
        seen["scheme"] = scope["scheme"]

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    scope = {
        "type": "http",
        "scheme": "http",
        "client": (peer, 51234),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(middleware(scope, None, None))
    return seen


FORWARDED = {"X-Forwarded-For": "100.101.102.103", "X-Forwarded-Proto": "https"}


@pytest.mark.parametrize("traefik_pod_ip", ["10.42.0.9", "10.42.1.200", "10.42.255.254"])
def test_headers_from_inside_the_trusted_network_are_believed(traefik_pod_ip):
    seen = seen_by_the_app(traefik_pod_ip, FORWARDED)
    assert seen["client"][0] == "100.101.102.103"
    assert seen["scheme"] == "https"


@pytest.mark.parametrize("peer", ["10.43.0.5", "192.168.1.50", "100.64.0.7"])
def test_headers_from_anywhere_else_are_ignored(peer):
    # Someone outside the pod network can't fake their address or "this was https".
    seen = seen_by_the_app(peer, FORWARDED)
    assert seen["client"][0] == peer
    assert seen["scheme"] == "http"


def test_several_entries_mix_addresses_and_networks():
    trusted = "172.29.80.10,10.42.0.0/16"
    assert seen_by_the_app("172.29.80.10", FORWARDED, trusted)["scheme"] == "https"
    assert seen_by_the_app("10.42.7.7", FORWARDED, trusted)["scheme"] == "https"
    assert seen_by_the_app("172.29.80.11", FORWARDED, trusted)["scheme"] == "http"


def test_websocket_scheme_follows_the_proxy_too():
    # The agent's wss:// arrives at uvicorn as plain ws:// from Traefik.
    seen = {}

    async def app(scope, receive, send):
        seen["scheme"] = scope["scheme"]

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=TRUSTED)
    scope = {
        "type": "websocket",
        "scheme": "ws",
        "client": ("10.42.0.9", 40000),
        "headers": [(b"x-forwarded-proto", b"https"), (b"x-forwarded-for", b"100.64.0.7")],
    }
    asyncio.run(middleware(scope, None, None))
    assert seen["scheme"] == "wss"
