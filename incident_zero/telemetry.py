"""Bounded read-only access to the demo's local telemetry backends."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SERVICES = (
    "ad", "cart", "checkout", "currency", "email", "frontend", "frontend-proxy",
    "image-provider", "payment", "product-catalog", "quote", "recommendation", "shipping",
)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class TelemetryError(Exception):
    pass


class TelemetryHTTPError(TelemetryError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Telemetry endpoint returned HTTP {status_code}")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TelemetryError("Telemetry endpoint redirected; check the configured base URL")


class LocalJsonClient:
    def __init__(self, base_url: str):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        ):
            raise ValueError("Telemetry base URL must be an HTTP loopback URL without credentials, query, or fragment")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("Invalid telemetry port") from exc
        self.guard = lambda: False
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def get(self, path: str, params: dict | None = None) -> dict:
        if not path.startswith("/") or path.startswith("//") or ".." in path or "?" in path:
            raise ValueError("Invalid telemetry query path")
        if self.guard():
            raise TelemetryError("Kill switch active before telemetry request")
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with self.opener.open(request, timeout=8) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TelemetryError("Telemetry response exceeded the 2 MiB limit; narrow the query")
            if path == "/api/v3/traces":
                data = {"frames": [json.loads(line) for line in raw.splitlines() if line.strip()]}
            else:
                data = json.loads(raw)
            if not isinstance(data, dict):
                raise TelemetryError("Telemetry response must be a JSON object")
            return data
        except HTTPError as exc:
            raise TelemetryHTTPError(exc.code) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise TelemetryError(f"Telemetry query failed: {exc}") from exc


def metric_queries(service: str, lookback: int) -> dict[str, str]:
    selector = f'service_name="{service}",span_kind="SPAN_KIND_SERVER"'
    calls = f'traces_span_metrics_calls_total{{{selector}}}'
    errors = f'traces_span_metrics_calls_total{{{selector},status_code="STATUS_CODE_ERROR"}}'
    return {
        "server_calls_per_second": f"sum(rate({calls}[{lookback}s]))",
        "server_errors_per_second": f"sum(rate({errors}[{lookback}s])) or (sum(rate({calls}[{lookback}s])) * 0)",
        "server_p95_milliseconds": (
            "histogram_quantile(0.95, sum by (le) (rate("
            f"traces_span_metrics_duration_milliseconds_bucket{{{selector}}}[{lookback}s])))"
        ),
    }


def metric_values(payload: dict) -> list[dict]:
    if payload.get("status") != "success":
        raise TelemetryError("Prometheus did not return a successful query")
    data = payload.get("data", {})
    if data.get("resultType") != "vector" or not isinstance(data.get("result"), list):
        raise TelemetryError("Expected a Prometheus instant vector")
    values = []
    for row in data["result"][:500]:
        try:
            timestamp, value = row["value"]
            numeric = float(value)
            if math.isfinite(numeric):
                values.append({"timestamp": float(timestamp), "value": numeric})
        except (KeyError, ValueError, TypeError) as exc:
            raise TelemetryError("Malformed Prometheus sample") from exc
    return values


def _calls_flag_service(operation, tags) -> bool:
    """A span that is a lookup against the flag service (flagd). Dropped: the agent must not
    see flag evaluations, and they are never a cause it may act on."""
    from agent.tools import redact
    return redact.is_flag_system(operation) or any(
        redact.is_flag_system(str(tags.get(k) or "")) for k in ("peer.service", "server.address", "rpc.service"))


def trace_spans(payload: dict, service: str) -> list[dict]:
    if payload.get("errors"):
        raise TelemetryError("Jaeger reported a query error")
    if not isinstance(payload.get("data"), list):
        raise TelemetryError("Expected a Jaeger trace list")
    result = []
    for trace in payload["data"][:10]:
        processes = trace.get("processes", {})
        for span in trace.get("spans", []):
            if processes.get(span.get("processID"), {}).get("serviceName") != service:
                continue
            # Project only timing, identifiers and status; exclude feature-flag attributes and log payloads.
            tags = {tag.get("key"): tag.get("value") for tag in span.get("tags", [])}
            if _calls_flag_service(span.get("operationName"), tags):
                continue
            result.append({
                "trace_id": trace.get("traceID"), "span_id": span.get("spanID"),
                "service": service, "operation": span.get("operationName"),
                "start_time_us": span.get("startTime"), "duration_us": span.get("duration"),
                "peer_service": tags.get("peer.service") or tags.get("server.address"),
                "child_services": sorted({processes.get(child.get("processID"), {}).get("serviceName")
                    for child in trace.get("spans", [])
                    if any(ref.get("spanID") == span.get("spanID") and ref.get("refType") == "CHILD_OF" for ref in child.get("references", []))
                    and processes.get(child.get("processID"), {}).get("serviceName") in (*SERVICES, "astronomy-db", "valkey-cart")
                    and processes.get(child.get("processID"), {}).get("serviceName") != service}),
                "rpc_service": tags.get("rpc.service"),
                "parent_span_ids": [reference.get("spanID") for reference in span.get("references", []) if reference.get("refType") == "CHILD_OF"],
                "error": tags.get("error") is True or tags.get("otel.status_code") == "ERROR",
            })
            if len(result) >= 100:
                return result
    return result


def otlp_trace_spans(payload, service):
    """Project Jaeger v3's newline-delimited OTLP JSON without exposing raw attributes."""
    spans = []
    for frame in payload.get("frames", []):
        if frame.get("error"):
            raise TelemetryError("Jaeger v3 reported a stream error")
        resources = frame.get("result", {}).get("resourceSpans")
        if not isinstance(resources, list):
            raise TelemetryError("Malformed Jaeger v3 trace frame")
        for resource in resources:
            name = next((a.get("value", {}).get("stringValue") for a in resource.get("resource", {}).get("attributes", []) if a.get("key") == "service.name"), None)
            if name not in (*SERVICES, "astronomy-db", "valkey-cart"):
                continue
            for scope in resource.get("scopeSpans", []):
                spans.extend((name, span) for span in scope.get("spans", []))
    children = {}
    for name, span in spans:
        children.setdefault((span.get("traceId"), span.get("parentSpanId")), set()).add(name)
    result = []
    for name, span in spans:
        if name != service:
            continue
        attrs = {a.get("key"): a.get("value", {}).get("stringValue") for a in span.get("attributes", [])
                 if a.get("key") in {"peer.service", "server.address", "rpc.service"}}
        if _calls_flag_service(span.get("name"), attrs):
            continue
        start, end = int(span.get("startTimeUnixNano", 0)), int(span.get("endTimeUnixNano", 0))
        result.append({"trace_id": span.get("traceId"), "span_id": span.get("spanId"), "service": name,
            "operation": span.get("name"), "start_time_us": start / 1000, "duration_us": (end-start) / 1000,
            "peer_service": attrs.get("peer.service") or attrs.get("server.address"), "rpc_service": attrs.get("rpc.service"),
            "child_services": sorted(children.get((span.get("traceId"), span.get("spanId")), set()) - {name}),
            "parent_span_ids": [span["parentSpanId"]] if span.get("parentSpanId") else [],
            "error": span.get("status", {}).get("code") in (2, "STATUS_CODE_ERROR")})
        if len(result) >= 100:
            break
    return result


