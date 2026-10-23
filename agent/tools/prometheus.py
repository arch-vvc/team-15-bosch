"""Span-metric tools (query-cookbook.md tools 1, 2 and 7).

All rates come from the collector's span_metrics connector:
  traces_span_metrics_calls_total{service_name, span_name, span_kind, status_code}
  traces_span_metrics_duration_milliseconds_bucket{..., le}
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from agent import anomaly, config
from agent.contracts import Evidence, Fact
from agent.tools import redact
from agent.tools.backends import Backends

CALLS = "traces_span_metrics_calls_total"
BUCKETS = "traces_span_metrics_duration_milliseconds_bucket"
SERVER = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
ERROR = 'status_code="STATUS_CODE_ERROR"'
NO_FLAGS = 'service_name!~"flagd|flagd-ui"'


def window() -> str:
    return config.thresholds()["prometheus"]["rate_window"]


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _vector(b: Backends, expr: str, by: str, at: float | None) -> dict[str, float]:
    rows = redact.clean_prom_rows(b.prom.query(expr=expr, at=at))
    out = {}
    for row in rows:
        v = _num(row["value"][1])
        if v is not None:
            out[row["metric"].get(by, "")] = v
    return out


def _sel(*parts: str) -> str:
    return "{" + ",".join(p for p in parts if p) + "}"


def health_table(b: Backends, at: float | None = None, win: str | None = None,
                 service: str | None = None, by: str = "service_name") -> dict[str, dict]:
    """{key: {req_rate, error_ratio, p95_ms}} for every service (or every operation of one
    service when by="span_name"). Raw numbers, no evidence; detect.watch polls this."""
    w = win or window()
    svc = f'service_name="{service}"' if service else NO_FLAGS
    kind = SERVER if by == "service_name" else ""
    totals = _vector(b, f"sum by ({by}) (rate({CALLS}{_sel(kind, svc)}[{w}]))", by, at)
    errors = _vector(b, f"sum by ({by}) (rate({CALLS}{_sel(kind, svc, ERROR)}[{w}]))", by, at)
    p95 = _vector(b, f"histogram_quantile(0.95, sum by (le, {by}) (rate({BUCKETS}{_sel(kind, svc)}[{w}])))", by, at)
    table = {}
    for key in set(totals) | set(p95):
        rate = totals.get(key)
        table[key] = {
            "req_rate": rate,
            "error_ratio": (errors.get(key, 0.0) / rate) if rate else None,
            "p95_ms": p95.get(key),
        }
    return table


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts is not None else None


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 4)


def service_health_overview(b: Backends, baseline: dict[str, dict] | None = None) -> list[Evidence]:
    """Tool 1: request rate, error ratio and p95 for every service, against baseline.
    A service with baseline traffic but no rows now gets a missing_telemetry fact."""
    baseline = baseline or {}
    table = health_table(b)
    facts = []
    for svc in sorted(set(table) | set(baseline)):
        row, base = table.get(svc, {}), baseline.get(svc, {})
        abnormal = anomaly.service_anomalies(row, base)
        if not row:
            if abnormal.get("req_rate") == "absent":
                facts.append(Fact(service=svc, signal="missing_telemetry", value=True,
                                  baseline=_round(base.get("req_rate")), direction="absent"))
            continue
        for signal in ("req_rate", "error_ratio", "p95_ms"):
            if row.get(signal) is None:
                continue
            facts.append(Fact(service=svc, signal=signal, value=_round(row[signal]),
                              baseline=_round(base.get(signal)),
                              direction=abnormal.get(signal) or ("same" if base else None)))
    return [Evidence(tool="service_health_overview", query={"window": window()}, ts=b.now_dt(), facts=facts)]


def service_detail(b: Backends, service: str, baseline_at: float | None = None) -> list[Evidence]:
    """Tool 2: the same three numbers per operation of one service, which shows where
    inside it things fail (e.g. only EmptyCart, only GetProduct)."""
    now = health_table(b, service=service, by="span_name")
    base = health_table(b, at=baseline_at, service=service, by="span_name",
                        win=f"{config.thresholds()['baseline']['window_min']}m") if baseline_at else {}
    ranked = sorted(now, key=lambda op: (-(now[op]["error_ratio"] or 0), -(now[op]["req_rate"] or 0)))
    facts = []
    for op in ranked[:15]:
        row, b_row = now[op], base.get(op, {})
        for signal in ("error_ratio", "p95_ms", "req_rate"):
            if row.get(signal) is None:
                continue
            d = anomaly.check(signal, row[signal], b_row.get(signal), row.get("req_rate")) if b_row else None
            facts.append(Fact(service=service, signal=signal, scope=redact.mask(op), value=_round(row[signal]),
                              baseline=_round(b_row.get(signal)), direction=d or ("same" if b_row else None)))
    return [Evidence(tool="service_detail", query={"service": service, "window": window()},
                     ts=b.now_dt(), facts=facts)]


def _range_expr(service: str, signal: str, w: str) -> str:
    svc = f'service_name="{service}"'
    if signal == "error_ratio":
        return (f"(sum(rate({CALLS}{_sel(SERVER, svc, ERROR)}[{w}])) or vector(0))"
                f" / sum(rate({CALLS}{_sel(SERVER, svc)}[{w}]))")
    if signal == "p95_ms":
        return f"histogram_quantile(0.95, sum by (le) (rate({BUCKETS}{_sel(SERVER, svc)}[{w}])))"
    if signal == "req_rate":
        return f"sum(rate({CALLS}{_sel(SERVER, svc)}[{w}]))"
    raise ValueError(f"onset not supported for {signal}")


def onset_from_series(points: list[tuple[float, float]], signal: str, baseline: float,
                      direction: str, start: float, end: float, step: float,
                      need: int = 2) -> tuple[float, float] | None:
    """First (ts, value) where the signal turned abnormal and stayed so for `need` points.
    For "absent", a missing point counts as abnormal too."""
    values = {round(t): v for t, v in points}
    t, run = round(start), []
    while t <= end:
        v = values.get(round(t))
        if direction == "absent":
            bad = v is None or anomaly.check("req_rate", v, baseline) == "absent"
        else:
            bad = v is not None and v > anomaly.limit(signal, baseline)
        run = run + [(t, v if v is not None else 0.0)] if bad else []
        if len(run) >= need:
            return run[0]
        t += step
    return None


def onset(b: Backends, service: str, signal: str, baseline: float, direction: str = "up",
          lookback_min: int = 30) -> list[Evidence]:
    """Tool 7: when did this service's signal first become abnormal? Uses query_range at
    the configured step; two consecutive abnormal points are needed."""
    step = config.thresholds()["prometheus"]["step_s"]
    end = b.now()
    start = end - lookback_min * 60
    start -= start % step
    sig = "req_rate" if direction == "absent" else signal
    rows = redact.clean_prom_rows(b.prom.query_range(expr=_range_expr(service, sig, window()),
                                                     start=start, end=end, step=step))
    points = [(float(t), v) for row in rows[:1] for t, raw in row.get("values", [])
              if (v := _num(raw)) is not None]
    hit = onset_from_series(points, sig, baseline, direction, start, end, step)
    facts = []
    if hit:
        facts.append(Fact(service=service, signal="missing_telemetry" if direction == "absent" else signal,
                          scope="onset", value=_round(hit[1]) if direction != "absent" else True,
                          baseline=_round(baseline), direction=direction))
    return [Evidence(tool="onset", query={"service": service, "signal": signal, "lookback_min": lookback_min,
                                          "step_s": step}, ts=b.now_dt(), onset_ts=_dt(hit[0]) if hit else None,
                     facts=facts)]
