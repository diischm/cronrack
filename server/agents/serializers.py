"""Strict validation of everything an agent sends.

Agent data is untrusted (container and unit names come from the host, and a
stolen token lets anyone send anything), so these serializers accept exactly the
format agent/report.py builds and nothing else:

- exact JSON types: "3" is not a number, 1 is not a boolean, 3 is not a string
- every key present (a check the agent couldn't run is null, not missing)
- unknown keys rejected at every level
- length limits on every string and list, and no control characters in strings

If agent/report.py changes, change this file in the same pull request.
"""

import re
from collections.abc import Mapping
from datetime import datetime

from rest_framework import serializers
from rest_framework.settings import api_settings

# Lists longer than this are refused. A homelab host has nowhere near 500 containers.
MAX_ITEMS = 500
# Names end up in finding subjects like "container:<name>" (Suppression.subject is
# 200 characters), so they must fit with the longest prefix ("container:").
MAX_NAME = 190

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class StrictCharField(serializers.CharField):
    """A string, as-is: no type coercion, no trimming, no control characters."""

    default_error_messages = {  # noqa: RUF012 - DRF reads this class attribute
        "invalid": "Must be a string.",
        "control": "Must not contain control characters.",
    }

    def __init__(self, **kwargs):
        kwargs.setdefault("trim_whitespace", False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        if _CONTROL_CHARS.search(data):
            self.fail("control")
        return data


class StrictIntegerField(serializers.IntegerField):
    """A JSON integer. Rejects floats, numeric strings and booleans (True is an int in Python)."""

    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, int):
            self.fail("invalid")
        return data


class StrictBooleanField(serializers.BooleanField):
    """Exactly true or false; not 1, 0, "true" or "yes"."""

    def to_internal_value(self, data):
        if not isinstance(data, bool):
            self.fail("invalid")
        return data


class StrictChoiceField(serializers.ChoiceField):
    """One of a fixed set of strings, compared as-is."""

    def to_internal_value(self, data):
        if not isinstance(data, str) or data not in self.choices:
            self.fail("invalid_choice", input=str(data)[:50])
        return data


class StrictSerializer(serializers.Serializer):
    """A JSON object with exactly the declared keys. Unknown keys are an error."""

    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            raise serializers.ValidationError(
                {api_settings.NON_FIELD_ERRORS_KEY: ["Must be an object."]}
            )
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            # Only the first few names, cut short: the error is logged, never trusted.
            raise serializers.ValidationError(
                {str(key)[:50]: ["Unknown field."] for key in unknown[:5]}
            )
        return super().to_internal_value(data)


def _list(child, **kwargs):
    return serializers.ListField(child=child, max_length=MAX_ITEMS, **kwargs)


# --- report sections (same order as agent/report.py) ---------------------------


class UpdatesSerializer(StrictSerializer):
    total = StrictIntegerField(min_value=0, max_value=100_000)
    security = StrictIntegerField(min_value=0, max_value=100_000)

    def validate(self, attrs):
        if attrs["security"] > attrs["total"]:
            raise serializers.ValidationError("security can't be more than total.")
        return attrs


class ContainerSerializer(StrictSerializer):
    name = StrictCharField(max_length=MAX_NAME)
    state = StrictCharField(max_length=32)
    status = StrictCharField(max_length=255, allow_blank=True)
    health = StrictChoiceField(choices=["healthy", "unhealthy", "starting"], allow_null=True)
    exit_code = StrictIntegerField(min_value=-(2**31), max_value=2**31 - 1, allow_null=True)


class TimerSerializer(StrictSerializer):
    timer = StrictCharField(max_length=MAX_NAME)
    activates = StrictCharField(max_length=MAX_NAME, allow_null=True)
    last_run = StrictCharField(max_length=64, allow_null=True)
    next_run = StrictCharField(max_length=64, allow_null=True)


class DiskSerializer(StrictSerializer):
    mount = StrictCharField(max_length=MAX_NAME)
    percent = StrictIntegerField(min_value=0, max_value=100)


class ErrorSerializer(StrictSerializer):
    check = StrictCharField(max_length=32)
    error = StrictCharField(max_length=100)


class ReportSerializer(StrictSerializer):
    """The report built by agent/report.py: build_report()."""

    hostname = StrictCharField(max_length=253)
    agent_version = StrictCharField(max_length=32)
    timestamp = StrictCharField(max_length=20)
    updates = UpdatesSerializer(allow_null=True)
    reboot_required = StrictBooleanField(allow_null=True)
    containers = ContainerSerializer(many=True, allow_null=True, max_length=MAX_ITEMS)
    failed_units = _list(StrictCharField(max_length=MAX_NAME), allow_null=True)
    timers = TimerSerializer(many=True, allow_null=True, max_length=MAX_ITEMS)
    disks = DiskSerializer(many=True, allow_null=True, max_length=MAX_ITEMS)
    errors = ErrorSerializer(many=True, max_length=50)

    def validate_timestamp(self, value: str) -> str:
        """UTC with a 'Z', whole seconds: 2026-10-12T12:00:00Z (agent/report.py)."""
        if not _TIMESTAMP.fullmatch(value):
            raise serializers.ValidationError("Expected YYYY-MM-DDTHH:MM:SSZ.")
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise serializers.ValidationError("Not a real date and time.") from None
        return value


# --- messages ------------------------------------------------------------------


class HelloSerializer(StrictSerializer):
    type = StrictChoiceField(choices=["hello"])
    agent_version = StrictCharField(max_length=32)
    hostname = StrictCharField(max_length=253)
    os = StrictCharField(max_length=100)


class ReportMessageSerializer(StrictSerializer):
    type = StrictChoiceField(choices=["report"])
    report = ReportSerializer()
