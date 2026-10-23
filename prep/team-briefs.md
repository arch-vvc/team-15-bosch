# Incident Zero: team briefs

**How to use this file**
1. Everyone pastes **Part 1 (shared context)** into their AI (Claude Code or ChatGPT) first.
2. Then paste **your own brief** from Part 3.
3. Tell your AI: *"Follow these briefs. Do not change interfaces in Part 2 without telling the team. Explain every piece of code you write so I can explain it in my one-to-one."*

**Split:** A, B and C have Claude Code and carry most of the code. D and E use ChatGPT and own smaller, self-contained modules plus evaluation, docs and the demo. **Everyone commits code and must be able to explain it**, as the rules require. Declare Claude Code and ChatGPT on the team form.

Fill in names: A = ______  B = ______  C = ______  D = ______  E = ______

---

## PART 1: Shared context (paste into every AI)

### The hackathon
- **Event:** Bosch × PES University "Incident Zero", two days.
- **Task:** build an **AI On-Call Engineer** for the **OpenTelemetry Demo** (the "Astronomy Shop", about 15 microservices) running locally in Docker (minimal mode: `make start-minimal`). The agent:
  1. detects an incident,
  2. investigates with read-only telemetry tools,
  3. names the root cause with evidence and confidence,
  4. proposes a fix from an allowlist,
  5. gets **explicit human approval**,
  6. applies the fix **only through the organisers' Action API**,
  7. verifies recovery,
  8. writes a JSON plus readable incident report.
- **Scoring (100 points):**
  - Eyes (Day 1, 15:30): 10
  - Brain (Day 1, 18:30): 20
  - Hands (Day 2, 12:00): 20
  - Hidden test on unseen incidents, including infrastructure faults and combinations, scored automatically: 30
  - Judges (safety, design, explainability, honesty, demo): 20
  - Plus an individual one-to-one for each member.
- **Code freeze:** Day 2, 16:30. Feature freeze from 12:00 on Day 2.

### Hard rules (breaking these risks disqualification)
1. **Never read feature-flag state.** No flagd, no flag UI, no flag API. **Also strip flag data out of telemetry.** Some services put `feature_flag` span events and attributes, and text like "Feature Flag Enabled", into traces and logs. Every tool output must be redacted before it reaches the evidence ledger or the LLM.
2. **The LLM never has action tools** and never chooses or executes actions.
3. **All state changes go through the Action API, only after explicit human approval.** The only exception is read-only dry runs.
4. **No hard-coded answers.** Rules encode observable telemetry patterns (for example `error_ratio > 0.2`), never incident IDs or names.
5. All code is written on the day. Every member commits. Every member can explain their code.
6. Guardrails:
   - kill switch checked before every action,
   - dry run before approval,
   - append-only action log,
   - one service per action,
   - at most 1 action before verification,
   - **at most 2 corrective actions per incident**,
   - one rollback, then escalate.

### Architecture (13 steps)
**Detect and diagnose**
1. **Alert watcher:** Prometheus thresholds on error ratio, p95 latency and restarts versus baseline.
2. **Triage snapshot:** per-service request rate, error ratio, p95, resources, container state, and the **onset time** of each anomaly.
3. **Incident memory:** a deterministic fingerprint similarity check against past *verified* incidents. It **only reorders suspects** and never adds confidence.
4. **Find suspects:** the dependency graph generates root-cause candidates. "Deepest failing service" is only a heuristic.
5. **Rank hypotheses:** competing hypotheses, scored on anomaly, temporal precedence, trace and dependency support, downstream propagation, and a penalty for showing symptoms only. The engine runs **discriminating checks** before committing. Each hypothesis has supporting, contradicting and missing evidence.
6. **Diagnosis state:**
   - CONFIDENT (≥ 0.7 with a clear margin) goes to step 8,
   - UNCERTAIN goes to step 7,
   - UNKNOWN escalates.
7. **LLM investigator:** at most 8 read-only tool calls and structured JSON output. **No action tools.**
8. **Validator:**
   - evidence IDs exist,
   - values match the ledger,
   - typed facts support each claim,
   - contradicting evidence is represented correctly.

   If it fails, escalate.

