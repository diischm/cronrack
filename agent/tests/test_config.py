import os
from pathlib import Path

import pytest

from agent.config import MVP_CHECKS, AgentConfig, ConfigError, check_permissions, load_config

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


def test_env_token_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, 'token = "from-file"\n')
    monkeypatch.setenv("CRONRACK_TOKEN", "from-env")

    assert load_config(path).token == "from-env"


def test_env_token_works_without_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRONRACK_TOKEN", "from-env")

    assert load_config(str(tmp_path / "missing.toml")).token == "from-env"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602, 0o644, 0o666])
def test_group_or_other_access_rejected(tmp_path: Path, mode: int) -> None:
    path = write_config(tmp_path, f'token = "{SECRET}"\n', mode=mode)

    with pytest.raises(ConfigError, match="chmod 600"):
        load_config(path)
    with pytest.raises(ConfigError):
        check_permissions(path)


@pytest.mark.parametrize("mode", [0o600, 0o400])
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
