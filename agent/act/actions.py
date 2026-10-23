"""No HTTP adapter exists until organisers supply the official contract.
Adapter methods are local Python boundaries, not assumed API wire payloads.
A new Actions instance is required for every incident. Single orchestrator owner.
"""
from datetime import datetime, timezone, timedelta
import json
from functools import wraps
from threading import RLock
from typing import Protocol
from agent.contracts import ActionResult, DryRunResult
from .remedy import Remedy


def _serialized(method):
    """Reject overlapping callers; nested approval rechecks use the same lock."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if not self._state_lock.acquire(blocking=False):
            raise ValueError('Another operation owns this incident action client')
        try:
            return method(self, *args, **kwargs)
        finally:
            self._state_lock.release()
    return guarded

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
        self._state_lock = RLock()
        self.adapter, self.kill, self.log = adapter, kill, log
        self.policy = policy or Remedy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ttl = timedelta(seconds=ttl_s)
        if self.ttl <= timedelta(0):
            raise ValueError('Approval TTL must be positive')
        self.preview = self.binding = self.pending = None
        self.attempts, self.used, self.rolled_back = 0, set(), False
        self.execution_ok = self.verification_failed = self.faulted = False

    @staticmethod
    def key(p):
        return json.dumps(p.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))

    def _rollback_allowed(self, p):
        if (self.faulted or self.rolled_back or self.attempts >= 2
                or self.pending != self.key(p) or not self.execution_ok
                or not self.verification_failed or not self.adapter.supports_rollback):
            raise ValueError('Rollback unavailable; requires a known execution and failed verification')

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

    @_serialized
    def rollback_dry_run(self, p):
        self._rollback_allowed(p)
        result = self._preview(p, "rollback_dry_run")
        return result.model_copy(update={
            'would_do': 'ROLLBACK (separate approval; type the target service): ' + result.would_do})

    @_serialized
    def dry_run(self, p):
        return self._preview(p, "dry_run")

    def _preview(self, p, operation):
        if self.faulted or self.rolled_back:
            raise ValueError('Incident action client is closed; escalate')
        self.policy.check(p)
        self.binding = self.preview = None
        r = DryRunResult.model_validate(self._call(operation, p))
        if self.key(r.proposal) != self.key(p) or r.checks_failed:
            raise ValueError('Dry run mismatch or failed checks')
        self.preview = (self.key(p), self.clock(), operation)
        return r

    @_serialized
    def bind_approval(self, p, approval):
        now = self.clock()
        if not self.preview or self.preview[0] != self.key(p):
            raise ValueError('No matching dry run')
        if approval.ts.tzinfo is None or not self.preview[1] <= approval.ts <= now:
            raise ValueError('Stale approval')
        if now - self.preview[1] > self.ttl or not approval.approved or not approval.operator.strip():
            raise ValueError('Rejected or expired approval')
        if (p.tier == 2 or self.preview[2] == 'rollback_dry_run') and approval.typed_confirmation != p.target:
            raise ValueError('Typed service confirmation required')
        self.binding = (self.key(p), approval.model_dump_json())

    @_serialized
    def execute(self, p, approval):
        self.policy.check(p)
        if self.faulted or self.rolled_back:
            raise ValueError('Incident action client is closed; escalate')
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
        self.execution_ok = self.verification_failed = False
        try:
            result = ActionResult.model_validate(self._call('execute', p, approval))
        except Exception:
            self.faulted = True
            raise
        self.execution_ok = result.ok
        if not result.ok:
            self.faulted = True  # Failed acceptance may represent a partial mutation.
        return result

    @_serialized
    def verified(self, p, result):
        if self.pending != self.key(p) or not self.execution_ok or self.faulted or self.rolled_back:
            raise ValueError('No matching pending action')
        if not result.evidence_ids:
            raise ValueError('Verification requires evidence IDs')
        self.verification_failed = not result.recovered
        if result.recovered:
            self.pending = None

    def rollback(self, p):
        # Rollback is another mutation: fresh preview and approval are mandatory.
        raise RuntimeError('Use approved_rollback with a separate rollback preview and approval')

    @_serialized
    def approved_rollback(self, p, approval):
        self._rollback_allowed(p)
        if self.binding != (self.key(p), approval.model_dump_json()):
            raise ValueError('Rollback approval not bound')
        self.bind_approval(p, approval)
        if self.preview[2] != "rollback_dry_run":
            raise ValueError("Rollback requires rollback preview")
        self.rolled_back = True
        self.attempts += 1  # The two-mutation budget includes rollback.
        self.binding = None
        try:
            result = ActionResult.model_validate(self._call('rollback', p, approval))
        except Exception:
            self.faulted = True
            self.execution_ok = False
            raise
        if not result.ok:
            self.faulted = True
            self.execution_ok = False
        return result
