"""Public downloads for installing the agent on a host. No login: a fresh host has
no session, and nothing here is secret (the token is typed in at install time)."""

from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_GET

from agents.package import TARBALL_NAME, agent_package, install_script


def _no_cache(response: HttpResponse) -> HttpResponse:
    # Always revalidate, so a re-run of install.sh after a server upgrade gets the new agent.
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def install_sh(request: HttpRequest) -> HttpResponse:
    """GET /agent/install.sh — plain text, so it can be read before it's run."""
    return _no_cache(HttpResponse(install_script(), content_type="text/plain; charset=utf-8"))


@require_GET
def tarball(request: HttpRequest) -> HttpResponse:
    """GET /agent/download/cronrack-agent.tar.gz"""
    response = HttpResponse(agent_package().data, content_type="application/gzip")
    response["Content-Disposition"] = f'attachment; filename="{TARBALL_NAME}"'
    return _no_cache(response)


@require_GET
def tarball_sha256(request: HttpRequest) -> HttpResponse:
    """GET /agent/download/cronrack-agent.tar.gz.sha256 — `sha256sum` format."""
    body = f"{agent_package().sha256}  {TARBALL_NAME}\n"
    return _no_cache(HttpResponse(body, content_type="text/plain; charset=utf-8"))
