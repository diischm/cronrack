from typing import ClassVar

from django import forms

from alerts.discord import is_allowed_webhook
from prefs.models import Prefs

WEBHOOK_ERROR = "Use a Discord webhook URL: https://discord.com/api/webhooks/…"


class PrefsForm(forms.ModelForm):
    silent_after_seconds = forms.IntegerField(
        min_value=60,
        max_value=86_400,
        label="Mark a host offline after (seconds of silence)",
        widget=forms.NumberInput(attrs={"class": "well"}),
    )

    class Meta:
        model = Prefs
        fields = ("discord_webhook_url", "silent_after_seconds")
        labels: ClassVar[dict[str, str]] = {"discord_webhook_url": "Discord webhook URL"}
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "discord_webhook_url": forms.URLInput(attrs={"class": "well"})
        }

    def clean_discord_webhook_url(self) -> str:
        url = self.cleaned_data["discord_webhook_url"].strip()
        # SSRF guard, part 1 (save time). alerts.discord checks again before sending.
        if url and not is_allowed_webhook(url):
            raise forms.ValidationError(WEBHOOK_ERROR)
        return url
