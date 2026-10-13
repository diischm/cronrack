import pytest

from hosts.models import LEARNING_PERIOD, AgentToken, hash_token

pytestmark = pytest.mark.django_db


def test_issue_stores_only_the_hash(host):
    raw = AgentToken.issue(host)

    token = AgentToken.objects.get(host=host)
    assert token.token_hash == hash_token(raw)
    assert len(token.token_hash) == 64
    # The plaintext appears in no column of the row.
    row = AgentToken.objects.filter(pk=token.pk).values().get()
    assert raw not in [str(value) for value in row.values()]


def test_issue_twice_is_refused(host):
    AgentToken.issue(host)
    with pytest.raises(ValueError):
        AgentToken.issue(host)


def test_verify_accepts_the_right_token(host):
    raw = AgentToken.issue(host)

    assert AgentToken.verify(raw) == host
    assert AgentToken.objects.get(host=host).last_used_at is not None


@pytest.mark.parametrize("wrong", ["", "not-the-token", "x" * 10_000, None, 123])
def test_verify_rejects_wrong_tokens(host, wrong):
    AgentToken.issue(host)

    assert AgentToken.verify(wrong) is None


def test_verify_rejects_a_revoked_token(host):
    raw = AgentToken.issue(host)
    AgentToken.objects.filter(host=host).update(revoked=True)

    assert AgentToken.verify(raw) is None


def test_rotate_invalidates_the_old_token(host):
    old = AgentToken.issue(host)

    new = AgentToken.rotate(host)

    assert new != old
    assert AgentToken.verify(old) is None
    assert AgentToken.verify(new) == host
    assert AgentToken.objects.filter(host=host).count() == 1


def test_rotate_unrevokes(host):
    AgentToken.issue(host)
    AgentToken.objects.filter(host=host).update(revoked=True)

    new = AgentToken.rotate(host)

    assert AgentToken.verify(new) == host


def test_new_host_starts_learning_for_24h(host):
    assert host.learning_until == host.created_at + LEARNING_PERIOD
