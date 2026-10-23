"""Incident memory (step 3). Fingerprint = the set of service:signal:direction items that are
abnormal in a snapshot; similar() returns past verified incidents by Jaccard similarity.
Only recovered incidents are stored, the store starts empty, and hints only reorder
suspects in the engine: they never change confidence.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent import anomaly
from agent.contracts import Incident, TriageSnapshot

DEFAULT_PATH = Path("state/memory.json")


def fingerprint(snapshot: TriageSnapshot) -> set[str]:
    items = set()
    for s in snapshot.services:
        row = {"req_rate": s.req_rate, "error_ratio": s.error_ratio, "p95_ms": s.p95_ms}
        for signal, direction in anomaly.service_anomalies(row, s.baseline).items():
            items.add(f"{s.service}:{signal}:{direction}")
    for c in snapshot.containers:
        if c.status not in ("running", "missing"):
            items.add(f"{c.service}:status:absent")
        if c.restarts_since_baseline:
            items.add(f"{c.service}:restarts:up")
        if c.oom_killed:
            items.add(f"{c.service}:oom_killed:up")
    return items


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


class Memory:
    def __init__(self, path: str | Path = DEFAULT_PATH, enabled: bool = True, threshold: float = 0.6):
        self.path, self.enabled, self.threshold = Path(path), enabled, threshold

    def _load(self) -> list[dict]:
        if not self.enabled or not self.path.exists():
            return []
        return json.loads(self.path.read_text())

    def similar(self, snapshot: TriageSnapshot) -> list[tuple[str, float, str, str]]:
        """(incident id, score, component, cause), best first, score >= threshold."""
        fp = fingerprint(snapshot)
        scored = [(m["id"], jaccard(fp, set(m["fingerprint"])), m["component"], m["cause"]) for m in self._load()]
        return sorted((s for s in scored if s[1] >= self.threshold), key=lambda s: -s[1])

    def store(self, incident: Incident) -> None:
        if not self.enabled or incident.outcome != "recovered" or not incident.diagnoses:
            return
        final = incident.diagnoses[-1]
        chosen = next((h for h in final.hypotheses if h.id == final.selected_id), None)
        if chosen is None:
            return
        items = self._load()
        items.append({"id": f"M{len(items) + 1}", "ts": incident.alert.ts.isoformat(),
                      "fingerprint": sorted(fingerprint(incident.snapshot)),
                      "component": chosen.component, "cause": chosen.cause})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, indent=2))
