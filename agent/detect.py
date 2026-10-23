"""Alert watcher (step 1). Polls span metrics and Docker every 15 s and raises an Alert when a
signal stays abnormal for 2 consecutive polls:
  error_ratio > max(3 x baseline, baseline + 0.05)   p95 > max(2 x baseline, baseline + 100 ms)
  req_rate > 3 x baseline (flood) or ~0 (went quiet) container not running, restarted,
  OOM-killed, or its CPU / memory limit lowered.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone

from agent import anomaly, config
from agent.baseline import Baseline
from agent.contracts import Alert
from agent.tools import docker_state, prometheus
from agent.tools.backends import Backends, BackendError

# When several signals fire on the same poll, the alert names the most severe one.
SEVERITY = ["status", "missing_telemetry", "oom_killed", "restarts", "error_ratio", "mem_limit",
            "cpu_limit", "p95_ms", "req_rate"]


def _ratio(a: Alert) -> float:
    return a.value / a.baseline if a.baseline else float("inf")


def poll(b: Backends, baseline: Baseline, containers: bool = True) -> list[Alert]:
    """Every abnormal (service, signal) right now, as candidate alerts. containers=False
    skips Docker entirely (span metrics only)."""
    ts = b.now_dt()
    out: list[Alert] = []
    table = prometheus.health_table(b)
    for svc in sorted(set(table) | set(baseline.services)):
        row, base = table.get(svc, {}), baseline.service(svc)
        for signal, direction in anomaly.service_anomalies(row, base).items():
            if direction == "absent":
                out.append(Alert(service=svc, signal="missing_telemetry", value=row.get("req_rate") or 0.0,
                                 baseline=base.get("req_rate") or 0.0, ts=ts))
            else:
                out.append(Alert(service=svc, signal=signal, value=row[signal], baseline=base.get(signal) or 0.0, ts=ts))
    try:
        table = docker_state.container_table(b, baseline.containers, with_usage=False) if containers else []
    except BackendError:
        table = []
    a = config.thresholds()["anomaly"]
    for c in table:
        base = baseline.containers.get(c.service, {})
        compose_mb = config.services()["containers"].get(c.service, {}).get("mem_limit_mb")
        was_running = not baseline.containers or base.get("status") == "running"
        if c.status != "running" and was_running:
            out.append(Alert(service=c.service, signal="status", value=0.0, baseline=1.0, ts=ts))
        if c.restarts_since_baseline > 0:
            out.append(Alert(service=c.service, signal="restarts", value=float(c.restarts),
                             baseline=float(c.restarts - c.restarts_since_baseline), ts=ts))
        if c.oom_killed:
            out.append(Alert(service=c.service, signal="oom_killed", value=1.0, baseline=0.0, ts=ts))
        if c.mem_limit and compose_mb and c.mem_limit < a["mem_limit_ratio"] * compose_mb:
            out.append(Alert(service=c.service, signal="mem_limit", value=c.mem_limit, baseline=float(compose_mb), ts=ts))
        base_cpu = base.get("cpu_limit")
        if c.cpu_limit is not None and (base_cpu is None or c.cpu_limit < a["cpu_limit_ratio"] * base_cpu):
            out.append(Alert(service=c.service, signal="cpu_limit", value=c.cpu_limit, baseline=base_cpu or 0.0, ts=ts))
    return out


def pick(alerts: list[Alert]) -> Alert:
    return min(alerts, key=lambda a: (SEVERITY.index(a.signal) if a.signal in SEVERITY else 99, -_ratio(a)))


def watch(b: Backends, baseline: Baseline, max_polls: int | None = None,
          sleep: Callable[[float], None] = time.sleep,
          on_poll: Callable[[datetime, list[Alert], dict], None] | None = None) -> Alert | None:
    """Block until some (service, signal) is abnormal on `consecutive_points` polls in a row,
    then return the most severe such alert. Returns None if max_polls runs out first."""
    cfg = config.thresholds()["alert"]
    need, interval = cfg["consecutive_points"], cfg["poll_interval_s"]
    # latency is noisy on a busy laptop; errors, stopped containers and restarts are not
    need_for = lambda signal: cfg.get("consecutive_points_latency", need) if signal == "p95_ms" else need
    streak: dict[tuple[str, str], int] = {}
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        try:
            candidates = poll(b, baseline)
        except BackendError as e:
            candidates = []
            if on_poll:
                on_poll(b.now_dt(), [], {"error": str(e)})
        current = {(a.service, a.signal): a for a in candidates}
        streak = {k: streak.get(k, 0) + 1 for k in current}
        if on_poll:
            on_poll(b.now_dt(), candidates, dict(streak))
        firing = [current[k] for k, n in streak.items() if n >= need_for(k[1])]
        if firing:
            return pick(firing)
        sleep(interval)
    return None


def fmt_alert(a: Alert) -> str:
    when = a.ts.astimezone(timezone.utc).strftime("%H:%M:%SZ")
    return f"{a.service} {a.signal} = {a.value:.3g} (baseline {a.baseline:.3g}) at {when}"
