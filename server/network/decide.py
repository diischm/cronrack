"""Which point of the path failed, and what that means: pure functions, no database.

classify()        one report's network data -> a Verdict (state + one status per point)
hold()            flapping protection: a new state must be seen N reports in a row
router_rebooted() a short router outage that ended: an activity entry, not an alert

Points in path order: the router (gateway), up to 2 extra local points (e.g. the ISP box),
then the internet targets, which count as ONE point: it is down only when every target is
down, and slow when even the best one is slow (one bad anycast target never makes the
internet look down). Everything after the first point that is down is "untested": with
the router gone nothing behind it can be measured.

Precedence, first match wins:
  1 router down (or no default route)          router_down
  2 a later local point down                   local_down ("<name> down")
  3 every internet target down                 internet_down
  4 DNS fails while the internet answers       dns_failing
  5 best internet target slow (p95 or loss)    internet_slow
  6 otherwise                                  ok
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

OK = "ok"
DOWN = "down"
SLOW = "slow"
UNTESTED = "untested"

ROUTER_DOWN = "router_down"
LOCAL_DOWN = "local_down"
INTERNET_DOWN = "internet_down"
INTERNET_SLOW = "internet_slow"
DNS_FAILING = "dns_failing"
STATE_OK = "ok"
UNKNOWN = "unknown"

DEFAULT_LATENCY_MS = 150.0  # network.internet_slow threshold: p95 of the best target
DEFAULT_LOSS_PERCENT = 5.0  # network.packet_loss threshold
# A router that came back within this long rebooted (or was power-cycled).
REBOOT_WINDOW = timedelta(minutes=10)

# The strip's text per state; local_down uses the point's own name.
STATE_TEXT = {
    STATE_OK: "Network OK",
    ROUTER_DOWN: "Router down",
    INTERNET_DOWN: "Internet down",
    INTERNET_SLOW: "Internet slow",
    DNS_FAILING: "DNS failing",
    UNKNOWN: "Network unknown",
}


@dataclass(frozen=True)
class Verdict:
    state: str
    point: str = ""  # the name of the point that is down (local_down)
    # One status per point in path order: the local points, then the internet.
    chain: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        return (self.state, self.point)


def state_text(state: str, point: str = "") -> str:
    if state == LOCAL_DOWN:
        return f"{point} down"
    return STATE_TEXT.get(state, STATE_TEXT[UNKNOWN])


def loss_percent(point: Mapping[str, Any]) -> float:
    sent = point.get("sent") or 0
    return 100.0 * (sent - (point.get("received") or 0)) / sent if sent else 100.0


def _down(point: Mapping[str, Any]) -> bool:
    return not point.get("received")


def classify(
    network: Mapping[str, Any],
    latency_ms: float = DEFAULT_LATENCY_MS,
    loss_pct: float = DEFAULT_LOSS_PERCENT,
) -> Verdict:
    """The state of one report's "network" data (already validated by NetworkSerializer)."""
    points = list(network.get("points") or [])
    local = [p for p in points if p["role"] in ("gateway", "local")]
    internet = [p for p in points if p["role"] == "internet"]
    n = len(local) + 1  # + the internet point

    def chain_upto(i: int, status: str) -> tuple[str, ...]:
        """ok before point i, `status` at i, untested after it."""
        return (OK,) * i + (status,) + (UNTESTED,) * (n - i - 1)

    if not network.get("gateway_found"):
        # No default route: nothing past this host can be reached. Shown as the router.
        return Verdict(ROUTER_DOWN, "Router", (DOWN,) + (UNTESTED,) * n)
    for i, point in enumerate(local):
        if _down(point):
            state = ROUTER_DOWN if point["role"] == "gateway" else LOCAL_DOWN
            return Verdict(state, point["name"], chain_upto(i, DOWN))
    if not internet or all(_down(p) for p in internet):
        return Verdict(INTERNET_DOWN, "Internet", chain_upto(len(local), DOWN))
    best = min(
        (p for p in internet if not _down(p)),
        key=lambda p: (loss_percent(p), p.get("p95_ms") or 0),
    )
    detail = {"p95": best.get("p95_ms"), "loss": round(loss_percent(best), 1)}
    dns = network.get("dns")
    if isinstance(dns, Mapping) and not dns.get("ok"):
        return Verdict(DNS_FAILING, "", (OK,) * n, detail)
    slow_latency = (best.get("p95_ms") or 0) > latency_ms
    slow_loss = loss_percent(best) > loss_pct
    if slow_latency or slow_loss:
        reason = "both" if slow_latency and slow_loss else ("latency" if slow_latency else "loss")
        return Verdict(
            INTERNET_SLOW, "Internet", chain_upto(len(local), SLOW), {**detail, "reason": reason}
        )
    return Verdict(STATE_OK, "", (OK,) * n, detail)


