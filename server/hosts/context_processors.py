"""Template data every page needs: the sidebar's "N hosts connected" card and the Clusters
nav item (base.html)."""

from django.db.models import Count, Q
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from clusters.models import Cluster
from hosts.models import Host


def _connected_hosts(user) -> dict[str, int]:
    # Owner-scoped like every other host query.
    return Host.objects.filter(owner=user).aggregate(
        total=Count("id"), connected=Count("id", filter=Q(connected=True))
    )


def _clusters(user) -> dict[str, int]:
    return Cluster.objects.filter(owner=user).aggregate(total=Count("id"))


def _sound(user) -> dict:
    """{"on": True, "volume": 0-100} when the user turned in-app sounds on, else {}
    (sound.js then stays idle)."""
    from prefs.models import Prefs

    row = (
        Prefs.objects.filter(owner=user, sounds_enabled=True)
        .values_list("sound_volume", flat=True)
        .first()
    )
    return {} if row is None else {"on": True, "volume": row}


def shell(request: HttpRequest) -> dict:
    """Lazy: the query only runs when a page actually shows the card (logged-in pages)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {
        "shell_hosts": SimpleLazyObject(lambda: _connected_hosts(user)),
        # The nav shows "Clusters" only when there is one.
        "shell_clusters": SimpleLazyObject(lambda: _clusters(user)),
        "shell_sound": SimpleLazyObject(lambda: _sound(user)),
    }
