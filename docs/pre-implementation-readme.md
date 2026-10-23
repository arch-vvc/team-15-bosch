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

## Setup
The OpenTelemetry Demo is included as a **git submodule** in `opentelemetry-demo/`, pinned to commit `ad06a6f` (22 Oct 2026), the version our prep notes were written against.

1. **Install** Docker Desktop and Python 3.11.
2. **Give Docker about 5 GB of memory:** Docker Desktop → Settings → Resources → Memory limit → Apply. On an 8 GB laptop, don't go higher.
3. **Make the `docker` command available** (Docker Desktop on macOS doesn't always add it to the PATH):
   ```bash
   echo 'export PATH="$HOME/.docker/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
   docker compose version
   ```
4. **Clone this repo with the demo:**
   ```bash
   git clone --recurse-submodules https://github.com/arch-vvc/team-15-bosch.git
   ```
   If you already cloned without it, run `git submodule update --init` inside the repo.
5. **Pull the demo images once** (several GB; use good Wi-Fi). Limiting parallel downloads avoids "unexpected EOF" failures, and if it fails anyway, rerun it and it resumes:
   ```bash
   cd team-15-bosch/opentelemetry-demo
   COMPOSE_PARALLEL_LIMIT=3 docker compose --env-file .env --env-file .env.override -f compose.yaml -f compose.observability.yaml -f compose.extras.yaml pull
   ```
6. **Start it** (minimal mode: the shop plus Prometheus, Jaeger, Grafana, OpenSearch, without Kafka):
   ```bash
   make start-minimal
   ```
   Open the shop at http://localhost:8080, Jaeger at http://localhost:8080/jaeger/ui and Grafana at http://localhost:8080/grafana/.
7. **Stop it** when done, to free memory: `make stop`
8. Then follow `prep/prep-runbook.md`.

**Comes from the organisers at kickoff, not in this repo yet:** the template repo, `faultctl`, the Action API, the incident JSON schema, the offline incident bundles, and the LLM API key.

## Non-negotiables
- Never read feature-flag state (flagd, the flag UI or the flag API), and strip flag data from all telemetry.
- The LLM has read-only tools only and never chooses or executes actions.
- Every change needs explicit human approval and goes through the Action API.
- No hard-coded incident answers.
