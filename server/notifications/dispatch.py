"""One Discord message per problem: open, "now red", resolved.

Two steps, so the HTTP call never runs inside a database transaction:

1. sync() runs in the transaction that saved an evaluation (a host report, a cluster
   snapshot, the network's state, or the scheduler marking something offline). It updates
   one FindingState row per finding (keyed by the finding's fingerprint) and leaves the
   message still to send in the row's `announce` column (the outbox).
2. deliver() runs after the commit (and from the scheduler, for whatever the cooldown held
   back). It sends the outbox of one host, cluster or network as ONE message.

Rules, in plain words:
- Level "alert" sends; "show" only keeps the row (the daily summary reads it); "off"
  never gets here (the rule engine drops the finding).
- The same problem keeps its row: the text and severity are updated in place, and
  crossing into red sends one "now red" message, never a resolve plus a reopen.
- Flap guard: a finding counts as resolved only after RESOLVE_AFTER evaluations in a row
  without it; one that comes back within FLAP_WINDOW of resolving reuses its row and
  sends nothing new.
- Offline findings are resolved by the scheduler (sweep_offline), after 2 passes with the
  host or cluster back.
- Quiet hours: only "needs you" (red) goes out; the rest waits for one bundled "while
  you were asleep" message when quiet hours end (skipped if it all resolved meanwhile).
- Maintenance holds a host's messages until it ends.
- A subject listed in BaselinePending (it existed before this feature) has its first
  evaluation recorded silently: no burst of old problems right after the upgrade.
"""

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from alerts import discord
from clusters.models import Cluster
from hosts.humanize import user_zone
from hosts.models import Host
from network.models import Network
from notifications import text as words
from notifications.config import NotifyConfig, load_config
from notifications.models import BaselinePending, FindingState
from notifications.rules import ALERT, OFF
from rules.base import IGNORE, RED, SEVERITY_RANK, Finding
from rules.engine import OFFLINE, OFFLINE_RULES

logger = logging.getLogger(__name__)

FLAP_WINDOW = timedelta(minutes=15)
RESOLVE_AFTER = 2  # evaluations (or scheduler passes, for offline) without the finding
RETENTION = timedelta(days=30)
# Subjects come from agents, so a misbehaving one could invent a new name every report.
# Past this many open problems per host (cluster, network), new ones get no row: they are
# still on the dashboard, just not sent. Far more than any real homelab host has.
MAX_OPEN_PER_SUBJECT = 500
TEXT_MAX = FindingState._meta.get_field("text").max_length
SUBJECT_MAX = FindingState._meta.get_field("subject").max_length

OPEN = FindingState.Announce.OPEN
ESCALATE = FindingState.Announce.ESCALATE
RESOLVE = FindingState.Announce.RESOLVE
NOTHING = FindingState.Announce.NONE

Subject = Host | Cluster | Network


def _scope(subject: Subject) -> dict:
    if isinstance(subject, Cluster):
        return {"cluster": subject}
    if isinstance(subject, Network):
        return {"network": subject}
    return {"host": subject}


def _scope_name(subject: Subject) -> str:
    return next(iter(_scope(subject)))


# --- step 1: sync (inside the evaluation's transaction) ----------------------------------------


