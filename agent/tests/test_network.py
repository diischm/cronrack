"""agent/network.py and the [network] table of agent.toml. No real packet is sent: the
probes are replaced by fakes; the gateway comes from a fixture route table."""

import io
import logging
import struct
from pathlib import Path

import pytest

from agent import network
from agent.config import ConfigError, NetworkConfig, NetworkHop, _parse

ROUTE = (
    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
    "eth0\t0002000A\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"  # 10.0.2.0/24, no gateway
    "eth0\t00000000\t010200C0\t0003\t0\t0\t100\t00000000\n"  # default via 192.0.2.1
)


# --- config ---------------------------------------------------------------------------------


def test_network_is_off_by_default():
    config = _parse({})
    assert config.network.enabled is False
    assert config.network.public_ip_check is False  # never talks to a third party unasked
    assert config.network.internet == ["1.1.1.1", "8.8.8.8"]
    assert config.network.gateway == "auto"


def test_full_network_table():
    config = _parse({"network": {
        "enabled": True, "gateway": "192.0.2.1",
        "hops": [{"name": "ISP box", "address": "192.0.2.254"}],
        "internet": ["9.9.9.9", "9.9.9.9", "8.8.8.8"],
        "dns": False, "dns_name": "example.org",
        "public_ip_check": True, "public_ip_url": "https://ip.example.org/",
    }}).network  # fmt: skip
    assert config.hops == [NetworkHop("ISP box", "192.0.2.254")]
    assert config.internet == ["9.9.9.9", "8.8.8.8"]  # duplicates dropped
    assert (config.dns, config.dns_name, config.public_ip_check) == (False, "example.org", True)


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"surprise": 1}, "unknown"),
        ({"enabled": "yes"}, "'enabled'"),
        ({"gateway": "8.8.8.8"}, "'gateway'"),  # a router is a private address
        ({"gateway": "router.lan"}, "'gateway'"),  # names would need DNS
        ({"hops": [{"name": "ISP box", "address": "1.1.1.1"}]}, "'hops'"),
        ({"hops": [{"name": "x", "address": "192.0.2.9", "port": 1}]}, "'hops'"),
        ({"hops": [{"name": "a\nb", "address": "192.168.1.2"}]}, "printable"),
        ({"hops": [{"name": "x" * 33, "address": "192.168.1.2"}]}, "printable"),
        ({"hops": [{"name": n, "address": "192.168.1.2"} for n in "abc"]}, "at most 2"),
        ({"hops": [{"name": "router", "address": "192.168.1.2"}]}, "own name"),
        ({"hops": [{"name": "a", "address": "192.168.1.2"}] * 2}, "own name"),
        ({"hops": [{"name": "1.1.1.1", "address": "192.168.1.2"}]}, "not be an address"),
        ({"hops": [{"name": "a", "address": "240.0.0.1"}]}, "'hops'"),
        ({"before": [{"name": "Switch", "address": "1.1.1.1"}]}, "'before'"),
        ({"before": [{"name": "x", "address": "192.0.2.9", "port": 1}]}, "'before'"),
        ({"before": "Switch"}, "'before'"),
        ({"before": [{"name": "a\nb", "address": "192.0.2.9"}]}, "printable"),
        ({"before": [{"name": "x" * 33, "address": "192.0.2.9"}]}, "printable"),
        ({"before": [{"name": n, "address": "192.0.2.9"} for n in "abc"]}, "at most 2"),
        ({"before": [{"name": "You", "address": "192.0.2.9"}]}, "own name"),
        ({"before": [{"name": "router", "address": "192.0.2.9"}]}, "own name"),
        ({"before": [{"name": "INTERNET", "address": "192.0.2.9"}]}, "own name"),
        ({"before": [{"name": "a", "address": "192.0.2.9"}] * 2}, "own name"),
        ({"before": [{"name": "1.1.1.1", "address": "192.0.2.9"}]}, "not be an address"),
        (
            {
                "before": [{"name": "Same", "address": "192.0.2.2"}],
                "hops": [{"name": "Same", "address": "192.0.2.3"}],
            },
            "own name",
        ),
        (
            {"gateway": "192.0.2.1", "before": [{"name": "Switch", "address": "192.0.2.1"}]},
            "router's address",
        ),
        ({"internet": ["192.168.1.1"]}, "'internet'"),  # must be a public address
        ({"internet": ["198.51.100.53"]}, "'internet'"),  # documentation range: not public
        ({"internet": []}, "'internet'"),
        ({"internet": ["1.1.1.1"] * 5}, "'internet'"),
        ({"dns_name": "not a name!"}, "'dns_name'"),
        ({"public_ip_url": "http://ip.example.org"}, "https"),
        ({"public_ip_url": "https://u:p@ip.example.org"}, "user name"),
    ],
)
def test_bad_network_settings_are_refused(table, message):
    with pytest.raises(ConfigError, match=message) as error:
        _parse({"network": table})
    for address in ("1.1.1.1", "192.168", "192.0.2"):  # errors name the key, never the value
        assert address not in str(error.value)


