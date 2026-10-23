# Incident Zero — AI On-Call Engineer

Rules-first implementation of the supplied architecture: alert → triage → dependency localization → rules → bounded LLM fallback → evidence validation → trusted remedy → human approval → Action API → recovery verification → report and verified memory.

**Start all agent commands from the repository root.** Python 3.11+ is required. Install the dependencies once (the Eyes tools in `agent/` need them):

```sh
python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

The upstream application is the `opentelemetry-demo/` submodule (`git submodule update --init`).

## 1. Run the complete demonstration now

No API key, Docker containers or network access is needed for these synthetic exercises:

```sh
cd team-15-bosch
python3.11 -m incident_zero simulate --exercise
python3.11 -m incident_zero simulate --scenario combined --exercise
python3.11 -m incident_zero simulate --scenario unknown --exercise
python3.11 -m incident_zero evaluate
```

The first case should resolve after one simulated action. The combined case uses two separate simulated approvals, verifies the first target, rechecks the second cause and verifies all affected targets at the end. The unknown case should escalate with evidence and no mutation. Reports are JSON and Markdown under `output/`; evaluation summary: `output/evaluation/evaluation.md`.

`--exercise` supplies **test approvals in synthetic data only**. It never approves a live action, calls Docker to mutate a container, or measures LLM accuracy. Omit it to inspect a pending proposal. Scenarios: `recovered`, `failed`, `missing`, `rejected`, `killed`, `unknown`, `combined`, `oom`, `restart-loop`, `tier2`, `rollback`, `three-causes`.

## 2. Start the OpenTelemetry application

The upstream repository is already cloned in this workspace. For a fresh copy of the source archive, first run `git clone https://github.com/open-telemetry/opentelemetry-demo.git` from the extracted agent root. Open Docker Desktop, then in another terminal:

```sh
cd opentelemetry-demo
docker compose --parallel 3 -f compose.yaml -f compose.observability.yaml up -d --no-build --wait --wait-timeout 180
docker compose -f compose.yaml -f compose.observability.yaml ps -a
```

Image downloads can take a long time. Do not launch a duplicate if the earlier Compose download is still running. Successful startup must be confirmed by healthy/running containers. Shop: http://localhost:8080/ ; traces: http://localhost:8080/jaeger/ui/ ; Grafana: http://localhost:8080/grafana/ ; Prometheus: http://localhost:9090/ . Find the log port with:

```sh
docker compose -f compose.yaml -f compose.observability.yaml port opensearch 9200
```

## 3. Configure the optional LLM fallback

Put your key in the root `.env` file as `OPENAI_API_KEY=...`. Keep the configured `INCIDENT_MODEL` and `INCIDENT_MODEL_URL`, or use an accessible Responses-compatible local model endpoint. Do not put the key in chat, a command argument, Git, or a report. The model is constructed only if rules are unsure. A confident rule works without a key; absent credentials on an unknown case produce an evidence-backed escalation.

## 4. Investigate live telemetry

Return to the agent root:

```sh
cd team-15-bosch
python3.11 -m incident_zero doctor
python3.11 -m incident_zero observe --service checkout
python3.11 -m incident_zero investigate --service checkout --alert "Checkout errors" --container-health
```

Add `--opensearch-url http://localhost:PORT` using the actual port from step 2 to include logs. A bounded watcher is also available:

```sh
python3.11 -m incident_zero watch --service checkout --container-health --cycles 10 --interval 30
```

It detects sustained errors, p95 latency and stopped/restarting containers. No missing samples are interpreted as healthy. Investigation does not execute fixes.

## 5. Connect documented remedies, then approve each change

The public demo repository does **not** include the Bosch Action API, official incident schema or practice bundles. Fill a local copy of `config/action-contract.example.json` using the organizer's actual contract. Every mapping specifies an exact target, endpoint, fixed body, acceptance response, tier, telemetry signatures and optional rollback action. Do not mark placeholders confirmed.

Supply `--action-contract config/action-contract.local.json` to `investigate` or `watch` once reviewed. If a unique permitted remedy exists, the report contains its proposal ID and digest:

