"""Fixed sample data for the dev preview (cronrack.dev_views): the 5 states of
design-ref/glass/network-minimal.html plus the path with points before the router, in the
shape of network.presenters. Documentation
addresses only (RFC 5737), plus the default public resolvers. Nothing here is ever counted."""

from network import decide
from network.presenters import _row, _sentence, with_words

_ROUTER = {"name": "Router", "role": "gateway", "address": "192.0.2.1", "sent": 5}
_BOX = {"name": "ISP box", "role": "local", "address": "192.0.2.254", "sent": 5}
_NET = {"name": "1.1.1.1", "role": "internet", "address": "1.1.1.1", "sent": 5}
_SWITCH = {"name": "Switch", "role": "before", "address": "192.0.2.2", "sent": 5}
_AP = {"name": "Office access point", "role": "before", "address": "192.0.2.3", "sent": 5}
_MODEM = {"name": "Fibre modem", "role": "local", "address": "192.0.2.253", "sent": 5}


def _answers(point, median, p95, received=5):
    return {**point, "received": received, "median_ms": median, "p95_ms": p95}


def _silent(point):
    return {**point, "received": 0, "median_ms": None, "p95_ms": None}


_OK_LOCAL = (_answers(_ROUTER, 2.1, 2.9), _answers(_BOX, 4.0, 5.2))
_STATES = {
    "ok": (decide.STATE_OK, "", (*_OK_LOCAL, _answers(_NET, 18.0, 22.0)), {}, "12 ms"),
    "router": (
        decide.ROUTER_DOWN,
        "Router",
        (_silent(_ROUTER), _silent(_BOX), _silent(_NET)),
        {},
        "failing",
    ),
    "isp": (
        decide.LOCAL_DOWN,
        "ISP box",
        (_answers(_ROUTER, 2.1, 2.9), _silent(_BOX), _silent(_NET)),
        {},
        "failing",
    ),
    "internet": (decide.INTERNET_DOWN, "Internet", (*_OK_LOCAL, _silent(_NET)), {}, "failing"),
    "slow": (
        decide.INTERNET_SLOW,
        "Internet",
        (*_OK_LOCAL, _answers(_NET, 210.0, 320.0, received=4)),
        {"p95": 320.0, "loss": 20.0, "reason": "both"},
        "40 ms",
    ),
    "unknown": (decide.UNKNOWN, "", (*_OK_LOCAL, _answers(_NET, 18.0, 22.0)), {}, "12 ms"),
    "switch": (
        decide.LOCAL_DOWN,
        "Switch",
        (_silent(_SWITCH), _silent(_ROUTER), _silent(_BOX), _silent(_NET)),
        {},
        "failing",
    ),
    "switch-router": (
        decide.ROUTER_DOWN,
        "Router",
        (_answers(_SWITCH, 1.0, 1.4), _silent(_ROUTER), _silent(_BOX), _silent(_NET)),
        {},
        "failing",
    ),
    "switch-internet": (
        decide.INTERNET_DOWN,
        "Internet",
        (_answers(_SWITCH, 1.0, 1.4), *_OK_LOCAL, _silent(_NET)),
        {},
        "failing",
    ),
    "switch-slow": (
        decide.INTERNET_SLOW,
        "Internet",
        (_answers(_SWITCH, 1.0, 1.4), *_OK_LOCAL, _answers(_NET, 210.0, 320.0, received=4)),
        {"p95": 320.0, "loss": 20.0, "reason": "both"},
        "40 ms",
    ),
    # The longest path: 2 before + the router + 2 after, with long names.
    "max": (
        decide.STATE_OK,
        "",
        (
            _answers(_SWITCH, 1.0, 1.4),
            _answers(_AP, 1.2, 1.6),
            *_OK_LOCAL,
            _answers(_MODEM, 5.0, 6.1),
            _answers(_NET, 18.0, 22.0),
        ),
        {},
        "12 ms",
    ),
}
NETWORK_STATES = tuple(_STATES)


def _latest(points):
    return {"gateway_found": True, "points": list(points)}


def sample_summary(name: str) -> dict:
    state, point, points, _, _ = _STATES[name]
    latest = _latest(points)
    shown = decide.points_for_display(latest, decide.chain_for_held(state, point, latest))
    return {
        "state": state,
        "text": decide.state_text(state, point),
        "points": with_words(shown),
        "url": "/dev/preview/network/?net=" + name,
    }


def _trend(step: int) -> dict:
    line = " ".join(f"{i},{12 + ((i * step) % 9):.1f}" for i in range(96))
    return {"has_data": True, "line": line, "bars": [{"x": 70, "y": "16.0", "h": "8.0"}],
            "top": "24 ms", "w": 96, "h": 24}  # fmt: skip


def sample_context(name: str) -> dict:
    state, point, points, detail, dns = _STATES[name]
    summary = sample_summary(name)
    by_name = {p["name"]: p for p in points}
    rows = []
    for item in summary["points"][1:]:
        if item["kind"] == "internet":
            row = _row(
                "Internet", "1.1.1.1, 8.8.8.8", item["status"], by_name["1.1.1.1"], item["kind"]
            )
        else:
            source = by_name[item["name"]]
            row = _row(item["name"], source["address"], item["status"], source, item["kind"])
        row["trend"] = _trend(len(rows) + 2)
        rows.append(row)
    return {
        "network": {
            "state": state,
            "text": summary["text"],
            "sentence": _sentence(state, point, detail),
            "points": summary["points"],
            "rows": rows,
            "facts": {
                "dns": dns,
                "public_ip": None,
                "last_reboot": "3 days ago",
                "method": "ping",
                "tcp": False,
                "checked": "just now",
            },
            "advice": [],
            "confirm_reports": 3,
            "view_host": {"id": 1, "name": "host-a"},
            "view_is_auto": True,
        },
        "reporters": [
            {
                "id": 1,
                "name": "host-a",
                "server_local": True,
                "is_view": True,
                "last_report": "just now",
            },
            {
                "id": 2,
                "name": "host-b",
                "server_local": False,
                "is_view": False,
                "last_report": "20s ago",
            },
        ],
    }
