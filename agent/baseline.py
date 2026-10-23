"""What "normal" looks like: per-service rates, error ratios and p95 over a quiet window,
plus container restart counts and limits, plus the healthy call graph.

Taken once at startup (or read from state/baseline.json). Because Prometheus keeps history,
the window can end in the past (end_offset_min), so a baseline is still possible when the
agent starts after an incident has begun.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent import anomaly, config
from agent.tools import docker_state, jaeger, prometheus
from agent.tools.backends import Backends, BackendError

DEFAULT_PATH = Path("state/baseline.json")


class Baseline(BaseModel):
    taken_at: float                                  # end of the window, unix seconds
    window_min: int
    services: dict[str, dict[str, float | None]]     # req_rate, error_ratio, p95_ms
    containers: dict[str, dict[str, Any]]            # status, restarts, cpu_limit, mem_limit, mem_used, cpu_used
    edges: list[tuple[str, str]]
    warnings: list[str] = []

    def service(self, name: str) -> dict:
        return self.services.get(name, {})

    def save(self, path: str | Path = DEFAULT_PATH) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> "Baseline":
        return cls.model_validate(json.loads(Path(path).read_text()))


def compute(b: Backends, end_offset_min: int = 0, window_min: int | None = None) -> Baseline:
    window_min = window_min or config.thresholds()["baseline"]["window_min"]
    at = b.now() - end_offset_min * 60
    services = prometheus.health_table(b, at=at, win=f"{window_min}m")
    containers = {}
    try:
        for c in docker_state.container_table(b):
            containers[c.service] = {"status": c.status, "restarts": c.restarts, "cpu_limit": c.cpu_limit, "mem_limit": c.mem_limit,
                                     "mem_used": c.mem_used, "cpu_used": c.cpu_used}
    except BackendError as e:
        containers = {}
        warnings = [f"docker unavailable: {e}"]
    else:
        warnings = []
    try:
        edges = jaeger.dependencies(b, lookback_min=window_min, end=at)
    except BackendError as e:
        edges, warnings = [], warnings + [f"jaeger unavailable: {e}"]
    for svc, row in services.items():
        # a baseline should be quiet; flag anything that already looks broken
        if (row.get("error_ratio") or 0) > anomaly.limit("error_ratio", 0.0) * 2:
            warnings.append(f"{svc} already has error_ratio {row['error_ratio']:.2f} in the baseline window")
    for name in config.shop_services():
        if name not in services and name != "load-generator":
            warnings.append(f"{name} had no span metrics in the baseline window")
    return Baseline(taken_at=at, window_min=window_min, services=services, containers=containers,
                    edges=[tuple(e) for e in edges], warnings=warnings)
