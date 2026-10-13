"""Owner-scoped lookups. Use these instead of Host.objects.get() in views."""

from django.http import Http404
from django.shortcuts import get_object_or_404

from hosts.models import Host


def get_owned_host(user, host_id) -> Host:
    """Return the host only if `user` owns it; otherwise 404.

    Another user's host gives the same 404 as a missing one, so an attacker
    can't even learn which host IDs exist (IDOR protection).
    """
    if not user.is_authenticated:
        raise Http404
    return get_object_or_404(Host, pk=host_id, owner=user)
