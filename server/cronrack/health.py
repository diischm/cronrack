"""GET /healthz: the Docker health check. Public, and tells nobody anything.

It answers "ok" as long as the server process is up and handling requests. It does
not touch the database or the session, so it never sets a cookie or leaks state.

It's answered by HealthCheckMiddleware, the first middleware in settings.MIDDLEWARE,
before anything else looks at the request. In particular Django's Host header check
(ALLOWED_HOSTS, in CommonMiddleware) never runs for it, so the health check works the
same whatever CRONRACK_DOMAIN is: a name, a LAN IP or a Tailscale IP. The first-run
redirect and the https redirect never apply to it either.
"""

from django.http import HttpRequest, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

HEALTHZ_PATH = "/healthz"


@never_cache
@require_GET
def healthz(request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")


class HealthCheckMiddleware:
    """Answers /healthz itself; every other request passes through untouched."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == HEALTHZ_PATH:
            return healthz(request)
        return self.get_response(request)
