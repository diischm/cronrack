"""Recent raw samples of one host, for the "sustained" rules (rules.resources).

Loaded lazily: the database is only read when a rule actually asks (a report without
metrics never does), and at most once per evaluation.
"""

from datetime import datetime, timedelta
from functools import cached_property

from metrics.models import ContainerSample, HostSample, Resolution

# The longest window any rule looks at (10 minutes), with room for a late report.
LOOKBACK = timedelta(minutes=15)

Points = list[tuple[datetime, float]]


class HostHistory:
    def __init__(self, host_id: int, now: datetime) -> None:
        self.host_id = host_id
        self.now = now

    @cached_property
    def _host_rows(self) -> list[dict]:
        return list(
            HostSample.objects.filter(
                host_id=self.host_id,
                res=Resolution.RAW,
                at__gte=self.now - LOOKBACK,
                at__lte=self.now,
            )
            .order_by("at")
            .values("at", "cpu_pct", "mem_pct", "temp_c")
        )

    @cached_property
    def _container_rows(self) -> dict[str, list[dict]]:
        rows: dict[str, list[dict]] = {}
        for row in (
            ContainerSample.objects.filter(
                host_id=self.host_id,
                res=Resolution.RAW,
                at__gte=self.now - LOOKBACK,
                at__lte=self.now,
            )
            .order_by("at")
            .values("name", "at", "cpu_pct", "mem", "mem_limit")
        ):
            rows.setdefault(row["name"], []).append(row)
        return rows

    def host(self, field: str) -> Points:
        """(time, value) of a HostSample column, oldest first; missing values left out."""
        return [(r["at"], r[field]) for r in self._host_rows if r[field] is not None]

    def container_cpu(self, name: str) -> Points:
        rows = self._container_rows.get(name, [])
        return [(r["at"], r["cpu_pct"]) for r in rows if r["cpu_pct"] is not None]

    def container_mem_of_limit(self, name: str) -> Points:
        """Memory as a percent of the container's limit (only samples that had one)."""
        rows = self._container_rows.get(name, [])
        return [
            (r["at"], r["mem"] * 100.0 / r["mem_limit"])
            for r in rows
            if r["mem"] is not None and r["mem_limit"]
        ]
