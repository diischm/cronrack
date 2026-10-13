"""Template data every page needs: the sidebar's "N hosts connected" card (base.html)."""

from django.db.models import Count, Q
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from hosts.models import Host


def _connected_hosts(user) -> dict[str, int]:
    # Owner-scoped like every other host query.
    return Host.objects.filter(owner=user).aggregate(
        total=Count("id"), connected=Count("id", filter=Q(connected=True))
    )


def shell(request: HttpRequest) -> dict:
    """Lazy: the query only runs when a page actually shows the card (logged-in pages)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"shell_hosts": SimpleLazyObject(lambda: _connected_hosts(user))}
