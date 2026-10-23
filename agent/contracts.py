from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)

# --- evidence (A produces, everyone reads) ---
class Fact(Contract):
    service: str                 # e.g. "payment"
    signal: str                  # one of SIGNALS below
    @field_validator('signal')
    @classmethod
    def valid_signal(cls, value):
        if value not in SIGNALS:
            raise ValueError('Unknown signal')
        return value

    scope: str | None = None     # operation, hop "checkout->payment", attribute, etc.
    value: float | str | bool
    baseline: float | None = None
    direction: Literal["up", "down", "same", "absent"] | None = None

SIGNALS = ["error_ratio","p95_ms","req_rate","hop_gap_ms","cpu_used","cpu_limit","mem_used",
           "mem_limit","restarts","oom_killed","status","gc_time","error_concentration",
           "missing_telemetry","missing_edge"]

class Evidence(Contract):
    id: str                      # "E1", "E2"… assigned by the ledger
    tool: str
    query: dict
    ts: datetime                 # when we queried
    onset_ts: datetime | None    # when the anomaly started (from traces or query_range)
    facts: list[Fact]
    examples: list[str] = []     # trace IDs, log line snippets (already redacted)
    redacted: bool = True

# --- snapshot and alert (A) ---
class ServiceStatus(Contract):
    service: str; req_rate: float | None; error_ratio: float | None; p95_ms: float | None
    baseline: dict[str, float]; onset_ts: dict[str, datetime] = {}
class ContainerStatus(Contract):
    service: str; status: str; restarts: int; restarts_since_baseline: int; oom_killed: bool
    mem_used: float | None; mem_limit: float | None; cpu_used: float | None; cpu_limit: float | None
class TriageSnapshot(Contract):
    ts: datetime; services: list[ServiceStatus]; containers: list[ContainerStatus]
    dependencies: list[tuple[str, str]]; evidence_ids: list[str]
class Alert(Contract):
    service: str; signal: str; value: float; baseline: float; ts: datetime

# --- diagnosis (B and C produce) ---
class Hypothesis(Contract):
    id: str                      # "H1"
    component: str               # must be a real service name (validated)
    cause: str                   # short category, e.g. "service_errors", "container_down",
                                 # "network_latency", "cpu_starvation", "memory_leak", "traffic_flood"…
    confidence: float = Field(ge=0, le=1)
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    missing_evidence: list[str]  # descriptions of checks not yet run
    source: Literal["rules", "llm"]
class Diagnosis(Contract):
    state: Literal["CONFIDENT", "UNCERTAIN", "UNKNOWN"]
    hypotheses: list[Hypothesis]
    selected_id: str | None      # a hypothesis id, or None for UNKNOWN
    reason: str
class ValidationResult(Contract):
    ok: bool; problems: list[str]

# --- action (C produces, E displays) ---
class ActionProposal(Contract):
    action: Literal["restart_service","toggle_flag","throttle_load","scale_service"]
    target: str; params: dict = {}; tier: int = Field(ge=0, le=3); reason: str; hypothesis_id: str
class DryRunResult(Contract):
    proposal: ActionProposal; would_do: str; checks_passed: list[str]; checks_failed: list[str]
class Approval(Contract):
    approved: bool; operator: str; typed_confirmation: str | None; ts: datetime
class ActionResult(Contract):
    ok: bool; response: dict; ts: datetime
class VerifyResult(Contract):
    recovered: bool; evidence_ids: list[str]; remaining_symptoms: list[str]

# --- whole incident (C assembles, E reports, E's memory stores) ---
class Incident(Contract):
    alert: Alert; snapshot: TriageSnapshot; diagnoses: list[Diagnosis]
    actions: list[tuple[ActionProposal, Approval | None, ActionResult | None, VerifyResult | None]]
    outcome: Literal["recovered","escalated","rejected","unknown"]; timeline: list[tuple[datetime, str]]
