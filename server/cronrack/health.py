"""GET /healthz: the Docker health check. Public, and tells nobody anything.

It answers "ok" as long as the server process is up and handling requests. It does
not touch the database or the session, so it never sets a cookie or leaks state.
"""

from django.http import HttpRequest, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def healthz(request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")
