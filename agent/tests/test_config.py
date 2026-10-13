import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config import (
    MVP_CHECKS,
    AgentConfig,
    ConfigError,
    _check_stat,
    check_permissions,
    load_config,
    websocket_url,
)

SECRET = "s3cr3t-token-value-abc123"


@pytest.fixture(autouse=True)
def no_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRONRACK_TOKEN", raising=False)


def write_config(tmp_path: Path, text: str, mode: int = 0o600) -> str:
    path = tmp_path / "agent.toml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    config = load_config(str(tmp_path / "does-not-exist.toml"))

    assert config == AgentConfig()
    assert config.token is None
    assert config.interval_seconds == 60
    assert config.enabled_checks == list(MVP_CHECKS)
    assert config.actions == {}


def test_valid_file(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        f'server_url = "wss://cronrack.home.lan/ws/agent/"\n'
        f'token = "{SECRET}"\n'
        f'ca_file = "/etc/cronrack/ca.pem"\n'
        f"interval_seconds = 120\n"
        f'enabled_checks = ["updates", "disks"]\n'
        f"[actions]\n",
    )

    config = load_config(path)

    assert config.server_url == "wss://cronrack.home.lan/ws/agent/"
    assert config.token == SECRET
    assert config.ca_file == "/etc/cronrack/ca.pem"
    assert config.interval_seconds == 120
    assert config.enabled_checks == ["updates", "disks"]
    assert config.actions == {}


def test_invalid_toml(tmp_path: Path) -> None:
    path = write_config(tmp_path, 'token = "unterminated\n')

    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(path)


@pytest.mark.parametrize(
    "text",
    [
        'interval_seconds = "60"\n',
        "interval_seconds = 1.5\n",
        "interval_seconds = true\n",
        "interval_seconds = 0\n",
        "server_url = 42\n",
        'ca_file = ["a"]\n',
        'enabled_checks = "updates"\n',
        "enabled_checks = [1, 2]\n",
        'actions = "none"\n',
        "intervall_seconds = 60\n",
    ],
)
def test_wrong_types_or_values(tmp_path: Path, text: str) -> None:
    path = write_config(tmp_path, text)

    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_check(tmp_path: Path) -> None:
    path = write_config(tmp_path, 'enabled_checks = ["updates", "kubernetes"]\n')

    with pytest.raises(ConfigError, match="kubernetes"):
        load_config(path)


FILE_TOKEN = "token-from-the-config-file"
ENV_TOKEN = "token-from-the-environment"


def test_env_token_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, f'token = "{FILE_TOKEN}"\n')
    monkeypatch.setenv("CRONRACK_TOKEN", ENV_TOKEN)

    assert load_config(path).token == ENV_TOKEN


def test_env_token_works_without_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRONRACK_TOKEN", ENV_TOKEN)

    assert load_config(str(tmp_path / "missing.toml")).token == ENV_TOKEN


@pytest.mark.parametrize(
    "token", ["short", "has space in it-xxxxxxxx", "x" * 129, "tok\\nen-xxxxxxxxxxxxxxxx"]
)
def test_malformed_token_rejected_without_echo(tmp_path: Path, token: str) -> None:
    path = write_config(tmp_path, f'token = "{token}"\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert token not in str(excinfo.value)


def test_malformed_env_token_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRONRACK_TOKEN", "bad token")

    with pytest.raises(ConfigError, match="token"):
        load_config(str(tmp_path / "missing.toml"))


# --- file permissions -----------------------------------------------------------

ME, MY_GROUP, OTHER_GROUP, ROOT = 1000, 1000, 999, 0


def fake_stat(mode: int, uid: int = ROOT, gid: int = MY_GROUP) -> SimpleNamespace:
    return SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid, st_gid=gid)


def check(st: SimpleNamespace) -> None:
    _check_stat(st, "/etc/cronrack/agent.toml", uid=ME, groups={MY_GROUP})


def test_installed_service_layout_ok() -> None:
    check(fake_stat(0o640, uid=ROOT, gid=MY_GROUP))  # root:cronrack 0640


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o640, 0o440])
def test_own_file_ok(mode: int) -> None:
    check(fake_stat(mode, uid=ME, gid=MY_GROUP))


@pytest.mark.parametrize("mode", [0o644, 0o604, 0o602, 0o601, 0o666, 0o647])
def test_any_other_bits_refused(mode: int) -> None:
    with pytest.raises(ConfigError, match="others"):
        check(fake_stat(mode))