def sync(
    subject: Subject,
    findings: Iterable[Finding],
    now: datetime,
    config: NotifyConfig | None = None,
    complete: bool = True,
    suppressed: Iterable[str] = (),
) -> None:
    """Bring the subject's FindingState rows in line with this evaluation.

    complete=True: `findings` is everything the subject has now, so open rows that are
    missing count one evaluation towards resolving. complete=False (the offline sweep's
    single finding): only add or update.
    `suppressed`: fingerprints the user marked "This is expected" or snoozed. Still there,
    so never "resolved": their rows end quietly. If a snooze runs out and the problem is
    still there, it opens (and alerts) again.
    """
    suppressed = set(suppressed)
    if config is None:
        config = load_config(subject.owner_id)
    scope, scope_name = _scope(subject), _scope_name(subject)
    rows = {r.fingerprint: r for r in FindingState.objects.filter(**scope)}
    silent = complete and BaselinePending.objects.filter(**scope).delete()[0] > 0

    seen = set()
    open_rows = sum(1 for r in rows.values() if r.resolved_at is None)
    for finding in findings:
        level = config.level(finding.rule_id)
        if finding.severity == IGNORE or level == OFF:
            continue
        if not config.applies(finding.rule_id, scope_name, subject.pk):
            continue
        seen.add(finding.fingerprint)
        row = rows.get(finding.fingerprint)
        if row is None:
            if open_rows >= MAX_OPEN_PER_SUBJECT:
                continue
            _open(subject, scope, finding, level, now, silent)
            open_rows += 1
        else:
            _update(row, finding, level, now)

    if not complete:
        return
    for fingerprint, row in rows.items():
        if fingerprint in seen or row.resolved_at is not None or row.rule_id in OFFLINE_RULES:
            continue
        # A rule the user just turned off (or limited to other hosts), or a finding just
        # snoozed or marked expected, ends without a word.
        quietly = fingerprint in suppressed or not config.enabled(
            row.rule_id, scope_name, subject.pk
        )
        _clear(row, now, quietly=quietly)


def _open(subject, scope, finding: Finding, level: str, now, silent: bool) -> None:
    row = FindingState(
        owner_id=subject.owner_id,
        fingerprint=finding.fingerprint,
        rule_id=finding.rule_id,
        subject=finding.subject[:SUBJECT_MAX],
        text=finding.text[:TEXT_MAX],
        severity=finding.severity,
        level=level,
        opened_at=now,
        last_seen_at=now,
        **scope,
    )
    if level == ALERT:
        if silent:
            # Known before the upgrade: no message now, but a resolved message later.
            row.announced_severity = finding.severity
        else:
            row.announce = OPEN
    try:
        # A savepoint: if another evaluation of the same subject created this row a moment
        # ago, only this insert is undone, never the report or snapshot being saved.
        with transaction.atomic():
            row.save()
    except IntegrityError:
        logger.info("Finding state %s already exists; kept the other one", finding.rule_id)


_UPDATE_FIELDS = [
    "text",
    "severity",
    "level",
    "last_seen_at",
    "clear_streak",
    "opened_at",
    "resolved_at",
    "announced_severity",
    "announce",
    "held_since",
]


def _update(row: FindingState, finding: Finding, level: str, now: datetime) -> None:
    previous_level = row.level
    row.text = finding.text[:TEXT_MAX]
    row.severity = finding.severity
    row.level = level
    row.last_seen_at = now
    row.clear_streak = 0

    if row.resolved_at is not None:
        if now - row.resolved_at <= FLAP_WINDOW:
            # Back within the flap window: same row, no new alert. An unsent "resolved"
            # is simply dropped.
            row.resolved_at = None
            if row.announce == RESOLVE:
                row.announce = NOTHING
        else:
            row.opened_at = now
            row.resolved_at = None
            row.announced_severity = ""
            row.held_since = None
            row.announce = OPEN if level == ALERT else NOTHING
    elif level != ALERT:
        if row.announce in (OPEN, ESCALATE):
            row.announce = NOTHING  # turned to "show only" before it went out
    elif not row.announced_severity:
        if row.announce == NOTHING and previous_level != ALERT:
            # Turned from "show only" to "alert" while open. (Not after a quiet flap
            # reopen: that row was already "alert" and stays silent.)
            row.announce = OPEN
    elif row.announce in (NOTHING, ESCALATE):
        rank = SEVERITY_RANK.get
        if row.severity == RED and rank(row.announced_severity, 0) < rank(RED):
            row.announce = ESCALATE
        elif row.announce == ESCALATE:
            row.announce = NOTHING  # back below red before the "now red" went out

    if row.severity == RED:
        row.held_since = None  # needs you: never waits for the morning
    row.save(update_fields=_UPDATE_FIELDS)


def _clear(row: FindingState, now: datetime, quietly: bool = False) -> bool:
    """One evaluation without the finding. True if that resolved it."""
    row.clear_streak += 1
    resolved = quietly or row.clear_streak >= RESOLVE_AFTER
    if resolved:
        row.resolved_at = now
        if quietly or not row.announced_severity:
            row.announce = NOTHING  # nobody was told it opened: nothing to take back
            if quietly:
                row.announced_severity = ""
        else:
            row.announce = RESOLVE
    row.save(update_fields=["clear_streak", "resolved_at", "announce", "announced_severity"])
    return resolved


