# Incident Zero: query cookbook

The exact queries behind each read-only tool, for P1 to turn into code on the day. Endpoints and metric names come from the demo's config files (23 Oct 2026). Anything marked **verify** must be checked once the demo is running.

## Endpoints (from the host, minimal mode)
| Backend | URL | Notes |
|---|---|---|
| Prometheus | `http://localhost:9090/api/v1/` | Published on a fixed port (`9090:9090`). |
| Jaeger API | `http://localhost:8080/jaeger/ui/api/` | Through the Envoy proxy; Jaeger's base path is `/jaeger/ui`. |
| OpenSearch | `http://localhost:<port>/otel-logs-*/_search` | **No fixed host port.** Find it with `docker port opensearch 9200`. Index is `otel-logs-yyyy-MM-dd`. |
| Docker | Docker SDK / `docker inspect`, `docker logs` | Unix socket. |

**Never call** `/flagservice/`, `/feature`, or anything on `flagd` / `flagd-ui`. In every Prometheus query, filter out flag services with `service_name!~"flagd|flagd-ui"`.

---

## Tool 1: `service_health_overview()`
One call gives a table of every service. Used by triage (step 2) and the alert watcher (step 1).

```promql
# A. request rate per service (server spans), req/s
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER",service_name!~"flagd|flagd-ui"}[2m]))

# B. error ratio per service
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER",status_code="STATUS_CODE_ERROR",service_name!~"flagd|flagd-ui"}[2m]))
  / sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER",service_name!~"flagd|flagd-ui"}[2m]))

# C. p95 latency per service, ms
histogram_quantile(0.95, sum by (le, service_name) (rate(traces_span_metrics_duration_milliseconds_bucket{span_kind="SPAN_KIND_SERVER",service_name!~"flagd|flagd-ui"}[2m])))
```
- **Output per service:** `{service, req_rate, error_ratio, p95_ms}`, plus the same figures for the **baseline window** (Tool 7) so the change is visible.
- **Gotcha:** a service with **no rows** is either idle or dead. Cross-check with Tool 5 before calling it healthy.
- **Verify:** whether frontend spans are `SPAN_KIND_SERVER` (Next.js HTTP) or need `span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"`.

## Tool 2: `service_detail(service)`
Breaks one service down by operation, which shows *where* inside it things fail.

```promql
# errors and latency per operation
sum by (span_name) (rate(traces_span_metrics_calls_total{service_name="$S",status_code="STATUS_CODE_ERROR"}[2m]))
sum by (span_name) (rate(traces_span_metrics_calls_total{service_name="$S"}[2m]))
histogram_quantile(0.95, sum by (le, span_name) (rate(traces_span_metrics_duration_milliseconds_bucket{service_name="$S"}[2m])))
```
Catches "only EmptyCart fails" (cart) and "only GetProduct fails" (product-catalog).

## Tool 3: `hop_latency(caller, callee)`
The network-fault check: client-side vs. server-side latency for one hop.

```promql
# caller's view of the call (includes network)
histogram_quantile(0.95, sum by (le) (rate(rpc_client_duration_milliseconds_bucket{service_name="$CALLER",rpc_service=~".*$CALLEE_SVC.*"}[2m])))
# callee's own processing time
histogram_quantile(0.95, sum by (le) (rate(rpc_server_duration_milliseconds_bucket{service_name="$CALLEE"}[2m])))
```
- **Fact:** `gap_ms = client_p95 − server_p95`. A large gap with a normal server time means **network**. A slow server means **the callee itself is slow**.
- **Verify:** the `rpc_service` label values (e.g. `oteldemo.ShippingService`), and that Go/Rust/Node services emit `rpc_client_*` (some may emit `http_client_*` instead).
- **Fallback:** the same comparison from Jaeger, using the client span duration minus the server child span duration in one trace.

## Tool 4: `search_traces(service, errors_only=True, operation=None, lookback="15m", limit=20)`
```
GET http://localhost:8080/jaeger/ui/api/traces
    ?service=$S&operation=$OP&tags={"error":"true"}&lookback=15m&limit=20
```
- **Extract per error span** (not whole traces): `traceID, spanID, serviceName, operationName, startTime (µs), duration (µs), status/error tags, exception.message`, and the **parent service** from `references` (CHILD_OF), which shows who called it.
- **Aggregate before handing it to the ledger or LLM:**
  - error count per operation,
  - top 3 distinct error messages (after redaction),
  - **earliest error `startTime`**, which is the onset for temporal reasoning,
  - **attribute concentration**: when one value of `demo.product.id`, `user.id` or `demo.user_context.loyalty_level` covers 90% or more of the errors, record it as a fact.
  - Keep 2–3 example trace IDs as citable evidence.
- **Redaction (mandatory):** drop span events or logs named `feature_flag`; drop tags whose key contains `feature_flag`; mask `(?i)feature[ _.-]?flag|flagd|openfeature` and the flag key names in every string. See `fault-signatures.md`, section 2.

