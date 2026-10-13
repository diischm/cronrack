from django.contrib import admin

from hosts.models import AgentToken, Host


@admin.register(Host)
class HostAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "status", "connected", "last_seen_at")
    list_filter = ("status", "connected")
    search_fields = ("name",)
    # Written by the server from agent reports, not by hand.
    readonly_fields = (
        "created_at",
        "status",
        "connected",
        "last_seen_at",
        "latest_report",
        "report_received_at",
    )


@admin.register(AgentToken)
class AgentTokenAdmin(admin.ModelAdmin):
    """Shows only the hash. The plaintext token is never stored, so it can't be shown."""

    list_display = ("host", "created_at", "last_used_at", "revoked")
    list_filter = ("revoked",)
    fields = ("host", "token_hash", "created_at", "last_used_at", "revoked")
    readonly_fields = ("host", "token_hash", "created_at", "last_used_at")

    # Tokens are created with AgentToken.issue()/rotate(), never typed into a form.
    def has_add_permission(self, request):
        return False
