"""Deploy history: what changed recently. The brief lists it beside logs, metrics and traces.

Locally there is no CI/CD, so the change log is Docker's own event stream: containers
created, (re)started, stopped, killed, OOM-killed, updated (new CPU or memory limits),
paused, or turning unhealthy. A change just before the symptoms is strong evidence.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from agent import config
from agent.contracts import Evidence, Fact
from agent.tools import redact
from agent.tools.backends import Backends

KEEP = {"create", "start", "restart", "stop", "kill", "die", "oom", "update", "pause", "unpause",
        "destroy", "rename", "health_status"}


def _event_time(ev: dict) -> float:
    if ev.get("timeNano"):
        return ev["timeNano"] / 1e9
    return float(ev.get("time", 0))


def _describe(action: str, attrs: dict) -> str:
    if action == "die" and attrs.get("exitCode") is not None:
        return f"die exitCode={attrs['exitCode']}"
    if action == "kill" and attrs.get("signal"):
        return f"kill signal={attrs['signal']}"
    return action


def changes(b: Backends, lookback_min: int, service: str | None = None) -> list[tuple[float, str, str, str]]:
    """(ts, service, action, description) for every kept container event, oldest first."""
    known = set(config.all_containers())
    now = b.now()
    out = []
    for ev in b.docker.events(since=now - lookback_min * 60, until=now):
        attrs = (ev.get("Actor") or {}).get("Attributes") or {}
        name = attrs.get("com.docker.compose.service") or attrs.get("name")
        raw = ev.get("Action") or ev.get("status") or ""
        action = raw.split(":")[0].strip()
        if action not in KEEP or redact.is_flag_system(name) or name not in known:
            continue
        if service and name != service:
            continue
        desc = raw.strip() if action == "health_status" else _describe(action, attrs)
        out.append((_event_time(ev), name, action, redact.mask(desc)))
    return sorted(out)


def deploy_history(b: Backends, lookback_min: int | None = None, service: str | None = None) -> list[Evidence]:
    """One Evidence per container that changed in the window (onset_ts = its first change),
    or a single empty Evidence saying nothing changed."""
    lookback_min = lookback_min or config.thresholds()["deploy_history"]["lookback_min"]
    query = {"lookback_min": lookback_min, "service": service}
    per_service: dict[str, list] = defaultdict(list)
    for ts, name, action, desc in changes(b, lookback_min, service):
        per_service[name].append((ts, action, desc))
    out = []
    for name, events in sorted(per_service.items(), key=lambda kv: kv[1][0][0]):
        facts = [Fact(service=name, signal="deploy_event", scope=action,
                      value=f"{desc} at {datetime.fromtimestamp(ts, timezone.utc):%H:%M:%SZ}")
                 for ts, action, desc in events]
        out.append(Evidence(tool="deploy_history", query=query, ts=b.now_dt(),
                            onset_ts=datetime.fromtimestamp(events[0][0], timezone.utc), facts=facts))
    if not out:
        out.append(Evidence(tool="deploy_history", query=query, ts=b.now_dt(), facts=[]))
    return out
