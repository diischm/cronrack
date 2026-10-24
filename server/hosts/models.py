import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.utils import timezone

# How long a new host stays in learning mode (deviation rules stay quiet).
LEARNING_PERIOD = timedelta(hours=24)

# Real tokens are 43 characters; anything far longer is not worth hashing.
MAX_TOKEN_LENGTH = 128

host_name_validator = RegexValidator(
    r"^[A-Za-z0-9_-]+\Z",
    "Use only letters, digits, '-' and '_'.",
)


class Host(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting for first report"
        GREEN = "green", "Green"
        YELLOW = "yellow", "Yellow"
        RED = "red", "Red"
        OFFLINE = "offline", "Offline"
        MAINTENANCE = "maintenance", "Maintenance"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="hosts"
    )
    name = models.CharField(max_length=64, validators=[host_name_validator])
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.WAITING)
    connected = models.BooleanField(default=False)
    # The channel name of the agent socket that set connected=True. A socket closing
    # only clears `connected` if it is still this one, so an old socket going away
    # after its replacement said hello leaves the host connected.
    connection_id = models.CharField(max_length=128, blank=True, default="", editable=False)
    # When the last Discord alert for this host was sent, and the status it announced
    # (alerts.discord: cooldown, and catching up on changes held back by it).
    last_alert_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_alert_status = models.CharField(max_length=16, blank=True, default="", editable=False)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    latest_report = models.JSONField(null=True, blank=True)
    report_received_at = models.DateTimeField(null=True, blank=True)
    maintenance_until = models.DateTimeField(null=True, blank=True)
    learning_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("name",)
        constraints = (
            models.UniqueConstraint(fields=["owner", "name"], name="unique_host_name_per_owner"),
        )

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if self._state.adding and self.learning_until is None:
            self.learning_until = self.created_at + LEARNING_PERIOD
        super().save(*args, **kwargs)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AgentToken(models.Model):
    """The secret an agent sends when it connects. Only its SHA-256 hash is stored."""

    host = models.OneToOneField(Host, on_delete=models.CASCADE, related_name="agent_token")
    # unique=True also creates the database index used by verify().
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    revoked = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"Token for {self.host}"

    @classmethod
    def issue(cls, host: Host) -> str:
        """Create the host's token and return the plaintext. Show it once; it can't be recovered."""
        if cls.objects.filter(host=host).exists():
            raise ValueError("This host already has a token. Use AgentToken.rotate().")
        raw = secrets.token_urlsafe(32)
        cls.objects.create(host=host, token_hash=hash_token(raw))
        return raw

    @classmethod
    def verify(cls, raw) -> Host | None:
        """Return the host this token belongs to, or None if it is unknown or revoked."""
        if not isinstance(raw, str) or not raw or len(raw) > MAX_TOKEN_LENGTH:
            return None
        digest = hash_token(raw)
        token = cls.objects.select_related("host").filter(token_hash=digest).first()
        if token is None or token.revoked:
            return None
        # The database lookup already matched; this constant-time compare is a
        # second check that doesn't depend on how the database compares strings.
        if not hmac.compare_digest(token.token_hash, digest):
            return None
        token.last_used_at = timezone.now()
        token.save(update_fields=["last_used_at"])
        return token.host

    @classmethod
    def rotate(cls, host: Host) -> str:
        """Replace the host's token with a new one and return the new plaintext.

        The old hash is overwritten, so the old token stops working immediately,
        and once the change is saved the agent's open WebSocket is closed (4410).
        """
        raw = secrets.token_urlsafe(32)
        with transaction.atomic():
            token = cls.objects.select_for_update().filter(host=host).first()
            if token is None:
                cls.objects.create(host=host, token_hash=hash_token(raw))
                return raw
            token.token_hash = hash_token(raw)
            token.created_at = timezone.now()
            token.last_used_at = None
            token.revoked = False
            token.save()
            # Imported here: hosts.services imports this module.
            from hosts.services import CLOSE_TOKEN_REVOKED, close_agent

            transaction.on_commit(lambda: close_agent(host, CLOSE_TOKEN_REVOKED))
        return raw
