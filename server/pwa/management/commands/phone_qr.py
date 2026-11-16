"""`python manage.py phone_qr`: print the QR code that opens CronRack's install page.

Scan it with the phone camera: it opens https://<your address>/install, which shows the
install button. The code holds only that address, never a login, token or setup code.
In Docker: `docker compose exec app python manage.py phone_qr`.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.http.request import split_domain_port, validate_host

from pwa.qr import INSTALL_PATH, install_qr_block, make_install_qr, public_install_url

NO_URL = (
    "No QR code: set CRONRACK_PUBLIC_URL to the https address you open CronRack with, "
    "e.g. CRONRACK_PUBLIC_URL=https://cronrack.example.com (in .env), then restart."
)


def host_is_allowed(url: str) -> bool:
    """Would Django answer requests for this address's host name? (Same rule as Django's.)"""
    allowed = settings.ALLOWED_HOSTS
    if settings.DEBUG and not allowed:
        allowed = [".localhost", "127.0.0.1", "[::1]"]
    host = url.split("://", 1)[1].split("/", 1)[0]
    domain, _port = split_domain_port(host)
    return bool(domain) and validate_host(domain, allowed)


class Command(BaseCommand):
    help = "Print a QR code for the phone install page (https://<address>/install)."

    def handle(self, *args, **options):
        url = public_install_url()
        if url is None:
            # One line, and never the value that was set: it may be a typo or a secret.
            self.stdout.write(NO_URL)
            return
        qr = make_install_qr(url[: -len(INSTALL_PATH)])
        self.stdout.write(install_qr_block(qr))
        if not host_is_allowed(qr.url):
            self.stdout.write(
                "\nWarning: the server won't answer to this name until you add it to "
                "ALLOWED_HOSTS (and CSRF_TRUSTED_ORIGINS)."
            )
