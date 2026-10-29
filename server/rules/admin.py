from django.contrib import admin

from rules.models import RuleOverride, Suppression


@admin.register(RuleOverride)
class RuleOverrideAdmin(admin.ModelAdmin):
    list_display = (
        "rule_id",
        "owner",
        "host",
        "cluster",
        "subject",
        "enabled",
        "severity",
        "notify",
    )
    list_filter = ("severity", "enabled")
    search_fields = ("rule_id", "subject")


@admin.register(Suppression)
class SuppressionAdmin(admin.ModelAdmin):
    list_display = ("rule_id", "host", "cluster", "subject", "kind", "expires_at", "owner")
    list_filter = ("kind",)
    search_fields = ("rule_id", "subject", "note")
    readonly_fields = ("fingerprint", "created_at")
