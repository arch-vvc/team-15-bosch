"""Threshold detection: missing telemetry never becomes a healthy zero."""

import math


def detect(records, service, error_ratio=0.05, latency_ms=2000):
    values = {}
    for row in records:
        if row["status"] == "observed" and row["observations"]:
            value = row["observations"][0].get("value")
            if type(value) in (float, int) and math.isfinite(value):
                values[row["kind"]] = value
    calls = values.get("server_calls_per_second")
    errors = values.get("server_errors_per_second")
    latency = values.get("server_p95_milliseconds")
    reasons = []
    for row in records:
        if row["kind"] == "container_health" and row["status"] == "observed":
            for health in row["observations"]:
                if health.get("restarting") is True:
                    reasons.append("container is restarting")
                elif health.get("running") is False and health.get("status") in {"dead", "exited"}:
                    reasons.append("container is stopped")
    if calls is not None and errors is not None and calls > 0 and 0 <= errors <= calls and errors / calls >= error_ratio:
        reasons.append(f"server error ratio {errors/calls:.2%} >= {error_ratio:.2%}")
    if latency is not None and latency >= latency_ms:
        reasons.append(f"server p95 latency {latency:.1f} ms >= {latency_ms:.1f} ms")
    return {"service": service, "summary": "; ".join(reasons), "source": "metric_threshold"} if reasons else None
