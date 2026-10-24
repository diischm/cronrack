"""The first-run setup code.

Until the first account exists, whoever opens /setup/ first becomes the admin. The
setup code makes sure that is the person who can read the server's log, not just
anyone who can reach the page:

1. On app start (python manage.py startup, run by docker-entrypoint.sh), while no
   account exists, a fresh random code is made. Only its hash goes into the
   database; the code itself is printed to the log (docker compose logs app).
2. /setup/ asks for it. Creating the account deletes ("claims") the stored hash in
   the same transaction, so the code works exactly once.
"""

import hashlib
import hmac
import logging
import secrets

from django.contrib.auth import get_user_model
from django.db import transaction

from accounts.models import SetupCode

logger = logging.getLogger(__name__)

# No 0/O, 1/I/L: the code is read off a terminal and typed by hand.
# 16 characters from 31 gives about 79 bits of randomness.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 16
GROUP = 4


def _normalize(code: str) -> str:
    """'abcd-efgh jkmn-pqrs' -> 'ABCDEFGHJKMNPQRS': dashes, spaces and case don't matter."""
    return "".join(ch for ch in str(code).upper() if ch not in "- \t")


def _hash(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode("utf-8")).hexdigest()


def _generate() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + GROUP] for i in range(0, CODE_LENGTH, GROUP))


def new_setup_code() -> str | None:
    """Replace any stored code with a fresh one and return it, or None once an account exists.

    Called on every app start: an old code (from an earlier start, maybe printed in
    an old log) stops working.
    """
    with transaction.atomic():
        SetupCode.objects.all().delete()
        if get_user_model().objects.exists():
            return None
        code = _generate()
        SetupCode.objects.create(code_hash=_hash(code))
    return code


def ensure_setup_code() -> None:
    """Make (and log) a code if none exists yet.

    For running without docker-entrypoint.sh (e.g. uvicorn on a dev machine), where
    the startup command never ran. Then the code is in that terminal's output.
    """
    if SetupCode.objects.exists() or get_user_model().objects.exists():
        return
    code = new_setup_code()
    if code is not None:
        logger.warning("CronRack first-run setup code: %s", code)


def setup_code_matches(code: str) -> bool:
    """True if `code` is the current setup code. Constant-time comparison."""
    expected = SetupCode.objects.values_list("code_hash", flat=True).first()
    if not expected or not code:
        return False
    return hmac.compare_digest(expected, _hash(code))


def claim_setup_code(code: str) -> bool:
    """Use up the setup code. True only for the one caller that deleted it.

    Call inside the transaction that creates the first account: if two requests race,
    only one delete can remove the row, and the other gets False.
    """
    if not setup_code_matches(code):
        return False
    deleted, _ = SetupCode.objects.filter(code_hash=_hash(code)).delete()
    return deleted == 1
