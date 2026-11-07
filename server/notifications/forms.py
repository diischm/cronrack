"""Parsing Settings → Notifications. Everything posted is checked against fixed lists.

The page posts the whole form every time (the no-JS Save key, and the autosave alike):
- the general settings (quiet hours, daily summary, sounds) through GeneralForm;
- per rule, `level:<id>`, `counts:<id>`, `limit:<id>`, `applies:<id>` and `hosts:<id>` /
  `clusters:<id>`, read only for the rule IDs in the registry (anything else posted is
  ignored), and only with values from the registry's fixed choices. Host and cluster IDs
  must be the user's own.
A rule whose values all equal the preset's defaults has no override row; changing any of
them makes one.
"""

from dataclasses import dataclass, field
from typing import ClassVar

from django import forms

from notifications.rules import COUNTS_AS, LEVELS, SPECS, RuleSpec
from prefs.models import Prefs

APPLIES_TO = ("all", "selected")


class GeneralForm(forms.ModelForm):
    quiet_hours_start = forms.TimeField(input_formats=["%H:%M"])
    quiet_hours_end = forms.TimeField(input_formats=["%H:%M"])
    digest_time = forms.TimeField(input_formats=["%H:%M"])
    sound_volume = forms.IntegerField(min_value=0, max_value=100)

    class Meta:
        model = Prefs
        fields = (
            "quiet_hours_enabled",
            "quiet_hours_start",
            "quiet_hours_end",
            "digest_enabled",
            "digest_time",
            "sounds_enabled",
            "sound_volume",
        )
        labels: ClassVar[dict[str, str]] = {
            "quiet_hours_start": "Quiet hours start",
            "quiet_hours_end": "Quiet hours end",
            "digest_time": "Summary time",
            "sound_volume": "Volume",
        }

    def clean(self):
        data = super().clean()
        start, end = data.get("quiet_hours_start"), data.get("quiet_hours_end")
        if start is not None and start == end:
            self.add_error("quiet_hours_end", "Quiet hours need a different start and end.")
        return data


@dataclass
class RuleValues:
    rule_id: str
    level: str
    counts_as: str
    limit: str
    applies_to: str
    hosts: set[int] = field(default_factory=set)
    clusters: set[int] = field(default_factory=set)

    def differs_from(self, spec: RuleSpec, preset: str) -> bool:
        return (
            self.level != spec.defaults[preset]
            or self.counts_as != spec.counts_as
            or (spec.limit is not None and self.limit != spec.limit.default)
            or self.applies_to != "all"
        )


def _ids(raw: list[str], allowed: set[int]) -> set[int] | None:
    """The posted IDs, or None if any isn't one of `allowed` (someone else's, or junk)."""
    out = set()
    for value in raw:
        if not (value.isascii() and value.isdigit()) or int(value) not in allowed:
            return None
        out.add(int(value))
    return out


def parse_rules(
    data, preset: str, host_ids: set[int], cluster_ids: set[int]
) -> tuple[dict[str, RuleValues], list[str]]:
    """{rule_id: RuleValues} for every rule, and the error messages (empty when valid)."""
    values: dict[str, RuleValues] = {}
    errors: list[str] = []
    for spec in SPECS.values():
        level = data.get(f"level:{spec.id}", spec.defaults[preset])
        counts_as = data.get(f"counts:{spec.id}", spec.counts_as)
        limit = data.get(f"limit:{spec.id}", spec.limit.default if spec.limit else "")
        applies_to = data.get(f"applies:{spec.id}", "all") if spec.applies_to else "all"
        if level not in LEVELS or counts_as not in COUNTS_AS:
            errors.append(f"{spec.label}: pick one of the listed options.")
            continue
        if spec.limit is not None and limit not in spec.limit.keys:
            errors.append(f"{spec.label}: pick one of the listed limits.")
            continue
        if applies_to not in APPLIES_TO:
            errors.append(f"{spec.label}: pick who it applies to.")
            continue
        rule = RuleValues(spec.id, level, counts_as, limit if spec.limit else "", applies_to)
        if applies_to == "selected":
            if spec.applies_to == "hosts":
                chosen = _ids(data.getlist(f"hosts:{spec.id}"), host_ids)
            else:
                chosen = _ids(data.getlist(f"clusters:{spec.id}"), cluster_ids)
            if chosen is None:
                errors.append(f"{spec.label}: you can only choose your own hosts and clusters.")
                continue
            if spec.applies_to == "hosts":
                rule.hosts = chosen
            else:
                rule.clusters = chosen
        values[spec.id] = rule
    return values, errors
