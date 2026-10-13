"""The files a host downloads to install the agent: install.sh and the agent tarball.

Both come straight from this repository (agent/ and deploy/), so the server always
hands out the agent that matches its own version. Nothing in them is secret: the
token is typed in at install time and never part of a download.
"""

import gzip
import hashlib
import io
import tarfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

REPO_ROOT = Path(settings.BASE_DIR).parent
INSTALL_SCRIPT = REPO_ROOT / "deploy" / "install.sh"
TARBALL_NAME = "cronrack-agent.tar.gz"

# An allowlist, not a blocklist: only these files ever go into the package.
# agent/tests/ (fixtures with real command output) is deliberately not listed.
AGENT_FILE_PATTERNS = ("agent/*.py", "agent/requirements.txt")
# The systemd unit travels inside the checksummed tarball, so install.sh never
# installs a service file it hasn't verified.
EXTRA_FILES = ("deploy/cronrack-agent.service",)

# Fixed metadata so the same files always give byte-for-byte the same tarball.
_FIXED_MTIME = 0


@dataclass(frozen=True)
class Package:
    data: bytes
    sha256: str


def package_files() -> list[str]:
    """Repo-relative paths that go into the tarball, sorted."""
    files = {
        str(path.relative_to(REPO_ROOT))
        for pattern in AGENT_FILE_PATTERNS
        for path in REPO_ROOT.glob(pattern)
        if path.is_file()
    }
    files.update(EXTRA_FILES)
    return sorted(files)


def _add(tar: tarfile.TarFile, name: str, data: bytes | None) -> None:
    info = tarfile.TarInfo(name)
    info.mtime = _FIXED_MTIME
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if data is None:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
    else:
        info.mode = 0o644
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def build_package() -> Package:
    """Build the tarball in memory: deterministic (sorted, fixed owners/times, no gzip name)."""
    files = package_files()
    directories = sorted({str(Path(name).parent) for name in files})
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=_FIXED_MTIME) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar,
    ):
        for directory in directories:
            _add(tar, directory, None)
        for name in files:
            _add(tar, name, (REPO_ROOT / name).read_bytes())
    data = buffer.getvalue()
    return Package(data=data, sha256=hashlib.sha256(data).hexdigest())


@lru_cache(maxsize=1)
def agent_package() -> Package:
    """Built once per server process (restart the server to pick up agent changes)."""
    return build_package()


@lru_cache(maxsize=1)
def install_script() -> bytes:
    return INSTALL_SCRIPT.read_bytes()
