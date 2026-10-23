# Person C implementation handoff

Branch: `feature/person-c`. This is a tested local vertical slice, not a live-stack integration or an organiser-schema compliance claim.

## Files and purpose

- `agent/contracts.py`: Part 2 models with unchanged field names, Pydantic v2 extra-field rejection, finite numbers, bounded confidence/tier and the documented signal vocabulary. Mutable defaults are copied by Pydantic.
- `agent/main.py`: injected Part 2 collaborators, timestamped control-flow transitions, rules-first investigation, validation, sequential correction/verification, rollback approval, reporting and verified-only memory storage. `python -m agent.main` prints configuration status; use `run(alert, Dependencies(...))` to run an incident. A fresh `Actions` instance is required per incident. Failure before a valid snapshot raises rather than fabricating an Incident.
- `agent/llm/investigator.py`: async provider adapter, explicit read-only tool wrapper, eight tool calls total, 10-second call and 90-second overall budgets, structured output with one invalid-output retry, service restrictions and ledger ID/content checks. Person B still owns semantic evidence/claim validation. Tool callbacks must be asynchronous and respect cancellation/backend timeouts.
- `agent/act/remedy.py`: deterministic cause-to-remedy lookup; no incident identifiers. Unknown causes escalate.
- `agent/act/tiers.py`: T1/T2 safety floors 0.7/0.85; stateful restarts are T2; T3 never executes.
- `agent/act/actions.py`: isolated Python adapter protocol and disabled default adapter, dry-run matching, approval freshness and proposal binding, two attempts maximum, no repeat target, verification gate, one separately previewed/approved supported rollback. Failed/ambiguous executions consume their attempt and escalate without retry. State is in-process: do not reuse after restart or run concurrently; persistent incident recovery is not implemented.
- `agent/act/killswitch.py`: `KILL` file or `INCIDENT_ZERO_KILL` environment variable, rechecked immediately before adapter calls.
- `agent/act/action_log.py`: append-only JSONL, intent fsynced before calls; logging failures stop progress. A post-call logging failure cannot undo a mutation and therefore leaves the action pending and escalates.
- `agent/config/remedies.yaml`: actual demo service names, conservative remedy/allowlist policy. Toggle is disabled. Scale has no mapping and unsupported parameters are rejected until the official contract arrives.
- Empty `agent/__init__.py`, `agent/act/__init__.py`, `agent/llm/__init__.py`: package markers.
- `tests/test_person_c.py`: explicitly fake Action API, model, ledger and teammate doubles; 35 focused safety and vertical-slice tests.
- `requirements-person-c.txt`: Python dependencies for this slice; no framework or provider assumption.
- `.gitignore`: local environments, Python caches and local runtime audit/kill files.

## Team coordination / additive local interfaces

Part 2 model field names and function arguments were preserved. No teammate-owned files were edited. No shared `thresholds.yaml` was created or changed. B should agree how calibrated thresholds will be loaded; safety floors currently live in `tiers.py`, pending that agreement. All other remedy policy is YAML.

Announcements for integration review (local extensions, not changes to Part 2):

1. E's `Approval` has no proposal digest. The orchestrator therefore calls `Actions.bind_approval(p, approval)` immediately after E's synchronous request returns. Binding is in memory, single owner, expires after 120 seconds, and rejects approval predating the preview or a different proposal. An approval returned later by an asynchronous UI will need a proposal-bound request token agreed with E.
2. All changes need approval, including rollback. Optional adapter methods `rollback_dry_run` and capability `supports_rollback`, plus local `Actions.approved_rollback(p, approval)`, allow that safely. Part 2 `rollback(p)` fails closed without separate preview/approval. No official rollback semantics have been assumed.
3. A's registry representation is not implemented yet. `ReadOnlyTool(call, schema, read_only=True)` is an isolated adapter boundary, not an assumed actual registry format. A must wrap the actual `tools.registry` and supply `safe_payload` using A's complete redactor, including flag-key patterns. Do not substitute an identity redactor outside tests. Registry callbacks supply typed redacted Evidence; they may already have added it to the ledger. The investigator checks identity/content against that ledger.
4. LLM provider adapter is `async complete(history, system, tools, output_schema, temperature)` returning either `{'tool_calls': [{'name': ..., 'arguments': {...}}]}` or `{'output': <dict or JSON>}`. This is a testable local boundary; no actual provider SDK contract is claimed.

## Inspection and commands

Read README, Part 1/2/Person C and ownership boundaries in team briefs, fault signatures and query cookbook, and extracted architecture PDF. The supplied `bosch.pdf` and `docs/hackathon-brief.pdf` are byte-identical (SHA-256 `7f3a4aadf4c98def437d8290e4d7f5eba090cac685ed585c22d73e6a4a40769a`). All seven scanned slides were rendered and visually reviewed. They describe allowed actions and rules but provide no endpoint, payload, authentication, dry-run or rollback API contract.

Commands included `pwd`, `rg --files`, `git status --short`, `ls`, `find agent`, `cat`/`sed` on prep files, Python dependency availability checks, `python3 -m venv .venv`, pip installations of test/PDF tooling, PDF extraction/rendering, `git switch -c feature/person-c`, `python -m compileall -q agent`, `python -m agent.main`, and `python -m pytest -q tests`. A separate `python3.11 -m venv /tmp/bosch-person-c-py311` was used to validate the required runtime.

Failures: sandbox DNS blocked initial pip installation; approved escalation succeeded. Sandbox Git-ref writes blocked branch creation; approved escalation succeeded. Text extraction found image-only organiser slides; rendering allowed visual review. An oversized image response was retried using smaller renders. Initial pytest: 29 passed / 1 failed because the ledger double generated different query timestamps for the same evidence ID; the test fixture was corrected to reuse the same evidence. Additional safety cases were then added.

Final verification:

- Python 3.14.7, Pydantic 2.14.1, PyYAML 6.0.3, pytest 9.1.1: **35 passed** (`.venv/bin/python -m pytest -q tests`).
- Python 3.11: **35 passed** (`/tmp/bosch-person-c-py311/bin/python -m pytest -q tests`).
- Both runtimes: `python -m agent.main` succeeds and states real Action API is disabled.

The demo submodule gained an existing concurrent modification during this session. Only status metadata was inspected; its flag state was never read or changed. No Docker remediation, scoring harness, evaluation runner, README, approval CLI, memory, report or architecture file was modified.

## Next actions from teammates / organisers

- A: provide real snapshot/ledger/tools/redactor/verification modules and baseline argument adapter; current orchestrator passes the snapshot as baseline context to `verify.check`, requiring agreement with A's concrete implementation.
- B: provide engine and typed evidence validator; coordinate shared threshold loading and clear-margin policy. The investigator checks existence, not semantic proof of claims.
- E: provide synchronous approval UI (default reject), report builder and verified incident memory. Do not cache or replay approvals between previews.
- D: connect offline practice bundles and eval/end-to-end runner; live three-practice-incident Hands validation remains outstanding.
- Organisers: supply Action API and incident JSON schema, template and LLM credentials/provider choice. Only then implement the real adapter and provider wrapper. No live action has run.
