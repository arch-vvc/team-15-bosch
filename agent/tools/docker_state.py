"""Container health (query-cookbook.md tool 5), read from the Docker API.

Restarts and OOM kills are not exported as metrics, so docker inspect is the only source.
Live limits are compared with the compose values (config/services.yaml) and the baseline:
a limit that went down means CPU or memory starvation was injected.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from agent import config
from agent.contracts import ContainerStatus, Evidence, Fact
from agent.tools.backends import Backends, BackendError

MB = 1024 * 1024


def _parse_ts(s: str | None) -> float | None:
    if not s or s.startswith("0001-"):
        return None
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        frac = "".join(ch for ch in frac if ch.isdigit())[:6]
        s = f"{head}.{frac}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def cpu_limit_cores(host_config: dict) -> float | None:
    """Cores allowed, or None when unlimited."""
    if host_config.get("NanoCpus"):
        return host_config["NanoCpus"] / 1e9
    quota, period = host_config.get("CpuQuota") or 0, host_config.get("CpuPeriod") or 0
    return quota / period if quota > 0 and period > 0 else None


def usage_from_stats(stats: dict | None) -> tuple[float | None, float | None]:
    """(cores used, memory used in MB) from one `docker stats` sample."""
    if not stats:
        return None, None
    cpu, pre = stats.get("cpu_stats") or {}, stats.get("precpu_stats") or {}
    cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (pre.get("cpu_usage") or {}).get("total_usage", 0)
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    online = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    cores = cpu_delta / sys_delta * online if sys_delta > 0 else None
    mem = stats.get("memory_stats") or {}
    inner = mem.get("stats") or {}
    cache = inner.get("inactive_file", inner.get("total_inactive_file", 0))
    used = (mem["usage"] - cache) / MB if "usage" in mem else None
    return cores, used


def _one(b: Backends, name: str, base: dict, with_usage: bool = True) -> ContainerStatus:
    attrs = b.docker.inspect(name=name)
    if attrs is None:
        return ContainerStatus(service=name, status="missing", restarts=0, restarts_since_baseline=0,
                               oom_killed=False, mem_used=None, mem_limit=None, cpu_used=None, cpu_limit=None)
    state, host = attrs.get("State") or {}, attrs.get("HostConfig") or {}
    status = state.get("Status", "unknown")
    if (state.get("Health") or {}).get("Status") == "unhealthy":
        status = "unhealthy"
    cores = used = None
    if state.get("Running") and with_usage:
        try:
            cores, used = usage_from_stats(b.docker.stats(name=name))
        except BackendError:
            pass
    restarts = int(attrs.get("RestartCount", 0))
    return ContainerStatus(
        service=name, status=status, restarts=restarts,
        restarts_since_baseline=max(0, restarts - int(base.get("restarts", restarts))),
        oom_killed=bool(state.get("OOMKilled")),
        mem_used=None if used is None else round(used, 1),
        mem_limit=round(host["Memory"] / MB, 1) if host.get("Memory") else None,
        cpu_used=None if cores is None else round(cores, 3),
        cpu_limit=cpu_limit_cores(host),
    )


def container_table(b: Backends, baseline: dict[str, dict] | None = None,
                    names: list[str] | None = None, with_usage: bool = True) -> list[ContainerStatus]:
    """One ContainerStatus per container. with_usage=False skips `docker stats` (about a
    second per container), which the 15-second alert poll does not need."""
    baseline = baseline or {}
    names = names or config.all_containers()
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda n: _one(b, n, baseline.get(n, {}), with_usage), names))


def started_at(b: Backends, name: str) -> float | None:
    attrs = b.docker.inspect(name=name)
    return _parse_ts(((attrs or {}).get("State") or {}).get("StartedAt"))


def container_facts(c: ContainerStatus, base: dict, detail: bool) -> list[Fact]:
    """Facts for one container. With detail=False only status plus anything abnormal,
    so a scan of 20 containers stays short."""
    a = config.thresholds()["anomaly"]
    compose_mb = config.services()["containers"].get(c.service, {}).get("mem_limit_mb")
    running = c.status in ("running", "unhealthy")
    facts = [Fact(service=c.service, signal="status", value=c.status,
                  direction="same" if c.status == "running" else "absent" if not running else "up")]
    if c.restarts_since_baseline > 0 or detail:
        facts.append(Fact(service=c.service, signal="restarts", value=c.restarts,
                          baseline=float(c.restarts - c.restarts_since_baseline),
                          direction="up" if c.restarts_since_baseline > 0 else "same"))
    if c.oom_killed or detail:
        facts.append(Fact(service=c.service, signal="oom_killed", value=c.oom_killed,
                          direction="up" if c.oom_killed else "same"))
    if c.mem_limit is not None and compose_mb:
        lowered = c.mem_limit < a["mem_limit_ratio"] * compose_mb
        if lowered or detail:
            facts.append(Fact(service=c.service, signal="mem_limit", value=c.mem_limit, baseline=float(compose_mb),
                              direction="down" if lowered else "same"))
    base_cpu = base.get("cpu_limit")
    cpu_lowered = c.cpu_limit is not None and (base_cpu is None or c.cpu_limit < a["cpu_limit_ratio"] * base_cpu)
    if cpu_lowered or (detail and c.cpu_limit is not None):
        facts.append(Fact(service=c.service, signal="cpu_limit", value=c.cpu_limit, baseline=base_cpu,
                          direction="down" if cpu_lowered else "same"))
    if c.mem_used is not None and c.mem_limit:
        near = c.mem_used >= a["mem_near_limit"] * c.mem_limit
        if near or detail:
            facts.append(Fact(service=c.service, signal="mem_used", value=c.mem_used, baseline=base.get("mem_used"),
                              direction="up" if near else None))
    if c.cpu_used is not None:
        pinned = c.cpu_limit is not None and c.cpu_used >= a["cpu_near_limit"] * c.cpu_limit
        if pinned or detail:
            facts.append(Fact(service=c.service, signal="cpu_used", value=c.cpu_used, baseline=base.get("cpu_used"),
                              direction="up" if pinned else None))
    return facts


def container_health(b: Backends, service: str | None = None, baseline: dict[str, dict] | None = None,
                     table: list[ContainerStatus] | None = None) -> list[Evidence]:
    """Tool 5: status, restarts, OOM kills, limits and usage for one container (all facts)
    or for every known container (status plus anything abnormal). `table` lets triage reuse
    a scan it already made."""
    baseline = baseline or {}
    table = table if table is not None else container_table(b, baseline, [service] if service else None)
    facts = []
    for c in table:
        facts += container_facts(c, baseline.get(c.service, {}), detail=service is not None)
        if service:
            start = started_at(b, c.service)
            if start is not None:
                facts.append(Fact(service=c.service, signal="uptime_s", value=round(b.now() - start)))
    return [Evidence(tool="container_health", query={"service": service}, ts=b.now_dt(), facts=facts)]
