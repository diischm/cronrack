from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from hosts.models import Host
from rules.models import RuleOverride, Suppression, fingerprint_for

pytestmark = pytest.mark.django_db


def test_snooze_without_expires_at_fails_validation(alice, host):
    snooze = Suppression(owner=alice, host=host, rule_id="disk.usage", kind="snooze")

    with pytest.raises(ValidationError):
        snooze.full_clean()


def test_snooze_without_expires_at_is_refused_by_the_database(alice, host):
    with pytest.raises(IntegrityError):
        Suppression.objects.create(owner=alice, host=host, rule_id="disk.usage", kind="snooze")


def test_snooze_with_expires_at_is_valid(alice, host):
    snooze = Suppression(
        owner=alice,
        host=host,
        rule_id="disk.usage",
        kind="snooze",
        expires_at=timezone.now() + timedelta(days=1),
    )
    snooze.full_clean()
    snooze.save()

    assert snooze.fingerprint == fingerprint_for("disk.usage", host.pk, "")


def test_expected_is_valid_without_expires_at(alice, host):
    Suppression(owner=alice, host=host, rule_id="disk.usage", kind="expected").full_clean()


def test_suppression_on_another_users_host_fails_validation(bob, host):
    suppression = Suppression(owner=bob, host=host, rule_id="disk.usage", kind="expected")

    with pytest.raises(ValidationError):
        suppression.full_clean()


def test_rule_override_unique_per_host(alice, host):
    RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=host, subject="mount:/")

    with pytest.raises(IntegrityError), transaction.atomic():
        RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=host, subject="mount:/")


def test_rule_override_unique_for_all_hosts(alice):
    RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=None, subject="")

    with pytest.raises(IntegrityError), transaction.atomic():
        RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=None, subject="")


def test_rule_override_different_scopes_are_allowed(alice, bob, host):
    other_host = Host.objects.create(owner=alice, name="pi")
    RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=host)
    RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=other_host)
    RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=host, subject="mount:/")
    RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=None)
    RuleOverride.objects.create(owner=bob, rule_id="disk.usage", host=None)

    assert RuleOverride.objects.count() == 5


def test_duplicate_override_fails_model_validation(alice, host):
    RuleOverride.objects.create(owner=alice, rule_id="disk.usage", host=host)

    duplicate = RuleOverride(owner=alice, rule_id="disk.usage", host=host)
    with pytest.raises(ValidationError):
        duplicate.full_clean()
