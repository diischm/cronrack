"""Run once when the app starts: `python manage.py startup` (docker-entrypoint.sh does it).

- No agent can be connected before the server runs, so every host's "connected"
  flag is cleared. Agents reconnect within seconds and set it again.
- While no account exists, a fresh one-time setup code is made and printed here, so
  it shows up in `docker compose logs app`. /setup/ asks for it.
- On that same first start, if CronRack knows its public https address (CRONRACK_PUBLIC_URL,
  or https://<CRONRACK_DOMAIN>), a QR code for its /install page is printed under the code,
  so a phone can open it from `docker compose logs app`. The setup code is never in the QR.
  Nothing is printed for a missing or invalid address.
"""

from django.core.management.base import BaseCommand

from accounts.setup_code import new_setup_code
from hosts.services import reset_connections
from pwa.qr import INSTALL_PATH, install_qr_block, make_install_qr, public_install_url


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
        url = public_install_url()
        if url is None:
            return
        try:
            block = install_qr_block(make_install_qr(url[: -len(INSTALL_PATH)]))
        except Exception:  # noqa: BLE001 - the web server must start even if the QR fails
            # The QR is only a convenience; the web server starts right after this command,
            # so a QR problem must never stop it. The setup code above is already printed.
            return
        self.stdout.write("\n" + block)
