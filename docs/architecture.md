# Architecture mapping

Implementation follows the supplied Incident Zero diagram, retaining working telemetry and approval primitives from the earlier prototype.

| Diagram step | Implementation | Behavior |
| --- | --- | --- |
| 1 Alert watcher | alerts.py, CLI watch | Two consecutive metric/container breaches; bounded polling and five-minute in-process cooldown |
| 2 Triage snapshot | pipeline.py, tools.py | Request/error rate, p95, projected traces, logs, health, CPU/memory; absent telemetry stays absent |
| 3 Incident memory | store.py, rules.py | Fingerprint matches from verified resolved incidents, same live/synthetic mode; hints only, zero confidence bonus (within ≤0.1 limit) |
| 4 Localize cause | rules.py | Trace child-service/peer edges; bounded traversal of up to eight services, deepest failing node first; cycles bounded |
| 5 Rule engine | rules.py, config/rules.json | Generic stopped/OOM/restarting signatures; declarative multi-source CPU/memory correlation; one or two causes |
| 6 Confidence gate | rules.py | Minimum 0.7 and 0.1 margin between alternatives for the same component; ambiguous hypotheses go to fallback |
| 7 LLM investigator | agent.py, model.py | Constructed only when unsure; at most eight model/tool calls; initial ledger supplied; action_id constrained to null |
| 8 Validator | agent.py | Exact evidence ID, observation index, field, type/value; narrative causality remains a hypothesis |
| 9 Remedy + tier | pipeline.py | Trusted signature-to-action table; one exact target; missing, duplicate, unknown or unsafe remedy escalates |
| 10 Approval gate | store.py | Exact digest, identity, expiry, single-use claim; tier 2 also requires dry run and typed service |
| 11 Execute | actions.py | Reviewed loopback Action API contract only; persistent kill switch; log before dispatch; uncertain outcome never retries |
| 12 Verify recovery | actions.py | Three post-action samples over three minutes; first target verified before next cause; all targets checked on final recovery |
| 13 Report + learn | reporting.py, store.py | Ledger-derived JSON/Markdown always available, including escalation; only resolved incidents enter memory |

## Deliberate conservative choices

- At most two mutations total per incident, including rollback. If both forward actions were used, failure escalates without a third mutation.
- Rollback needs a documented inverse mapping, confidence ≥0.85, dry run and separate strict human approval. No implicit reversal or automatic restart.
- Unknown fault handling means bounded further investigation and a useful escalation when evidence or a remedy is insufficient. It does not guarantee a correct answer to every unseen incident.
- CPU/memory thresholds are heuristics. Docker CPU percentage can exceed 100 on multicore workloads; thresholds require workload calibration. A memory snapshot cannot prove a memory leak.
- Memory currently uses exact fingerprints (binary similarity), no embeddings and no automatic confidence boost.
- Reports use deterministic ledger facts. The diagram's optional LLM prose in step 13 is not implemented; this has no effect on detection or remediation decisions.
- Incomplete dependency traversal, more than two causes, ambiguous rules, absent model access and invalid citations escalate.
- Current adapters accept explicitly synthetic bundles; organizer offline formats and the official output schema still require their actual definitions.
- SQLite triggers prevent routine update/delete of evidence and events. This is local append-only behavior, not tamper resistance against an administrator who owns the database.
- Runtime state is persisted between CLI calls. Watcher streak and cooldown are in-process; restart resets them.

## Ownership for five teammates

P1: telemetry/evidence/memory. P2: alerting and evaluation. P3: dependencies/rules. P4: LLM fallback, validator and reports. P5: remedy policy, approval, action execution and verification. Team names and contributions remain in team-form.md.