def test_before_points_keep_their_order():
    config = _parse({"network": {"before": [
        {"name": "Switch", "address": "192.0.2.2"}, {"name": "AP", "address": "192.0.2.3"},
    ]}}).network  # fmt: skip
    assert config.before == [NetworkHop("Switch", "192.0.2.2"), NetworkHop("AP", "192.0.2.3")]
    assert _parse({}).network.before == []


@pytest.mark.parametrize(
    ("address", "local", "internet"),
    [
        ("192.168.77.1", True, False),
        ("10.0.0.1", True, False),
        ("172.16.0.1", True, False),
        ("100.64.0.1", True, False),  # CGNAT: the ISP box side of some routers
        ("169.254.1.1", True, False),
        ("192.0.2.1", True, False),  # documentation range (RFC 5737): never routed
        ("127.0.0.1", False, False),
        ("0.0.0.0", False, False),
        ("224.0.0.1", False, False),
        ("1.1.1.1", False, True),
        ("not-an-ip", False, False),
    ],
)
def test_address_kinds(address, local, internet):
    from agent.config import is_internet_address, is_local_address

    assert is_local_address(address) is local
    assert is_internet_address(address) is internet


# --- gateway --------------------------------------------------------------------------------


def test_default_gateway_from_the_route_table(tmp_path: Path):
    assert network.parse_default_gateway(ROUTE) == "192.0.2.1"
    path = tmp_path / "route"
    path.write_text(ROUTE)
    assert network.read_default_gateway(str(path)) == "192.0.2.1"


@pytest.mark.parametrize(
    "text",
    [
        "Iface\tDestination\tGateway\tFlags\n",
        "Iface\tDestination\tGateway\tFlags\neth0\t00000000\t010200C0\t0001\n",  # not a gateway route
        "Iface\tDestination\tGateway\tFlags\neth0\t00000000\tZZ\t0003\n",
        "garbage",
    ],
)
def test_no_default_gateway(text):
    assert network.parse_default_gateway(text) is None


def test_unreadable_route_table_is_no_gateway(tmp_path: Path):
    assert network.read_default_gateway(str(tmp_path / "missing")) is None


# --- probes and statistics -------------------------------------------------------------------


def test_echo_request_checksum_is_valid():
    packet = network.echo_request(7)
    assert packet[0] == 8 and struct.unpack("!H", packet[6:8])[0] == 7
    assert network._checksum(packet) == 0  # a valid ICMP checksum sums to zero


def test_summarize():
    result = network.summarize([10.0, None, 30.0, 20.0, 100.0])
    assert result == {"sent": 5, "received": 4, "median_ms": 25.0, "p95_ms": 100.0}
    assert network.summarize([None] * 5) == {
        "sent": 5, "received": 0, "median_ms": None, "p95_ms": None,
    }  # fmt: skip


def test_probes_are_spaced_never_a_flood():
    sleeps = []
    result = network.probe_target(lambda seq: 1.0, count=5, spacing=0.2, sleep=sleeps.append)
    assert result["sent"] == 5 and len(sleeps) == 4
    assert all(0.15 < s <= 0.2 for s in sleeps)


def test_tcp_probe_counts_a_refusal_as_an_answer(monkeypatch):
    def refuse(address, timeout):
        raise ConnectionRefusedError

    monkeypatch.setattr(network.socket, "create_connection", refuse)
    assert network.tcp_probe("192.0.2.1", (53,)) is not None


def test_tcp_probe_tries_the_next_port_then_gives_up(monkeypatch):
    tried = []

    def drop(address, timeout):
        tried.append(address[1])
        raise TimeoutError

    monkeypatch.setattr(network.socket, "create_connection", drop)
    assert network.tcp_probe("192.0.2.1", (53, 80)) is None
    assert tried == [53, 80]


