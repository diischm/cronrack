"""GET /healthz and GET /readyz: health checks. Public, and they tell nobody anything.

- /healthz ("liveness"): "ok" as long as the server process is up and handling
  requests. It does not touch the database or the session, so it is cheap, never sets
  a cookie and never leaks state. The Docker HEALTHCHECK and a Kubernetes
  livenessProbe call it: if it stops answering, the process is stuck and needs a restart.
- /readyz ("readiness"): "ready" (200) only if the database answers AND every
  migration in this image has been applied; otherwise "not ready" (503). A Kubernetes
  readinessProbe calls it: while it says 503, no traffic is sent to this pod, but the
  pod is not restarted (a database that is busy or not migrated yet is not fixed by a
  restart). The reason is logged, never put in the response.

Both are answered by HealthCheckMiddleware, the first middleware in settings.MIDDLEWARE,
before anything else looks at the request. In particular Django's Host header check
(ALLOWED_HOSTS, in CommonMiddleware) never runs for them, so they work the same
whatever CRONRACK_DOMAIN is: a name, a LAN IP, a Tailscale IP, or the pod IP that
Kubernetes probes send as the Host header. The first-run redirect and the https
redirect never apply to them either.
"""

import logging

from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

HEALTHZ_PATH = "/healthz"
READYZ_PATH = "/readyz"

# Set once every migration was seen applied. The migrations this process knows about
# are the ones in its image, and they never change while it runs, so after the first
# "all applied" the probe only has to check that the database answers.
_migrations_applied = False


def _text(body: str, status: int = 200) -> HttpResponse:
    return HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")


@never_cache
@require_GET
def healthz(request: HttpRequest) -> HttpResponse:
    return _text("ok")


def database_answers() -> None:
    """Raise if the database can't run a trivial query. Runs on every /readyz."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def migrations_applied() -> bool:
    """True if no migration is waiting. Remembered once it is True."""
    global _migrations_applied
    if not _migrations_applied:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        _migrations_applied = not plan
    return _migrations_applied


@never_cache
@require_GET
def readyz(request: HttpRequest) -> HttpResponse:
    try:
        database_answers()
        if not migrations_applied():
            logger.warning("Not ready: database migrations are pending")
            return _text("not ready", status=503)
    except DatabaseError as exc:
        # Only the kind of error: its message could contain paths or SQL. (Anything
        # other than a database error ends as a 500, which fails the probe just the same.)
        logger.warning("Not ready: database check failed (%s)", type(exc).__name__)
        return _text("not ready", status=503)
    return _text("ready")


class HealthCheckMiddleware:
    """Answers /healthz and /readyz itself; every other request passes through untouched."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == HEALTHZ_PATH:
            return healthz(request)
        if request.path == READYZ_PATH:
            return readyz(request)
        return self.get_response(request)
