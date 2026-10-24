"""The Dockerfile's user and writable paths, as Kubernetes' securityContext checks them.

runAsNonRoot can only verify a *numeric* USER (a name could map to root); fsGroup gives
the volume to a group, so /data must be writable by the group too.
"""

import re
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
UID = GID = "10001"


def final_stage() -> str:
    return DOCKERFILE.rsplit("\nFROM ", 1)[1]


def test_user_is_numeric_and_not_root():
    users = re.findall(r"^USER\s+(\S+)\s*$", final_stage(), re.MULTILINE)
    assert users == [f"{UID}:{GID}"]


def test_the_user_and_group_have_the_fixed_ids():
    stage = final_stage()
    assert f"groupadd --system --gid {GID} cronrack" in stage
    assert f"useradd --system --uid {UID} --gid cronrack" in stage


def test_data_is_owned_by_the_user_and_group_writable():
    match = re.search(r"install -d -o (\S+) -g (\S+) -m (\d+) /data\b", final_stage())
    assert match, "the Dockerfile no longer creates /data"
    owner, group, mode = match.groups()
    assert (owner, group) == (UID, GID)
    assert int(mode, 8) & 0o070 == 0o070  # group: read, write, enter
    assert int(mode, 8) & 0o007 == 0  # nobody else


def test_the_database_lives_in_data():
    assert re.search(r"^\s+SQLITE_PATH=/data/db\.sqlite3\s*$", final_stage(), re.MULTILINE)


def test_nothing_else_is_made_writable_for_the_app_user():
    # Only /data (and the /tmp mount) may be written; the code stays root-owned.
    chowned = re.findall(r"-o (\S+) -g \S+ -m \d+ (\S+)", final_stage())
    assert chowned == [(UID, "/data")]
