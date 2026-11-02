"""Fixtures for the install.sh tests: a test image, agent packages, throwaway containers.

deploy/install.sh needs root and systemd, so it runs as root inside a throwaway
Debian container (tests/install/Dockerfile) with a fake systemctl, started with
--network none. The packages it downloads are served from inside the container by
serve.py, over http on 127.0.0.1:8000 and https on 127.0.0.1:443.

Everything gets the cronrack-installtest prefix and label and is removed again.
Without Docker the tests are skipped, unless CRONRACK_REQUIRE_INSTALL_TESTS=1 (as in
CI), which makes them fail instead.
"""

import gzip
import hashlib
import io
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from agents import package

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
PREFIX = "cronrack-installtest"
LABEL = "cronrack-installtest=1"

TOKEN = "fake-agent-token-for-tests-only"
K8S_TOKEN = "fakeheader0.fakepayload0.fakesignatur"

NEXT_VERSION = "0.99.0"
BROKEN_VERSION = "0.99.1"


def _docker(*args: str, timeout: int = 120, check: bool = True, **kwargs):
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        **kwargs,
    )


def _docker_available() -> str | None:
    """None if Docker works here, otherwise why not."""
    if shutil.which("docker") is None:
        return "docker is not installed"
    try:
        result = _docker("info", "--format", "{{.ServerVersion}}", timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker info failed: {exc}"
    if result.returncode != 0:
        return f"docker info failed: {result.stderr.strip()[:200]}"
    return None


@pytest.fixture(scope="session")
def docker_ready() -> None:
    reason = _docker_available()
    if reason is None:
        return
    if os.environ.get("CRONRACK_REQUIRE_INSTALL_TESTS") == "1":
        pytest.fail(f"install tests are required (CRONRACK_REQUIRE_INSTALL_TESTS=1): {reason}")
    pytest.skip(f"install tests need Docker: {reason}")


@pytest.fixture(scope="session")
def image(docker_ready, tmp_path_factory) -> str:
    context = tmp_path_factory.mktemp("image")
    for name in ("Dockerfile", "fake_systemctl.sh", "entrypoint.sh", "serve.py"):
        shutil.copy(HERE / name, context / name)
    shutil.copy(REPO_ROOT / "agent" / "requirements.txt", context / "requirements.txt")
    tag = f"{PREFIX}:{uuid.uuid4().hex[:12]}"
    _docker("build", "-q", "--label", LABEL, "-t", tag, str(context), timeout=600)
    yield tag
    _docker("rmi", "-f", tag, check=False)


# --- agent packages ------------------------------------------------------------------


def _rewrite(data: bytes, replacements: dict[str, bytes]) -> bytes:
    """A copy of the tarball with some files replaced (same deterministic metadata)."""
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as old,
        gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as new,
    ):
        for info in old.getmembers():
            if info.isfile():
                content = replacements.get(info.name, old.extractfile(info).read())
                info.size = len(content)
                new.addfile(info, io.BytesIO(content))
            else:
                new.addfile(info)
    return out.getvalue()


def _with_version(data: bytes, version: str, extra: dict[str, bytes] | None = None) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        init = tar.extractfile("agent/__init__.py").read().decode()
        main = tar.extractfile("agent/__main__.py").read()
    lines = [
        f'AGENT_VERSION = "{version}"' if line.startswith("AGENT_VERSION = ") else line
        for line in init.splitlines()
    ]
    replacements = {"agent/__init__.py": ("\n".join(lines) + "\n").encode()}
    if extra:
        replacements.update({k: v(main) if callable(v) else v for k, v in extra.items()})
    return _rewrite(data, replacements)


@dataclass(frozen=True)
class Packages:
    root: Path
    sha256: dict[str, str]
    current_version: str
    next_version: str = NEXT_VERSION
    broken_version: str = BROKEN_VERSION


@pytest.fixture(scope="session")
def packages(tmp_path_factory) -> Packages:
    """current = the repo's agent; next = a newer version; broken = a newer version
    that crashes on start (for the rollback)."""
    current = package.build_package().data
    with tarfile.open(fileobj=io.BytesIO(current), mode="r:gz") as tar:
        init = tar.extractfile("agent/__init__.py").read().decode()
    current_version = re.search(r'^AGENT_VERSION = "([^"]+)"$', init, re.MULTILINE).group(1)
    variants = {
        "current": current,
        "next": _with_version(current, NEXT_VERSION),
        "broken": _with_version(
            current,
            BROKEN_VERSION,
            {"agent/__main__.py": lambda main: b'raise SystemExit("broken on purpose")\n' + main},
        ),
    }
    root = tmp_path_factory.mktemp("pkgs")
    sums = {}
    for name, data in variants.items():
        download = root / name / "agent" / "download"
        download.mkdir(parents=True)
        (download / "cronrack-agent.tar.gz").write_bytes(data)
        sums[name] = hashlib.sha256(data).hexdigest()
        (download / "cronrack-agent.tar.gz.sha256").write_text(
            f"{sums[name]}  cronrack-agent.tar.gz\n"
        )
    # The container's root reads these through a bind mount.
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    return Packages(root=root, sha256=sums, current_version=current_version)


