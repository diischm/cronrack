from django.db import models
from django.utils import timezone


class SetupCode(models.Model):
    """The one-time code that /setup/ asks for before the first account exists.

    Only the SHA-256 hash is stored; the code itself is printed once in the app's
    log (see accounts.setup_code). At most one row exists, and it is deleted when the
    first account is created.
    """

    code_hash = models.CharField(max_length=64, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self) -> str:
        return f"Setup code from {self.created_at:%Y-%m-%d %H:%M}"
