# Eyes (person A): read-only tools, redaction, ledger, alert watcher, triage, recovery check

Checkpoint 1 asks for: *tools for logs, metrics, traces and deploy history; the agent summarises the symptoms of one practice incident.* `python -m agent.eyes watch` does exactly that: it waits for an alert, runs every tool once, and prints a symptom summary in which every line cites ledger evidence.

## Run it on the laptop

```bash
cd ~/Desktop/team-15-bosch
git fetch && git checkout feature/eyes
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q                                   # 56 tests, no Docker needed

# no fault active, load generator running for at least 10 minutes
python -m agent.eyes baseline               # saves state/baseline.json
python -m agent.eyes snapshot               # should report nothing abnormal

# the checkpoint: start watching, then inject the practice incident in another terminal
python -m agent.eyes --record recordings/practice1.jsonl watch
```

Output is printed and saved in `runs/<time>/`: `summary.md`, `ledger.json`, `snapshot.json`, `alert.json`.

Without Docker (on any machine): `python -m agent.eyes --replay tests/fixtures/payment_failure.jsonl snapshot`.

Run one tool by hand: `python -m agent.eyes tool search_traces '{"service": "checkout"}'`.

## The tools (all read-only, all redacted, all stored in the ledger)

| Tool | Source | Facts it produces |
|---|---|---|
| `service_health_overview` | span metrics | `req_rate`, `error_ratio`, `p95_ms` per service vs baseline; `missing_telemetry` |
| `service_detail(service)` | span metrics | the same per operation, e.g. only `EmptyCart` fails |
| `hop_latency(caller, callee)` | Jaeger traces | `hop_gap_ms` (client minus server time), client and server `p95_ms`, vs baseline |
| `search_traces(service)` | Jaeger traces | `error_count` per operation, `error_origin` (deepest failing span), `error_message`, `error_concentration`; onset = first error span |
| `container_health(service?)` | Docker inspect and stats | `status`, `restarts`, `oom_killed`, `mem_limit` / `cpu_limit` vs compose and baseline, `mem_used`, `cpu_used`, `uptime_s` |
| `search_logs(service)` | docker logs, plus OpenSearch when reachable | `log_count` per level, top `error_message`s; onset = first ERROR line |
| `onset(service, signal)` | Prometheus query_range | when the signal first crossed its threshold for 2 points in a row |
| `service_dependencies` | Jaeger dependencies | call edges, `missing_edge` for edges present at baseline but gone now |
| `deploy_history(service?)` | Docker events | `deploy_event`: create, start, restart, stop, kill, die (exit code), oom, update (limits), unhealthy |

`agent/tools/registry.py` exposes exactly these nine to the LLM with JSON schemas, validates every argument, and `assert_read_only()` proves at startup that nothing else is exposed.

## Other pieces

- **Redaction** (`agent/tools/redact.py`, patterns in `config/redaction.yaml`): drops `feature_flag` span events and attributes, drops flagd and flagd-ui and every call to them (spans, series, edges), masks flag wording and all 20 flag keys. The ledger scrubs again and refuses anything still carrying flag data. Recordings are scrubbed before they touch disk.
- **Ledger** (`agent/ledger.py`): append-only, IDs `E1, E2, ...`, returns copies, saves to JSON.
- **Baseline** (`agent/baseline.py`): per-service numbers over a 10 min quiet window, container restarts and limits, the healthy call graph. `--offset-min` ends the window in the past, so a baseline works even when started after a fault.
- **Alert watcher** (`agent/detect.py`): polls every 15 s; fires after 2 consecutive abnormal polls. Thresholds are in `config/thresholds.yaml` and shared with triage and verify through `agent/anomaly.py`.
- **Triage** (`agent/triage.py`): every tool once, then onset, traces and logs for the 4 most abnormal services.
- **Verify** (`agent/verify.py`): re-polls for up to 180 s, needs 3 healthy polls in a row, and returns `remaining_symptoms` for combined faults. A restart done as the fix is not counted as a new symptom.
- **Record and replay** (`agent/tools/backends.py`): `--record FILE` saves every backend response, `--replay FILE` runs from it. B, C and D can build and test against recordings of real incidents without the demo running.

## Interface changes for the team (announce before merging)

- `SIGNALS` gains `error_count`, `error_origin`, `error_message`, `log_count`, `deploy_event`, `uptime_s`.
- `Evidence.id` defaults to `""` because the ledger assigns it.
- Tools take a `Toolbox(backends, ledger, baseline)`; each call stores its evidence and returns it with IDs.
- `verify.check(tb, alert, window_s)` takes the toolbox as its first argument.
- `ServiceStatus.onset_ts` keys: the metric signal names, plus `first_error_span` and `first_error_log`.

**For B:** within one failing request every span on the chain errors and the caller's span starts first, so span start order says nothing about cause. Use `error_origin` (the deepest failing span) for that; use `first_error_span` only to compare separate faults.

## Still to check live (needs the demo running)

1. Span metrics: does the frontend appear with `span_kind` SERVER, and how often do samples arrive (sets whether `rate_window: 2m` is enough)?
2. Jaeger: does `/api/dependencies` return edges under Jaeger v2? If not, the tool falls back to edges read from frontend traces.
3. Error spans: confirm `error=true` or `otel.status_code=ERROR` tags and where `exception.message` sits.
4. OpenSearch field names (`config/endpoints.yaml`, `opensearch_fields`) against one real document.
5. Which services print to `docker logs` at all.
6. Run `python -m agent.eyes snapshot` with each flag on and check the ledger for any flag text (the tests cover the leaks we know of).

## Not done yet

- The offline incident bundle adapter: needs the organisers' bundle format. Replay files are the stand-in.
- Deploy history does not read the organisers' Action API log yet; it will once the format is known.
- Prometheus container metrics (memory trend over time for leak vs starvation) are not used; `docker stats` gives the current value only.
