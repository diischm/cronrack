"""Run once when the app starts: `python manage.py startup` (docker-entrypoint.sh does it).

- No agent can be connected before the server runs, so every host's "connected"
  flag is cleared. Agents reconnect within seconds and set it again.
- While no account exists, a fresh one-time setup code is made and printed here, so
  it shows up in `docker compose logs app`. /setup/ asks for it.
"""

from django.core.management.base import BaseCommand

from accounts.setup_code import new_setup_code
from hosts.services import reset_connections


class Command(BaseCommand):
    help = "Reset agent connection flags and print the first-run setup code if needed."

    def handle(self, *args, **options):
        reset_connections()
        code = new_setup_code()
        if code is None:
            return
        line = "=" * 64
        self.stdout.write(
            f"{line}\n"
            f"CronRack first-run setup code: {code}\n"
            "Open CronRack in your browser; /setup/ asks for this code.\n"
            "It works once and changes every time the app restarts.\n"
            f"{line}"
        )
