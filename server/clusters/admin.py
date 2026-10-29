from django.contrib import admin

from clusters.models import Cluster, UpdateCheck


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "status", "reporter", "snapshot_received_at")
    list_filter = ("status",)
    search_fields = ("name",)
    # Written by the server from agent reports, not by hand.
    readonly_fields = (
        "uid",
        "created_at",
        "status",
        "last_seen_at",
        "latest_snapshot",
        "snapshot_received_at",
    )


@admin.register(UpdateCheck)
class UpdateCheckAdmin(admin.ModelAdmin):
    list_display = ("source", "checked_at", "attempted_at", "last_error")
    readonly_fields = ("source", "data", "checked_at", "attempted_at", "last_error")

    def has_add_permission(self, request):
        return False
