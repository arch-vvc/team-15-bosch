"""The tool registry handed to the LLM investigator: names, JSON schemas, and a call()
that validates arguments and stores results in the ledger.

READ-ONLY ONLY. Nothing here can change the system, and assert_read_only() lets the
orchestrator prove that at startup (team rule 2: the LLM never gets action tools).
"""
from __future__ import annotations

import re
from typing import Any

from agent import config
from agent.tools import Toolbox

ACTION_WORDS = re.compile(r"restart|toggle|throttle|scale|stop|kill|start|update|delete|exec|write|set_",
                          re.IGNORECASE)


def _specs() -> dict[str, dict]:
    shop = config.shop_services()
    containers = config.all_containers()
    svc = {"type": "string", "enum": shop}
    return {
        "service_health_overview": {
            "description": "Request rate, error ratio and p95 latency for every service, compared with baseline. "
                           "Start here.",
            "properties": {}, "required": []},
        "service_detail": {
            "description": "The same three numbers per operation of one service, to see which operation fails.",
            "properties": {"service": svc}, "required": ["service"]},
        "hop_latency": {
            "description": "Client-side vs server-side p95 for calls from caller to callee, from traces. "
                           "A large gap with a normal server time points at the network.",
            "properties": {"caller": svc, "callee": svc}, "required": ["caller", "callee"]},
        "search_traces": {
            "description": "Error spans from recent traces that touch a service: error counts per operation, "
                           "where errors originate, top error messages, attribute concentration, earliest error.",
            "properties": {"service": svc, "errors_only": {"type": "boolean"},
                           "operation": {"type": "string"},
                           "lookback": {"type": "string", "pattern": r"^\d{1,2}m$"}},
            "required": ["service"]},
        "container_health": {
            "description": "Docker state: status, restarts, OOM kills, CPU and memory use and limits. "
                           "Omit service to scan every container.",
            "properties": {"service": {"type": "string", "enum": containers}}, "required": []},
        "search_logs": {
            "description": "Log counts by level and the top distinct WARN/ERROR messages for a service.",
            "properties": {"service": {"type": "string", "enum": containers},
                           "pattern": {"type": "string"},
                           "level": {"type": "string", "enum": ["ERROR", "WARN"]},
                           "since_min": {"type": "integer", "minimum": 1, "maximum": 60}},
            "required": ["service"]},
        "onset": {
            "description": "When a service's signal first became abnormal (from metric history). "
                           "Use direction=absent for a service that went quiet.",
            "properties": {"service": svc,
                           "signal": {"type": "string", "enum": ["error_ratio", "p95_ms", "req_rate"]},
                           "direction": {"type": "string", "enum": ["up", "absent"]}},
            "required": ["service", "signal"]},
        "service_dependencies": {
            "description": "The call graph from traces, with edges that existed at baseline but are now missing.",
            "properties": {}, "required": []},
        "deploy_history": {
            "description": "Recent container changes: created, restarted, stopped, killed, OOM, limits updated, "
                           "unhealthy. Omit service for all.",
            "properties": {"service": {"type": "string", "enum": containers},
                           "lookback_min": {"type": "integer", "minimum": 1, "maximum": 120}},
            "required": []},
    }


READ_ONLY_TOOLS = frozenset(_specs())


def assert_read_only() -> None:
    """Raise if the registry ever exposes anything that is not a known read-only tool."""
    for name in _specs():
        if name not in READ_ONLY_TOOLS or ACTION_WORDS.search(name):
            raise AssertionError(f"registry exposes a non read-only tool: {name}")


def schemas() -> list[dict]:
    """Tool definitions in the shape LLM APIs expect (name, description, input_schema)."""
    return [{"name": name, "description": spec["description"],
             "input_schema": {"type": "object", "properties": spec["properties"],
                              "required": spec["required"], "additionalProperties": False}}
            for name, spec in _specs().items()]


def _validate(spec: dict, args: dict) -> str | None:
    props = spec["properties"]
    for key in spec["required"]:
        if key not in args:
            return f"missing argument {key}"
    for key, value in args.items():
        if key not in props:
            return f"unknown argument {key}"
        p = props[key]
        kind = {"string": str, "boolean": bool, "integer": int}[p["type"]]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            return f"{key} must be {p['type']}"
        if "enum" in p and value not in p["enum"]:
            return f"{key} must be one of {p['enum']}"
        if "pattern" in p and not re.fullmatch(p["pattern"], value):
            return f"{key} must match {p['pattern']}"
        if "minimum" in p and not p["minimum"] <= value <= p["maximum"]:
            return f"{key} must be between {p['minimum']} and {p['maximum']}"
    if "pattern" in args:
        try:
            re.compile(args["pattern"])
        except re.error as e:
            return f"pattern is not a valid regex: {e}"
    return None


def call(toolbox: Toolbox, name: str, args: dict | None = None) -> dict[str, Any]:
    """Run one tool for the LLM. Returns {"evidence": [...]} with ledger IDs, or {"error": ...}.
    Never raises on bad input, so a confused model gets a message it can recover from."""
    args = dict(args or {})
    spec = _specs().get(name)
    if spec is None:
        return {"error": f"unknown tool {name}; available: {sorted(READ_ONLY_TOOLS)}"}
    if problem := _validate(spec, args):
        return {"error": problem}
    items = getattr(toolbox, name)(**args)
    return {"evidence": [ev.model_dump(mode="json", exclude={"redacted"}) for ev in items]}