def sweep_offline(now: datetime | None = None) -> int:
    """Scheduler: count passes with an offline host or cluster back; resolve at 2.

    Returns how many offline findings were resolved.
    """
    if now is None:
        now = timezone.now()
    resolved = 0
    ids = FindingState.objects.filter(
        resolved_at__isnull=True, rule_id__in=OFFLINE_RULES
    ).values_list("pk", flat=True)
    for pk in list(ids):
        # Re-read each row under a lock: a report may have changed it since the query.
        with transaction.atomic():
            row = (
                FindingState.objects.select_for_update()
                .select_related("host", "cluster")
                .filter(pk=pk, resolved_at__isnull=True)
                .first()
            )
            subject = (row.host or row.cluster) if row else None
            if subject is None:
                continue
            if subject.status == OFFLINE:
                if row.clear_streak:
                    row.clear_streak = 0
                    row.save(update_fields=["clear_streak"])
                continue
            if _clear(row, now):
                resolved += 1
    return resolved


# --- step 2: deliver (after the commit, and from the scheduler) ---------------------------------


def in_quiet_hours(prefs, now: datetime) -> bool:
    """True inside the user's quiet hours, in the user's own time zone."""
    if prefs is None or not prefs.quiet_hours_enabled:
        return False
    start, end = prefs.quiet_hours_start, prefs.quiet_hours_end
    if start is None or end is None or start == end:
        return False
    local = now.astimezone(user_zone(prefs.timezone)).time()
    if start < end:
        return start <= local < end
    return local >= start or local < end  # e.g. 23:00 to 08:00


def _in_maintenance(subject: Subject, now: datetime) -> bool:
    until = getattr(subject, "maintenance_until", None)
    return until is not None and until > now


def _prefs(owner_id: int):
    from prefs.models import Prefs

    return Prefs.objects.filter(owner_id=owner_id).first()


def _line(row: FindingState) -> words.Line:
    return words.line(row.announce, row.rule_id, row.text, row.severity)


Snapshot = list[tuple[int, str, str, datetime | None]]


def _mark_sent(rows: Iterable[FindingState]) -> Snapshot:
    """Empty the outbox of these rows. Returns what they held, for _restore()."""
    before = []
    for row in rows:
        before.append((row.pk, row.announce, row.announced_severity, row.held_since))
        if row.announce == RESOLVE:
            row.announced_severity = ""
        elif row.announce != NOTHING:
            row.announced_severity = row.severity
        # NOTHING (a held row that resolved or turned "show only" meanwhile): nobody is
        # told, so it must not count as announced; only the hold is lifted.
        row.announce = NOTHING
        row.held_since = None
        row.save(update_fields=["announced_severity", "announce", "held_since"])
    return before


def _drop(rows: Iterable[FindingState]) -> None:
    """No webhook: forget what was waiting, without pretending anyone was told (so no
    "resolved" turns up later for a problem nobody heard about)."""
    for row in rows:
        if row.announce == RESOLVE:
            row.announced_severity = ""
        row.announce = NOTHING
        row.held_since = None
        row.save(update_fields=["announced_severity", "announce", "held_since"])


def _restore(before: Snapshot) -> None:
    """Discord didn't take the message: put the outbox back so the scheduler retries after
    the cooldown. Only rows nothing else touched meanwhile are put back."""
    for pk, announce, announced, held_since in before:
        FindingState.objects.filter(pk=pk, announce=NOTHING).update(
            announce=announce, announced_severity=announced, held_since=held_since
        )
    logger.warning("Discord didn't accept the message; %s alert(s) will be retried", len(before))


def _subject_of(row: FindingState) -> Subject:
    return row.host or row.cluster or row.network


