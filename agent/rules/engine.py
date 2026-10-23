"""Rules engine (step 5 and 6): competing hypotheses from the triage snapshot's typed facts.

Signatures live in config/signatures.yaml (telemetry patterns, never incident names).
Every matching (component, cause) becomes a hypothesis citing the evidence IDs whose facts
matched. The diagnosis state follows the brief:
  CONFIDENT  top confidence >= 0.7 and it leads every competing hypothesis by >= 0.15,
  UNKNOWN    nothing matched at or above 0.3 and nothing is abnormal,
  UNCERTAIN  anything else, including abnormal telemetry no pattern explains (the LLM's turn).
Hypotheses compete when they name the same component or components on one call path;
two faults in unrelated services do not make each other uncertain.
Memory hints only reorder equally confident hypotheses; they never change confidence.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from agent.contracts import Diagnosis, Fact, Hypothesis, TriageSnapshot

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "signatures.yaml"


def matches(cond: dict, fact: Fact) -> bool:
    if fact.signal != cond["signal"]:
        return False
    if "direction" in cond and fact.direction != cond["direction"]:
        return False
    if "value" in cond and fact.value != cond["value"]:
        return False
    if "value_in" in cond and fact.value not in cond["value_in"]:
        return False
    if "min" in cond and not (isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool)
                              and fact.value >= cond["min"]):
        return False
    return True


def _reach(edges: list[tuple[str, str]]) -> dict[str, set[str]]:
    """Every service reachable downstream of each service."""
    graph: dict[str, set[str]] = {}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
    out = {}
    for start in graph:
        seen, stack = set(), [start]
        while stack:
            for nxt in graph.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        out[start] = seen
    return out


class Engine:
    def __init__(self, path: str | Path = DEFAULT_PATH):
        self.config = yaml.safe_load(Path(path).read_text())

    def diagnose(self, snapshot: TriageSnapshot, ledger, hints=()) -> Diagnosis:
        facts = [(eid, f) for eid in snapshot.evidence_ids for f in ledger.get(eid).facts]
        components = sorted({f.service for _, f in facts})
        found: dict[tuple[str, str], dict] = {}
        for sig in self.config["signatures"]:
            for comp in components:
                # "none": a fact that rules this pattern out (e.g. a lowered CPU limit rules out
                # a CPU-hungry service: the usage is pinned at a cap someone else set)
                if any(f.service == comp and matches(cond, f) for cond in sig.get("none", []) for _, f in facts):
                    continue
                support: list[str] = []
                for cond in sig["all"]:
                    hits = [eid for eid, f in facts if f.service == comp and matches(cond, f)]
                    if not hits:
                        break
                    support += hits
                else:
                    key = (comp, sig["cause"])
                    prev = found.get(key)
                    if prev is None or sig["confidence"] > prev["confidence"]:
                        found[key] = {"component": comp, "cause": sig["cause"], "confidence": sig["confidence"],
                                      "signature": sig["id"], "summary": sig["summary"],
                                      "support": sorted(set(support) | set(prev["support"] if prev else ())),
                                      "missing": sig.get("missing", [])}
                    else:
                        prev["support"] = sorted(set(prev["support"]) | set(support))

        reach = _reach(snapshot.dependencies)
        depth = {c: len(reach.get(c, ())) for c in components}          # fewer downstream = deeper
        hinted = {(h[2], h[3]) for h in hints}
        ranked = sorted(found.values(), key=lambda h: (-h["confidence"], (h["component"], h["cause"]) not in hinted,
                                                       depth.get(h["component"], 0), h["component"]))
        hypotheses = [Hypothesis(id=f"H{i}", component=h["component"], cause=h["cause"], confidence=h["confidence"],
                                 supporting_evidence=h["support"], contradicting_evidence=[],
                                 missing_evidence=h["missing"], source="rules")
                      for i, h in enumerate(ranked, 1)]
        abnormal = {f"{f.service}:{f.signal}" for _, f in facts
                    if f.direction in ("up", "down", "absent") and f.signal not in ("error_count",)}
        return self._state(hypotheses, ranked, reach, abnormal)

    def _state(self, hypotheses: list[Hypothesis], ranked: list[dict], reach, abnormal: set[str]) -> Diagnosis:
        c = self.config
        if not hypotheses or hypotheses[0].confidence < c["unknown_below"]:
            if abnormal:
                # something is clearly wrong but no known pattern explains it: the LLM's turn
                return Diagnosis(state="UNCERTAIN", hypotheses=hypotheses, selected_id=None,
                                 reason="Abnormal telemetry matched no known fault pattern: "
                                        + ", ".join(sorted(abnormal)[:8]) + ".")
            return Diagnosis(state="UNKNOWN", hypotheses=hypotheses, selected_id=None,
                             reason="No fault pattern matched the current telemetry.")
        top = hypotheses[0]
        related = lambda a, b: a == b or b in reach.get(a, ()) or a in reach.get(b, ())
        rivals = [h for h in hypotheses[1:] if related(top.component, h.component)]
        why = f"{ranked[0]['signature']}: {ranked[0]['summary']}"
        if top.confidence < c["minimum_confidence"]:
            return Diagnosis(state="UNCERTAIN", hypotheses=hypotheses, selected_id=None,
                             reason=f"Best match {top.id} ({why}) is below {c['minimum_confidence']}.")
        if rivals and top.confidence - rivals[0].confidence < c["minimum_margin"]:
            return Diagnosis(state="UNCERTAIN", hypotheses=hypotheses, selected_id=None,
                             reason=f"{top.id} and {rivals[0].id} are within {c['minimum_margin']} of each other "
                                    "on the same call path.")
        return Diagnosis(state="CONFIDENT", hypotheses=hypotheses, selected_id=top.id, reason=why)


_default: Engine | None = None


def diagnose(snapshot, ledger, hints=()) -> Diagnosis:
    """Module-level entry point from the Part 2 interface."""
    global _default
    _default = _default or Engine()
    return _default.diagnose(snapshot, ledger, hints)
