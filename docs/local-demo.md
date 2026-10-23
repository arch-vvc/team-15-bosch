# Local demo setup

The upstream checkout is recorded in `demo-source.json`. It is separate from the Incident Zero agent code. Upstream source and configuration have not been edited. This development setup is not yet the organizer-pinned environment.

## Start

From the workspace root:

```sh
cd opentelemetry-demo
docker compose --parallel 3 -f compose.yaml -f compose.observability.yaml up -d --no-build --wait --wait-timeout 180
```

This uses the core demo plus observability, without the additional Kafka/accounting/fraud layer or the demo's separate shopping chatbot/agent. It includes the upstream load generator. Docker has approximately 7.65 GiB allocated on this machine. Full image downloads can take substantial time on this connection. The `--wait-timeout` applies to readiness after image pulling; it does not bound download duration.

The current startup command was launched with logs at `/private/tmp/incident-zero-compose.log`. A launched command or partially downloaded images do not mean the application is running. Confirm readiness with the commands below.

## Inspect

```sh
docker compose -f compose.yaml -f compose.observability.yaml ps -a
docker compose -f compose.yaml -f compose.observability.yaml logs --tail 30 otel-collector
```

Inspect only telemetry/health while developing the investigator. Do not use failure-flag configuration, flag service logs, or injection state as diagnosis inputs.

Once running, expected addresses from the checked-out configuration are:

| Component | Address |
| --- | --- |
| Store | http://localhost:8080/ |
| Jaeger UI and query base | http://localhost:8080/jaeger/ui/ |
| Grafana | http://localhost:8080/grafana/ |
| Prometheus | http://localhost:9090/ |

Endpoints are configuration-derived and must be checked after startup. Do not mistake the collector's OTLP ingestion endpoint for a telemetry query API. OpenSearch publishes a dynamic host port; discover it using `docker compose -f compose.yaml -f compose.observability.yaml port opensearch 9200` when adding the log adapter.

## Observe

From the workspace root after startup:

```sh
python3.11 -m incident_zero observe --service checkout --lookback 300 --output output/checkout-observation.json
```

This queries four evidence sources (with a v3 fallback request when the legacy Jaeger route returns 404): server call rate, server error rate, server p95 span latency, and a bounded recent-trace query. Queries follow the metrics used in the upstream span-metrics dashboard. Only allowlisted application services can be selected. The adapter accepts loopback HTTP URLs, refuses redirects, sets an eight-second timeout per request, and caps each response at 2 MiB.

Each evidence record includes source URL, exact query, time window, collection time, and an evidence identifier. Trace projection excludes other services' spans and raw attributes/log payloads, including flag-evaluation attributes. Trace samples are bounded to ten traces and one hundred matching spans.

Interpretation:

- `observed`: actual finite metric samples or matching spans were returned.
- `no_data`: query succeeded but yielded no usable observations. This does not mean healthy or zero errors.
- `unavailable`: endpoint, transport, or response parsing failed. Other evidence is retained.

The command exits 1 when any backend query is unavailable. It exits 0 for successful queries, including empty queries; always inspect evidence status. These are development observations, not official incident reports. Do not claim root cause or recovery from these snapshots alone. The rules-first investigation command separately adds logs, container health, resource snapshots and gated model reasoning. Live remediation still needs the reviewed organizer Action API contract.

## Stop

From `opentelemetry-demo`, use `docker compose -f compose.yaml -f compose.observability.yaml stop` when you are done. Stopping services is an explicit local setup operation, not an agent remedy. During evaluated incidents all agent changes must use the organizer Action API after human approval.


Live verification on 2026-10-23: metrics, Jaeger v3 traces, OpenSearch logs and container health were observed successfully. Jaeger v3 uses bounded newline-delimited OTLP responses; the adapter retains legacy support and switches on HTTP 404.