**Decide and act**

9. **Remedy and tier:** a deterministic table produces an ActionProposal. It checks the allowlist, the target, the tier and the confidence threshold.
10. **Approval gate:** kill switch off, dry-run preview, explicit human yes. Tier 2 also requires the operator to type the service name.
11. **Execute** through the Action API, and log the call.
12. **Verify:** re-check metrics for 2–3 minutes. If not recovered, roll back once and escalate. If symptoms remain, re-triage and re-diagnose (subject to the 2-action cap).
13. **Report:** JSON per the organisers' schema plus markdown. Facts come from the ledger only. Only verified incidents are stored in memory.

**Tiers:**
- **T0:** read-only work, dry runs and reports. No gate.
- **T1:** restart a stateless service or throttle load. Needs confidence ≥ 0.7, then yes/no.
- **T2:** toggle a flag, restart the cache or DB, scale. Needs ≥ 0.85, a dry run, and a typed confirmation.
- **T3:** never act; escalate.

These thresholds are starting values, calibrated during practice.

### Stack and repo
- **Stack:** Python 3.11, `httpx`, `docker` (Docker SDK), `pydantic` v2, `pytest`, `pyyaml`, plus the LLM provider's own SDK. **No agent frameworks, vector databases or embeddings.**
- **Layout:**
```
agent/
  contracts.py              # shared pydantic models (Part 2)            — C (with everyone, hour 1)
  main.py                   # orchestrator / state machine               — C
  config/                   # thresholds.yaml, signatures.yaml, remedies.yaml, redaction.yaml
  tools/                    # prometheus.py, jaeger.py, logs.py, docker_state.py, redact.py — A
  ledger.py                 # evidence ledger                            — A
  detect.py, triage.py      # alert watcher, snapshot, baseline, onset   — A
  verify.py                 # recovery check                             — A
  rules/                    # suspects.py, hypotheses.py, engine.py      — B
  validate.py               # evidence validator                         — B
  llm/investigator.py       # LLM tool loop                              — C
  act/                      # remedy.py, tiers.py, actions.py, killswitch.py, action_log.py — C
  approval/                 # cli.py (and optional web chat)             — E
  memory.py                 # incident memory                            — E
  report.py                 # JSON + markdown reports                    — E
eval/runner.py, eval/scoring.py, eval/results.csv                        — D
tests/                      # each owner tests their own module; D owns end-to-end tests
```

### Working rules
- `main` always runs.
- Work on `feature/<name>` branches and merge small pieces often.
- Run `pytest` before merging.
- **Don't edit another person's files without asking them.**
- Any interface change in Part 2 is announced to the whole team first.
- Thresholds, signatures, remedies and redaction patterns live in YAML, not in code.

---

## PART 2: Interfaces (agree in hour 1, then build against them)

Field names may change to match the organisers' JSON schema. If they do, C updates `contracts.py` and tells everyone.

