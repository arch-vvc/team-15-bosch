# Team 15: Incident Zero (Bosch × PES University)

An AI On-Call Engineer for the OpenTelemetry Demo: deterministic rules first, an LLM fallback only when the rules are unsure, human approval for every change, and changes made only through the organisers' Action API.

**This repo currently holds prep material only (design and notes, no agent code).** The agent is written on the day, per the hackathon rules.

## Contents
| Path | What it is |
|---|---|
| `docs/incident-zero-architecture.pdf` | Architecture diagram: 13 steps, owners, tiers, guardrails |
| `docs/hackathon-brief.pdf` | The organisers' slides (rules, scoring, timeline) |
| `prep/team-briefs.md` | **Start here.** Shared context, interfaces, and one brief per person (A–E) to paste into your Claude Code or ChatGPT |
| `prep/fault-signatures.md` | What each failure looks like in telemetry, look-alikes, and the flag-redaction list |
| `prep/query-cookbook.md` | Exact Prometheus, Jaeger, OpenSearch and Docker queries for each read-only tool |
| `prep/prep-runbook.md` | Step-by-step prep session: baseline, 14 application faults, 8 infrastructure faults, combinations |

## Setting up the demo locally
```bash
git clone --depth 1 https://github.com/open-telemetry/opentelemetry-demo.git
cd opentelemetry-demo
make start-minimal        # 8 GB laptops: minimal mode; give Docker about 5 GB of memory
```
- If image pulls fail with "unexpected EOF", rerun the command with `COMPOSE_PARALLEL_LIMIT=3`.
- Then follow `prep/prep-runbook.md`.

## Non-negotiables
- Never read feature-flag state (flagd, the flag UI or the flag API), and strip flag data from all telemetry.
- The LLM has read-only tools only and never chooses or executes actions.
- Every change needs explicit human approval and goes through the Action API.
- No hard-coded incident answers.
