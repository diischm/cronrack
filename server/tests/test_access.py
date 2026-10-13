import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import Http404

from hosts.access import get_owned_host

pytestmark = pytest.mark.django_db


def test_owner_gets_their_host(alice, host):
    assert get_owned_host(alice, host.pk) == host


def test_another_users_host_is_404(bob, host):
    with pytest.raises(Http404):
        get_owned_host(bob, host.pk)


def test_missing_host_is_404(alice):
    with pytest.raises(Http404):
        get_owned_host(alice, 999_999)


def test_anonymous_user_is_404(host):
    with pytest.raises(Http404):
        get_owned_host(AnonymousUser(), host.pk)
