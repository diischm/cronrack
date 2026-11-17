"""agent/requirements.txt pins the agent's one library by version AND by hash.

install.sh runs `pip install --require-hashes`, so a file whose SHA-256 isn't listed is
refused, wherever it comes from. These tests keep that file in the shape that needs.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENT_REQUIREMENTS = REPO / "agent" / "requirements.txt"
DEV_REQUIREMENTS = REPO / "requirements-dev.txt"
INSTALL_SH = REPO / "deploy" / "install.sh"

_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([0-9][0-9A-Za-z.]*)", re.MULTILINE)


def requirement_lines(path: Path) -> str:
    """The file without comments, with backslash continuations joined."""
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")
    ]
    return "\n".join(lines).replace("\\\n", " ")


def test_one_exact_pin_with_hashes():
    text = requirement_lines(AGENT_REQUIREMENTS)
    entries = [line for line in text.splitlines() if line.strip()]
    assert len(entries) == 1, "the agent has exactly one dependency"
    (entry,) = entries
    name, version = _PIN.match(entry.strip()).groups()
    assert version == "17.2" or re.fullmatch(r"\d+\.\d+(\.\d+)?", version)
    assert name == "websockets"
    hashes = re.findall(r"--hash=sha256:([0-9a-f]+)", entry)
    assert all(len(h) == 64 for h in hashes)
    assert len(hashes) == len(set(hashes))
    # Every published file (wheels for every platform plus the sdist): 148 for 17.2.
    # Fewer would mean some platform (e.g. a Raspberry Pi) can't install.
    assert len(hashes) >= 100


def test_dev_requirements_use_the_same_version():
    agent = dict(_PIN.findall(requirement_lines(AGENT_REQUIREMENTS)))
    dev = dict(_PIN.findall(requirement_lines(DEV_REQUIREMENTS)))
    assert dev["websockets"] == agent["websockets"]
    # Not included with -r: its hashes would force hash mode on every dev package.
    assert "agent/requirements.txt" not in requirement_lines(DEV_REQUIREMENTS)


def test_installer_refuses_unhashed_files():
    text = INSTALL_SH.read_text(encoding="utf-8")
    pip_lines = [line for line in text.splitlines() if '/bin/pip" install' in line]
    assert pip_lines, "install.sh installs the agent's library with pip"
    block = text[text.index(pip_lines[0]) :].split("\n\n", 1)[0]
    assert "--require-hashes" in block
    assert "--no-deps" in block


def test_installer_uses_pypi_only():
    """No pip.conf, PIP_* variable or extra index (piwheels on a Pi) can add a source."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    pip_lines = [line for line in text.splitlines() if '/bin/pip" install' in line]
    assert len(pip_lines) == 1, "install.sh runs pip install exactly once"
    block = text[text.index(pip_lines[0]) :].split("\n\n", 1)[0]
    command = " ".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
    assert command.startswith('PIP_CONFIG_FILE=/dev/null "$VENV_DIR/bin/pip" install')
    assert "--isolated" in command
    assert "--index-url https://pypi.org/simple " in command
    for option in ("--extra-index-url", "--find-links", "--trusted-host", "--no-index", "-i "):
        assert option not in command
    # Nowhere else in the script may an index be added either.
    for name in ("--extra-index-url", "PIP_EXTRA_INDEX_URL", "PIP_INDEX_URL", "--trusted-host"):
        assert name not in text
