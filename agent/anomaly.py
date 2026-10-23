"""One definition of "abnormal", shared by the alert watcher, triage, onset and verify,
so a service never counts as sick in one step and healthy in the next."""
from __future__ import annotations

from agent import config


def _cfg() -> dict:
    return config.thresholds()["anomaly"]


def limit(signal: str, baseline: float) -> float:
    """The value above which a signal is abnormal ("up" signals only)."""
    c = _cfg()
    if signal == "error_ratio":
        return max(c["error_ratio_mult"] * baseline, baseline + c["error_ratio_add"])
    if signal == "p95_ms":
        return max(c["p95_mult"] * baseline, baseline + c["p95_min_delta_ms"])
    if signal == "req_rate":
        return max(c["req_rate_mult"] * baseline, baseline + c["req_rate_min_delta"])
    raise ValueError(f"no threshold for {signal}")


def check(signal: str, value: float | None, baseline: float | None, req_rate: float | None = None) -> str | None:
    """Return "up", "absent" or None (normal) for one service signal.

    error_ratio and p95_ms are ignored for services too quiet to judge. req_rate is "absent"
    when a service that had traffic at baseline has (almost) none now.
    """
    c = _cfg()
    base = baseline or 0.0
    if signal == "req_rate":
        if base >= c["min_req_rate"] and (value is None or value < c["silent_ratio"] * base):
            return "absent"
        if value is not None and value > limit("req_rate", base):
            return "up"
        return None
    if value is None or (req_rate is not None and req_rate < c["min_req_rate"]):
        return None
    if signal == "p95_ms" and baseline is None:
        return None  # no baseline latency to compare against
    return "up" if value > limit(signal, base) else None


def service_anomalies(row: dict, base: dict) -> dict[str, str]:
    """{signal: direction} for every abnormal signal of one service's health row."""
    out = {}
    for signal in ("error_ratio", "p95_ms", "req_rate"):
        d = check(signal, row.get(signal), base.get(signal), row.get("req_rate"))
        if d:
            out[signal] = d
    return out
