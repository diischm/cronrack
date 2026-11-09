"""Host metrics: CPU, memory, disks and (with Docker access) running containers.

Everything here is read from files the kernel already provides (/proc, /sys) and, for
containers, from the Docker API's stats endpoint (agent/docker_api.py). No command runs
and no new privilege is needed.

Rates (CPU %, disk and network bytes per second) are the change since the previous
report, so MetricsSampler keeps the previous counters in memory. The first report after
the agent starts has null rates: there is nothing to compare with yet.

Like the collectors, the parsers never raise on odd input: a line that doesn't look right
is skipped, and a value that can't be read is None. The server validates it all again.

The shape of the report's "metrics" key (server/agents/serializers.py MetricsSerializer
mirrors it):

    {
      "interval_s": 60 | null,
      "cpu": {"busy_pct", "iowait_pct", "load": [1, 5, 15 min], "cores", "temp_c"},
      "memory": {"total", "available", "swap_total", "swap_used"} | null,
      "mounts": [{"mount", "fs", "size", "free", "used_pct", "inodes_pct"}],
      "disk_io": {"read_bps", "write_bps"} | null,
      "containers": [{"name", "cpu_pct", "mem", "mem_limit", "rx_bps", "tx_bps"}] | null,
      "containers_truncated": bool
    }
"""

import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent import docker_api

PROC_ROOT = "/proc"
THERMAL_ROOT = "/sys/class/thermal"

MAX_MOUNTS = 32
MAX_THERMAL_ZONES = 32
MAX_FILE_BYTES = 1024 * 1024  # /proc files are tiny; anything bigger is not what we expect
MAX_MOUNT_LEN = 190  # the server's limit for names in finding subjects ("mount:<path>")
LOCK_TIMEOUT = 20  # seconds: longer than one reading can take (the Docker budget is 8 s)

# Local filesystems only. statvfs() on a network share whose server is gone blocks forever
# (it can't be timed out like a command), which would stop every report, so NFS, CIFS,
# SSHFS and friends are never measured here, and neither is anything on FUSE (ntfs-3g,
# mergerfs): a stuck FUSE daemon blocks statvfs() the same way. disk.usage (from df, with
# a timeout) still covers them.
LOCAL_FS_TYPES = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "zfs",
        "bcachefs",
        "f2fs",
        "jfs",
        "reiserfs",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
    }
)

# Whole disks in /proc/diskstats. Partitions (sda1, mmcblk0p2), device-mapper and md devices
# sit on top of these, so counting them too would count the same bytes twice.
_WHOLE_DISK = re.compile(r"(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)", re.ASCII)
SECTOR_BYTES = 512  # /proc/diskstats always counts 512-byte sectors

# Thermal zones that measure the CPU (Raspberry Pi, Intel, AMD, many ARM boards), best first.
CPU_ZONE_TYPES = ("cpu-thermal", "cpu_thermal", "x86_pkg_temp", "soc_thermal", "k10temp", "acpitz")
MIN_TEMP_C = -40.0
MAX_TEMP_C = 150.0

_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")
_CPU_N = re.compile(r"cpu\d+", re.ASCII)
_ZONE = re.compile(r"thermal_zone\d+", re.ASCII)


@dataclass(frozen=True)
class CpuTimes:
    total: int
    idle: int  # idle + iowait
    iowait: int
    cores: int


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(MAX_FILE_BYTES)
    except OSError:
        return None


def _pct(part: float, whole: float) -> float | None:
    if whole <= 0:
        return None
    return round(max(0.0, min(100.0, part * 100.0 / whole)), 1)


# --- CPU ------------------------------------------------------------------------------


def parse_proc_stat(text: str) -> CpuTimes | None:
    """The "cpu" line of /proc/stat: user nice system idle iowait irq softirq steal ...

    guest and guest_nice are already part of user and nice, so only the first 8 count.
    """
    times: CpuTimes | None = None
    cores = 0
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "cpu" and len(fields) >= 9:
            try:
                values = [int(v) for v in fields[1:9]]
            except ValueError:
                return None
            if any(v < 0 for v in values):
                return None
            times = CpuTimes(
                total=sum(values), idle=values[3] + values[4], iowait=values[4], cores=0
            )
        elif _CPU_N.fullmatch(fields[0]):
            cores += 1
    if times is None:
        return None
    return CpuTimes(times.total, times.idle, times.iowait, cores=max(cores, 1))


def cpu_percent(prev: CpuTimes | None, cur: CpuTimes | None) -> tuple[float | None, float | None]:
    """(busy %, iowait %) between two readings; (None, None) if they can't be compared."""
    if prev is None or cur is None:
        return None, None
    total = cur.total - prev.total
    idle = cur.idle - prev.idle
    iowait = cur.iowait - prev.iowait
    if total <= 0 or idle < 0 or iowait < 0 or idle > total:
        return None, None  # counters went backwards (a reboot we didn't see, or a bug)
    return _pct(total - idle, total), _pct(iowait, total)