class FakeClock:
    """Time that only moves when a fake connect or sleep says so."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_local_points_are_tried_on_dns_web_and_https_never_ssh():
    assert network.LOCAL_TCP_PORTS == (53, 80, 443)
    assert 22 not in network.LOCAL_TCP_PORTS + network.INTERNET_TCP_PORTS


def test_a_point_answering_only_on_443_is_reachable(monkeypatch):
    """A filtered port no longer eats the budget of the ports behind it."""
    clock = FakeClock()
    monkeypatch.setattr(network.time, "monotonic", clock.monotonic)

    def only_443(address, timeout):
        if address[1] != 443:
            clock.advance(timeout)  # filtered: nothing comes back until the timeout
            raise TimeoutError
        clock.advance(0.002)
        return io.BytesIO()

    monkeypatch.setattr(network.socket, "create_connection", only_443)
    assert network.tcp_probe("192.0.2.2", network.LOCAL_TCP_PORTS) is not None


def test_a_fully_filtered_point_is_down_within_the_budget(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(network.time, "monotonic", clock.monotonic)
    tried = []

    def drop(address, timeout):
        tried.append(address[1])
        clock.advance(timeout)
        raise TimeoutError

    monkeypatch.setattr(network.socket, "create_connection", drop)
    start = clock.now
    result = network.probe_target(
        lambda _seq: network.tcp_probe("192.0.2.2", network.LOCAL_TCP_PORTS),
        sleep=clock.advance,
    )
    took = clock.now - start
    assert result["received"] == 0
    assert tried == list(network.LOCAL_TCP_PORTS) * network.PROBES  # every port got its turn
    assert took <= network.PROBES * network.PROBE_TIMEOUT + 0.01  # same cost as before
    assert took < network.ROUND_DEADLINE


def test_time_a_refused_port_saved_goes_to_the_next_ports(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(network.time, "monotonic", clock.monotonic)
    given = []

    def drop(address, timeout):
        given.append(timeout)
        if len(given) > 1:
            clock.advance(timeout)
        raise TimeoutError  # the first one fails at once (e.g. no route)

    monkeypatch.setattr(network.socket, "create_connection", drop)
    assert network.tcp_probe("192.0.2.2", (53, 80, 443), timeout=0.9) is None
    assert given == pytest.approx([0.3, 0.45, 0.45])


# --- one round ------------------------------------------------------------------------------


@pytest.fixture
def fake_probes(monkeypatch):
    """Router answers, the ISP box doesn't, the internet answers; no real packets."""
    answers = {"192.0.2.1": 2.0, "192.0.2.254": None, "198.51.100.53": 20.0}
    monkeypatch.setattr(network, "icmp_available", lambda: True)
    monkeypatch.setattr(network, "icmp_probe", lambda address, seq: answers[address])
    monkeypatch.setattr(network, "PROBE_SPACING", 0)
    monkeypatch.setattr(network, "check_dns", lambda name: {"ok": True, "ms": 5.0})
    monkeypatch.setattr(network, "server_is_local", lambda url: False)
    monkeypatch.setattr(network, "check_public_ip", failing)
    return answers


def failing(*args):
    raise AssertionError("must not run")


def test_a_round_in_path_order(fake_probes, tmp_path: Path):
    route = tmp_path / "route"
    route.write_text(ROUTE)
    config = NetworkConfig(
        enabled=True, hops=[NetworkHop("ISP box", "192.0.2.254")], internet=["198.51.100.53"]
    )
    result = network.collect_network(config, "https://cronrack.example", route_path=str(route))
    assert [(p["name"], p["role"], p["received"]) for p in result["points"]] == [
        ("Router", "gateway", 5),
        ("ISP box", "local", 0),
        ("198.51.100.53", "internet", 5),
    ]
    assert result["method"] == "icmp" and result["gateway_found"] is True
    assert result["dns"] == {"ok": True, "ms": 5.0}
    assert result["public_ip"] is None  # public_ip_check is off: never asked


def test_before_points_come_first_in_the_report(fake_probes, tmp_path: Path):
    fake_probes.update({"192.0.2.2": 1.0, "192.0.2.3": None})
    route = tmp_path / "route"
    route.write_text(ROUTE)
    config = NetworkConfig(
        enabled=True,
        before=[NetworkHop("Switch", "192.0.2.2"), NetworkHop("AP", "192.0.2.3")],
        hops=[NetworkHop("ISP box", "192.0.2.254")],
        internet=["198.51.100.53"],
    )
    result = network.collect_network(config, None, route_path=str(route))
    assert [(p["name"], p["role"], p["received"]) for p in result["points"]] == [
        ("Switch", "before", 5),
        ("AP", "before", 0),
        ("Router", "gateway", 5),
        ("ISP box", "local", 0),
        ("198.51.100.53", "internet", 5),
    ]


def test_before_points_are_still_sent_without_a_default_route(fake_probes, tmp_path: Path):
    fake_probes["192.0.2.2"] = 1.0
    route = tmp_path / "route"
    route.write_text("Iface\tDestination\tGateway\tFlags\n")
    config = NetworkConfig(
        enabled=True, before=[NetworkHop("Switch", "192.0.2.2")], internet=["198.51.100.53"]
    )
    result = network.collect_network(config, None, route_path=str(route))
    assert [p["role"] for p in result["points"]] == ["before", "internet"]
    assert result["gateway_found"] is False