def observe(service: str, lookback: int, prometheus: LocalJsonClient, jaeger: LocalJsonClient,
            end: float | None = None) -> dict:
    if service not in SERVICES:
        raise ValueError("Select an allowlisted application service")
    if not 60 <= lookback <= 3600:
        raise ValueError("Lookback must be between 60 and 3600 seconds")
    end = time.time() if end is None else end
    start = end - lookback
    evidence = []

    def collect(kind: str, client: LocalJsonClient, path: str, params: dict, parse):
        record = {
            "kind": kind, "source_url": client.base_url + path, "query": params,
            "window_start_unix": start, "window_end_unix": end,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            try:
                payload = client.get(path, params)
            except TelemetryHTTPError as exc:
                if kind != "recent_traces" or exc.status_code != 404:
                    raise
                path = "/api/v3/traces"
                params = {"query.serviceName": service,
                          "query.startTimeMin": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                          "query.startTimeMax": datetime.fromtimestamp(end, timezone.utc).isoformat(),
                          "query.searchDepth": 3}
                record.update(source_url=client.base_url + path, query=params)
                payload = client.get(path, params)
                parse = lambda data: otlp_trace_spans(data, service)
            record["observations"] = parse(payload)
            record["status"] = "observed" if record["observations"] else "no_data"
            record["warnings"] = payload.get("warnings", [])
        except (TelemetryError, KeyError, TypeError, AttributeError, ValueError) as exc:
            record.update(status="unavailable", observations=[], error=str(exc))
        digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:16]
        record["evidence_id"] = "ev_" + digest
        evidence.append(record)

    for name, query in metric_queries(service, lookback).items():
        collect(name, prometheus, "/api/v1/query", {"query": query, "time": end}, metric_values)
    collect("recent_traces", jaeger, "/api/traces", {
        "service": service, "start": int(start * 1_000_000),
        "end": int(end * 1_000_000), "limit": 10,
    }, lambda payload: trace_spans(payload, service))
    return {
        "report_type": "development_telemetry_observation",
        "official_incident_schema": False, "read_only": True, "service": service,
        "window_start_unix": start, "window_end_unix": end,
        "evidence": evidence,
        "limitations": [
            "This is a telemetry snapshot, not an AI diagnosis or a recovery claim.",
            "Missing samples are not proof of health. Metrics may need several export intervals to appear.",
            "Recent traces are a bounded sample, not an exhaustive incident history.",
            "This snapshot contains metrics and traces; the investigation pipeline separately collects logs, health and resource samples.",
        ],
    }
