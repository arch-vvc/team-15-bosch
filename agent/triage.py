"""Triage snapshot (step 2): one pass over every tool, so the Brain starts from a complete
picture instead of whatever the alert happened to name.

  1. health of every service vs baseline          (service_health_overview)
  2. every container's state, limits and usage     (container_health)
  3. the call graph and edges that disappeared     (service_dependencies)
  4. recent container changes                      (deploy_history)
  5. for the most abnormal services: when each signal turned bad (onset), error traces
     (where errors originate, messages, earliest error) and logs
"""
from __future__ import annotations

from datetime import datetime

from agent import config
from agent.contracts import Alert, ContainerStatus, Evidence, ServiceStatus, TriageSnapshot
from agent.tools import Toolbox, docker_state, jaeger
from agent.tools.backends import BackendError

DEEP_DIVE = 4            # services that get onset, trace and log checks
RECENT_EDGES_MIN = 5     # call graph window during an incident; short, so dead edges show up


def abnormal_services(evidence: list[Evidence], containers: list[ContainerStatus],
                      alert: Alert | None) -> list[str]:
    """Services ordered most-abnormal first: the alerting service, then telemetry anomalies
    ranked by how far they moved, then datastores and other containers with problems."""
    score: dict[str, float] = {}
    for ev in evidence:
        for f in ev.facts:
            if f.direction in ("up", "absent", "down") and f.signal != "status":
                moved = 10.0 if f.direction == "absent" else (
                    float(f.value) / f.baseline if isinstance(f.value, (int, float)) and f.baseline else 5.0)
                score[f.service] = max(score.get(f.service, 0.0), min(moved, 100.0))
    for c in containers:
        if c.status not in ("running", "missing") or c.restarts_since_baseline or c.oom_killed:
            score[c.service] = max(score.get(c.service, 0.0), 50.0)
    if alert:
        score[alert.service] = 1000.0
    return sorted(score, key=lambda s: -score[s])


def snapshot(tb: Toolbox, alert: Alert | None = None) -> TriageSnapshot:
    start = len(tb.ledger)
    b, base = tb.b, tb.baseline

    overview = tb.service_health_overview()
    try:
        table = docker_state.container_table(b, base.containers if base else {})
    except BackendError:
        table = []
    containers_ev = tb.record("container_health", {"service": None}, lambda: docker_state.container_health(
        b, None, base.containers if base else {}, table=table))
    try:
        edges = jaeger.dependencies(b, lookback_min=RECENT_EDGES_MIN)
    except BackendError:
        edges = []
    tb.record("service_dependencies", {}, lambda: jaeger.service_dependencies(
        b, RECENT_EDGES_MIN, base.edges if base else None, edges=edges))
    tb.deploy_history()

    suspects = abnormal_services(overview + containers_ev, table, alert)
    onsets: dict[str, dict[str, datetime]] = {}
    abnormal_signals = {(f.service, f.signal, f.direction) for ev in overview for f in ev.facts
                        if f.direction in ("up", "absent")}
    traced: set[str] = set()
    for svc in suspects[:DEEP_DIVE]:
        for s, signal, direction in sorted(abnormal_signals):
            if s != svc:
                continue
            sig = "req_rate" if signal == "missing_telemetry" else signal
            for ev in tb.onset(svc, sig, "absent" if direction == "absent" else "up"):
                if ev.onset_ts:
                    onsets.setdefault(svc, {})[signal] = ev.onset_ts
        # one search usually covers the whole call chain (checkout's error traces include
        # payment's spans), so skip services an earlier search already showed errors for
        if svc in config.shop_services() and svc not in traced:
            for ev in tb.search_traces(svc, lookback=config.thresholds()["traces"]["triage_lookback"]):
                traced.add(svc)
                traced.update(f.service for f in ev.facts if f.signal == "error_count" and f.value)
                for f in ev.facts:
                    if f.signal == "error_count" and ev.onset_ts and f.value:
                        current = onsets.setdefault(f.service, {}).get("first_error_span")
                        if current is None or ev.onset_ts < current:
                            onsets[f.service]["first_error_span"] = ev.onset_ts
                        break
        for ev in tb.search_logs(svc):
            if ev.onset_ts:
                onsets.setdefault(svc, {})["first_error_log"] = ev.onset_ts

    services = []
    current = {f.service: {} for ev in overview for f in ev.facts}
    for ev in overview:
        for f in ev.facts:
            if f.signal in ("req_rate", "error_ratio", "p95_ms") and isinstance(f.value, (int, float)):
                current[f.service][f.signal] = float(f.value)
    for svc in sorted(set(current) | set(base.services if base else {})):
        row = current.get(svc, {})
        base_row = {k: v for k, v in (base.service(svc) if base else {}).items() if v is not None}
        services.append(ServiceStatus(service=svc, req_rate=row.get("req_rate"), error_ratio=row.get("error_ratio"),
                                      p95_ms=row.get("p95_ms"), baseline=base_row, onset_ts=onsets.get(svc, {})))
    ids = [ev.id for ev in tb.ledger.all()[start:]]
    return TriageSnapshot(ts=b.now_dt(), services=services, containers=table, dependencies=edges, evidence_ids=ids)
