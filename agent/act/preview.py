"""Action adapter to use until the organisers publish the Action API.

dry_run describes the call (read-only, which the rules allow) so the whole approval gate can
be shown; execute and rollback refuse, so nothing changes and the incident escalates with
the approved proposal recorded. Replace with the real adapter once the contract is known.
"""
from __future__ import annotations

from agent.contracts import ActionProposal, DryRunResult


class PreviewOnlyAdapter:
    supports_rollback = False

    def dry_run(self, proposal: ActionProposal) -> DryRunResult:
        return DryRunResult(
            proposal=proposal,
            would_do=f"{proposal.action} on {proposal.target} through the organisers' Action API",
            checks_passed=["action on the allowlist", "target is a known service", f"tier {proposal.tier} policy"],
            checks_failed=[])

    def rollback_dry_run(self, proposal: ActionProposal) -> DryRunResult:
        raise RuntimeError("Rollback needs the organisers' Action API")

    def execute(self, proposal: ActionProposal):
        raise RuntimeError("Action API not configured yet; nothing was changed")

    rollback = execute