```python
# --- evidence (A produces, everyone reads) ---
class Fact(BaseModel):
    service: str                 # e.g. "payment"
    signal: str                  # one of SIGNALS below
    scope: str | None = None     # operation, hop "checkout->payment", attribute, etc.
    value: float | str | bool
    baseline: float | None = None
    direction: Literal["up", "down", "same", "absent"] | None = None

SIGNALS = ["error_ratio","p95_ms","req_rate","hop_gap_ms","cpu_used","cpu_limit","mem_used",
           "mem_limit","restarts","oom_killed","status","gc_time","error_concentration",
           "missing_telemetry","missing_edge"]

class Evidence(BaseModel):
    id: str                      # "E1", "E2"… assigned by the ledger
    tool: str
    query: dict
    ts: datetime                 # when we queried
    onset_ts: datetime | None    # when the anomaly started (from traces or query_range)
    facts: list[Fact]
    examples: list[str] = []     # trace IDs, log line snippets (already redacted)
    redacted: bool = True

# --- snapshot and alert (A) ---
class ServiceStatus(BaseModel):
    service: str; req_rate: float | None; error_ratio: float | None; p95_ms: float | None
    baseline: dict[str, float]; onset_ts: dict[str, datetime] = {}
class ContainerStatus(BaseModel):
    service: str; status: str; restarts: int; restarts_since_baseline: int; oom_killed: bool
    mem_used: float | None; mem_limit: float | None; cpu_used: float | None; cpu_limit: float | None
class TriageSnapshot(BaseModel):
    ts: datetime; services: list[ServiceStatus]; containers: list[ContainerStatus]
    dependencies: list[tuple[str, str]]; evidence_ids: list[str]
class Alert(BaseModel):
    service: str; signal: str; value: float; baseline: float; ts: datetime

# --- diagnosis (B and C produce) ---
class Hypothesis(BaseModel):
    id: str                      # "H1"
    component: str               # must be a real service name (validated)
    cause: str                   # short category, e.g. "service_errors", "container_down",
                                 # "network_latency", "cpu_starvation", "memory_leak", "traffic_flood"…
    confidence: float
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    missing_evidence: list[str]  # descriptions of checks not yet run
    source: Literal["rules", "llm"]
class Diagnosis(BaseModel):
    state: Literal["CONFIDENT", "UNCERTAIN", "UNKNOWN"]
    hypotheses: list[Hypothesis]
    selected_id: str | None      # a hypothesis id, or None for UNKNOWN
    reason: str
class ValidationResult(BaseModel):
    ok: bool; problems: list[str]

# --- action (C produces, E displays) ---
class ActionProposal(BaseModel):
    action: Literal["restart_service","toggle_flag","throttle_load","scale_service"]
    target: str; params: dict = {}; tier: int; reason: str; hypothesis_id: str
class DryRunResult(BaseModel):
    proposal: ActionProposal; would_do: str; checks_passed: list[str]; checks_failed: list[str]
class Approval(BaseModel):
    approved: bool; operator: str; typed_confirmation: str | None; ts: datetime
class ActionResult(BaseModel):
    ok: bool; response: dict; ts: datetime
class VerifyResult(BaseModel):
    recovered: bool; evidence_ids: list[str]; remaining_symptoms: list[str]

# --- whole incident (C assembles, E reports, E's memory stores) ---
class Incident(BaseModel):
    alert: Alert; snapshot: TriageSnapshot; diagnoses: list[Diagnosis]
    actions: list[tuple[ActionProposal, Approval | None, ActionResult | None, VerifyResult | None]]
    outcome: Literal["recovered","escalated","rejected","unknown"]; timeline: list[tuple[datetime, str]]
```

**Function boundaries:**
| Owner | Function |
|---|---|
| A | `ledger.add(ev) -> str`, `ledger.get(id) -> Evidence`, `ledger.all() -> list[Evidence]` |
| A | `tools.*(...) -> list[Evidence]` (8 tools, see `query-cookbook.md`), `tools.registry` (name → callable + JSON schema, **read-only only**) |
| A | `detect.watch() -> Alert` (blocking poll), `triage.snapshot() -> TriageSnapshot`, `verify.check(alert, baseline, window_s=150) -> VerifyResult` |
| E | `memory.similar(snapshot) -> list[tuple[str, float, str, str]]` (incident id, score, component, cause), `memory.store(incident)` |
| B | `engine.diagnose(snapshot, ledger, hints) -> Diagnosis`, `validate.check(diagnosis, ledger) -> ValidationResult` |
| C | `investigator.investigate(snapshot, ledger, diagnosis) -> Diagnosis` (source="llm") |
| C | `remedy.propose(hypothesis, diagnosis) -> ActionProposal \| None`, `actions.dry_run(p) -> DryRunResult`, `actions.execute(p, approval) -> ActionResult`, `actions.rollback(p) -> ActionResult` |
| E | `approval.request(proposal, diagnosis, dry_run, ledger) -> Approval` |
| E | `report.build(incident, ledger) -> tuple[dict, str]` (JSON, markdown) |
| D | `eval/runner.py`: inject a practice incident → run the agent → reset → score |

---

## PART 3: Individual briefs

### PERSON A (Claude Code): Eyes, signals and verification
*(Was P1 plus parts of P2. Leads **Checkpoint 1: Eyes**.)*

**Mission:** everything that *reads* the system. If it touches Prometheus, Jaeger, logs or Docker state, it's yours. Your output is the evidence everyone else reasons over, so correctness and redaction matter most.

