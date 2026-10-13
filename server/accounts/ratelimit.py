"""Login rate limit: at most 5 failed attempts per IP + username per 15 minutes.

Counted in Django's cache (in memory, one server process is enough for a homelab).
The cache key is a hash, so odd characters in a typed username can't break it.
"""

import hashlib

from django.core.cache import cache

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60


def _key(request, username: str) -> str:
    ip = request.META.get("REMOTE_ADDR", "") if request is not None else ""
    raw = f"{ip}|{(username or '').strip().lower()}"
    return "login-fail:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_login_rate(request, username: str) -> bool:
    """True if this IP may still try this username."""
    return cache.get(_key(request, username), 0) < MAX_FAILURES


def record_login_failure(request, username: str) -> None:
    key = _key(request, username)
    # add() only sets the key if it doesn't exist, so the 15-minute window starts
    # at the first failure and isn't extended by later ones.
    cache.add(key, 0, WINDOW_SECONDS)
    try:
        cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, WINDOW_SECONDS)


def clear_login_failures(request, username: str) -> None:
    cache.delete(_key(request, username))
