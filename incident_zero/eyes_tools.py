"""Eyes tools (agent/) plugged into the pipeline as extra read-only tools.

Each tool runs the agent/ implementation and converts its typed facts into this package's
evidence records, so rules, the LLM investigator and the validator treat them like any
other evidence. One fact becomes one observation:

    {"signal": "error_ratio", "error_ratio": 0.5, "scope": None, "baseline": 0.0,
     "direction": "up", "error_ratio_up": True}

The signal-named field and the "<signal>_<direction>" flag let the declarative rules in
config/rules.json match a fact with a single condition.
"""
from __future__ import annotations

from pathlib import Path

from agent import baseline as baseline_mod
from agent.contracts import Evidence
from agent.ledger import Ledger
from agent.tools import Toolbox, prometheus, redact
from agent.tools.backends import Backends
from incident_zero.telemetry import SERVICES, TelemetryError
from incident_zero.tools import HEALTH_SERVICES, record

# tool name -> (evidence kind, needs Docker, services it accepts)
EYES_TOOLS = {
    "read_service_health": ("service_health", False, SERVICES),
    "read_operations": ("operations", False, SERVICES),
    "read_trace_errors": ("trace_errors", False, SERVICES),
    "read_hop_latency": ("hop_latency", False, SERVICES),
    "read_onset": ("onset", False, SERVICES),
    "read_container_limits": ("container_limits", True, HEALTH_SERVICES),
    "read_deploy_history": ("deploy_history", True, HEALTH_SERVICES),
}

# collected for every service the pipeline visits; the rest are for the LLM to ask for
PIPELINE_TOOLS = ("read_service_health", "read_trace_errors", "read_container_limits", "read_deploy_history")

DESCRIPTIONS = {
    "read_service_health": "Error ratio, p95 latency and request rate of a service compared with its baseline.",
    "read_operations": "Error ratio, p95 and rate per operation of a service, to see which operation fails.",
    "read_trace_errors": "Error spans from recent traces through a service: errors per operation, where errors "
                         "originate (deepest failing span), top messages, one attribute value shared by most errors.",
    "read_hop_latency": "For calls into this service from each caller: client vs server p95. A large gap with a "
                        "normal server time points at the network.",
    "read_onset": "When each abnormal signal of this service first crossed its threshold.",
    "read_container_limits": "Container CPU and memory limits vs compose and baseline, usage, restarts, OOM kills, uptime.",
    "read_deploy_history": "Recent container changes: created, restarted, stopped, killed, OOM, limits updated, unhealthy.",
}


def observation(fact) -> dict:
    obs = {"signal": fact.signal, fact.signal: fact.value, "scope": fact.scope,
           "baseline": fact.baseline, "direction": fact.direction}
    if fact.direction in ("up", "down", "absent"):
        obs[f"{fact.signal}_{fact.direction}"] = True
    return obs


def to_records(items: list[Evidence], kind: str, service: str, start: float, end: float) -> list[dict]:
    """One record per (evidence item, service) so facts about another service (payment's
    spans seen from checkout's traces) are filed under that service."""
    rows = []
    for ev in items:
        if ev.query.get("error"):
            row = record(service, kind, [], f"eyes://{ev.tool}", start, end, query=ev.query, status="unavailable")
            row["error"] = ev.query["error"]
            rows.append(row)
            continue
        by_service: dict[str, list[dict]] = {}
        for fact in ev.facts:
            by_service.setdefault(fact.service, []).append(observation(fact))
        for svc, observations in (by_service or {service: []}).items():
            row = record(svc, kind, observations, f"eyes://{ev.tool}", start, end, query=ev.query)
            if ev.onset_ts:
                row["onset_ts"] = ev.onset_ts.isoformat()
            if ev.examples:
                row["examples"] = ev.examples
            rows.append(row)
    return rows


class EyesTools:
    def __init__(self, backends: Backends | None = None, baseline_path: Path = baseline_mod.DEFAULT_PATH,
                 docker_enabled: bool = False, urls: dict | None = None):
        self._backends, self.urls = backends, urls or {}
        self.docker_enabled = docker_enabled
        self.baseline = baseline_mod.Baseline.load(baseline_path) if Path(baseline_path).exists() else None
        self.guard = lambda: False

    @property
    def backends(self) -> Backends:
        if self._backends is None:
            self._backends = Backends.live(**self.urls)
        return self._backends

    def toolbox(self) -> Toolbox:
        return Toolbox(self.backends, Ledger(), self.baseline)

    def call(self, name: str, service: str, lookback: int, end: float) -> list[dict]:
        if self.guard():
            raise TelemetryError("Kill switch active before telemetry request")
        kind, needs_docker, allowed = EYES_TOOLS[name]
        if service not in allowed or redact.is_flag_system(service):
            raise ValueError("Invalid service for this tool")
        if needs_docker and not self.docker_enabled:
            raise TelemetryError("Docker tools are disabled; enable explicitly with --container-health")
        tb = self.toolbox()
        minutes = max(1, lookback // 60)
        if name == "read_service_health":
            items = tb.record("service_health", {"service": service}, lambda: _one_service_health(tb, service))
        elif name == "read_operations":
            items = tb.service_detail(service)
        elif name == "read_trace_errors":
            items = tb.search_traces(service, lookback=f"{minutes}m")
        elif name == "read_hop_latency":
            callers = sorted({a for a, c in (self.baseline.edges if self.baseline else []) if c == service})[:3]
            items = [ev for caller in callers for ev in tb.hop_latency(caller, service)]
        elif name == "read_onset":
            items = _onsets(tb, service)
        elif name == "read_container_limits":
            items = tb.container_health(service)
        else:
            items = tb.deploy_history(service, lookback_min=max(minutes, 30))
        return to_records(items, kind, service, end - lookback, end)


def _one_service_health(tb: Toolbox, service: str) -> list[Evidence]:
    ev = prometheus.service_health_overview(tb.b, tb.baseline.services if tb.baseline else {})
    return [Evidence(tool=e.tool, query=e.query | {"service": service}, ts=e.ts,
                     facts=[f for f in e.facts if f.service == service]) for e in ev]


def _onsets(tb: Toolbox, service: str) -> list[Evidence]:
    """Onset for every signal of this service that is abnormal right now."""
    current = _one_service_health(tb, service)
    out = []
    for fact in (f for ev in current for f in ev.facts if f.direction in ("up", "absent")):
        signal = "req_rate" if fact.signal == "missing_telemetry" else fact.signal
        out += tb.onset(service, signal, "absent" if fact.direction == "absent" else "up")
    return out


def origin_services(rows: list[dict]) -> list[str]:
    """Services whose own spans are the deepest failing ones in sampled traces."""
    return sorted({row["service"] for row in rows if row.get("kind") == "trace_errors" and row["status"] == "observed"
                   and any(o.get("signal") == "error_origin" for o in row["observations"])
                   and row["service"] in HEALTH_SERVICES})