**You own:** `agent/tools/*`, `agent/ledger.py`, `agent/detect.py`, `agent/triage.py`, `agent/verify.py`, `agent/config/redaction.yaml`, plus the alert thresholds in `agent/config/thresholds.yaml`.

**Build, in order:**
1. **Hour 1:** with everyone, agree `contracts.py`. Then write stubs that return fake but valid `Evidence`, so B and C can start immediately.
2. **Redaction first (`tools/redact.py`):** drop span events named `feature_flag`; drop attributes whose key contains `feature_flag`; exclude the services `flagd` and `flagd-ui`; mask `(?i)feature[ _.-]?flag|flagd|openfeature` and every flag key name in all strings. Patterns go in `redaction.yaml`. **Test:** feed sample spans and logs containing flag text, and assert that none survives.
3. **Tools** (exact queries in `query-cookbook.md`):
   - `service_health_overview`
   - `service_detail(service)`
   - `hop_latency(caller, callee)`, which produces the `hop_gap_ms` fact
   - `search_traces(...)`: aggregate per operation; top 3 messages; earliest error `startTime` as onset; error concentration on one attribute value; 2–3 example trace IDs
   - `container_health`: `docker inspect` gives status, RestartCount, OOMKilled, Memory, NanoCpus; compare limits with compose values
   - `search_logs` (`docker logs` first, OpenSearch optional)
   - `onset` (via query_range)
   - `service_dependencies`

   Every tool returns `list[Evidence]` with **typed facts**, goes through redaction, and is registered in `tools.registry` with a JSON schema for the LLM. **The registry contains read-only tools only.**
4. **Ledger:** append-only, assigns IDs `E1…`, lookup by ID, and can serialize to JSON for the report.
5. **Baseline and alert:**
   - At startup, record a 10-minute baseline per service.
   - `detect.watch()` polls every 15 s and raises an `Alert` when `error_ratio > max(3×baseline, baseline + 0.05)`, or `p95 > 2×baseline`, or restarts increase. It needs 2 consecutive points to fire.
