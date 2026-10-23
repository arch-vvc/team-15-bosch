"""Thin read-only clients for Prometheus, Jaeger, Docker and OpenSearch, plus a clock.

Tools never talk to the network directly; they go through a Backends object. That makes
three modes possible with the same tool code:
  live    the real demo on this laptop,
  record  live, and every response is also saved to a JSONL file,
  replay  responses come from such a file (tests, offline bundles, demos without Docker).

Nothing in here can change the system: only GET-style queries, inspect, stats, logs and events.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from agent import config
from agent.tools import redact


class BackendError(RuntimeError):
    pass


class Clock:
    def now(self) -> float:
        return datetime.now(timezone.utc).timestamp()


class Prometheus:
    def __init__(self, url: str, http: httpx.Client):
        self.url, self.http = url.rstrip("/"), http

    def _get(self, path: str, params: dict) -> Any:
        for attempt in (1, 2):
            try:
                r = self.http.get(f"{self.url}/api/v1/{path}", params=params)
                r.raise_for_status()
                body = r.json()
                break
            except httpx.TimeoutException as e:
                # a busy Prometheus on a small laptop is often slow once, then fine
                if attempt == 2:
                    raise BackendError(f"prometheus {path}: timed out twice ({e})") from e
            except (httpx.HTTPError, ValueError) as e:
                raise BackendError(f"prometheus {path}: {e}") from e
        if body.get("status") != "success":
            raise BackendError(f"prometheus {path}: {body.get('error')}")
        return body["data"]["result"]

    def query(self, expr: str, at: float | None = None) -> list[dict]:
        params = {"query": expr} | ({"time": at} if at is not None else {})
        return self._get("query", params)

    def query_range(self, expr: str, start: float, end: float, step: float) -> list[dict]:
        return self._get("query_range", {"query": expr, "start": start, "end": end, "step": step})


class Jaeger:
    """Jaeger's HTTP query API. Uses the classic /api/traces and falls back to /api/v3/traces
    (OTLP JSON, newline-delimited) when the classic path answers 404, converting the result
    to the classic shape so the tools only ever see one format."""

    def __init__(self, url: str, http: httpx.Client):
        self.url, self.http = url.rstrip("/"), http

    def _get(self, path: str, params: dict) -> Any:
        try:
            r = self.http.get(f"{self.url}/api/{path}", params=params)
            r.raise_for_status()
            return r.json().get("data") or []
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise NotFound(f"jaeger {path}: 404") from e
            raise BackendError(f"jaeger {path}: {e}") from e
        except (httpx.HTTPError, ValueError) as e:
            raise BackendError(f"jaeger {path}: {e}") from e

    def traces(self, service: str, operation: str | None = None, tags: dict | None = None,
               lookback: str = "15m", limit: int = 20, start_us: int | None = None,
               end_us: int | None = None) -> list[dict]:
        if redact.is_flag_system(service):
            raise BackendError("refusing to query a flag service")
        params: dict[str, Any] = {"service": service, "limit": limit, "lookback": lookback}
        if operation:
            params["operation"] = operation
        if tags:
            params["tags"] = json.dumps(tags)
        if start_us is not None and end_us is not None:
            params |= {"start": start_us, "end": end_us, "lookback": "custom"}
        try:
            return self._get("traces", params)
        except NotFound:
            return self._traces_v3(service, operation, tags, lookback, limit, start_us, end_us)

    def _traces_v3(self, service, operation, tags, lookback, limit, start_us, end_us) -> list[dict]:
        if start_us is None or end_us is None:
            end_us = int(datetime.now(timezone.utc).timestamp() * 1e6)
            start_us = end_us - _seconds(lookback) * 1_000_000
        iso = lambda us: datetime.fromtimestamp(us / 1e6, timezone.utc).isoformat()
        params = {"query.serviceName": service, "query.startTimeMin": iso(start_us),
                  "query.startTimeMax": iso(end_us), "query.searchDepth": limit}
        if operation:
            params["query.operationName"] = operation
        try:
            r = self.http.get(f"{self.url}/api/v3/traces", params=params)
            if r.status_code == 404:
                return []          # v3 answers 404 when nothing matched
            r.raise_for_status()
            frames = [json.loads(line) for line in r.text.splitlines() if line.strip()]
        except (httpx.HTTPError, ValueError) as e:
            raise BackendError(f"jaeger v3 traces: {e}") from e
        traces = otlp_to_traces(frames)
        if tags and tags.get("error") == "true":
            traces = [t for t in traces if any(
                (t["processes"].get(s["processID"]) or {}).get("serviceName") == service
                and any(tag["key"] == "error" for tag in s["tags"]) for s in t["spans"])]
        return traces[:limit]

    def dependencies(self, end_ms: int, lookback_ms: int) -> list[dict]:
        return self._get("dependencies", {"endTs": end_ms, "lookback": lookback_ms})

    def services(self) -> list[str]:
        return self._get("services", {})


class NotFound(BackendError):
    pass


def _seconds(lookback: str) -> int:
    unit = {"s": 1, "m": 60, "h": 3600}.get(lookback[-1:], None)
    return int(lookback[:-1]) * unit if unit else 900


SPAN_KINDS = {1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer",
              "SPAN_KIND_INTERNAL": "internal", "SPAN_KIND_SERVER": "server", "SPAN_KIND_CLIENT": "client",
              "SPAN_KIND_PRODUCER": "producer", "SPAN_KIND_CONSUMER": "consumer"}


def _otlp_value(v: dict) -> Any:
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in v:
            return v[key]
    if "intValue" in v:
        return int(v["intValue"])
    if "arrayValue" in v:
        return [_otlp_value(x) for x in v["arrayValue"].get("values", [])]
    return None


def _otlp_attrs(items: list[dict] | None) -> list[dict]:
    return [{"key": a.get("key"), "value": _otlp_value(a.get("value") or {})} for a in items or []]


def otlp_to_traces(frames: list[dict]) -> list[dict]:
    """Jaeger v3 OTLP frames -> the classic Jaeger trace shape (traceID, spans, processes)."""
    traces: dict[str, dict] = {}
    for frame in frames:
        for rs in (frame.get("result") or {}).get("resourceSpans") or []:
            resource = {a["key"]: a["value"] for a in _otlp_attrs((rs.get("resource") or {}).get("attributes"))}
            service = resource.get("service.name") or "?"
            for scope in rs.get("scopeSpans") or []:
                for sp in scope.get("spans") or []:
                    tid = sp.get("traceId")
                    trace = traces.setdefault(tid, {"traceID": tid, "spans": [], "processes": {}})
                    pid = next((p for p, proc in trace["processes"].items() if proc["serviceName"] == service), None)
                    if pid is None:
                        pid = f"p{len(trace['processes']) + 1}"
                        trace["processes"][pid] = {"serviceName": service, "tags": []}
                    tags = _otlp_attrs(sp.get("attributes")) + [{"key": "span.kind", "value": SPAN_KINDS.get(sp.get("kind"), "")}]
                    status = sp.get("status") or {}
                    if status.get("code") in (2, "STATUS_CODE_ERROR"):
                        tags += [{"key": "error", "value": True}, {"key": "otel.status_code", "value": "ERROR"}]
                    if status.get("message"):
                        tags.append({"key": "otel.status_description", "value": status["message"]})
                    start, end = int(sp.get("startTimeUnixNano") or 0), int(sp.get("endTimeUnixNano") or 0)
                    trace["spans"].append({
                        "traceID": tid, "spanID": sp.get("spanId"), "operationName": sp.get("name", ""),
                        "references": [{"refType": "CHILD_OF", "traceID": tid, "spanID": sp["parentSpanId"]}]
                        if sp.get("parentSpanId") else [],
                        "startTime": start // 1000, "duration": max(0, end - start) // 1000, "processID": pid,
                        "tags": tags,
                        "logs": [{"timestamp": int(ev.get("timeUnixNano") or 0) // 1000,
                                  "fields": [{"key": "event", "value": ev.get("name")}] + _otlp_attrs(ev.get("attributes"))}
                                 for ev in sp.get("events") or []],
                    })
    return list(traces.values())


def _docker_client():
    import docker  # imported lazily so tests and replay never need a Docker daemon

    if not os.environ.get("DOCKER_HOST"):
        mac_socket = Path.home() / ".docker/run/docker.sock"
        if not Path("/var/run/docker.sock").exists() and mac_socket.exists():
            return docker.DockerClient(base_url=f"unix://{mac_socket}")
    return docker.from_env()


class Docker:
    """Read-only Docker access: inspect, stats, logs, events, ports."""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                self._client = _docker_client()
            except Exception as e:
                raise BackendError(f"docker: {e}") from e
        return self._client

    def _get(self, name: str):
        if redact.is_flag_system(name):
            raise BackendError("refusing to touch a flag service")
        import docker

        try:
            return self.client.containers.get(name)
        except docker.errors.NotFound:
            return None

    def inspect(self, name: str) -> dict | None:
        c = self._get(name)
        return c.attrs if c else None

    def stats(self, name: str) -> dict | None:
        c = self._get(name)
        if c is None or c.status != "running":
            return None
        return c.stats(stream=False)

    def logs(self, name: str, since: float, tail: int) -> list[str]:
        c = self._get(name)
        if c is None:
            return []
        raw = c.logs(since=int(since), tail=tail, timestamps=True, stdout=True, stderr=True)
        return raw.decode("utf-8", errors="replace").splitlines()

    def events(self, since: float, until: float) -> list[dict]:
        try:
            return list(self.client.events(since=int(since), until=int(until), decode=True,
                                           filters={"type": "container"}))
        except Exception as e:
            raise BackendError(f"docker events: {e}") from e

    def host_port(self, name: str, port: int) -> int | None:
        c = self._get(name)
        if c is None:
            return None
        bindings = (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).get(f"{port}/tcp") or []
        return int(bindings[0]["HostPort"]) if bindings else None


class OpenSearch:
    def __init__(self, url: str, http: httpx.Client):
        self.url, self.http = url.rstrip("/"), http

    def search(self, index: str, body: dict) -> dict:
        try:
            r = self.http.post(f"{self.url}/{index}/_search", json=body)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise BackendError(f"opensearch: {e}") from e


# --- record and replay ------------------------------------------------------------------

# Arguments that change with the wall clock; replay matches calls without them.
VOLATILE_ARGS = {"at", "start", "end", "since", "until", "end_ms", "start_us", "end_us"}


def _key(backend: str, method: str, kwargs: dict) -> str:
    stable = {k: v for k, v in kwargs.items() if k not in VOLATILE_ARGS}
    return json.dumps([backend, method, stable], sort_keys=True, default=str)


class Recorder:
    def __init__(self, inner: Any, name: str, path: Path):
        self._inner, self._name, self._path = inner, name, path

    def __getattr__(self, method: str):
        fn = getattr(self._inner, method)
        if not callable(fn):
            return fn

        def call(**kwargs):
            try:
                result, error = fn(**kwargs), None
            except BackendError as e:
                result, error = None, str(e)
            # args are our own queries (they name flagd only to exclude it), so they are kept
            # verbatim for replay matching; responses are scrubbed before they touch disk
            line = {"backend": self._name, "method": method, "args": kwargs,
                    "result": redact.scrub(result), "error": error}
            with open(self._path, "a") as f:
                f.write(json.dumps(line, default=str) + "\n")
            if error:
                raise BackendError(error)
            return result

        return call


class Replayer:
    """Serves recorded responses in order. When a call's queue runs dry it keeps
    returning the last response, so polling loops settle instead of crashing."""

    def __init__(self, name: str, calls: dict[str, deque], last: dict[str, dict]):
        self._name, self._calls, self._last = name, calls, last

    def __getattr__(self, method: str):
        def call(**kwargs):
            key = _key(self._name, method, kwargs)
            queue = self._calls.get(key)
            entry = queue.popleft() if queue else self._last.get(key)
            if entry is None:
                raise BackendError(f"no recording for {self._name}.{method}({kwargs})")
            self._last[key] = entry
            if entry.get("error"):
                raise BackendError(entry["error"])
            return entry["result"]

        return call


@dataclass
class Backends:
    prom: Any
    jaeger: Any
    docker: Any
    opensearch: Any | None
    clock: Any

    def now(self) -> float:
        return self.clock.now()

    def now_dt(self) -> datetime:
        return datetime.fromtimestamp(self.now(), timezone.utc)

    @classmethod
    def live(cls, prometheus_url: str | None = None, jaeger_url: str | None = None,
             opensearch_url: str | None = None) -> "Backends":
        ep = config.endpoints() | {k: v for k, v in (("prometheus_url", prometheus_url), ("jaeger_url", jaeger_url),
                                                    ("opensearch_url", opensearch_url)) if v is not None}
        http = httpx.Client(timeout=ep.get("http_timeout_s", 10))
        docker_state = Docker()
        os_url = ep.get("opensearch_url") or ""
        if os_url == "auto":
            try:
                port = docker_state.host_port("opensearch", 9200)
            except BackendError:
                port = None
            os_url = f"http://localhost:{port}" if port else ""
        return cls(prom=Prometheus(ep["prometheus_url"], http), jaeger=Jaeger(ep["jaeger_url"], http),
                   docker=docker_state, opensearch=OpenSearch(os_url, http) if os_url else None,
                   clock=Clock())

    @classmethod
    def recording(cls, path: str | Path) -> "Backends":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        b = cls.live()
        wrap = lambda obj, name: Recorder(obj, name, path) if obj is not None else None
        return cls(prom=wrap(b.prom, "prom"), jaeger=wrap(b.jaeger, "jaeger"),
                   docker=wrap(b.docker, "docker"), opensearch=wrap(b.opensearch, "opensearch"),
                   clock=wrap(b.clock, "clock"))

    @classmethod
    def replay(cls, path: str | Path) -> "Backends":
        calls: dict[str, deque] = defaultdict(deque)
        has_opensearch = False
        with open(path) as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    calls[_key(entry["backend"], entry["method"], entry["args"])].append(entry)
                    has_opensearch |= entry["backend"] == "opensearch"
        last: dict[str, dict] = {}
        rep = lambda name: Replayer(name, calls, last)
        return cls(prom=rep("prom"), jaeger=rep("jaeger"), docker=rep("docker"),
                   opensearch=rep("opensearch") if has_opensearch else None, clock=rep("clock"))
