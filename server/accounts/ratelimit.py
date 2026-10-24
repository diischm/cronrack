"""Rate limits for logging in and for the first-run setup code.

Login: at most 5 failed attempts per IP + username, and at most 20 failed attempts per
IP (any usernames), per 15 minutes. The per-IP limit matters because the cache only
holds so many entries: without it, one client could type hundreds of made-up usernames
until the cache throws away the entry that locks the real one. Once an IP is blocked,
nothing new is written for it, so a single client can't fill the cache.

Setup code: at most 5 wrong codes per IP per 15 minutes.

Counted in Django's cache (in memory, one server process is enough for a homelab).
The cache keys are hashes, so odd characters in a typed username can't break them.
"""

import hashlib

from django.core.cache import cache

MAX_FAILURES = 5  # per IP + username
MAX_FAILURES_PER_IP = 20  # per IP, any usernames
MAX_SETUP_CODE_FAILURES = 5  # per IP
WINDOW_SECONDS = 15 * 60


def _ip(request) -> str:
    return request.META.get("REMOTE_ADDR", "") if request is not None else ""


def _hashed(prefix: str, raw: str) -> str:
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _key(request, username: str) -> str:
    return _hashed("login-fail:", f"{_ip(request)}|{(username or '').strip().lower()}")


def _ip_key(request) -> str:
    return _hashed("login-fail-ip:", _ip(request))


def _setup_key(request) -> str:
    return _hashed("setup-fail-ip:", _ip(request))


def _count(key: str) -> int:
    return cache.get(key, 0)


def _hit(key: str) -> None:
    # add() only sets the key if it doesn't exist, so the 15-minute window starts
    # at the first failure and isn't extended by later ones.
    cache.add(key, 0, WINDOW_SECONDS)
    try:
        cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, WINDOW_SECONDS)


def check_login_rate(request, username: str) -> bool:
    """True if this IP may still try this username."""
    return (
        _count(_ip_key(request)) < MAX_FAILURES_PER_IP
        and _count(_key(request, username)) < MAX_FAILURES
    )


def record_login_failure(request, username: str) -> None:
    _hit(_key(request, username))
    _hit(_ip_key(request))


def clear_login_failures(request, username: str) -> None:
    # Only the IP + username counter. The per-IP counter runs out on its own, so
    # logging in to one account can't reset the limit for guessing others.
    cache.delete(_key(request, username))


def check_setup_code_rate(request) -> bool:
    """True if this IP may still try a setup code."""
    return _count(_setup_key(request)) < MAX_SETUP_CODE_FAILURES


def record_setup_code_failure(request) -> None:
    _hit(_setup_key(request))
