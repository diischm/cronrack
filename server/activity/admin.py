from django.contrib import admin

from activity.models import StatusEvent


@admin.register(StatusEvent)
class StatusEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "host", "cluster", "old_status", "new_status", "summary")
    list_filter = ("new_status",)
    readonly_fields = ("created_at",)