def deliver(subject: Subject, now: datetime | None = None) -> int:
    """Send the subject's outbox as one message. Returns 1 if a message was sent.

    Held back (and left in the outbox) during maintenance and the cooldown. During quiet
    hours, what isn't "needs you" is moved to the morning bundle.
    """
    if now is None:
        now = timezone.now()
    outbox = FindingState.objects.filter(held_since__isnull=True, **_scope(subject)).exclude(
        announce=NOTHING
    )
    if not outbox.exists():
        return 0
    prefs = _prefs(subject.owner_id)
    url = prefs.discord_webhook_url if prefs else ""
    if not url:
        # No webhook: nothing to send now, and nothing should pile up for later.
        with transaction.atomic():
            _drop(outbox.select_for_update())
        return 0
    if _in_maintenance(subject, now):
        return 0
    if in_quiet_hours(prefs, now):
        outbox.exclude(severity=RED).update(held_since=now)

    with transaction.atomic():
        rows = list(outbox.select_for_update().order_by("pk"))
        if not rows or not discord.claim_cooldown(type(subject), subject.pk, now):
            return 0
        message = words.subject_message(subject.name, [_line(r) for r in rows])
        before = _mark_sent(rows)
    # After the transaction: the HTTP call (up to 5 s) never holds the database lock.
    if not discord.send_discord(url, message):
        _restore(before)
        return 0
    return 1


def deliver_pending(now: datetime | None = None) -> int:
    """Scheduler: deliver every outbox that is waiting (cooldown, maintenance just ended).

    Returns how many messages went out.
    """
    if now is None:
        now = timezone.now()
    waiting = FindingState.objects.filter(held_since__isnull=True).exclude(announce=NOTHING)
    sent = 0
    for model, column in ((Host, "host_id"), (Cluster, "cluster_id"), (Network, "network_id")):
        ids = waiting.filter(**{f"{column}__isnull": False}).values_list(column, flat=True)
        for subject in model.objects.filter(pk__in=set(ids)):
            sent += deliver(subject, now)
    return sent


def send_quiet_bundles(now: datetime | None = None) -> int:
    """Scheduler: once a user's quiet hours are over, send what they held as ONE message.

    Skipped when nothing is left to say (it all resolved before it was sent).
    Returns how many bundles went out.
    """
    if now is None:
        now = timezone.now()
    held = FindingState.objects.filter(held_since__isnull=False)
    owners = set(held.values_list("owner_id", flat=True))
    sent = 0
    for owner_id in owners:
        prefs = _prefs(owner_id)
        if in_quiet_hours(prefs, now):
            continue
        url = prefs.discord_webhook_url if prefs else ""
        with transaction.atomic():
            rows = [
                r
                for r in held.filter(owner_id=owner_id)
                .select_for_update()
                .select_related("host", "cluster", "network")
                .order_by("pk")
                # A host still in maintenance keeps its held rows until it ends.
                if not _in_maintenance(_subject_of(r), now)
            ]
            if not url:
                _drop(rows)
                continue
            due = [r for r in rows if r.announce != NOTHING]
            blocks: dict[str, list[words.Line]] = {}
            for row in due:
                blocks.setdefault(_subject_of(row).name, []).append(_line(row))
            before = _mark_sent(rows)
        if not due:
            continue
        if discord.send_discord(url, words.bundle_message(blocks.items())):
            sent += 1
        else:
            _restore(before)
    return sent


def prune(now: datetime | None = None) -> int:
    """Scheduler: delete rows resolved more than RETENTION ago with nothing left to send.

    The quiet-hours bundle and the daily summary keep no rows of their own: they read
    these rows, so this also removes everything they already delivered.
    """
    if now is None:
        now = timezone.now()
    deleted, _ = FindingState.objects.filter(
        Q(resolved_at__lt=now - RETENTION), announce=NOTHING, held_since__isnull=True
    ).delete()
    return deleted


def after_commit(subject: Subject, now: datetime) -> None:
    """Deliver the subject's outbox once the current transaction commits."""

    def run() -> None:
        try:
            deliver(subject, now)
        except Exception:
            # An alert must never break report ingestion; the scheduler retries.
            logger.exception(
                "Delivering alerts for %s %s failed", type(subject).__name__, subject.pk
            )

    transaction.on_commit(run, robust=True)
