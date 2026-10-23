# The combined agent (`archit/merged`)

One agent built from the three codebases on `main`, taking from each the part that fills a
real gap in the others. `python -m agent.run` runs it.

| Step | Code | Came from |
|---|---|---|
| 1 Alert watcher | `agent/detect.py` | Eyes (A) |
| 2 Triage snapshot | `agent/triage.py`, `agent/tools/*` | Eyes (A) |
| 3 Incident memory | `agent/memory.py` | new for E's slot; fingerprint + Jaccard from the brief, verified-only like incident_zero |
| 4-6 Suspects, ranked hypotheses, state | `agent/rules/engine.py`, `agent/config/signatures.yaml` | incident_zero's rules ported to the shared ledger, plus the Eyes rules; fills B's slot |
| 7 LLM investigator | `agent/llm/investigator.py` + `agent/llm/responses.py` | Aryan (C), connected to incident_zero's OpenAI client |
| 8 Validator | `agent/validate.py` | incident_zero's exact citation checks ported to the shared ledger; fills B's slot |
| 9 Remedy and tier | `agent/act/remedy.py`, `tiers.py`, `config/remedies.yaml` | Aryan (C) |
| 10 Approval gate | `agent/approval/cli.py` + `agent/act/actions.py` | prompt new for E's slot; binding, freshness, typed confirmation from Aryan (C) |
| 11 Execute | `agent/act/actions.py`, `killswitch.py`, `action_log.py`, `preview.py` | Aryan (C); preview-only adapter until the Action API exists |
| 12 Verify | `agent/verify.py` (through `agent.main.EyesContext`) | Eyes (A) |
| 13 Report | `agent/report.py` | new for E's slot, ledger facts only |
| Orchestration | `agent/main.py` | Aryan (C) |

## Run it

```sh
python -m agent.eyes baseline
python -m agent.run once
python -m agent.run watch
```

The first needs no fault active. Without Docker, replay a recording:

```sh
python -m agent.run once --replay tests/fixtures/payment_failure.jsonl
python -m agent.run once --replay tests/fixtures/container_down.jsonl
```

`--auto-reject` never approves (for evaluation). The LLM fallback reads `OPENAI_API_KEY` and
`INCIDENT_MODEL` from `.env`; without them an uncertain case escalates. Reports land in
`runs/<time>/`, the action log in `state/action_log.jsonl`.

## What the rules cover

| Signature | Cause | Remedy |
|---|---|---|
| container_stopped | container_down | restart (tier 2 for the cache and DB) |
| oom_killed, restart_loop | crash_loop | restart |
| memory_near_limit | memory_leak | restart (mitigation; it will recur) |
| errors_originate_in_service | service_errors | none: escalate with the diagnosis |
| cpu_limit_lowered, memory_limit_lowered | cpu_starvation, memory_starvation | none: escalate |
| cpu_saturation | high_cpu | none: escalate |

Not covered by rules yet: network latency and packet loss, traffic flood, slow single
operations without errors. Those go to the LLM, or escalate when no model is configured.

## Gaps closed in `incident_zero` (raised in Aryan's handoff)

- Approval of any tier now needs a dry run of that exact proposal first (was tier 2 only).
- A second corrective action on the same service is refused.
- `Store.killed()` also honours the shared `KILL` file and `INCIDENT_ZERO_KILL` variable.

## Known limits

- The Action API is not wired: approved actions are refused at execute, and the incident
  escalates with the approved proposal on record. Write the real adapter in `agent/act/` once
  the organisers publish the contract (`dry_run`, `execute`, optional `rollback`).
- The report layout is a development format until the organisers' JSON schema is out.
- Confidences are starting values; calibrate them from practice runs.
- Everything here ran against the simulated demo and recordings, not the live demo.