@dataclass(frozen=True)
class Held:
    """What is held, and the candidate waiting to be confirmed."""

    state: str
    point: str = ""
    candidate_state: str = ""
    candidate_point: str = ""
    candidate_count: int = 0
    changed: bool = False


def hold(previous: Held, verdict: Verdict, needed: int) -> Held:
    """Flapping protection: a state is held once it was seen `needed` reports in a row.

    Both ways: into a problem and back to OK. The very first data (nothing held yet,
    "unknown") is taken at once: there is nothing to flap from.
    """
    key = verdict.key()
    if (previous.state, previous.point) == key:
        return Held(previous.state, previous.point)
    if previous.state == UNKNOWN:
        return Held(verdict.state, verdict.point, changed=True)
    same = (previous.candidate_state, previous.candidate_point) == key
    count = previous.candidate_count + 1 if same else 1
    if count >= max(1, needed):
        return Held(verdict.state, verdict.point, changed=True)
    return Held(previous.state, previous.point, verdict.state, verdict.point, count)


def router_rebooted(down_since: datetime | None, now: datetime, router_down_now: bool) -> bool:
    """True when the router answers again after being down for at most REBOOT_WINDOW."""
    return down_since is not None and not router_down_now and now - down_since <= REBOOT_WINDOW


def points_for_display(
    network: Mapping[str, Any] | None, chain: tuple[str, ...] | None
) -> list[dict[str, str]]:
    """The strip's points: You, the local points by name, Internet; each with a status.

    No data, or a chain that doesn't fit: every point "untested" ("Network unknown").
    """
    points = (network or {}).get("points") or []
    local = [p for p in points if p["role"] in ("gateway", "local")]
    if network is None or not network.get("gateway_found"):
        local = [{"name": "Router", "role": "gateway"}, *local]
    shown = [{"name": "You", "kind": "you"}]
    shown += [
        {"name": p["name"], "kind": "router" if p["role"] == "gateway" else "box"} for p in local
    ]
    shown.append({"name": "Internet", "kind": "internet"})
    if chain is None or len(chain) != len(shown) - 1:
        return [{**p, "status": UNTESTED} for p in shown]
    return [
        {**shown[0], "status": OK},
        *({**p, "status": s} for p, s in zip(shown[1:], chain, strict=True)),
    ]


def chain_for_held(
    state: str, point: str, network: Mapping[str, Any] | None
) -> tuple[str, ...] | None:
    """The chain to show for the HELD state (the strip follows it, not the latest report)."""
    if network is None or state == UNKNOWN:
        return None
    points = network.get("points") or []
    local = [p["name"] for p in points if p["role"] in ("gateway", "local")]
    if not network.get("gateway_found"):
        local = ["Router", *local]
    n = len(local) + 1

    def upto(i: int, status: str) -> tuple[str, ...]:
        return (OK,) * i + (status,) + (UNTESTED,) * (n - i - 1)

    if state == ROUTER_DOWN:
        return upto(0, DOWN)
    if state == LOCAL_DOWN:
        return upto(local.index(point), DOWN) if point in local else None
    if state == INTERNET_DOWN:
        return upto(n - 1, DOWN)
    if state == INTERNET_SLOW:
        return upto(n - 1, SLOW)
    return (OK,) * n


def line_before(previous: str, status: str) -> str:
    """The line into a point: solid green / yellow while the path works, else dashed."""
    if previous == OK and status == OK:
        return "ok"
    return "slow" if previous == OK and status == SLOW else "off"