```sh
python3.11 -m incident_zero plan PLAN_ID
python3.11 -m incident_zero execute PLAN_ID
python3.11 -m incident_zero decide PLAN_ID --digest DIGEST --by "Your name" --decision approve
# Tier 2 requires the dry run above plus --confirm-service EXACT_SERVICE.
python3.11 -m incident_zero execute PLAN_ID --live --action-contract config/action-contract.local.json --container-health
```

Live execution requires explicit approval for the exact proposal, valid for five minutes. Tier 1 requires confidence ≥0.7; tier 2 requires ≥0.85, a recorded dry run and typed service. Tier 3 escalates. A second cause and a rollback each require a fresh approval. At most two mutations total, including rollback, are allowed per incident. One rollback at most, then escalation. Ambiguous API timeouts never auto-retry.

Recovery uses three fresh samples over three minutes with positive traffic, ≤1% errors and p95 ≤2000ms. Infrastructure targets require running and healthy container state. These thresholds need calibration and service-specific adaptation with real practice data. An end-to-end customer transaction probe is not implemented.

```sh
python3.11 -m incident_zero kill on
python3.11 -m incident_zero kill off
python3.11 -m incident_zero report INCIDENT_ID
```

The kill switch blocks new tool/model/action calls; it cannot cancel an already dispatched request. Reports are refreshed after execute, including errors. Re-export after a manual decision if needed.

## Implementation and validation

- `pipeline.py`: architecture coordinator and code-selected remedies.
- `rules.py`, `config/rules.json`: generic container signatures, resource correlations, dependency ordering and confidence margin. These rules use telemetry, never incident IDs or fault settings.
- `agent.py`, `model.py`: up to eight fallback calls, strict cited evidence, no model-selected live mutation.
- `tools.py`, `telemetry.py`: projected read-only metrics, traces, logs, health and resource samples.
- `store.py`, `actions.py`: append-only evidence/events, durable approval state, tiers, bounded execution and verification.
- `reporting.py`: ledger-derived JSON/Markdown. Report facts are deterministic; optional LLM prose generation is not implemented.
- `demo.py`, `evaluation.py`: transparent synthetic evaluation; `synthetic.py` retains transport-test fixtures.

### Eyes tools (merged from `agent/`)

- `eyes_tools.py`: seven more read-only tools backed by `agent/tools`, stored as ordinary evidence records: `read_service_health` (error ratio, p95, rate vs baseline), `read_operations` (per operation), `read_trace_errors` (where errors originate, top messages, one attribute value shared by most errors), `read_hop_latency` (client vs server time per hop), `read_onset`, `read_container_limits` (CPU and memory limits vs compose and baseline) and `read_deploy_history` (Docker events). The pipeline collects the first four for every service it visits and follows services where errors originate; the LLM may ask for any of them.
- `config/rules.json` gains three rules built on them: `errors_originate_in_service`, `cpu_limit_lowered`, `memory_limit_lowered`.
- `watch` without `--service` watches every service and container against the Eyes baseline (`python -m agent.eyes baseline`, no fault active), fires after two abnormal polls, and runs the pipeline on the most severe alert.
- Flag redaction from `agent/tools/redact.py` now also covers this package: spans calling the flag service are dropped from trace projections, log bodies are masked, and `Store.add_evidence` masks and refuses any remaining flag data.
- `python -m agent.eyes` still runs the Eyes checkpoint demo on its own; see `docs/eyes.md`.

```sh
pytest -q
```

Loopback HTTP tests require permission to bind local ports. See `docs/architecture.md` for the numbered diagram mapping and `docs/build-plan.md` for remaining integrations. All synthetic cases passing means the workflow behaved as expected, **not that all unknown faults were diagnosed or fixed**. Hidden-incident performance, real LLM quality, official scoring and live remedies remain unverified until the organizer interfaces, model access and running demo are available.


Latest validation: **138 tests passed (after merging the Eyes tools); 12/12 synthetic cases passed**. Docker is now running, and live checkout metrics, traces, logs and container health were verified. The latest model request returned HTTP 429 and safely escalated before a completion. See `docs/evaluation-note.md` for the limits of these results.
