"""Owner-scoped lookups. Use this instead of Cluster.objects.get() in views."""

from django.http import Http404
from django.shortcuts import get_object_or_404

from clusters.models import Cluster


def get_owned_cluster(user, cluster_id) -> Cluster:
    """Return the cluster only if `user` owns it; otherwise 404.

    Another user's cluster gives the same 404 as a missing one (IDOR protection).
    """
    if not user.is_authenticated:
        raise Http404
    return get_object_or_404(Cluster, pk=cluster_id, owner=user)
