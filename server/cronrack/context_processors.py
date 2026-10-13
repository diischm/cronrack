"""Template data for every page."""

from django.http import HttpRequest

from cronrack import SERVER_VERSION


def version(request: HttpRequest) -> dict:
    """The server version, for the sidebar footer and the host detail page."""
    return {"server_version": SERVER_VERSION}
