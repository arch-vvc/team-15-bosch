"""Trace tools (query-cookbook.md tools 3, 4 and 8), all read through Jaeger's HTTP API.

Every trace goes through redact.clean_trace before anything is counted, so flag spans,
flag events and flag attributes never reach a fact or an example.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from agent import config
from agent.contracts import Evidence, Fact
from agent.tools import redact
from agent.tools.backends import Backends, BackendError


def _tags(span: dict) -> dict:
    return {t["key"]: t.get("value") for t in span.get("tags") or []}


def _is_error(span: dict) -> bool:
    tags = _tags(span)
    return tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR"


def _kind(span: dict) -> str:
    return str(_tags(span).get("span.kind", "")).lower()


def _message(span: dict) -> str | None:
    for log in span.get("logs") or []:
        fields = {f["key"]: f.get("value") for f in log.get("fields") or []}
        if fields.get("exception.message"):
            return str(fields["exception.message"])
    tags = _tags(span)
    msg = tags.get("otel.status_description") or tags.get("exception.message")
    return str(msg) if msg else None


def _service(trace: dict, span: dict) -> str:
    return (trace.get("processes", {}).get(span.get("processID")) or {}).get("serviceName", "?")


def _parent_id(span: dict) -> str | None:
    for ref in span.get("references") or []:
        if ref.get("refType") == "CHILD_OF":
            return ref.get("spanID")
    return None


def _dt_us(us: int | float) -> datetime:
    return datetime.fromtimestamp(us / 1e6, timezone.utc)


def _clean(traces: list[dict]) -> list[dict]:
    return [redact.clean_trace(t) for t in traces]


def search_traces(b: Backends, service: str, errors_only: bool = True, operation: str | None = None,
                  lookback: str | None = None, limit: int | None = None) -> list[Evidence]:
    """Tool 4: error spans from recent traces that touch `service`, aggregated per service.

    One Evidence per service that has error spans, each with:
      error_count per operation, error_origin (traces whose deepest error is here),
      up to 3 distinct error messages, error_concentration when one attribute value covers
      most errors, onset_ts = earliest error span start, and 2-3 example trace IDs.
    """
    cfg = config.thresholds()["traces"]
    lookback = lookback or cfg["lookback"]
    limit = limit or cfg["limit"]
    query = {"service": service, "errors_only": errors_only, "operation": operation,
             "lookback": lookback, "limit": limit}
    traces = _clean(b.jaeger.traces(service=service, operation=operation,
                                    tags={"error": "true"} if errors_only else None,
                                    lookback=lookback, limit=limit))
    counts: dict[str, Counter] = defaultdict(Counter)
    origins: dict[str, Counter] = defaultdict(Counter)
    messages: dict[str, Counter] = defaultdict(Counter)
    attrs: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    first: dict[str, float] = {}
    examples: dict[str, list[str]] = defaultdict(list)
    for trace in traces:
        spans = trace.get("spans") or []
        errors = [s for s in spans if _is_error(s)]
        parents_with_error_child = {_parent_id(s) for s in errors}
        for s in errors:
            svc, op = _service(trace, s), s.get("operationName", "?")
            counts[svc][op] += 1
            if s["spanID"] not in parents_with_error_child:
                origins[svc][op] += 1
            if msg := _message(s):
                messages[svc][(op, redact.mask(msg)[:200])] += 1
            for key in cfg["concentration_attributes"]:
                if (value := _tags(s).get(key)) is not None:
                    attrs[svc][key][str(value)] += 1
            first[svc] = min(first.get(svc, s["startTime"]), s["startTime"])
            if f"trace:{trace['traceID']}" not in examples[svc] and len(examples[svc]) < 3:
                examples[svc].append(f"trace:{trace['traceID']}")

    out = []
    for svc in sorted(counts, key=lambda x: (x != service, -sum(counts[x].values()))):
        total = sum(counts[svc].values())
        facts = [Fact(service=svc, signal="error_count", scope=op, value=n, direction="up")
                 for op, n in counts[svc].most_common(5)]
        facts += [Fact(service=svc, signal="error_origin", scope=op, value=n)
                  for op, n in origins[svc].most_common(3)]
        facts += [Fact(service=svc, signal="error_message", scope=f"trace:{op}", value=msg)
                  for (op, msg), _ in messages[svc].most_common(3)]
        for key, values in attrs[svc].items():
            value, n = values.most_common(1)[0]
            if total >= cfg["concentration_min_errors"] and n / total >= cfg["concentration_share"]:
                facts.append(Fact(service=svc, signal="error_concentration", scope=key, value=value))
        out.append(Evidence(tool="search_traces", query=query | {"traces": len(traces)}, ts=b.now_dt(),
                            onset_ts=_dt_us(first[svc]), facts=facts, examples=examples[svc]))
    if not out:
        out.append(Evidence(tool="search_traces", query=query | {"traces": len(traces)}, ts=b.now_dt(),
                            facts=[Fact(service=service, signal="error_count", value=0, direction="same")]))
    return out


def edges_from_traces(traces: list[dict]) -> list[tuple[str, str]]:
    edges = set()
    for trace in traces:
        by_id = {s["spanID"]: s for s in trace.get("spans") or []}
        for s in by_id.values():
            parent = by_id.get(_parent_id(s))
            if parent:
                a, c = _service(trace, parent), _service(trace, s)
                if a != c:
                    edges.add((a, c))
    return sorted(edges)


def dependencies(b: Backends, lookback_min: int = 15, end: float | None = None) -> list[tuple[str, str]]:
    """Caller -> callee edges seen in the `lookback_min` minutes before `end` (default now).
    Falls back to edges read from sampled frontend traces when Jaeger's dependency
    endpoint returns nothing."""
    end_ms = int((end if end is not None else b.now()) * 1000)
    try:
        raw = redact.clean_edges(b.jaeger.dependencies(end_ms=end_ms, lookback_ms=lookback_min * 60_000))
    except BackendError:
        raw = []           # Jaeger v2 builds without the dependencies endpoint answer 404
    edges = sorted({(e["parent"], e["child"]) for e in raw if e.get("parent") != e.get("child")})
    if not edges:
        traces = _clean(b.jaeger.traces(service="frontend", lookback=f"{lookback_min}m", limit=50))
        edges = edges_from_traces(traces)
    return edges


def service_dependencies(b: Backends, lookback_min: int = 15,
                         healthy_edges: list[tuple[str, str]] | None = None,
                         edges: list[tuple[str, str]] | None = None) -> list[Evidence]:
    """Tool 8: the call graph, plus a missing_edge fact for every edge that existed while
    healthy but has no calls now (a dead or unreachable callee)."""
    edges = edges if edges is not None else dependencies(b, lookback_min)
    now = set(edges)
    facts = [Fact(service=child, signal="missing_edge", scope=f"{parent}->{child}", value=True, direction="absent")
             for parent, child in sorted(set(map(tuple, healthy_edges or [])) - now)]
    return [Evidence(tool="service_dependencies", query={"lookback_min": lookback_min}, ts=b.now_dt(),
                     facts=facts, examples=[f"{p}->{c}" for p, c in edges])]


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def _hop_samples(traces: list[dict], caller: str, callee: str) -> tuple[list[float], list[float], list[float]]:
    client, server, gap = [], [], []
    for trace in traces:
        by_id = {s["spanID"]: s for s in trace.get("spans") or []}
        for s in by_id.values():
            parent = by_id.get(_parent_id(s))
            if (parent and _service(trace, s) == callee and _service(trace, parent) == caller
                    and _kind(parent) == "client" and _kind(s) in ("server", "")):
                c_ms, s_ms = parent["duration"] / 1000, s["duration"] / 1000
                client.append(c_ms)
                server.append(s_ms)
                gap.append(max(0.0, c_ms - s_ms))
    return client, server, gap


def hop_latency(b: Backends, caller: str, callee: str, lookback: str = "10m", limit: int = 50,
                baseline_at: float | None = None) -> list[Evidence]:
    """Tool 3: client-side vs server-side time for one hop, from the same traces.
    A big gap with a normal server time means the network; a slow server means the callee."""
    def measure(**window):
        traces = _clean(b.jaeger.traces(service=caller, limit=limit, **window))
        return _hop_samples(traces, caller, callee)

    client, server, gap = measure(lookback=lookback)
    base_gap = base_server = None
    if baseline_at is not None:
        span_us = config.thresholds()["baseline"]["window_min"] * 60 * 1_000_000
        end_us = int(baseline_at * 1_000_000)
        _, b_server, b_gap = measure(lookback="custom", start_us=end_us - span_us, end_us=end_us)
        base_gap, base_server = _p95(b_gap), _p95(b_server)
    hop = f"{caller}->{callee}"
    facts = []
    if gap:
        g, sv = _p95(gap), _p95(server)
        up = lambda v, base: None if base is None else ("up" if v > max(2 * base, base + 50) else "same")
        facts = [
            Fact(service=callee, signal="hop_gap_ms", scope=hop, value=round(g, 1),
                 baseline=None if base_gap is None else round(base_gap, 1), direction=up(g, base_gap)),
            Fact(service=callee, signal="p95_ms", scope=f"server:{hop}", value=round(sv, 1),
                 baseline=None if base_server is None else round(base_server, 1), direction=up(sv, base_server)),
            Fact(service=caller, signal="p95_ms", scope=f"client:{hop}", value=round(_p95(client), 1)),
        ]
    else:
        facts = [Fact(service=callee, signal="missing_edge", scope=hop, value=True, direction="absent")]
    return [Evidence(tool="hop_latency", query={"caller": caller, "callee": callee, "lookback": lookback,
                                                "samples": len(gap)}, ts=b.now_dt(), facts=facts)]
