"""No HTTP adapter exists until organisers supply the official contract.
Adapter methods are local Python boundaries, not assumed API wire payloads.
A new Actions instance is required for every incident. Single orchestrator owner.
"""
from datetime import datetime, timezone, timedelta
from typing import Protocol
from agent.contracts import ActionResult, DryRunResult
from .remedy import Remedy

class ActionAdapter(Protocol):
    supports_rollback: bool
    def dry_run(self, proposal) -> DryRunResult: ...
    def rollback_dry_run(self, proposal) -> DryRunResult: ...
    def execute(self, proposal) -> ActionResult: ...
    def rollback(self, proposal) -> ActionResult: ...

class DisabledAdapter:
    supports_rollback = False
    def dry_run(self, proposal):
        raise RuntimeError('Official Action API contract not configured')
    execute = dry_run
    rollback = dry_run

class Actions:
    def __init__(self, adapter, kill, log, policy=None, clock=None, ttl_s=120):
        self.adapter, self.kill, self.log = adapter, kill, log
        self.policy = policy or Remedy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ttl = timedelta(seconds=ttl_s)
        self.preview = self.binding = self.pending = None
        self.attempts, self.used, self.rolled_back = 0, set(), False

    @staticmethod
    def key(p):
        return p.model_dump_json()

    def _call(self, operation, p, approval=None):
        self.kill.check()
        self.log.append(event='intent', operation=operation, proposal=p, approval=approval)
        self.kill.check()
        try:
            result = getattr(self.adapter, operation)(p.model_copy(deep=True))
        except Exception as exc:
            self.log.append(event='error', operation=operation, error=type(exc).__name__)
            raise
        self.log.append(event='result', operation=operation, response=result)
        return result

    def rollback_dry_run(self, p):
        if not self.adapter.supports_rollback or self.pending != self.key(p) or self.rolled_back:
            raise ValueError("Rollback unavailable")
        return self._preview(p, "rollback_dry_run")

    def dry_run(self, p):
        return self._preview(p, "dry_run")

    def _preview(self, p, operation):
        self.policy.check(p)
        self.binding = self.preview = None
        r = DryRunResult.model_validate(self._call(operation, p))
        if self.key(r.proposal) != self.key(p) or r.checks_failed:
            raise ValueError('Dry run mismatch or failed checks')
        self.preview = (self.key(p), self.clock(), operation)
        return r

    def bind_approval(self, p, approval):
        now = self.clock()
        if not self.preview or self.preview[0] != self.key(p):
            raise ValueError('No matching dry run')
        if approval.ts.tzinfo is None or not self.preview[1] <= approval.ts <= now:
            raise ValueError('Stale approval')
        if now - self.preview[1] > self.ttl or not approval.approved or not approval.operator.strip():
            raise ValueError('Rejected or expired approval')
        if p.tier == 2 and approval.typed_confirmation != p.target:
            raise ValueError('Typed service confirmation required')
        self.binding = (self.key(p), approval.model_dump_json())

    def execute(self, p, approval):
        self.policy.check(p)
        if self.binding != (self.key(p), approval.model_dump_json()):
            raise ValueError('Approval not bound to this proposal')
        if self.preview[2] != "dry_run":
            raise ValueError("Execution requires action preview")
        self.bind_approval(p, approval)  # freshness recheck
        if self.attempts >= 2 or p.target in self.used or self.pending:
            raise ValueError('Action limit, repeat target, or verification pending')
        self.binding = None
        self.attempts += 1
        self.used.add(p.target)
        self.pending = self.key(p)
        return ActionResult.model_validate(self._call('execute', p, approval))

    def verified(self, p, result):
        if self.pending != self.key(p):
            raise ValueError('No matching pending action')
        if result.recovered:
            self.pending = None

    def rollback(self, p):
        # Rollback is another mutation: fresh preview and approval are mandatory.
        raise RuntimeError('Use approved_rollback with a separate rollback preview and approval')

    def approved_rollback(self, p, approval):
        if self.rolled_back or self.pending != self.key(p) or not self.adapter.supports_rollback:
            raise ValueError('Rollback unavailable')
        if self.binding != (self.key(p), approval.model_dump_json()):
            raise ValueError('Rollback approval not bound')
        self.bind_approval(p, approval)
        if self.preview[2] != "rollback_dry_run":
            raise ValueError("Rollback requires rollback preview")
        self.rolled_back = True
        self.binding = None
        return ActionResult.model_validate(self._call('rollback', p, approval))