def test_a_before_point_that_is_the_detected_router_is_dropped_with_one_warning(
    fake_probes, tmp_path: Path, caplog, monkeypatch
):
    monkeypatch.setattr(network, "_warned_before_is_router", False)
    route = tmp_path / "route"
    route.write_text(ROUTE)  # default via 192.0.2.1
    config = NetworkConfig(
        enabled=True, before=[NetworkHop("Switch", "192.0.2.1")], internet=["198.51.100.53"]
    )
    with caplog.at_level(logging.INFO, logger="agent.network"):
        first = network.collect_network(config, None, route_path=str(route))
        second = network.collect_network(config, None, route_path=str(route))
    for result in (first, second):
        assert [p["name"] for p in result["points"]] == ["Router", "198.51.100.53"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Switch" in warnings[0].getMessage()
    assert "192.0.2.1" not in caplog.text  # addresses stay at DEBUG


def test_targets_come_only_from_the_config(fake_probes, tmp_path: Path):
    route = tmp_path / "route"
    route.write_text("Iface\tDestination\tGateway\tFlags\n")  # no default route
    config = NetworkConfig(enabled=True, internet=["198.51.100.53"])
    result = network.collect_network(config, None, route_path=str(route))
    assert [p["address"] for p in result["points"]] == ["198.51.100.53"]
    assert result["gateway_found"] is False


def test_tcp_fallback_when_ping_sockets_are_not_allowed(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(network, "icmp_available", lambda: False)
    monkeypatch.setattr(network, "icmp_probe", failing)
    seen = []
    monkeypatch.setattr(network, "tcp_probe", lambda address, ports: seen.append(ports) or 3.0)
    monkeypatch.setattr(network, "PROBE_SPACING", 0)
    monkeypatch.setattr(network, "server_is_local", lambda url: False)
    config = NetworkConfig(enabled=True, gateway="192.0.2.1", internet=["198.51.100.53"], dns=False)
    result = network.collect_network(config, None)
    assert result["method"] == "tcp" and result["dns"] is None
    assert set(seen) == {network.LOCAL_TCP_PORTS, network.INTERNET_TCP_PORTS}


def test_a_hanging_probe_never_stalls_the_report(monkeypatch):
    monkeypatch.setattr(network, "ROUND_DEADLINE", 0.2)
    monkeypatch.setattr(network, "icmp_available", lambda: True)
    monkeypatch.setattr(network, "probe_target", lambda probe: __import__("time").sleep(1) or {})
    monkeypatch.setattr(network, "server_is_local", lambda url: False)
    config = NetworkConfig(enabled=True, gateway="192.0.2.1", internet=["198.51.100.53"], dns=False)
    result = network.collect_network(config, None)
    assert [p["received"] for p in result["points"]] == [0, 0]  # unfinished = no answer


def test_rounds_are_rate_limited():
    calls = []
    clock = [0.0]
    limited = network.RateLimitedNetwork(
        clock=lambda: clock[0], collect=lambda cfg, url: calls.append(1) or {"n": len(calls)}
    )
    config = NetworkConfig(enabled=True)
    assert limited(config, None) == {"n": 1}
    clock[0] = 10.0  # a "collect" right after: the last round is reused
    assert limited(config, None) == {"n": 1}
    clock[0] = network.MIN_ROUND_GAP
    assert limited(config, None) == {"n": 2}


def test_server_is_local_for_loopback_and_not_for_others(monkeypatch):
    assert network.server_is_local("https://127.0.0.1:8443") is True
    assert network.server_is_local(None) is False
    # 192.0.2.10 (TEST-NET-1) is never an address of this machine: bind() fails.
    assert network.server_is_local("https://192.0.2.10") is False


def test_public_ip_must_be_a_global_address(monkeypatch):
    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return self.body[:n]

    for body, expected in (
        (b"198.51.100.7\n", None),
        (b"8.8.4.4", "8.8.4.4"),
        (b"<html>", None),
        (b"192.168.1.1", None),
    ):
        monkeypatch.setattr(
            network.urllib.request, "urlopen", lambda url, timeout, b=body: Response(b)
        )
        assert network.check_public_ip("https://ip.example.org") == expected


def test_an_unusable_detected_gateway_counts_as_none(fake_probes, tmp_path: Path):
    route = tmp_path / "route"
    # Default via 224.0.0.1: the server would refuse the report; a public one stays.
    route.write_text("Iface\tDestination\tGateway\tFlags\neth0\t00000000\t010000E0\t0003\n")
    config = NetworkConfig(enabled=True, internet=["198.51.100.53"])
    assert network.collect_network(config, None, route_path=str(route))["gateway_found"] is False
