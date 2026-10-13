import json
import os
from pathlib import Path

import pytest

from agent import report
from agent.__main__ import main

from .test_report import EXPECTED_KEYS, fake_collectors


@pytest.fixture(autouse=True)
def fake_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    # No real commands: swap the default collectors for fakes.
    monkeypatch.setattr(report, "DEFAULT_COLLECTORS", fake_collectors())
    monkeypatch.delenv("CRONRACK_TOKEN", raising=False)


def test_once_prints_valid_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--once", "--config", str(tmp_path / "missing.toml")])

    out = capsys.readouterr().out
    assert code == 0
    data = json.loads(out)
    assert set(data) == EXPECTED_KEYS
    assert data["disks"] == [{"mount": "/", "percent": 42}]


def test_once_respects_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "agent.toml"
    path.write_text('enabled_checks = ["disks"]\n', encoding="utf-8")
    os.chmod(path, 0o600)

    assert main(["--once", "--config", str(path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["updates"] is None
    assert data["disks"] == [{"mount": "/", "percent": 42}]


def test_bad_config_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "agent.toml"
    path.write_text("interval_seconds = [\n", encoding="utf-8")
    os.chmod(path, 0o600)

    code = main(["--once", "--config", str(path)])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1
    assert "config error" in captured.err


def test_unsafe_permissions_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "agent.toml"
    path.write_text('token = "abc"\n', encoding="utf-8")
    os.chmod(path, 0o644)

    assert main(["--once", "--config", str(path)]) == 2
    assert "chmod 600" in capsys.readouterr().err


def test_run_without_server_url_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--config", str(tmp_path / "missing.toml")])

    assert code == 2
    assert "server_url" in capsys.readouterr().err


def test_run_without_token_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "agent.toml"
    path.write_text('server_url = "https://cronrack.lan"\n', encoding="utf-8")
    os.chmod(path, 0o600)

    code = main(["--config", str(path)])

    assert code == 2
    assert "no token" in capsys.readouterr().err


def test_once_works_without_a_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--once", "--config", str(tmp_path / "missing.toml")]) == 0
