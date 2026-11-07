from django.contrib import admin

from prefs.models import Prefs


@admin.register(Prefs)
class PrefsAdmin(admin.ModelAdmin):
    list_display = ("owner", "timezone", "notify_preset", "quiet_hours_enabled", "digest_enabled")
