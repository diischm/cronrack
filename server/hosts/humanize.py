"""Short, human time strings for the UI ("40s ago", "14:32", "Yesterday")."""

import zoneinfo
from datetime import datetime

from django.utils import timezone


def duration(seconds: float) -> str:
    """'40s', '14 min', '3 h', '2 days'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"


def ago(when: datetime | None, now: datetime) -> str:
    """'just now', '40s ago', '14 min ago'; 'never' when there is no time."""
    if when is None:
        return "never"
    seconds = (now - when).total_seconds()
    if seconds < 5:
        return "just now"
    return f"{duration(seconds)} ago"


def user_zone(tz_name: str | None) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(tz_name or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def clock(when: datetime, tz: zoneinfo.ZoneInfo) -> str:
    """'14:32' in the user's time zone."""
    return timezone.localtime(when, tz).strftime("%H:%M")


def day(when: datetime, now: datetime, tz: zoneinfo.ZoneInfo) -> str:
    """'Today', 'Yesterday' or '12 Oct 2026', for grouping the activity list."""
    local = timezone.localtime(when, tz).date()
    today = timezone.localtime(now, tz).date()
    delta = (today - local).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    return f"{local.day} {local:%b %Y}"
