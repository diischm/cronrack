"""Turn metric points into what templates/components/metric_chart.html draws.

Server-rendered SVG: the strict CSP allows no inline styles or scripts, and a chart here is
only an SVG path made of numbers this module computes. No agent text ever goes into a path.
The one piece of text is the accessible summary, which Django escapes like any other value.

Pure Python (no database), so every chart can be tested with a list of points.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

WIDTH = 520
HEIGHT = 96
SMALL_HEIGHT = 64
MAX_SMALL_POINTS = 96  # the per-container charts: they are rendered for every container

Points = Sequence[tuple[datetime, float]]


def downsample(points: Points, limit: int) -> list[tuple[datetime, float]]:
    """At most `limit` points: each one the average of an equal slice of the originals."""
    if len(points) <= limit:
        return list(points)
    size = len(points) / limit
    result = []
    for index in range(limit):
        part = points[int(index * size) : int((index + 1) * size)]
        result.append((part[-1][0], sum(v for _, v in part) / len(part)))
    return result


def _segments(points: Points, gap: timedelta) -> list[list[tuple[datetime, float]]]:
    """Split where samples are missing, so an outage is a hole and not a straight line."""
    segments: list[list[tuple[datetime, float]]] = []
    for point in points:
        if segments and point[0] - segments[-1][-1][0] <= gap:
            segments[-1].append(point)
        else:
            segments.append([point])
    return segments


def _number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") if "." in f"{value:.1f}" else f"{value:.1f}"


def _label(value: float, unit: str) -> str:
    return f"{value:.0f}{unit}"


def build_chart(
    points: Points,
    start: datetime,
    end: datetime,
    *,
    title: str,
    unit: str,
    range_words: str,
    gap: timedelta,
    limit: float | None = None,
    max_value: float = 100.0,
    height: int = HEIGHT,
    extra: str = "",
    level: str = "",
) -> dict[str, Any]:
    """The template's view of one chart. `limit`: where the dashed line and the colour start."""
    limit_label = _label(limit, unit) if limit is not None else ""
    base: dict[str, Any] = {
        "title": title,
        "extra": extra,
        "level": level,
        "limit_label": limit_label,
    }
    points = [p for p in points if start <= p[0] <= end]
    if not points:
        return {**base, "empty": True, "summary": f"{title}: no data yet for the {range_words}."}

    span = (end - start).total_seconds() or 1.0

    def x(at: datetime) -> float:
        return (at - start).total_seconds() / span * WIDTH

    def y(value: float) -> float:
        return height - max(0.0, min(1.0, value / max_value)) * height

    line, area = [], []
    for segment in _segments(points, gap):
        coords = [(x(at), y(value)) for at, value in segment]
        if len(coords) == 1:  # a lone sample still needs a visible mark
            coords.append((coords[0][0] + 2, coords[0][1]))
        path = " ".join(f"{_number(cx)},{_number(cy)}" for cx, cy in coords)
        line.append(f"M{path}")
        first, last = _number(coords[0][0]), _number(coords[-1][0])
        area.append(f"M{first},{height} L{path} L{last},{height} Z")

    values = [v for _, v in points]
    low, high, mean, now = min(values), max(values), sum(values) / len(values), values[-1]
    summary = (
        f"{title}, {range_words}: lowest {_label(low, unit)}, average {_label(mean, unit)}, "
        f"highest {_label(high, unit)}, now {_label(now, unit)}."
    )
    if limit is not None:
        summary += f" Limit {limit_label}."
        if high >= limit:
            summary += " It went over the limit."
    return {
        **base,
        "empty": False,
        "summary": summary,
        "min": _label(low, unit),
        "avg": _label(mean, unit),
        "max": _label(high, unit),
        "now": _label(now, unit),
        "width": WIDTH,
        "height": height,
        "line": " ".join(line),
        "area": " ".join(area),
        "limit_y": _number(y(limit)) if limit is not None else None,
        "over": limit is not None and high >= limit,
    }
