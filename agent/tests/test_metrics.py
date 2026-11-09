"""Host metrics (agent/metrics.py) against fixture /proc and /sys files."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import metrics
from agent.metrics import (
    CpuTimes,
    MetricsSampler,
    cpu_percent,
    mount_usage,
    parse_diskstats,
    parse_loadavg,
    parse_meminfo,
    parse_mountinfo,
    parse_proc_stat,
    pick_cpu_temperature,
    rate,
    read_thermal_zones,
)

FIXTURES = Path(__file__).parent / "fixtures" / "metrics"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- CPU ---------------------------------------------------------------------------------


def test_proc_stat_totals_and_cores():
    times = parse_proc_stat(fixture("proc_stat_1.txt"))
    assert times == CpuTimes(total=96200, idle=81000, iowait=1000, cores=4)


def test_cpu_percent_between_two_readings():
    first = parse_proc_stat(fixture("proc_stat_1.txt"))
    second = parse_proc_stat(fixture("proc_stat_2.txt"))
    # 1000 ticks passed, 630 of them idle (600 idle + 30 iowait).
    assert cpu_percent(first, second) == (37.0, 3.0)


def test_cpu_percent_needs_two_readings_and_forward_counters():
    one = parse_proc_stat(fixture("proc_stat_1.txt"))
    two = parse_proc_stat(fixture("proc_stat_2.txt"))
    assert cpu_percent(None, one) == (None, None)
    assert cpu_percent(two, one) == (None, None)  # counters went backwards
    assert cpu_percent(one, one) == (None, None)  # no time passed


@pytest.mark.parametrize(
    "text",
    ["", "cpu  a b c d e f g h", "cpu  1 2 3", "intr 1 2 3", "cpu  -1 0 0 0 0 0 0 0 0 0"],
)
def test_proc_stat_garbage_gives_none(text):
    assert parse_proc_stat(text) is None


def test_loadavg():
    assert parse_loadavg(fixture("loadavg.txt")) == [0.42, 0.38, 0.3]
    assert parse_loadavg("") is None
    assert parse_loadavg("x y z") is None
    assert parse_loadavg("nan 1 1") is None
    assert parse_loadavg("-1 1 1") is None


# --- temperature -------------------------------------------------------------------------


def test_raspberry_pi_cpu_thermal_zone():
    assert pick_cpu_temperature([("cpu-thermal\n", "52123\n")]) == 52.1


def test_known_cpu_zone_wins_over_others():
    zones = [("iwlwifi_1", "38000"), ("acpitz", "30000"), ("x86_pkg_temp", "61500")]
    assert pick_cpu_temperature(zones) == 61.5


def test_unknown_zones_and_absurd_values_give_none():
    assert pick_cpu_temperature([("iwlwifi_1", "38000")]) is None
    assert pick_cpu_temperature([("cpu-thermal", "999999")]) is None
    assert pick_cpu_temperature([("cpu-thermal", "hot")]) is None
    assert pick_cpu_temperature([]) is None


def test_read_thermal_zones_from_a_sysfs_tree(tmp_path):
    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "type").write_text("cpu-thermal\n")
    (zone / "temp").write_text("48700\n")
    (tmp_path / "cooling_device0").mkdir()  # not a zone
    assert read_thermal_zones(str(tmp_path)) == [("cpu-thermal\n", "48700\n")]


def test_no_thermal_directory_means_no_zones(tmp_path):
    assert read_thermal_zones(str(tmp_path / "missing")) == []


# --- memory ------------------------------------------------------------------------------


def test_meminfo_uses_mem_available_not_mem_free():
    memory = parse_meminfo(fixture("meminfo.txt"))
    assert memory == {
        "total": 8029176 * 1024,
        "available": 3407872 * 1024,  # MemAvailable, not MemFree (412348 kB)
        "swap_total": 2097148 * 1024,
        "swap_used": (2097148 - 1835004) * 1024,
    }


def test_meminfo_without_swap():
    memory = parse_meminfo(fixture("meminfo_no_swap.txt"))
    assert memory["swap_total"] == 0
    assert memory["swap_used"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "",
        "MemTotal: 100 kB",  # no MemAvailable (kernel before 3.14)
        "MemTotal: 0 kB\nMemAvailable: 0 kB",
        "MemTotal: 100 kB\nMemAvailable: 200 kB",
    ],
)
def test_meminfo_garbage_gives_none(text):
    assert parse_meminfo(text) is None


# --- disks -------------------------------------------------------------------------------


def test_mountinfo_keeps_local_filesystems_once_per_device():
    assert parse_mountinfo(fixture("mountinfo.txt")) == [
        ("/", "ext4"),  # /srv/data is a bind mount of the same device
        ("/boot/firmware", "vfat"),
        ("/srv/media", "btrfs"),  # "/srv/media backup" is the same btrfs
    ]


def test_two_devices_at_one_path_are_one_mount():
    # The server refuses duplicate paths, and that would cost the host all its metrics.
    lines = "40 28 8:1 / /srv rw - ext4 /dev/sda1 rw\n41 40 8:2 / /srv rw - xfs /dev/sdb1 rw\n"
    assert parse_mountinfo(lines) == [("/srv", "xfs")]  # the later mount is the visible one


def test_a_mount_without_a_computable_percentage_is_skipped():
    full = statvfs_result(blocks=1000, bfree=1000, bavail=0)
    assert mount_usage("/", "ext4", lambda p: full) is None


def test_a_failed_read_makes_the_next_rate_null_not_double(proc, tmp_path):
    clock = FakeClock()
    sampler = make_sampler(proc, clock, tmp_path)
    sampler.collect(containers=False)
    (proc / "diskstats").unlink()  # the read fails once
    clock.now += 60
    assert sampler.collect(containers=False)["disk_io"] is None
    shutil.copy(FIXTURES / "diskstats_2.txt", proc / "diskstats")
    clock.now += 60
    assert sampler.collect(containers=False)["disk_io"] is None  # no bogus double interval


def test_a_stuck_earlier_reading_costs_only_this_reading(proc, tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "LOCK_TIMEOUT", 0.01)
    sampler = make_sampler(proc, FakeClock(), tmp_path)
    assert sampler._lock.acquire()
    with pytest.raises(TimeoutError):
        sampler.collect(containers=False)
    sampler._lock.release()
    assert sampler.collect(containers=False)["cpu"]["cores"] == 4


def test_mountinfo_never_includes_network_or_pseudo_filesystems():
    mounts = {m for m, _ in parse_mountinfo(fixture("mountinfo.txt"))}
    # /mnt/nas is NFS and /mnt/pool is FUSE: statvfs() on either can hang forever.
    for skipped in ("/mnt/nas", "/mnt/pool", "/proc", "/sys", "/run", "/dev", "/snap/core22/1234"):
        assert skipped not in mounts
    assert not any(m.startswith("/var/lib/docker") for m in mounts)


def test_mountinfo_unescapes_spaces():
    line = "40 28 8:17 / /srv/my\\040disk rw - ext4 /dev/sdb1 rw\n"
    assert parse_mountinfo(line) == [("/srv/my disk", "ext4")]


def test_mountinfo_skips_control_characters_and_long_paths():
    newline = "41 28 8:18 / /srv/a\\012b rw - ext4 /dev/sdb2 rw\n"
    long = f"42 28 8:19 / /{'x' * 200} rw - ext4 /dev/sdb3 rw\n"
    assert parse_mountinfo(newline + long) == []


def test_mountinfo_is_capped():
    lines = "".join(f"{i} 28 8:{i} / /m{i:03d} rw - ext4 /dev/x{i} rw\n" for i in range(100))
    assert len(parse_mountinfo(lines)) == metrics.MAX_MOUNTS


def statvfs_result(blocks=1000, bfree=400, bavail=350, files=100, ffree=75, frsize=4096):
    return SimpleNamespace(
        f_blocks=blocks,
        f_bfree=bfree,
        f_bavail=bavail,
        f_files=files,
        f_ffree=ffree,
        f_frsize=frsize,
    )


def test_mount_usage_matches_df():
    usage = mount_usage("/", "ext4", lambda path: statvfs_result())
    assert usage == {
        "mount": "/",
        "fs": "ext4",
        "size": 1000 * 4096,
        "free": 350 * 4096,
        "used_pct": 63.2,  # 600 used / (600 + 350 available), like df's Use%
        "inodes_pct": 25.0,
    }


def test_filesystems_without_inodes_report_none():
    usage = mount_usage("/srv/media", "btrfs", lambda path: statvfs_result(files=0, ffree=0))
    assert usage["inodes_pct"] is None


def test_unreadable_mount_is_skipped():
    def denied(path):
        raise PermissionError(13, "Permission denied")

    assert mount_usage("/home", "ext4", denied) is None
    assert mount_usage("/", "ext4", lambda p: statvfs_result(blocks=0, bfree=0, bavail=0)) is None


def test_diskstats_counts_whole_disks_only():
    # mmcblk0 + sda + nvme0n1; partitions, loop, ram and dm-0 are not added again.
    assert parse_diskstats(fixture("diskstats_1.txt")) == (
        200000 + 800000 + 40000,
        400000 + 1200000 + 30000,
    )


def test_diskstats_garbage():
    assert parse_diskstats("") is None
    assert parse_diskstats("8 0 sda x y z a b c d e f") is None


def test_rate():
    assert rate(100, 700, 60) == 10
    assert rate(None, 700, 60) is None
    assert rate(700, 100, 60) is None  # counter reset
    assert rate(100, 700, 0) is None


# --- the sampler -------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def proc(tmp_path):
    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    shutil.copy(FIXTURES / "proc_stat_1.txt", root / "stat")
    shutil.copy(FIXTURES / "loadavg.txt", root / "loadavg")
    shutil.copy(FIXTURES / "meminfo.txt", root / "meminfo")
    shutil.copy(FIXTURES / "diskstats_1.txt", root / "diskstats")
    shutil.copy(FIXTURES / "mountinfo.txt", root / "self" / "mountinfo")
    thermal = tmp_path / "thermal" / "thermal_zone0"
    thermal.mkdir(parents=True)
    (thermal / "type").write_text("cpu-thermal\n")
    (thermal / "temp").write_text("52100\n")
    return root


def make_sampler(proc: Path, clock: FakeClock, tmp_path: Path) -> MetricsSampler:
    return MetricsSampler(
        proc_root=str(proc),
        thermal_root=str(tmp_path / "thermal"),
        statvfs=lambda path: statvfs_result(),
        clock=clock,
        docker_socket=str(tmp_path / "no-docker.sock"),
    )


def test_first_reading_has_no_rates(proc, tmp_path):
    sampler = make_sampler(proc, FakeClock(), tmp_path)
    result = sampler.collect(containers=False)
    assert result["interval_s"] is None
    assert result["cpu"] == {
        "busy_pct": None,
        "iowait_pct": None,
        "load": [0.42, 0.38, 0.3],
        "cores": 4,
        "temp_c": 52.1,
    }
    assert result["disk_io"] is None
    assert result["memory"]["available"] == 3407872 * 1024
    assert [m["mount"] for m in result["mounts"]] == [
        "/",
        "/boot/firmware",
        "/srv/media",
    ]
    assert result["containers"] is None
    assert result["containers_truncated"] is False


def test_second_reading_has_rates(proc, tmp_path):
    clock = FakeClock()
    sampler = make_sampler(proc, clock, tmp_path)
    sampler.collect(containers=False)
    shutil.copy(FIXTURES / "proc_stat_2.txt", proc / "stat")
    shutil.copy(FIXTURES / "diskstats_2.txt", proc / "diskstats")
    clock.now += 60
    result = sampler.collect(containers=False)
    assert result["interval_s"] == 60
    assert result["cpu"]["busy_pct"] == 37.0
    assert result["cpu"]["iowait_pct"] == 3.0
    # (1200 + 12000) sectors read and (5000 + 24000) written, 512 bytes each, in 60 s.
    assert result["disk_io"] == {
        "read_bps": round(13200 * 512 / 60),
        "write_bps": round(29000 * 512 / 60),
    }


def test_missing_proc_files_give_nulls_not_errors(tmp_path):
    sampler = MetricsSampler(
        proc_root=str(tmp_path / "empty"),
        thermal_root=str(tmp_path / "none"),
        docker_socket=str(tmp_path / "none.sock"),
    )
    result = sampler.collect(containers=True)
    assert result["cpu"] == {
        "busy_pct": None,
        "iowait_pct": None,
        "load": None,
        "cores": None,
        "temp_c": None,
    }
    assert result["memory"] is None
    assert result["mounts"] == []
    assert result["disk_io"] is None
    assert result["containers"] is None  # no Docker socket: skipped cleanly


def test_docker_is_not_asked_when_containers_are_off(proc, tmp_path, monkeypatch):
    sampler = make_sampler(proc, FakeClock(), tmp_path)
    monkeypatch.setattr(sampler.docker, "collect", lambda total: pytest.fail("asked Docker"))
    assert sampler.collect(containers=False)["containers"] is None


def test_the_real_host_can_be_read():
    # Smoke test on the machine running the tests: no crash, and the shape is complete.
    if not os.path.exists("/proc/stat"):
        pytest.skip("no /proc on this system")
    result = MetricsSampler(docker_socket="/nonexistent/docker.sock").collect(containers=False)
    assert set(result) == {
        "interval_s",
        "cpu",
        "memory",
        "mounts",
        "disk_io",
        "containers",
        "containers_truncated",
    }
