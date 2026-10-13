import pytest

from hosts.models import Host


@pytest.fixture
def alice(django_user_model):
    return django_user_model.objects.create_user(username="alice", password="unused-pw-1")


@pytest.fixture
def bob(django_user_model):
    return django_user_model.objects.create_user(username="bob", password="unused-pw-2")


@pytest.fixture
def host(alice):
    return Host.objects.create(owner=alice, name="nas")
