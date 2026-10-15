"""Guard: the test suite must never pull in daphne or twisted again.

They came in only through Channels' testing package (its live-server test case imports
daphne, which needs twisted), and twisted had a known vulnerability with no fixed
release. tests/ws.py replaces what we used from it. This imports every test module and
checks that neither package was loaded along the way.

It runs in a fresh Python process: in this one, other tests' imports are already in
sys.modules, and a module imported earlier would hide where daphne came from.
"""

import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
REPO = SERVER.parent

# Same import paths as pytest (pyproject.toml): the repo root (for `agent`) and server/.
# Imports every test module: server/tests/*.py and agent/tests/*.py.
CHECK = """
import importlib, os, pathlib, sys
sys.path[:0] = [{repo!r}, {server!r}]
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cronrack.settings_test")
import django
django.setup()
for package, folder in (("tests", {server!r} + "/tests"), ("agent.tests", {repo!r} + "/agent/tests")):
    for path in sorted(pathlib.Path(folder).glob("*.py")):
        importlib.import_module(package + "." + path.stem)
bad = sorted(name for name in sys.modules if name.split(".")[0] in ("daphne", "twisted"))
print(",".join(bad))
""".format(repo=str(REPO), server=str(SERVER))


def test_importing_the_tests_loads_no_daphne_or_twisted():
    result = subprocess.run(
        [sys.executable, "-c", CHECK],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"loaded: {result.stdout.strip()}"
