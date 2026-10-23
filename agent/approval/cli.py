"""Approval gate (step 10) in the terminal. Shows the diagnosis, the evidence behind it,
the competing hypotheses, the proposal and the dry run, then asks.

  Tier 1:              "Approve? [y/N]"  anything but y/yes is a reject
  Tier 2 or rollback:  the operator must type the target service name exactly
Never approves by itself; an empty answer, EOF or Ctrl-C is a reject.
"""
from __future__ import annotations

import getpass
from datetime import datetime, timezone

from agent.contracts import ActionProposal, Approval, Diagnosis, DryRunResult


def _fact_text(f) -> str:
    base = f" (baseline {f.baseline})" if f.baseline is not None else ""
    scope = f" [{f.scope}]" if f.scope else ""
    return f"{f.service} {f.signal}{scope} = {f.value}{base}"


def render(p: ActionProposal, diagnosis: Diagnosis, dry: DryRunResult, ledger) -> str:
    h = next((h for h in diagnosis.hypotheses if h.id == p.hypothesis_id), None)
    lines = ["", "=" * 72, "APPROVAL NEEDED", "=" * 72]
    if h:
        lines.append(f"Diagnosis {h.id}: {h.component} / {h.cause}  confidence {h.confidence:.2f}  ({h.source})")
        lines.append(f"Why: {diagnosis.reason}")
        lines.append("Evidence:")
        for eid in h.supporting_evidence[:3]:
            ev = ledger.get(eid)
            facts = [f for f in ev.facts if f.service == h.component][:2] or ev.facts[:2]
            lines += [f"  [{eid}] {ev.tool}: {_fact_text(f)}" for f in facts]
        if h.missing_evidence:
            lines.append("Not checked yet: " + "; ".join(h.missing_evidence))
    others = [o for o in diagnosis.hypotheses if o.id != p.hypothesis_id][:3]
    if others:
        lines.append("Other hypotheses: " + ", ".join(f"{o.id} {o.component}/{o.cause} {o.confidence:.2f}" for o in others))
    lines += ["", f"Proposed action: {p.action} on {p.target}  (tier {p.tier})", f"Reason: {p.reason}",
              f"Dry run: {dry.would_do}"]
    if dry.checks_passed:
        lines.append("Checks passed: " + "; ".join(dry.checks_passed))
    if dry.checks_failed:
        lines.append("Checks FAILED: " + "; ".join(dry.checks_failed))
    return "\n".join(lines)


class CliApproval:
    def __init__(self, input_fn=input, print_fn=print, operator: str | None = None):
        self.input, self.print = input_fn, print_fn
        self.operator = operator

    def request(self, p: ActionProposal, diagnosis: Diagnosis, dry: DryRunResult, ledger) -> Approval:
        self.print(render(p, diagnosis, dry, ledger))
        operator = self.operator or getpass.getuser()
        strict = p.tier >= 2 or dry.would_do.startswith("ROLLBACK")
        try:
            if strict:
                answer = self.input(f"Type the service name '{p.target}' to approve, anything else rejects: ").strip()
                approved, typed = answer == p.target, answer
            else:
                answer = self.input("Approve? [y/N] ").strip().lower()
                approved, typed = answer in ("y", "yes"), None
        except (EOFError, KeyboardInterrupt):
            approved, typed = False, None
        self.print("APPROVED" if approved else "REJECTED")
        return Approval(approved=approved, operator=operator, typed_confirmation=typed or None,
                        ts=datetime.now(timezone.utc))


class AutoReject:
    """For evaluation runs: shows nothing, approves nothing (eval scores the diagnosis only)."""

    def request(self, p, diagnosis, dry, ledger) -> Approval:
        return Approval(approved=False, operator="auto-reject", typed_confirmation=None,
                        ts=datetime.now(timezone.utc))