@pytest.mark.parametrize("mode", [0o660, 0o620, 0o650])
def test_group_write_or_execute_refused(mode: int) -> None:
    with pytest.raises(ConfigError, match="group"):
        check(fake_stat(mode))


def test_group_readable_with_a_foreign_group_refused() -> None:
    with pytest.raises(ConfigError, match="readable by group"):
        check(fake_stat(0o640, uid=ROOT, gid=OTHER_GROUP))


def test_foreign_group_without_group_bits_ok() -> None:
    check(fake_stat(0o600, uid=ROOT, gid=OTHER_GROUP))


def test_foreign_owner_refused() -> None:
    with pytest.raises(ConfigError, match="another user"):
        check(fake_stat(0o600, uid=4242))


def test_error_names_the_fix() -> None:
    with pytest.raises(ConfigError) as excinfo:
        check(fake_stat(0o644))
    assert "chown root:cronrack" in str(excinfo.value)
    assert "chmod 640" in str(excinfo.value)


@pytest.mark.parametrize("mode", [0o644, 0o604, 0o666, 0o660])
def test_real_file_with_unsafe_mode_rejected(tmp_path: Path, mode: int) -> None:
    path = write_config(tmp_path, f'token = "{SECRET}"\n', mode=mode)

    with pytest.raises(ConfigError, match="chmod"):
        load_config(path)
    with pytest.raises(ConfigError):
        check_permissions(path)


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o640])
def test_owner_only_file_accepted(tmp_path: Path, mode: int) -> None:
    path = write_config(tmp_path, f'token = "{SECRET}"\n', mode=mode)

    check_permissions(path)
    assert load_config(path).token == SECRET


def test_token_absent_from_repr_and_str() -> None:
    config = AgentConfig(token=SECRET)

    assert SECRET not in repr(config)
    assert SECRET not in str(config)
    assert "<hidden>" in repr(config)


def test_token_absent_from_error_messages(tmp_path: Path) -> None:
    # A wrong-type token: the message must name the key, not echo the value.
    path = write_config(tmp_path, f'token = ["{SECRET}"]\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert SECRET not in str(excinfo.value)
    assert "token" in str(excinfo.value)


def test_token_absent_from_permission_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, f'token = "{SECRET}"\n', mode=0o644)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert SECRET not in str(excinfo.value)


# --- server_url ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("server_url", "expected"),
    [
        ("https://cronrack.lan", "wss://cronrack.lan/ws/agent/"),
        ("https://cronrack.lan/", "wss://cronrack.lan/ws/agent/"),
        ("https://cronrack.lan:8443", "wss://cronrack.lan:8443/ws/agent/"),
        ("https://home.lan/cronrack", "wss://home.lan/cronrack/ws/agent/"),
        ("wss://cronrack.lan/ws/agent/", "wss://cronrack.lan/ws/agent/"),
        ("http://127.0.0.1:8000", "ws://127.0.0.1:8000/ws/agent/"),
        ("http://localhost:8000/", "ws://localhost:8000/ws/agent/"),
        ("http://[::1]:8000", "ws://[::1]:8000/ws/agent/"),
        ("ws://127.0.0.1:8000/ws/agent", "ws://127.0.0.1:8000/ws/agent/"),
    ],
)
def test_websocket_url_is_derived(tmp_path: Path, server_url: str, expected: str) -> None:
    path = write_config(tmp_path, f'server_url = "{server_url}"\n')

    config = load_config(path)

    assert websocket_url(config.server_url) == expected


@pytest.mark.parametrize(
    "server_url",
    [
        "http://cronrack.lan",  # plain http to a real host: the token would travel unencrypted
        "http://192.168.1.10:8000",
        "ws://cronrack.lan/ws/agent/",
        "http://127.0.0.2:8000",
        "http://localhost.evil.com",
        "ftp://cronrack.lan",
        "cronrack.lan",
        "https://",
        "https://user:pass@cronrack.lan",
        "https://cronrack.lan/?x=1",
        "https://cronrack.lan/#frag",
        "https://cronrack.lan:99999",
    ],
)
def test_bad_server_urls_rejected(tmp_path: Path, server_url: str) -> None:
    path = write_config(tmp_path, f'server_url = "{server_url}"\n')

    with pytest.raises(ConfigError, match="server_url"):
        load_config(path)
