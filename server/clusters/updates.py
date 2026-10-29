"""Online update checks: what are the newest k3s, Argo CD and Helm versions?

This is the only place besides Discord where the server contacts the internet, so it is
deliberately narrow:
- exactly three fixed URLs (below), nothing is built from user or agent data
- nothing but the request itself is sent: no host names, versions or IDs
- no redirects, 5 s timeout, at most 256 KiB read, strict parsing of what comes back
- it runs only if at least one user turned "Check for updates online" on (default: off)
  and there is a cluster or a Kubernetes node to compare
- it never raises: a failure is logged by class name and tried again in an hour

The results are public and the same for everyone, so they are stored once, in the
database (the web server and the scheduler are separate processes).
"""

import http.client
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from clusters.models import Cluster, UpdateCheck
from cronrack.outbound import opener as _opener
from hosts.models import Host
from prefs.models import Prefs
from rules.versions import parse_version

logger = logging.getLogger(__name__)

K3S_URL = "https://update.k3s.io/v1-release/channels"
ARGOCD_URL = "https://api.github.com/repos/argoproj/argo-cd/releases/latest"
HELM_URL = "https://api.github.com/repos/helm/helm/releases/latest"

TIMEOUT_SECONDS = 5
MAX_BYTES = 256 * 1024
# After a success wait a day; after a failure try again in an hour.
SUCCESS_INTERVAL = timedelta(hours=24)
RETRY_INTERVAL = timedelta(hours=1)

# stable, latest, testing, and the per-minor channels (v1.31). Not "v1.31-testing".
_CHANNEL_ID = re.compile(r"(?:stable|latest|testing|v1\.\d{1,3})")
MAX_CHANNELS = 200


class UpdateCheckError(Exception):
    """Fixed text only: the class name or a short word goes into the log, nothing else."""


def fetch_json(url: str) -> Any:
    """GET one of the three fixed URLs and return the decoded JSON.

    Raises UpdateCheckError for anything unusual: another URL, a redirect, an HTTP
    error, a timeout, a body over MAX_BYTES, or something that isn't JSON.
    """
    if url not in SOURCES_BY_URL:
        raise UpdateCheckError("url_not_allowed")
    headers = {"User-Agent": "CronRack", "Accept": "application/json"}
    if url.startswith("https://api.github.com/"):
        headers["Accept"] = "application/vnd.github+json"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        # B310 reviewed: `url` is one of the three https constants above (checked just
        # before), so no file:// or other scheme can reach this call; redirects are refused.
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            if not 200 <= response.status < 300:
                raise UpdateCheckError(f"http_{response.status}")
            body = response.read(MAX_BYTES + 1)
    except UpdateCheckError:
        raise
    except urllib.error.HTTPError as exc:
        # Includes a refused redirect (the 3xx comes back as an error).
        raise UpdateCheckError(f"http_{exc.code}") from None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # Timeouts, DNS, TLS, connection resets.
        raise UpdateCheckError(type(exc).__name__) from None
    if len(body) > MAX_BYTES:
        raise UpdateCheckError("too_large")
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise UpdateCheckError("bad_json") from None


def parse_k3s_channels(data: Any) -> dict[str, str]:
    """{"stable": "v1.31.5+k3s1", "v1.30": "v1.30.9+k3s1", ...} from the channel list."""
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise UpdateCheckError("bad_shape")
    channels: dict[str, str] = {}
    for entry in entries[:MAX_CHANNELS]:
        if not isinstance(entry, dict):
            continue
        name, latest = entry.get("id"), entry.get("latest")
        if not isinstance(name, str) or not _CHANNEL_ID.fullmatch(name):
            continue
        if parse_version(latest) is None:
            continue
        channels[name] = latest
    if "stable" not in channels:
        raise UpdateCheckError("bad_shape")
    return channels


def parse_github_release(data: Any) -> dict[str, str]:
    """{"latest": "v3.5.6"} from GitHub's "latest release", or an error for anything else."""
    if not isinstance(data, dict) or data.get("draft") is not False:
        raise UpdateCheckError("bad_shape")
    if data.get("prerelease") is not False or parse_version(data.get("tag_name")) is None:
        raise UpdateCheckError("bad_shape")
    return {"latest": data["tag_name"]}


SOURCES: dict[str, tuple[str, Callable[[Any], dict[str, str]]]] = {
    "k3s": (K3S_URL, parse_k3s_channels),
    "argocd": (ARGOCD_URL, parse_github_release),
    "helm": (HELM_URL, parse_github_release),
}
SOURCES_BY_URL = {url: name for name, (url, _) in SOURCES.items()}


def wanted() -> bool:
    """Is there anyone to check for? A user who opted in, and something to compare."""
    if not Prefs.objects.filter(check_updates_online=True).exists():
        return False
    return (
        Cluster.objects.exists()
        or Host.objects.filter(latest_report__has_key="kubernetes").exists()
    )


def _due(row: UpdateCheck | None, now: datetime) -> bool:
    if row is None or row.attempted_at is None:
        return True
    wait = RETRY_INTERVAL if row.last_error else SUCCESS_INTERVAL
    return now - row.attempted_at >= wait


def _check_one(source: str, now: datetime) -> bool:
    url, parse = SOURCES[source]
    row = UpdateCheck.objects.filter(source=source).first()
    if not _due(row, now):
        return False
    try:
        data = parse(fetch_json(url))
    except UpdateCheckError as exc:
        error = str(exc)[:64]
        logger.warning("Update check for %s failed: %s", source, error)
        UpdateCheck.objects.update_or_create(
            source=source, defaults={"attempted_at": now, "last_error": error}
        )
        return False
    UpdateCheck.objects.update_or_create(
        source=source,
        defaults={"data": data, "checked_at": now, "attempted_at": now, "last_error": ""},
    )
    return True


def maybe_check_updates(now: datetime | None = None) -> list[str]:
    """Fetch what is due, if anyone wants it. Returns the sources updated. Never raises."""
    try:
        if now is None:
            now = timezone.now()
        if not wanted():
            return []
        updated = []
        for source in SOURCES:
            try:
                if _check_one(source, now):
                    updated.append(source)
            # Deliberately broad: a failed check must never fail the scheduler's pass.
            except Exception as exc:  # noqa: BLE001
                logger.warning("Update check for %s crashed: %s", source, type(exc).__name__)
        return updated
    except Exception as exc:  # noqa: BLE001
        logger.warning("Update checks crashed: %s", type(exc).__name__)
        return []


def latest_versions(owner_id: int) -> dict[str, dict[str, str]]:
    """The newest versions for the version rules; empty unless this owner opted in."""
    if not Prefs.objects.filter(owner_id=owner_id, check_updates_online=True).exists():
        return {}
    return {
        row.source: row.data
        for row in UpdateCheck.objects.all()
        if isinstance(row.data, dict) and row.data
    }