# --- one throwaway container per test --------------------------------------------------


@dataclass
class Result:
    code: int
    out: str


class Box:
    """A running test container, with helpers to run install.sh in it."""

    token = TOKEN
    k8s_token = K8S_TOKEN

    def __init__(self, name: str):
        self.name = name

    def sh(self, script: str, stdin: str | None = None, timeout: int = 180) -> Result:
        # stdout and stderr together, in order, as a user would see them.
        result = subprocess.run(
            ["docker", "exec", "-i", self.name, "bash", "-c", script],
            input=stdin if stdin is not None else "",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return Result(result.returncode, result.stdout)

    def ok(self, script: str) -> str:
        result = self.sh(script)
        assert result.code == 0, result.out
        return result.out

    def read(self, path: str) -> str:
        return self.ok(f"cat {shlex.quote(path)}")

    def exists(self, path: str) -> bool:
        return self.sh(f"test -e {shlex.quote(path)}").code == 0

    def mode(self, path: str) -> str:
        return self.ok(f"stat -c '%U:%G %a' {shlex.quote(path)}").strip()

    def use_package(self, name: str) -> None:
        self.ok(f"ln -sfn /pkgs/{name} /srv/www")

    def systemctl_calls(self) -> list[str]:
        return self.ok("cat /var/log/fake-systemctl.log 2>/dev/null || true").splitlines()

    def clear_systemctl_calls(self) -> None:
        self.ok("rm -f /var/log/fake-systemctl.log")

    def is_active(self) -> bool:
        return self.sh("systemctl is-active --quiet cronrack-agent.service").code == 0

    def is_enabled(self) -> bool:
        return self.sh("systemctl is-enabled --quiet cronrack-agent.service").code == 0

    def interactive(self, args: str, answers: str) -> Result:
        """install.sh on a fake terminal (script(1)); answers are typed in, one per line."""
        command = shlex.quote(f"bash /install.sh {args}")
        return self.sh(f"script -qec {command} /dev/null", stdin=answers)

    def install(self, args: str = "", answers: str | None = None, *, k8s_view=False) -> Result:
        """A fresh install over https with the throwaway CA; asserts it worked."""
        if answers is None:
            answers = (f"{K8S_TOKEN}\n" if k8s_view else "") + f"{TOKEN}\n"
        result = self.interactive(
            f"--server https://localhost --ca-file /srv/tls/ca.crt {args}", answers
        )
        assert result.code == 0, result.out
        return result

    def upgrade(self, args: str = "--server https://localhost", stdin: str = "pipe") -> Result:
        """install.sh --upgrade in a new session with no terminal.

        stdin="pipe": the script arrives on stdin, as with curl | sudo bash -s.
        stdin="closed": stdin is closed. Either way /dev/tty can't be opened.
        """
        if stdin == "pipe":
            return self.sh(f"cat /install.sh | setsid -w bash -s -- {args} --upgrade")
        return self.sh(f"setsid -w bash /install.sh {args} --upgrade <&-")


@pytest.fixture
def box(image, packages):
    name = f"{PREFIX}-{uuid.uuid4().hex[:12]}"
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        LABEL,
        "--network",
        "none",
        "-v",
        f"{packages.root}:/pkgs:ro",
        "-v",
        f"{INSTALL_SH}:/install.sh:ro",
        image,
    )
    try:
        container = Box(name)
        # Wait until both package servers answer.
        container.ok(
            "for i in $(seq 50); do"
            " curl -fs -o /dev/null http://127.0.0.1:8000/agent/download/cronrack-agent.tar.gz"
            " && curl -fs -o /dev/null --cacert /srv/tls/ca.crt"
            " https://localhost/agent/download/cronrack-agent.tar.gz && exit 0;"
            " sleep 0.1; done; exit 1"
        )
        yield container
    finally:
        _docker("rm", "-f", "-v", name, check=False)
