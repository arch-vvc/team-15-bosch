"""Symptom summary for the Eyes checkpoint: what the agent sees, in plain words, with every
line citing the ledger evidence it came from. It describes symptoms only; naming the root
cause is the Brain's job.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.contracts import Alert, Evidence, Fact, TriageSnapshot
from agent.ledger import Ledger

NAMES = {"error_ratio": "error ratio", "p95_ms": "p95 latency", "req_rate": "request rate",
         "missing_telemetry": "no telemetry", "mem_limit": "memory limit", "cpu_limit": "CPU limit",
         "mem_used": "memory used", "cpu_used": "CPU used", "restarts": "restarts", "oom_killed": "OOM killed",
         "status": "status", "hop_gap_ms": "network gap"}


def _t(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M:%SZ") if dt else "?"


def _n(x) -> str:
    """3.0 -> "3", 2.5 -> "2.5"."""
    return str(int(x)) if isinstance(x, float) and x.is_integer() else str(x)


def _v(signal: str, value) -> str:
    if signal in ("restarts", "uptime_s", "error_count", "log_count") and isinstance(value, (int, float)):
        return _n(value)
    if signal == "error_ratio" and isinstance(value, (int, float)):
        return f"{value:.0%}"
    if signal in ("p95_ms", "hop_gap_ms") and isinstance(value, (int, float)):
        return f"{value:,.0f} ms"
    if signal == "req_rate" and isinstance(value, (int, float)):
        return f"{value:.2f} req/s"
    if signal in ("mem_limit", "mem_used") and isinstance(value, (int, float)):
        return f"{value:,.0f} MB"
    if signal in ("cpu_limit", "cpu_used") and isinstance(value, (int, float)):
        return f"{value:.2f} cores"
    return str(value)


def _fact_line(f: Fact) -> str:
    if f.signal in ("missing_telemetry", "missing_edge"):
        base = f" (baseline {_v('req_rate', f.baseline)})" if f.baseline is not None else ""
        return f"{NAMES.get(f.signal, f.signal)}{base}"
    base = f" (baseline {_v(f.signal, f.baseline)})" if f.baseline is not None else ""
    scope = f" [{f.scope}]" if f.scope and f.scope not in ("onset",) else ""
    return f"{NAMES.get(f.signal, f.signal)}{scope} {_v(f.signal, f.value)}{base}"


def build(alert: Alert | None, snap: TriageSnapshot, ledger: Ledger) -> str:
    evs = [ledger.get(i) for i in snap.evidence_ids]
    by_tool: dict[str, list[Evidence]] = {}
    for ev in evs:
        by_tool.setdefault(ev.tool, []).append(ev)
    lines = ["# Incident symptoms", ""]
    if alert and alert.signal == "status":
        lines += [f"**Alert:** the {alert.service} container is not running, at {_t(alert.ts)}", ""]
    elif alert:
        lines += [f"**Alert:** {alert.service} {NAMES.get(alert.signal, alert.signal)} = "
                  f"{_v(alert.signal, alert.value)} (baseline {_v(alert.signal, alert.baseline)}) at {_t(alert.ts)}", ""]
    lines.append(f"Snapshot at {_t(snap.ts)}, {len(evs)} evidence items ({snap.evidence_ids[0]}-{snap.evidence_ids[-1]})."
                 if evs else "No evidence collected.")

    # 1. service telemetry
    lines += ["", "## Services out of their normal range"]
    abnormal = [(ev.id, f) for ev in by_tool.get("service_health_overview", []) for f in ev.facts
                if f.direction in ("up", "absent")]
    if abnormal:
        for svc in dict.fromkeys(f.service for _, f in abnormal):
            parts = [f"{_fact_line(f)} [{eid}]" for eid, f in abnormal if f.service == svc]
            lines.append(f"- **{svc}**: " + "; ".join(parts))
    else:
        ok = [ev.id for ev in by_tool.get("service_health_overview", [])]
        lines.append(f"- none: every service is within its baseline range [{', '.join(ok)}]")

    # 2. containers
    lines += ["", "## Containers"]
    bad = [(ev.id, f) for ev in by_tool.get("container_health", []) for f in ev.facts
           if f.direction in ("up", "absent", "down")]
    total = sum(1 for ev in by_tool.get("container_health", []) for f in ev.facts if f.signal == "status")
    if bad:
        for eid, f in bad:
            lines.append(f"- **{f.service}**: {_fact_line(f)} [{eid}]")
    else:
        ids = ", ".join(ev.id for ev in by_tool.get("container_health", []))
        lines.append(f"- all {total} containers running, no restarts, no OOM kills, limits unchanged [{ids}]")

    # 3. traces
    trace_evs = [ev for ev in by_tool.get("search_traces", []) if any(f.signal == "error_count" and f.value for f in ev.facts)]
    if trace_evs:
        lines += ["", "## Errors in traces"]
        seen = set()
        for ev in sorted(trace_evs, key=lambda e: e.onset_ts or snap.ts):
            svc = ev.facts[0].service
            if svc in seen:
                continue
            seen.add(svc)
            counts = ", ".join(f"{f.scope} x{_n(f.value)}" for f in ev.facts if f.signal == "error_count")
            origin = ", ".join(f"{f.scope} ({_n(f.value)} traces)" for f in ev.facts if f.signal == "error_origin")
            lines.append(f"- **{svc}** first error span {_t(ev.onset_ts)}: {counts} [{ev.id}]")
            if origin:
                lines.append(f"  - errors originate here: {origin}")
            for f in ev.facts:
                if f.signal == "error_message":
                    lines.append(f"  - message ({f.scope.removeprefix('trace:')}): \"{f.value}\"")
                if f.signal == "error_concentration":
                    lines.append(f"  - every error shares {f.scope} = {f.value}")
            if ev.examples:
                lines.append(f"  - example: {', '.join(ev.examples)}")

    # 4. logs
    log_lines = []
    for ev in by_tool.get("search_logs", []):
        counts = {f.scope: f.value for f in ev.facts if f.signal == "log_count"}
        msgs = [f for f in ev.facts if f.signal == "error_message"]
        if counts.get("ERROR") or counts.get("WARN"):
            svc = ev.query.get("service")
            log_lines.append(f"- **{svc}** ({ev.query.get('source')}): {_n(counts.get('ERROR', 0))} ERROR, "
                             f"{_n(counts.get('WARN', 0))} WARN in {ev.query.get('since_min')} min [{ev.id}]")
            log_lines += [f"  - {m.scope.removeprefix('log:')}: \"{m.value}\"" for m in msgs[:3]]
    if log_lines:
        lines += ["", "## Logs"] + log_lines

    # 5. call graph
    missing = [(ev.id, f) for ev in by_tool.get("service_dependencies", []) for f in ev.facts if f.signal == "missing_edge"]
    if missing:
        lines += ["", "## Calls that stopped"]
        lines += [f"- {f.scope}: present at baseline, no calls now [{eid}]" for eid, f in missing]

    # 6. deploy history
    lines += ["", "## Recent changes (deploy history)"]
    changes = [(ev.id, f) for ev in by_tool.get("deploy_history", []) for f in ev.facts]
    if changes:
        lines += [f"- **{f.service}**: {f.value} [{eid}]" for eid, f in changes]
    else:
        ids = ", ".join(ev.id for ev in by_tool.get("deploy_history", []))
        window = (by_tool.get("deploy_history") or [None])[0]
        mins = window.query.get("lookback_min") if window else "?"
        lines.append(f"- no container was created, restarted, stopped, killed or reconfigured in the last {mins} min [{ids}]")

    # 7. timeline
    events, seen_events = [], set()
    for ev in sorted(evs, key=lambda e: e.onset_ts or snap.ts):
        if ev.onset_ts and ev.tool in ("onset", "search_traces", "search_logs", "deploy_history"):
            svc = ev.facts[0].service if ev.facts else ev.query.get("service")
            went_quiet = any(f.signal == "missing_telemetry" for f in ev.facts)
            what = {"onset": "stopped sending telemetry" if went_quiet else
                    f"{NAMES.get(ev.query.get('signal'), ev.query.get('signal'))} turned abnormal",
                    "search_traces": "first error span", "search_logs": "first ERROR log line",
                    "deploy_history": "first container change"}[ev.tool]
            if (svc, what) not in seen_events:
                seen_events.add((svc, what))
                events.append((ev.onset_ts, f"{svc}: {what} [{ev.id}]", ev.tool != "onset"))
    if events:
        lines += ["", "## Timeline (earliest first)"]
        lines += [f"- {_t(ts)} {text}" for ts, text, _ in sorted(events)]
        # metric onsets lag by up to the rate window, so prefer exact timestamps when there are any
        precise = sorted(e for e in events if e[2]) or sorted(events)
        lines += ["", f"First sign of trouble: {_t(precise[0][0])} [{precise[0][1].rsplit('[', 1)[1]}. "
                      "Metric onsets lag by up to the rate window; ties under ~30 s are not meaningful."]
    # within one failing request every span on the chain errors and the caller's span starts
    # first, so the start order says nothing about cause; the deepest failing span does
    origins = sorted(((f.value, f.service, f.scope, ev.id) for ev in trace_evs for f in ev.facts
                      if f.signal == "error_origin"), reverse=True)
    if origins:
        n, svc, op, eid = origins[0]
        lines.append(f"Deepest failing span: **{svc}** {op}, in {_n(n)} error traces [{eid}].")
    return "\n".join(lines) + "\n"
