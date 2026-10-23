"""The model receives these read-only tools, never Docker or shell access."""

import json
import subprocess
import time
from datetime import datetime, timezone

from agent.tools import redact
from incident_zero.telemetry import SERVICES, LocalJsonClient, TelemetryError, observe

HEALTH_SERVICES = (*SERVICES, "astronomy-db", "valkey-cart")


def record(service, kind, observations, source, start, end, query=None, status=None):
    return {"service": service, "kind": kind, "source_url": source,
            "query": query or {}, "window_start_unix": start, "window_end_unix": end,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "status": status or ("observed" if observations else "no_data"), "observations": observations}


class LiveTools:
    mode = "live"

    def __init__(self, prometheus="http://localhost:9090", jaeger="http://localhost:8080/jaeger/ui", opensearch=None, container_health=False):
        from incident_zero.eyes_tools import EYES_TOOLS, EyesTools  # imported here: eyes_tools imports this module
        self.prometheus = LocalJsonClient(prometheus)
        self.jaeger = LocalJsonClient(jaeger)
        self.logs = LocalJsonClient(opensearch) if opensearch else None
        self.container_health_enabled = container_health
        self.eyes = EyesTools(docker_enabled=container_health, urls={"prometheus_url": prometheus, "jaeger_url": jaeger})
        self.extra_tools = tuple(EYES_TOOLS)
        self.guard = lambda: False

    def set_guard(self, guard):
        self.guard = guard
        self.eyes.guard = guard
        for client in (self.prometheus, self.jaeger, self.logs):
            if client is not None:
                client.guard = guard

    def inspect_service(self, service, lookback, end):
        return [{**item, "service": service} for item in observe(service, lookback, self.prometheus, self.jaeger, end=end)["evidence"]]

    def read_logs(self, service, lookback, end):
        if self.logs is None:
            raise TelemetryError("OpenSearch is not configured; set --opensearch-url to its local published address")
        query = {"size": 20, "sort": [{"observedTimestamp": "desc"}],
                 "_source": ["observedTimestamp", "severity.text", "body", "traceId", "spanId"],
                 "query": {"bool": {"filter": [
                     {"term": {"resource.service.name.keyword": service}},
                     {"range": {"observedTimestamp": {"gte": int((end-lookback)*1000), "lte": int(end*1000), "format": "epoch_millis"}}},
                 ], "must_not": [{"match_phrase": {"body": "feature_flag"}}, {"match_phrase": {"body": "flagd"}}]}}}
        payload = self.logs.get("/otel-logs-*/_search", {"source": json.dumps(query), "source_content_type": "application/json"})
        if payload.get("error") or payload.get("timed_out") or payload.get("_shards", {}).get("failed", 0):
            raise TelemetryError("OpenSearch returned an incomplete or failed query")
        hits = payload.get("hits", {}).get("hits")
        if not isinstance(hits, list):
            raise TelemetryError("Malformed OpenSearch log response")
        observations = []
        for hit in hits[:20]:
            source = hit.get("_source", {})
            body = source.get("body", "")
            text = body if isinstance(body, str) else json.dumps(body)
            if any(word in text.lower() for word in ("feature_flag", "feature flag", "flagd", "api_key", "password", "authorization", "card_number")):
                continue
            observations.append({"log_id": hit.get("_id"), "timestamp": source.get("observedTimestamp"),
                                 "body": redact.mask(text[:1500]), "severity": source.get("severity", {}).get("text", source.get("severity.text")),
                                 "trace_id": source.get("traceId")})
        return [record(service, "recent_logs", observations, self.logs.base_url + "/otel-logs-*/_search", end-lookback, end, query)]

    def read_container_health(self, service, lookback, end):
        if self.guard():
            raise TelemetryError("Kill switch active")
        if not self.container_health_enabled:
            raise TelemetryError("Container health tool is disabled; enable explicitly with --container-health")
        template = '{"state":{{json .State}},"restart_count":{{.RestartCount}}}'
        try:
            result = subprocess.run(["docker", "inspect", "--format", template, service], capture_output=True, text=True, timeout=8, check=False)
            if result.returncode:
                raise TelemetryError("Container inspection failed; check Docker access and container existence")
            data = json.loads(result.stdout)
            state = data["state"]
            health = {"status": state.get("Status"), "running": state.get("Running"),
                      "oom_killed": state.get("OOMKilled"), "restarting": state.get("Restarting"),
                      "exit_code": state.get("ExitCode"), "health": state.get("Health", {}).get("Status"),
                      "restart_count": data["restart_count"]}
            return [record(service, "container_health", [health], "docker://" + service + "/state", end, end)]
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
            raise TelemetryError("Container health unavailable") from exc

    def read_resources(self, service, lookback, end):
        if self.guard():
            raise TelemetryError("Kill switch active")
        if not self.container_health_enabled:
            raise TelemetryError("Docker resource sampling is disabled")
        result = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", service], capture_output=True, text=True, timeout=8, check=False)
        if result.returncode:
            raise TelemetryError("Docker resource sampling unavailable")
        data = json.loads(result.stdout)
        usage = {"cpu_percent": float(data["CPUPerc"].rstrip("%")),
                 "memory_percent": float(data["MemPerc"].rstrip("%"))}
        if any(not __import__("math").isfinite(value) or value < 0 for value in usage.values()):
            raise TelemetryError("Invalid resource sample")
        return [record(service, "container_resources", [usage], "docker://" + service + "/stats", end, end)]

    def call(self, name, service, lookback, end):
        if name in self.extra_tools:
            if not 60 <= lookback <= 3600:
                raise ValueError("Invalid service or query window")
            return self.eyes.call(name, service, lookback, end)
        if name not in {"inspect_service", "read_logs", "read_container_health", "read_resources"}:
            raise ValueError("Unknown read-only tool")
        allowed = HEALTH_SERVICES if name in {"read_container_health", "read_resources"} else SERVICES
        if service not in allowed or not 60 <= lookback <= 3600:
            raise ValueError("Invalid service or query window")
        return getattr(self, name)(service, lookback, end)


class BundleTools:
    mode = "synthetic"

    def __init__(self, bundle):
        if bundle.get("synthetic") is not True:
            raise ValueError("Bundle adapter currently accepts explicitly synthetic development bundles only")
        self.bundle = bundle
        self.recovered = False

    def call(self, name, service, lookback, end):
        if service not in HEALTH_SERVICES or not 60 <= lookback <= 3600:
            raise ValueError("Invalid service or query window")
        kinds = {"inspect_service": {"server_calls_per_second", "server_errors_per_second", "server_p95_milliseconds", "recent_traces"},
                 "read_logs": {"recent_logs"}, "read_container_health": {"container_health"}, "read_resources": {"container_resources"}}
        if name not in kinds:
            raise ValueError("Unknown tool")
        key = "after" if self.recovered else "before"
        rows = self.bundle.get(key, {}).get(service, [])
        return [record(service, row["kind"], row["observations"], "synthetic://development/" + service, end-lookback, end)
                for row in rows if row["kind"] in kinds[name]]
