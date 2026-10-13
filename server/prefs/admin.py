from django.contrib import admin

from prefs.models import Prefs


@admin.register(Prefs)
class PrefsAdmin(admin.ModelAdmin):
    list_display = ("owner", "timezone", "digest_enabled")
