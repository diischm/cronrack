"""Cluster pages and their JSON endpoint.

Access rule: every view that takes a cluster ID loads it ONLY through
get_owned_cluster(), which answers 404 for anyone else's cluster (IDOR protection).
All views require login; POST views are CSRF-protected by CsrfViewMiddleware.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from clusters.access import get_owned_cluster
from clusters.forms import ClusterNameForm, DeleteClusterForm
from clusters.models import Cluster
from clusters.presenters import cluster_context, cluster_status, cluster_summary
from hosts.presenters import OwnerData
from rules.engine import STATUS_ORDER


@login_required
@require_GET
def cluster_list(request: HttpRequest) -> HttpResponse:
    """Always answers 200, with or without clusters (the nav links here)."""
    now = timezone.now()
    data = OwnerData(request.user)
    clusters = [cluster_summary(c, now, data) for c in Cluster.objects.filter(owner=request.user)]
    clusters.sort(key=lambda c: (STATUS_ORDER.get(c["status"], 99), c["name"].lower(), c["id"]))
    return render(request, "clusters/list.html", {"clusters": clusters})


@login_required
@require_GET
def cluster_detail(request: HttpRequest, cluster_id: int) -> HttpResponse:
    cluster = get_owned_cluster(request.user, cluster_id)
    return render(request, "clusters/detail.html", cluster_context(cluster, timezone.now()))


@login_required
@require_GET
def cluster_detail_json(request: HttpRequest, cluster_id: int) -> JsonResponse:
    cluster = get_owned_cluster(request.user, cluster_id)
    return JsonResponse(cluster_context(cluster, timezone.now()))


def _settings_page(request, cluster, name_form=None, delete_form=None, status=200):
    return render(
        request,
        "clusters/settings.html",
        {
            "cluster": cluster,
            "title_status": cluster_status(cluster, timezone.now(), OwnerData(cluster.owner)),
            "name_form": name_form or ClusterNameForm(owner=request.user, instance=cluster),
            "delete_form": delete_form or DeleteClusterForm(cluster=cluster),
        },
        status=status,
    )


@login_required
@require_http_methods(["GET", "POST"])
def cluster_settings(request: HttpRequest, cluster_id: int) -> HttpResponse:
    cluster = get_owned_cluster(request.user, cluster_id)
    if request.method == "GET":
        return _settings_page(request, cluster)
    form = ClusterNameForm(request.POST, owner=request.user, instance=cluster)
    if not form.is_valid():
        return _settings_page(request, cluster, name_form=form, status=400)
    cluster.name = form.cleaned_data["name"]
    cluster.save(update_fields=["name"])
    messages.success(request, "Cluster renamed.")
    return redirect("cluster-settings", cluster_id=cluster.pk)


@login_required
@require_POST
def cluster_delete(request: HttpRequest, cluster_id: int) -> HttpResponse:
    """Deletes the cluster and its history; its hosts are only unlinked.

    If the agent keeps sending the cluster view, the cluster comes back with its next report.
    """
    cluster = get_owned_cluster(request.user, cluster_id)
    form = DeleteClusterForm(request.POST, cluster=cluster)
    if not form.is_valid():
        return _settings_page(request, cluster, delete_form=form, status=400)
    name = cluster.name
    cluster.delete()
    messages.success(request, f"Deleted {name}.")
    return redirect("clusters")