## Tool 5: `container_health(service=None)`
```
docker inspect <name>  →  State.Status, State.Running, State.StartedAt, State.ExitCode,
                          State.OOMKilled, RestartCount, HostConfig.Memory, HostConfig.NanoCpus
```
```promql
# live usage (docker_stats, 10 s resolution) — VERIFY the exact names
container_cpu_utilization_ratio{container_name="$S"}
container_memory_usage_total_bytes{container_name="$S"}
container_memory_percent_ratio{container_name="$S"}
```
- **Facts:**
  - `status` (running or exited),
  - `restarts` (and the change since baseline),
  - `oom_killed`,
  - `uptime_s`,
  - `mem_used / mem_limit`,
  - `cpu_used`,
  - `cpu_limit`. If `NanoCpus` is 0 there's no limit; otherwise cores = NanoCpus / 1e9.
- **Compare the limits with `compose.yaml`.** A limit lower than the compose value means **resource starvation was injected**.
- **Restarts are not a metric**, so they come only from inspect. Snapshot them at baseline and compare.
- Exclude `flagd` and `flagd-ui` here as well.

## Tool 6: `search_logs(service, pattern=None, level=None, since="10m")`
Option A, light: `docker logs --since 10m --tail 300 <service>`, then grep for the pattern or level.
Option B, richer: OpenSearch.
```json
POST /otel-logs-*/_search
{ "size": 50, "sort": [{"@timestamp": "asc"}],
  "query": { "bool": { "filter": [
    { "term": { "resource.service.name": "$S" } },
    { "range": { "@timestamp": { "gte": "now-10m" } } },
    { "terms": { "severity.text": ["ERROR", "WARN", "Error", "Warning"] } } ] } } }
```
- **Verify:** the field names (`resource.service.name`, `severity.text`, `body`, `traceId`); check one document first. Also check whether a given service's logs reach `docker logs` at all, since some services send logs only through OTLP.
- **Output:** count by level, the top 5 distinct messages (redacted and de-duplicated), and the timestamp of the first ERROR, which is another onset source.
- Redact before returning, as in Tool 4.

## Tool 7: `baseline()` and `onset(signal, service)`
**Baseline:** run Tool 1 over a quiet window with no fault, for example `[10m]` ending before the alert, or the stored figures from the startup snapshot.

**Onset from metrics:**
```
GET /api/v1/query_range?query=<error-ratio or p95 expr for $S>&start=<now-30m>&end=<now>&step=15s
```
`onset_ts` is the first point where the value exceeds `max(3 × baseline, baseline + 0.05)` for error ratio (or `2 × baseline` for p95) **and** stays above it for 2 consecutive points.

**Onset from traces (preferred for precedence):** the earliest error span `startTime` from Tool 4, which has microsecond precision.

**Precedence fact:** `A_before_B_by_s = onset_B − onset_A`. Treat anything under one metric step (about 15–60 s) as a **tie**, which is neutral.

## Tool 8: `service_dependencies(lookback="15m")`
```
GET http://localhost:8080/jaeger/ui/api/dependencies?endTs=<now_ms>&lookback=<900000>
```
- Returns `[{parent, child, callCount}]`, which is the call graph used by "Find suspects". Drop edges involving `flagd`.
- Cache it from a healthy period, because during an outage edges to a dead service disappear, and that **missing edge is itself evidence**.

## Data-store health (part of Tool 5)
```promql
redis_clients_connected            # valkey-cart; series disappears if valkey is down   (verify names)
redis_memory_used_bytes
postgresql_backends                # astronomy-db connections
db_client_operation_duration_seconds_bucket{service_name="product-catalog"}   # DB call latency
nginx_requests_total               # image-provider
httpcheck_status{http_url=~".*frontend-proxy.*"}   # proxy reachable (verify name)
```

---

## Turning results into ledger evidence (typed facts)
Each tool returns evidence items such as:
```json
{ "id": "E7", "tool": "service_detail", "query": {"service": "payment"},
  "ts": "<query time>", "onset_ts": "<first anomalous point or null>",
  "facts": [ {"service": "payment", "signal": "error_ratio", "scope": "Charge",
              "value": 0.48, "baseline": 0.0, "direction": "up"} ],
  "examples": ["trace:4bf92f35…"], "redacted": true }
```
**Signal vocabulary**, kept small so the validator can check claims:
`error_ratio, p95_ms, req_rate, hop_gap_ms, cpu_used, cpu_limit, mem_used, mem_limit, restarts, oom_killed, status, gc_time, error_concentration, missing_telemetry, missing_edge`.

---

## Live verification checklist (first 15 minutes with the demo up)
- [ ] `curl -s localhost:9090/api/v1/label/__name__/values | tr ',' '\n' | grep -E "container_|redis_|postgresql_|httpcheck|jvm_gc|rpc_client|rpc_server|span_metrics"`, and record the real names here.
- [ ] Tool 1 queries return all 14 shop services, and no flagd rows.
- [ ] `curl -s localhost:8080/jaeger/ui/api/services` lists the services.
- [ ] One error-trace search works, and you've checked a raw span for `feature_flag` events and tags.
- [ ] `docker port opensearch 9200`, then fetch one log document and note its field names.
- [ ] `docker logs --tail 20 <svc>` for each service: which ones actually print logs?
- [ ] Record baseline numbers (req_rate, error_ratio, p95) per service with no fault running.
