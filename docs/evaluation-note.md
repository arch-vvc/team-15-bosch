# Evaluation note

Run `python3.11 -m incident_zero evaluate` from the agent root. The summary is `output/evaluation/evaluation.md` with machine-readable results alongside it.

Twelve synthetic cases exercise recovery, failed verification, missing evidence, rejected approval, kill switch, unknown symptoms, two causes, OOM, restart loop, tier 2 approval, separately approved rollback and excess causes. Expected escalations count as workflow correctness; they are not successful fault diagnosis or recovery.

Unit/integration tests also exercise citation fabrication, exact approvals and replay, transport boundaries, conflicting rules, fallback discovery, service generalization, memory isolation, append-only storage and second-cause freshness. Local HTTP fixtures are not real model/API tests.

Real LLM accuracy, organizer practice performance, hidden faults, official scoring and live Action API compatibility have not been measured. Record those separately when the required environment is available; never combine synthetic workflow success with model accuracy.


## Latest local validation (2026-10-23)

- 65 unit/integration tests passed.
- 12/12 synthetic architecture scenarios reached their expected states.
- A real Responses API smoke request with synthetic unknown-incident data returned HTTP 429 before any model completion. The agent preserved evidence and escalated with no action. Model reasoning quality remains untested; inspect account quota/rate limits before retrying.
- Upstream working tree is clean. Docker image download remains active; no demo containers were running at the readiness check.

Follow-up: Docker startup succeeded. Fixed legacy Jaeger API 404 with v3 OTLP stream support; live checkout metrics, 37 trace spans, 20 log entries and container health verified. Full suite now passes 68 tests. Model access remains blocked by the observed HTTP 429.
