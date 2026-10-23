"""Shared pydantic models (team-briefs.md, Part 2).

C keeps this file. Changes from Part 2 made while building Eyes (announce to the team):
  * SIGNALS gains error_count, error_origin, error_message, log_count, deploy_event, uptime_s.
  * Evidence.id defaults to "" because the ledger assigns it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SIGNALS = [
    "error_ratio", "p95_ms", "req_rate", "hop_gap_ms", "cpu_used", "cpu_limit", "mem_used",
    "mem_limit", "restarts", "oom_killed", "status", "gc_time", "error_concentration",
    "missing_telemetry", "missing_edge",
    # added by A
    "error_count",      # error spans seen in sampled traces, scope = operation
    "error_origin",     # traces whose deepest error span is in this service, scope = operation
    "error_message",    # a redacted error text, scope = "trace:<op>" or "log:<level>"
    "log_count",        # log lines in the window, scope = level
    "deploy_event",     # a container lifecycle or config change, scope = docker action
    "uptime_s",         # seconds since the container started
]

Direction = Literal["up", "down", "same", "absent"]


# --- evidence (A produces, everyone reads) ---
class Fact(BaseModel):
    service: str
    signal: str
    scope: str | None = None
    value: float | str | bool
    baseline: float | None = None
    direction: Direction | None = None


class Evidence(BaseModel):
    id: str = ""
    tool: str
    query: dict
    ts: datetime
    onset_ts: datetime | None = None
    facts: list[Fact]
    examples: list[str] = []
    redacted: bool = True


# --- snapshot and alert (A) ---
class ServiceStatus(BaseModel):
    service: str
    req_rate: float | None
    error_ratio: float | None
    p95_ms: float | None
    baseline: dict[str, float]
    onset_ts: dict[str, datetime] = {}


class ContainerStatus(BaseModel):
    service: str
    status: str
    restarts: int
    restarts_since_baseline: int
    oom_killed: bool
    mem_used: float | None
    mem_limit: float | None
    cpu_used: float | None
    cpu_limit: float | None


class TriageSnapshot(BaseModel):
    ts: datetime
    services: list[ServiceStatus]
    containers: list[ContainerStatus]
    dependencies: list[tuple[str, str]]
    evidence_ids: list[str]


class Alert(BaseModel):
    service: str
    signal: str
    value: float
    baseline: float
    ts: datetime


# --- diagnosis (B and C produce) ---
class Hypothesis(BaseModel):
    id: str
    component: str
    cause: str
    confidence: float
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    missing_evidence: list[str]
    source: Literal["rules", "llm"]


class Diagnosis(BaseModel):
    state: Literal["CONFIDENT", "UNCERTAIN", "UNKNOWN"]
    hypotheses: list[Hypothesis]
    selected_id: str | None
    reason: str


class ValidationResult(BaseModel):
    ok: bool
    problems: list[str]


# --- action (C produces, E displays) ---
class ActionProposal(BaseModel):
    action: Literal["restart_service", "toggle_flag", "throttle_load", "scale_service"]
    target: str
    params: dict = {}
    tier: int
    reason: str
    hypothesis_id: str


class DryRunResult(BaseModel):
    proposal: ActionProposal
    would_do: str
    checks_passed: list[str]
    checks_failed: list[str]


class Approval(BaseModel):
    approved: bool
    operator: str
    typed_confirmation: str | None
    ts: datetime


class ActionResult(BaseModel):
    ok: bool
    response: dict
    ts: datetime


class VerifyResult(BaseModel):
    recovered: bool
    evidence_ids: list[str]
    remaining_symptoms: list[str]


# --- whole incident (C assembles, E reports, E's memory stores) ---
class Incident(BaseModel):
    alert: Alert
    snapshot: TriageSnapshot
    diagnoses: list[Diagnosis]
    actions: list[tuple[ActionProposal, Approval | None, ActionResult | None, VerifyResult | None]]
    outcome: Literal["recovered", "escalated", "rejected", "unknown"]
    timeline: list[tuple[datetime, str]] = Field(default_factory=list)
