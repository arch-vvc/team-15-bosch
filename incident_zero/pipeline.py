"""Architecture coordinator: triage -> rules -> gated LLM -> validator -> remedy."""
import time
import json
from pathlib import Path
from incident_zero.agent import Investigator, Budget
from incident_zero.rules import signatures, dependencies, localize, fingerprint, checked, configured_signatures, rank_candidates
from incident_zero.tools import HEALTH_SERVICES, record
from incident_zero.telemetry import SERVICES


class Pipeline:
    def __init__(self, store, tools, actions=(), model_factory=None, max_services=8, seconds=180, rules_path=None):
        self.store, self.tools = store, tools
        self.actions, self.model_factory = list(actions), model_factory
        self.max_services, self.seconds = max_services, seconds
        path = Path(rules_path) if rules_path else Path(__file__).resolve().parent.parent / "config/rules.json"
        self.policy = json.loads(path.read_text())
        if not .7 <= self.policy.get("minimum_confidence", .7) <= 1 or not .01 <= self.policy.get("minimum_margin", .1) <= 1:
            raise ValueError("Rule policy needs confidence >= .7 and a positive confidence margin")
        if hasattr(tools, "set_guard"):
            tools.set_guard(store.killed)

    def run(self, alert):
        if alert.get("service") not in HEALTH_SERVICES:
            raise ValueError("Alert service is not allowlisted")
        incident = self.store.create(alert, self.tools.mode)
        started, end = time.monotonic(), time.time()
        def collect(name, service):
            if self.store.killed() or time.monotonic() - started >= self.seconds:
                raise ValueError("Kill switch or triage time budget stopped investigation")
            try:
                rows = self.tools.call(name, service, 300, end)
            except Exception as exc:
                rows = [record(service, name, [], "tool://unavailable", end-300, end, status="unavailable")]
                rows[0]["error"] = type(exc).__name__
            saved = [self.store.add_evidence(incident, row) for row in rows]
            with self.store.db:
                self.store.event(incident, "tool_called", {"tool": name, "service": service,
                    "evidence_ids": [row["evidence_id"] for row in saved]})
            return saved
        try:
            queue, seen = [alert["service"]], set()
            while queue and len(seen) < self.max_services:
                service = queue.pop(0)
                if service in seen:
                    continue
                seen.add(service)
                rows = collect("inspect_service", service) if service in SERVICES else []
                collect("read_container_health", service)
                collect("read_resources", service)
                if service in SERVICES:
                    collect("read_logs", service)
                for _, peer in dependencies(rows):
                    if peer not in seen and peer not in queue:
                        queue.append(peer)
            evidence = self.store.evidence(incident)
            edges = dependencies(evidence)
            candidates, ambiguous = rank_candidates(signatures(evidence) + configured_signatures(evidence, self.policy), self.policy)
            candidates = localize(candidates, edges)
            fp = fingerprint(evidence)
            hints = self.store.verified_memories(incident, fp)
            with self.store.db:
                self.store.event(incident, "triage", {"services": sorted(seen), "edges": edges,
                    "unvisited": queue, "fingerprint": fp, "memory_hints": hints,
                    "memory_confidence_bonus": 0, "rule_candidates": candidates, "ambiguous": ambiguous})
            if len(candidates) > 2 or queue:
                raise ValueError("More than two causes or incomplete dependency traversal: human investigation required")
            if candidates and not ambiguous:
                causes = [checked(candidate, evidence) for candidate in candidates]
                source = "rules"
            else:
                with self.store.db:
                    self.store.event(incident, "llm_fallback", {"reason": "No confident rule signature", "max_calls": 8})
                if self.model_factory is None:
                    raise ValueError("Rules are unsure and no LLM is configured; escalate with evidence")
                model = self.model_factory()
                remaining = max(1, int(self.seconds - (time.monotonic() - started)))
                Investigator(self.store, model, self.tools, Budget(model_turns=8, tool_calls=8, seconds=remaining)).run(
                    alert, incident=incident, finalize=False)
                current = self.store.incident(incident)
                if current["state"] == "escalated":
                    return incident
                diagnosis = current["diagnosis"]
                if diagnosis is None or diagnosis["escalate"]:
                    raise ValueError("LLM could not produce a validated diagnosis")
                # Code determines any actionable signature from collected telemetry, never LLM text.
                fresh_evidence = self.store.evidence(incident)
                matches, still_ambiguous = rank_candidates(signatures(fresh_evidence) + configured_signatures(fresh_evidence, self.policy), self.policy)
                if still_ambiguous or len(matches) > 1:
                    raise ValueError("Conflicting or multiple remaining signatures require human review")
                signature = next((item["signature"] for item in matches if item["component"] == diagnosis["component"]), "unknown")
                causes = [{**diagnosis, "signature": signature}]
                source = "llm"
            diagnosis = {**causes[0], "causes": causes, "source": source, "fingerprint": fp, "rule_policy": self.policy}
            self.store.diagnose(incident, diagnosis)
            propose_next(self.store, incident, self.actions, causes)
        except Exception as exc:
            if self.store.incident(incident)["state"] in {"investigating", "diagnosed", "awaiting_approval"}:
                self.store.transition(incident, "escalated", {"reason": str(exc)})
        return incident


def propose_next(store, incident, actions, causes, rollback=False):
    """Trusted table chooses a single remedy. Unknown signatures always escalate."""
    cause = causes[0]
    matches = [action for action in actions if action["service"] == cause["component"]
               and cause["signature"] in action.get("signatures", []) and action.get("tier") in (1, 2)]
    if len(matches) != 1:
        store.transition(incident, "escalated", {"reason": "No unique allowlisted remedy for validated signature", "cause": cause})
        return None
    action = matches[0]
    threshold = .85 if action["tier"] == 2 else .7
    if cause["confidence"] < threshold:
        store.transition(incident, "escalated", {"reason": "Confidence below remedy tier threshold"})
        return None
    return store.propose(incident, {"architecture": "rules-first-v2", "action": action, "diagnosis": cause,
        "remaining_causes": causes[1:], "rollback": rollback, "tier": action["tier"]})
