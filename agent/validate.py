"""Evidence validator (step 8). Ported from incident_zero's citation checks to the shared
ledger: nothing a diagnosis claims may go beyond what the evidence holds.

For the selected hypothesis:
  * every cited evidence ID exists in the ledger and is redacted,
  * the component is a real service the tools watch,
  * at least one supporting item has a fact about that component,
  * no item is cited both for and against it,
  * confidence is a finite number in [0, 1] and the source is rules or llm.
Any problem means escalate; the orchestrator never acts on a failed validation.
"""
from __future__ import annotations

import math

from agent import config
from agent.contracts import Diagnosis, ValidationResult


def check(diagnosis: Diagnosis, ledger) -> ValidationResult:
    problems: list[str] = []
    if diagnosis.state != "CONFIDENT" or diagnosis.selected_id is None:
        return ValidationResult(ok=False, problems=["no confident selected hypothesis"])
    chosen = [h for h in diagnosis.hypotheses if h.id == diagnosis.selected_id]
    if len(chosen) != 1:
        return ValidationResult(ok=False, problems=[f"selected id {diagnosis.selected_id} is missing or duplicated"])
    h = chosen[0]
    if h.component not in config.all_containers():
        problems.append(f"component {h.component} is not a known service")
    if not h.supporting_evidence:
        problems.append("no supporting evidence")
    if set(h.supporting_evidence) & set(h.contradicting_evidence):
        problems.append("the same evidence is cited for and against")
    if not (isinstance(h.confidence, (int, float)) and math.isfinite(h.confidence) and 0 <= h.confidence <= 1):
        problems.append("confidence is not a number in [0, 1]")
    about_component = False
    for eid in h.supporting_evidence + h.contradicting_evidence:
        if not ledger.has(eid):
            problems.append(f"{eid} is not in the ledger")
            continue
        ev = ledger.get(eid)
        if not ev.redacted:
            problems.append(f"{eid} is not redacted")
        if eid in h.supporting_evidence and any(
                f.service == h.component or h.component in (f.scope or "") for f in ev.facts):
            about_component = True
    if h.supporting_evidence and not about_component:
        problems.append(f"no supporting evidence has a fact about {h.component}")
    return ValidationResult(ok=not problems, problems=problems)
