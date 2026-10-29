import json
import os
from pathlib import Path

import pytest

from agent import __main__ as agent_main
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


# --- --check-k8s ------------------------------------------------------------------------------

K8S_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.signature-part-of-the-jwt"


def row(resource, verb, result, code, group=""):
    return {"group": group, "resource": resource, "verb": verb, "result": result, "code": code}


def k8s_report(**overrides):
    report_ = {
        "reachable": True,
        "error": None,
        "sa": "system:serviceaccount:monitoring:cronrack-agent",
        "token_days": 300,
        "cert_days": 290,
        "rows": [
            row("/version", "get", "ok", 200),
            row("pods", "list", "ok", 200),
            row("deployments", "list", "ok", 200, "apps"),
            row("applications", "list", "missing", 404, "argoproj.io"),
        ],
    }
    report_.update(overrides)
    return report_


@pytest.fixture
def cluster_view_config(tmp_path: Path) -> str:
    token = tmp_path / "k8s-token"
    token.write_text(K8S_TOKEN, encoding="utf-8")
    os.chmod(token, 0o600)
    ca = tmp_path / "k8s-ca.crt"
    ca.write_text(
        "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8"
    )
    path = tmp_path / "agent.toml"
    path.write_text(
        f"[kubernetes]\nenabled = true\ncluster_view = true\n"
        f'token_file = "{token}"\nca_file = "{ca}"\n',
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return str(path)


def fake_permission_report(monkeypatch, report_):
    calls = []

    def fake(client, *args, **kwargs):
        calls.append(client)
        return report_

    monkeypatch.setattr(agent_main, "permission_report", fake)
    return calls


def test_check_k8s_all_ok_exits_0(cluster_view_config, monkeypatch, capsys) -> None:
    fake_permission_report(monkeypatch, k8s_report())

    code = main(["--check-k8s", "--config", cluster_view_config])

    out = capsys.readouterr().out
    assert code == 0
    assert "core/pods" in out
    assert "apps/deployments" in out
    assert "not installed (404)" in out
    assert "290 days" in out
    assert "300 days" in out
    assert "system:serviceaccount:monitoring:cronrack-agent" in out


def test_check_k8s_denied_exits_3(cluster_view_config, monkeypatch, capsys) -> None:
    rows = [row("pods", "list", "denied", 403), row("nodes", "list", "ok", 200)]
    fake_permission_report(monkeypatch, k8s_report(rows=rows))

    code = main(["--check-k8s", "--config", cluster_view_config])

    out = capsys.readouterr().out
    assert code == 3
    assert "denied (403)" in out


def test_check_k8s_unauthorized_exits_3(cluster_view_config, monkeypatch, capsys) -> None:
    rows = [row("/version", "get", "unauthorized", 401)]
    fake_permission_report(monkeypatch, k8s_report(rows=rows, error="unauthorized"))

    assert main(["--check-k8s", "--config", cluster_view_config]) == 3
    assert "unauthorized (401)" in capsys.readouterr().out


def test_check_k8s_unreachable_exits_3(cluster_view_config, monkeypatch, capsys) -> None:
    unreachable = k8s_report(reachable=False, error="tls", rows=[], sa=None, cert_days=None)
    fake_permission_report(monkeypatch, unreachable)

    code = main(["--check-k8s", "--config", cluster_view_config])

    assert code == 3
    assert "TLS certificate not trusted" in capsys.readouterr().out


def test_check_k8s_never_prints_the_token_or_object_data(
    cluster_view_config, monkeypatch, capsys
) -> None:
    fake_permission_report(monkeypatch, k8s_report())

    main(["--check-k8s", "--config", cluster_view_config])

    captured = capsys.readouterr()
    assert K8S_TOKEN not in captured.out + captured.err
    assert K8S_TOKEN.split(".")[1] not in captured.out + captured.err


def test_check_k8s_builds_the_client_from_the_config(cluster_view_config, monkeypatch) -> None:
    calls = fake_permission_report(monkeypatch, k8s_report())

    main(["--check-k8s", "--config", cluster_view_config])

    [client] = calls
    assert "k8s-token" in repr(client)
    assert K8S_TOKEN not in repr(client)


def test_check_k8s_without_cluster_view_exits_2(tmp_path, monkeypatch, capsys) -> None:
    calls = fake_permission_report(monkeypatch, k8s_report())

    code = main(["--check-k8s", "--config", str(tmp_path / "missing.toml")])

    captured = capsys.readouterr()
    assert code == 2
    assert "cluster_view" in captured.err
    assert captured.out == ""
    assert calls == []


def test_check_k8s_with_a_bad_config_exits_2(tmp_path, capsys) -> None:
    path = tmp_path / "agent.toml"
    path.write_text('[kubernetes]\napi_server = "http://127.0.0.1:6443"\n', encoding="utf-8")
    os.chmod(path, 0o600)

    assert main(["--check-k8s", "--config", str(path)]) == 2
    assert "api_server" in capsys.readouterr().err


def test_check_k8s_does_not_start_the_agent(cluster_view_config, monkeypatch) -> None:
    fake_permission_report(monkeypatch, k8s_report())
    monkeypatch.setattr(agent_main.asyncio, "run", lambda *a, **k: pytest.fail("must not connect"))

    assert main(["--check-k8s", "--config", cluster_view_config]) == 0
