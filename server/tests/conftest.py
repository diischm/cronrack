import pytest
from django.core.cache import cache

from clusters.models import Cluster
from hosts.models import Host


@pytest.fixture(autouse=True)
def _clear_cache():
    """The login rate limit lives in the cache; start every test with a clean one."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def alice(django_user_model):
    return django_user_model.objects.create_user(username="alice", password="unused-pw-1")


@pytest.fixture
def bob(django_user_model):
    return django_user_model.objects.create_user(username="bob", password="unused-pw-2")


@pytest.fixture
def host(alice):
    return Host.objects.create(owner=alice, name="nas")


@pytest.fixture
def alice_client(client, alice):
    client.force_login(alice)
    return client


CLUSTER_UID = "08528ee9-428d-5167-8a8e-df19e635932f"


@pytest.fixture
def cluster(alice):
    return Cluster.objects.create(owner=alice, uid=CLUSTER_UID, name="lab")
