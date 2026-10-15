"""Small request helpers shared by the views."""

from django.http import HttpRequest


def wants_json(request: HttpRequest) -> bool:
    """True when the page's JavaScript sent the request (fetch with Accept: application/json).

    A plain form POST (no JavaScript) sends an HTML Accept header instead; those views
    answer with a redirect back to a fixed page plus a flash message.
    """
    return "application/json" in request.headers.get("Accept", "")