6. **Triage snapshot:** assembles `TriageSnapshot`, including onset times and the dependency list.
7. **Day 2 (before 12:00): `verify.check`.** Re-query the alerting signals for 150 s and decide `recovered` against the baseline. Return `remaining_symptoms`, the other services still abnormal, for combined faults.
8. **Day 2:** hardening (timeouts, empty results, services missing), plus an adapter so the tools can read the **offline incident bundles** (for D's tests).

**Done when:** a live run on one practice incident produces a correct triage snapshot, with redaction tests green, by 15:30 on Day 1. That's the Eyes demo.

**Before the event (no code):** run section 1 of `prep-runbook.md` and the cookbook's live checklist. Record the real metric names and log field names in `query-cookbook.md`.

**Tell your Claude Code:** "Read `query-cookbook.md` and `fault-signatures.md` section 2 first. Every tool must be read-only, must redact, and must return typed `Fact`s from the fixed signal vocabulary. Write tests using recorded JSON responses, not the live system."

---

### PERSON B (Claude Code): Brain, reasoning and validation
*(Was P3 plus the validator. Leads **Checkpoint 2: Brain**, with C.)*

**Mission:** turn evidence into **competing hypotheses**, pick one only when the evidence clearly supports it, and make sure every claim is backed by the ledger.

**You own:** `agent/rules/*`, `agent/validate.py`, `agent/config/signatures.yaml`, and the scoring weights in `thresholds.yaml`.

**Build, in order:**
1. **Hour 1:** contracts with everyone. Then write `engine.diagnose` against A's fake evidence.
2. **`signatures.yaml`:** one entry per fault *pattern* (never per incident). Use `fault-signatures.md` section 5 and the prep-run table. Each entry has:
   - `cause`,
   - `requires` (facts that must be present),
   - `contradicted_by` (facts that rule it out),
   - `discriminating_checks` (tool calls that separate it from look-alikes),
   - `remedy_hint`.

   Example: `container_down` requires `status=exited` or `missing_telemetry` on the service, and is contradicted by `status=running`.
3. **`suspects.py`:** from the dependency graph plus the triage snapshot, list candidate components. These are abnormal services and their abnormal downstream dependencies, not just the deepest one.
4. **`hypotheses.py`:** for each candidate and matching signature, build a `Hypothesis` and score it as a weighted sum (weights in YAML):
   - **anomaly strength,**
   - **temporal precedence:** onset before its dependents; ties within about 30 s are neutral,
   - **trace support:** errors originate in this service's own spans,
   - **downstream propagation:** its callers show matching symptoms,
   - **symptom-only penalty:** its errors are explained by a sick dependency.

   Memory hints from E only **reorder** the candidates.
5. **Discriminating checks:** when the top two hypotheses are close, run the cheapest check listed in the signatures (through A's tools) and re-score.
6. **Diagnosis state:**
   - CONFIDENT if the top score is ≥ 0.7 and leads by ≥ 0.15,
   - UNKNOWN if nothing is above 0.3, or no anomaly is supported,
   - UNCERTAIN otherwise.
7. **Combined faults:** after a verified fix, re-run on `remaining_symptoms`. Don't cap the number of causes; C enforces the 2-action cap.
8. **`validate.check`:**
   - every cited ID exists,
   - every quoted value matches the ledger,
   - each supporting item has a fact whose `service` and `signal` fit the hypothesis's component and cause (mapping table in YAML),
   - contradicting evidence isn't listed as support,
   - `component` is a real service.

   On failure, return `ok=False` with the reasons; C's orchestrator then escalates.
9. **Day 2:** tune weights with D's results; reviewer agent (stretch goal), a second pass that tries to disprove the selected hypothesis using the contradicting checks.

**Done when:** correct diagnosis on at least 3 practice incidents chosen by the mentor, with evidence and confidence, in the JSON schema, by 18:30 on Day 1.

**Before the event (no code):** after the prep run, turn the observations into a draft `signatures.yaml` *in a notes file* (not committed), plus a list of look-alike pairs and their deciding checks.

**Tell your Claude Code:** "Rules must be data-driven from YAML and match telemetry facts only, never incident names. Keep scoring a simple, explainable weighted sum. Write a unit test per signature using hand-built evidence, including a look-alike that must *not* match."

---

### PERSON C (Claude Code): orchestrator, LLM investigator and Hands
*(Was P4 (LLM) plus P5 (Hands) plus integration. Leads **Checkpoint 3: Hands**.)*

**Mission:** wire the steps into one safe state machine, run the LLM only when the rules are unsure, and guarantee that nothing changes without approval through the Action API.

**You own:** `agent/contracts.py` (as keeper), `agent/main.py`, `agent/llm/*`, `agent/act/*`, `agent/config/remedies.yaml`, and tiers in `thresholds.yaml`.

**Build, in order:**
1. **Hour 1:** write `contracts.py` from Part 2 (adjusted to the organisers' schema), plus `main.py` as a **walking skeleton** with every step stubbed, producing a schema-valid but fake report. Everyone commits once.
2. **`main.py` state machine:** ALERT → TRIAGE → DIAGNOSE (B) → [UNCERTAIN → LLM (C)] → VALIDATE (B) → [fail or UNKNOWN → ESCALATE] → REMEDY → DRY RUN → APPROVAL (E's UI) → EXECUTE → VERIFY (A) → [not recovered → ROLLBACK → ESCALATE] → [remaining symptoms and actions < 2 → TRIAGE again] → REPORT (E) → MEMORY.STORE if verified (E). Log every transition with a timestamp into `Incident.timeline`.
3. **LLM investigator (Day 1, for Brain):**
   - Input: the snapshot, the current hypotheses and their `missing_evidence`.
   - Tools: **only** `tools.registry` (read-only). Assert this at startup.
   - At most 8 calls, temperature 0.
   - Output must parse as `{hypotheses: [...], selected_diagnosis: "<H-id or UNKNOWN>", reason}`. Invalid output gets one retry, then UNKNOWN.
   - The prompt says: cite evidence IDs only, list contradicting and missing evidence, and say UNKNOWN when unsure.
   - Never pass raw unredacted data; everything comes through A's tools.
4. **Action API client (start Day 1):** read the organisers' Action API docs and wrap only the allowlisted actions. A `dry_run=True` mode only describes the call. Check the **kill switch** (a file such as `KILL` in the repo root, plus an environment variable) before every call. Append every call to `action_log.jsonl`: timestamp, proposal, approval, response.
5. **Remedy and tiers:** `remedies.yaml` maps `cause` to `action` (for example `memory_leak → restart_service`, `traffic_flood → throttle_load`, `container_down → restart_service`). `toggle_flag` is used only if the organisers confirm it's allowed. `propose()` checks:
   - the action is allowlisted,
   - the target is a real service,
   - the tier permits it,
   - the confidence meets the tier threshold,
   - fewer than 2 actions so far,
   - no repeat action on the same service.

   Otherwise it returns `None`, which means escalate.
6. **Day 2 (before 12:00):** execute, call `verify` (A), roll back once on failure, then escalate. Complete one full run from alert to verified recovery, with the action log. That's the Hands checkpoint.

**Done when:** Hands at 12:00 on Day 2, and an end-to-end run on at least 3 practice incidents without manual intervention apart from the approval.

**Before the event (no code):** read the 2-page LLM tool-calling primer. Sketch the state machine on paper, and draft the LLM prompt *as text*.

**Tell your Claude Code:** "Safety is enforced in code. The LLM gets only the read-only registry. Every action passes allowlist, target, tier, threshold, action-count, kill-switch and explicit-approval checks before the Action API is called. Write tests proving each check blocks the action when it fails."

---

### PERSON D (ChatGPT): evaluation, testing and calibration
*(Was P2's evaluation work. Leads **Checkpoint 0: Setup**, and owns the evaluation note.)*

**Mission:** tell the team, with numbers, what the agent gets right and wrong, and keep the hidden-test score high. You decide what gets fixed next.

**You own:** `eval/runner.py`, `eval/scoring.py`, `eval/results.csv`, `tests/test_end_to_end.py`, and `EVALUATION.md` (a deliverable).

**Build, in order:**
1. **Checkpoint 0 (by 13:00, Day 1):**
   - the demo is running with load on every laptop that needs it,
   - the LLM key works,
   - the repo is forked,
   - all 5 people have committed.

   Keep a checklist.
2. **`eval/runner.py`** (about 100–150 lines; ask ChatGPT to help and make sure you understand every line). For each practice incident:
   1. inject it with `faultctl`,
   2. run the agent with **auto-reject approval**, so no actions run (scoring the diagnosis only),
   3. capture the report,
   4. reset with `faultctl`,
   5. wait for baseline.

   **Never pass the incident name to the agent.** Only the runner knows it.
3. **`eval/scoring.py`:** compare the agent's selected component and cause with the expected answer you write by hand in `eval/expected.yaml` (only the runner reads this). Record in `results.csv`:
   - incident,
   - expected,
   - predicted,
   - state,
   - confidence,
   - correct (y/n),
   - escalated (y/n),
   - time to diagnosis,
   - number of LLM calls.
4. **Calibration table:** group by confidence bucket (0.5–0.6, 0.6–0.7, …) and compute the accuracy of each. Give B and C the evidence for moving the 0.7 and 0.85 thresholds.
5. **End-to-end tests:** run the agent against A's offline-bundle adapter for 2–3 recorded incidents, and assert a schema-valid report.
6. **Day 2:** write `EVALUATION.md`. Which practice incidents are right or wrong, and why; the calibration table; known weaknesses. Be honest, because judges reward that.

**Before the event:** run sections 2–5 of `prep-runbook.md` (on your laptop, or with A). Fill in the observation template for as many faults as you can. That data drives B's rules.

**Tell your ChatGPT:** "I'm writing a Python 3.11 evaluation runner. It must never leak the incident name to the agent under test. Keep it simple and readable, and explain each function so I can explain it to a judge."

---

### PERSON E (ChatGPT): human interface, reports, memory and docs
*(Was the approval UI from P5, reports from P4, and memory from P1. Owns the README, team form and demo script.)*

**Mission:** everything a human sees: the approval screen, the incident report, the docs and the demo. Plus incident memory, which is small and self-contained.

**You own:** `agent/approval/*`, `agent/report.py`, `agent/memory.py`, `README.md`, `ARCHITECTURE.md` (the diagram PDF plus a short text), `TEAM_FORM.md`, and `DEMO.md`.

**Build, in order:**
1. **Approval CLI (`approval/cli.py`, Day 1 afternoon):** show the diagnosis (component, cause, confidence, the top 3 evidence items with values), the competing hypotheses, the ActionProposal, the tier and the **dry-run result**. Then ask for approval.
   - **Tier 1:** `Approve? [y/N]`
   - **Tier 2:** the operator must type the target service name exactly.
   - Anything else counts as reject. Return an `Approval`.
   - Never auto-approve. A timeout counts as reject.
   - **Optional Day 2:** a simple chat-style web page doing the same thing (stretch goal 4).
2. **`report.py`:** build the organisers' JSON schema from `Incident` plus the ledger: timeline, root cause, evidence (IDs plus values copied from the ledger), action taken, verification, and what would prevent a repeat. Also produce a readable markdown version. If C wants an LLM to write the prose, it may only rephrase ledger facts; you check that every number in the prose appears in the ledger.
3. **`memory.py`:**
   - **Fingerprint:** the set of `service:signal:direction` items from the snapshot, for example `payment:error_ratio:up`.
   - `similar()` returns past incidents with Jaccard similarity ≥ 0.6, best first.
   - `store()` only saves incidents with `outcome == "recovered"`.
   - Store as a JSON file, **empty by default**, controlled by a config flag.
   - Memory only reorders suspects; it never touches confidence.
4. **Docs (from Day 1 evening):**
   - **README:** setup, how to run, how the approval gate and kill switch work, known limitations.
   - **TEAM_FORM:** names, who built what, tools and AI models used (Claude Code, ChatGPT, the LLM API), third-party libraries.
   - **ARCHITECTURE.md:** the PDF plus one paragraph per step.
5. **DEMO.md (Day 2):** a 5-minute script. One incident solved by the rules, one that goes through the LLM path, one escalation, and show the kill switch. Also prepare answers for 3 minutes of questions.

**Before the event (no code):** draft the README outline, team form and demo-script outline as text. Read the organisers' JSON schema as soon as it's published.

**Tell your ChatGPT:** "I'm writing small, readable Python 3.11 modules using pydantic models given to me (paste Part 2). Approval must default to reject. Reports must only contain facts that exist in the evidence ledger. Explain every function so I can present it."

---

## PART 4: Timeline by person

| When | A | B | C | D | E |
|---|---|---|---|---|---|
| D1 11:30–13:00 | contracts; tool stubs; redaction | contracts; engine skeleton | contracts; **walking skeleton** | **Checkpoint 0**; setup help | contracts; approval CLI skeleton |
| D1 13:00–15:30 | real tools, ledger, triage, alert | signatures, suspects | LLM loop; Action API client (dry run) | eval runner | approval CLI; report JSON |
| **D1 15:30 Eyes** | **demo lead** | — | — | — | architecture sketch ready |
| D1 15:30–18:30 | onset, deps, hardening | ranking, discriminating checks, validator | LLM structured output; main wiring | runs practice set; results.csv | report markdown; README draft |
| **D1 18:30 Brain** | support | **demo lead** | **co-lead** (LLM path) | shows accuracy table | — |
| D2 09:30–12:00 | verify.check, remaining symptoms | combined faults; tuning | remedy, tiers, execute, rollback | calibration; e2e tests | memory; team form |
| **D2 12:00 Hands** | support | support | **demo lead** | runs the full test | approval UI shown |
| D2 12:00–13:30 (freeze) | bugs only | bugs only | bugs only | EVALUATION.md | README, ARCHITECTURE, DEMO |
| D2 13:30–14:00 | final checks | final checks | final push | schema compliance check | deliverables checklist |
| D2 14:00+ hidden test | on call | on call | on call | watches results | prepares the demo |

**Load:** A, B and C carry the core code (each about 25% of the work). D and E carry about 12–13% each: a real, explainable code module each, plus evaluation, docs and the demo.