def parse_loadavg(text: str) -> list[float] | None:
    fields = text.split()
    if len(fields) < 3:
        return None
    try:
        load = [round(float(v), 2) for v in fields[:3]]
    except ValueError:
        return None
    if not all(0.0 <= v < 100_000 for v in load):  # also false for NaN
        return None
    return load


def pick_cpu_temperature(zones: list[tuple[str, str]]) -> float | None:
    """°C from (type, temp) pairs of /sys/class/thermal zones; temp is in millidegrees.

    Only a zone of a known CPU type counts. Another zone may be a battery or a Wi-Fi chip,
    and a wrong number is worse than none.
    """
    readings: dict[str, float] = {}
    for zone_type, raw in zones:
        try:
            celsius = int(raw.strip()) / 1000.0
        except ValueError:
            continue
        if MIN_TEMP_C <= celsius <= MAX_TEMP_C:
            readings.setdefault(zone_type.strip(), celsius)
    for wanted in CPU_ZONE_TYPES:
        if wanted in readings:
            return round(readings[wanted], 1)
    return None


def read_thermal_zones(root: str = THERMAL_ROOT) -> list[tuple[str, str]]:
    try:
        names = sorted(n for n in os.listdir(root) if _ZONE.fullmatch(n))
    except OSError:
        return []
    zones = []
    for name in names[:MAX_THERMAL_ZONES]:
        zone_type = _read(os.path.join(root, name, "type"))
        temp = _read(os.path.join(root, name, "temp"))
        if zone_type is not None and temp is not None:
            zones.append((zone_type, temp))
    return zones


# --- memory ---------------------------------------------------------------------------


