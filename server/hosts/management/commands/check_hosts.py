"""The scheduler: `python manage.py check_hosts --loop --every 30`.

Each pass marks hosts and clusters that stopped checking in as offline (the dead-man's
switch), which records a status event and sends the alert. It then sends alerts that the
cooldown held back, with the current status (alerts.discord), and, only for users who
turned it on, looks up the newest k3s, Argo CD and Helm versions (clusters.updates, at
most once a day). Runs as its own small process
next to the web server. `--once` (the default without --loop) runs a single pass.

`--heartbeat-file` (used in docker compose) touches a file after every successful
pass, so the container's health check can tell a working loop from a stuck one.

Stopping: with --loop, SIGTERM (`docker stop`, Kubernetes deleting the pod) or SIGINT
(Ctrl+C) ends the loop cleanly, with exit code 0. A pass that is running finishes
first (it takes a moment; a Discord request gives up after 5 s), then the loop ends;
during the wait between passes it ends right away. This matters because the scheduler
runs as PID 1 in its container, and Linux ignores SIGTERM for a PID 1 that has no
handler: without one, Docker and Kubernetes wait out their grace period and kill it.
"""

import logging
import signal
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from alerts.discord import send_pending_alerts
from clusters.services import mark_silent_clusters_offline
from clusters.updates import maybe_check_updates
from hosts.services import mark_silent_hosts_offline

logger = logging.getLogger(__name__)

MIN_EVERY_SECONDS = 5
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class StopWaiting(BaseException):
    """Raised by the signal handler to cut the wait between passes short.

    A BaseException, so the `except Exception` around a pass can never swallow it.
    """


class Command(BaseCommand):
    help = "Mark hosts that stopped checking in as offline and send alerts."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_requested = False
        self.waiting = False  # True only while sleeping between passes

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--once", action="store_true", help="Run one pass and exit (default).")
        mode.add_argument("--loop", action="store_true", help="Run forever.")
        parser.add_argument(
            "--every", type=int, default=30, help="Seconds between passes with --loop."
        )
        parser.add_argument(
            "--heartbeat-file",
            help="With --loop: touch this file after every successful pass (for health checks).",
        )

    def handle(self, *args, **options):
        every = options["every"]
        if every < MIN_EVERY_SECONDS:
            raise CommandError(f"--every must be at least {MIN_EVERY_SECONDS} seconds")
        if not options["loop"]:
            self.run_pass()
            return
        previous = {sig: signal.signal(sig, self.request_stop) for sig in STOP_SIGNALS}
        self.stdout.write(f"Checking hosts every {every}s. Stops on SIGTERM or Ctrl+C.")
        try:
            self.loop(every, options["heartbeat_file"])
        except StopWaiting:
            pass  # a signal right as a wait ended; stop_requested is set
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        self.stdout.write("Stopped.")

    def request_stop(self, signum, frame) -> None:
        """Signal handler: finish the current pass (if any), then stop."""
        self.stop_requested = True
        if self.waiting:
            raise StopWaiting

    def loop(self, every: int, heartbeat_file: str | None) -> None:
        while not self.stop_requested:
            # A long-running process must drop stale database connections itself.
            close_old_connections()
            try:
                self.run_pass()
                if heartbeat_file:
                    Path(heartbeat_file).touch()
            except Exception:
                # One failed pass (e.g. the database was locked) must not stop the
                # dead-man's switch for good; log it and try again next time.
                logger.exception("check_hosts pass failed")
            self.wait(every)

    def wait(self, seconds: int) -> None:
        """Sleep between passes, unless a stop was requested (then return at once)."""
        # `waiting` is set before stop_requested is checked: a signal arriving in
        # between then still interrupts the sleep instead of being missed.
        self.waiting = True
        try:
            if not self.stop_requested:
                time.sleep(seconds)
        except StopWaiting:
            pass
        finally:
            self.waiting = False

    def run_pass(self) -> None:
        now = timezone.now()
        for host in mark_silent_hosts_offline(now):
            self.stdout.write(f"{host.name}: offline")
        for cluster in mark_silent_clusters_offline(now):
            self.stdout.write(f"{cluster.name}: cluster offline")
        for subject in send_pending_alerts(now):
            self.stdout.write(f"{subject.name}: held-back alert sent ({subject.status})")
        # Its own try/except: looking up versions must never fail the dead-man's switch.
        try:
            for source in maybe_check_updates(now):
                self.stdout.write(f"{source}: newest version updated")
        except Exception:
            logger.exception("update check failed")
