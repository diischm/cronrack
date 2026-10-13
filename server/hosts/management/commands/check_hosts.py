"""The scheduler: `python manage.py check_hosts --loop --every 30`.

Each pass marks hosts that stopped checking in as offline (the dead-man's switch),
which records a status event and sends the alert. Runs as its own small process
next to the web server. `--once` (the default without --loop) runs a single pass.

`--heartbeat-file` (used in docker compose) touches a file after every successful
pass, so the container's health check can tell a working loop from a stuck one.
"""

import logging
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from hosts.services import mark_silent_hosts_offline

logger = logging.getLogger(__name__)

MIN_EVERY_SECONDS = 5


class Command(BaseCommand):
    help = "Mark hosts that stopped checking in as offline and send alerts."

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
        self.stdout.write(f"Checking hosts every {every}s. Ctrl+C to stop.")
        try:
            while True:
                # A long-running process must drop stale database connections itself.
                close_old_connections()
                try:
                    self.run_pass()
                    if options["heartbeat_file"]:
                        Path(options["heartbeat_file"]).touch()
                except Exception:
                    # One failed pass (e.g. the database was locked) must not stop the
                    # dead-man's switch for good; log it and try again next time.
                    logger.exception("check_hosts pass failed")
                time.sleep(every)
        except KeyboardInterrupt:
            self.stdout.write("Stopped.")

    def run_pass(self) -> None:
        marked = mark_silent_hosts_offline(timezone.now())
        for host in marked:
            self.stdout.write(f"{host.name}: offline")