def parse_meminfo(text: str) -> dict[str, int] | None:
    """Bytes from /proc/meminfo. "available" is MemAvailable (what programs can still get
    without swapping), not MemFree, which counts the page cache as used."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        try:
            number = int(fields[0])
        except ValueError:
            continue
        unit = fields[1] if len(fields) > 1 else ""
        values[key.strip()] = number * 1024 if unit == "kB" else number
    try:
        total, available = values["MemTotal"], values["MemAvailable"]
    except KeyError:
        return None
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    if total <= 0 or not 0 <= available <= total or swap_total < 0:
        return None
    return {
        "total": total,
        "available": available,
        "swap_total": swap_total,
        "swap_used": max(0, min(swap_total, swap_total - swap_free)),
    }


# --- disks ----------------------------------------------------------------------------


def _unescape(path: str) -> str:
    # mountinfo writes space, tab, newline and backslash as \040, \011, \012 and \134.
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m[1], 8)), path)


def parse_mountinfo(text: str) -> list[tuple[str, str]]:
    """(mount point, fs type) of local filesystems, one per device, sorted by mount point.

    A device mounted twice (a bind mount, the same btrfs at two paths) is measured once,
    at its shortest path, like df does.
    """
    by_device: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or not tail:
            continue
        device, mount, fs_type = fields[2], _unescape(fields[4]), tail[0]
        if fs_type not in LOCAL_FS_TYPES or not mount.startswith("/"):
            continue
        if len(mount) > MAX_MOUNT_LEN or any(ord(c) < 32 or ord(c) == 127 for c in mount):
            continue
        known = by_device.get(device)
        if known is None or (len(mount), mount) < (len(known[0]), known[0]):
            by_device[device] = (mount, fs_type)
    # Two devices at one path (an over-mount): the later one is what's visible there. The
    # server refuses duplicate paths, so this also keeps one odd mount from costing the host
    # all of its metrics.
    by_path = dict(by_device.values())
    return sorted(by_path.items())[:MAX_MOUNTS]


def mount_usage(
    mount: str, fs_type: str, statvfs: Callable[[str], Any] = os.statvfs
) -> dict[str, Any] | None:
    """Size, free and used % of one mount (df's formula), inode use % if it has inodes."""
    try:
        st = statvfs(mount)
    except OSError:
        return None  # e.g. under ProtectHome=yes, /home is invisible to the agent
    size = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if size <= 0 or used < 0 or free < 0 or used + free <= 0:
        return None  # nothing to compute a percentage from (e.g. every block is reserved)
    inodes_pct = None
    if st.f_files > 0 and 0 <= st.f_ffree <= st.f_files:
        inodes_pct = _pct(st.f_files - st.f_ffree, st.f_files)
    return {
        "mount": mount,
        "fs": fs_type,
        "size": size,
        "free": free,
        # df's "Use%": used / (used + available), so blocks reserved for root count as full.
        "used_pct": _pct(used, used + free),
        "inodes_pct": inodes_pct,
    }


def parse_diskstats(text: str) -> tuple[int, int] | None:
    """Total sectors (read, written) of all whole disks in /proc/diskstats."""
    read = written = 0
    seen = False
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or not _WHOLE_DISK.fullmatch(fields[2]):
            continue
        try:
            sectors_read, sectors_written = int(fields[5]), int(fields[9])
        except ValueError:
            continue
        if sectors_read < 0 or sectors_written < 0:
            continue
        read += sectors_read
        written += sectors_written
        seen = True
    return (read, written) if seen else None


def rate(prev: int | None, cur: int | None, seconds: float | None) -> int | None:
    """Units per second between two counter readings; None if they can't be compared."""
    if prev is None or cur is None or not seconds or seconds <= 0 or cur < prev:
        return None
    return round((cur - prev) / seconds)


# --- the sampler ----------------------------------------------------------------------


class MetricsSampler:
    """Reads the metrics once per report and remembers the counters for the next one.

    `proc_root`, `thermal_root`, `statvfs`, `clock` and `docker_socket` are only changed by
    tests (fixture files, a fake Docker API).
    """

    def __init__(
        self,
        proc_root: str = PROC_ROOT,
        thermal_root: str = THERMAL_ROOT,
        statvfs: Callable[[str], Any] = os.statvfs,
        clock: Callable[[], float] = time.monotonic,
        docker_socket: str = docker_api.DOCKER_SOCKET,
    ) -> None:
        self.proc_root = proc_root
        self.thermal_root = thermal_root
        self.statvfs = statvfs
        self.clock = clock
        self.docker = docker_api.ContainerStats(docker_socket, clock=clock)
        self._lock = threading.Lock()
        self._at: float | None = None
        self._cpu: CpuTimes | None = None
        self._disk: tuple[int, int] | None = None

    def _proc(self, name: str) -> str | None:
        return _read(os.path.join(self.proc_root, name))

    def _mounts(self) -> list[dict[str, Any]]:
        mounts = []
        for mount, fs_type in parse_mountinfo(self._proc("self/mountinfo") or ""):
            usage = mount_usage(mount, fs_type, self.statvfs)
            if usage is not None:
                mounts.append(usage)
        return mounts

    def _disk_io(self, disk: tuple[int, int] | None, seconds: float | None) -> dict | None:
        if disk is None or self._disk is None:
            return None
        read_bps = rate(self._disk[0] * SECTOR_BYTES, disk[0] * SECTOR_BYTES, seconds)
        write_bps = rate(self._disk[1] * SECTOR_BYTES, disk[1] * SECTOR_BYTES, seconds)
        if read_bps is None or write_bps is None:
            return None
        return {"read_bps": read_bps, "write_bps": write_bps}

    def collect(self, containers: bool = True) -> dict[str, Any]:
        """One reading. containers=False skips Docker (the "containers" check is off).

        Locked: after a reconnect, the previous session's report thread may still be running
        here. The timeout means a stuck thread costs one report's metrics (TimeoutError,
        reported as null with an error entry), never the whole report.
        """
        if not self._lock.acquire(timeout=LOCK_TIMEOUT):
            raise TimeoutError
        try:
            return self._collect(containers)
        finally:
            self._lock.release()

    def _collect(self, containers: bool) -> dict[str, Any]:
        now = self.clock()
        seconds = None if self._at is None else now - self._at

        stat = self._proc("stat")
        cpu = parse_proc_stat(stat) if stat is not None else None
        busy, iowait = cpu_percent(self._cpu, cpu)
        load_text = self._proc("loadavg")
        meminfo = self._proc("meminfo")
        memory = parse_meminfo(meminfo) if meminfo is not None else None
        diskstats = self._proc("diskstats")
        disk = parse_diskstats(diskstats) if diskstats is not None else None

        container_list, truncated = None, False
        if containers:
            container_list, truncated = self.docker.collect(memory["total"] if memory else None)

        result = {
            "interval_s": None if seconds is None else max(0, round(seconds)),
            "cpu": {
                "busy_pct": busy,
                "iowait_pct": iowait,
                "load": parse_loadavg(load_text) if load_text is not None else None,
                "cores": cpu.cores if cpu else None,
                "temp_c": pick_cpu_temperature(read_thermal_zones(self.thermal_root)),
            },
            "memory": memory,
            "mounts": self._mounts(),
            "disk_io": self._disk_io(disk, seconds),
            "containers": container_list,
            "containers_truncated": truncated,
        }
        # All three together: after a failed read the next rate is null, not a delta over
        # two intervals divided by one.
        self._at, self._cpu, self._disk = now, cpu, disk
        return result
