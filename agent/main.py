"""Dependency-injected vertical slice against Part 2 synchronous teammate interfaces.
Run with `python -m agent.main` for configuration status; tests supply explicit doubles.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from agent.contracts import Incident

@dataclass
class EyesContext:
    """Adapt Person A's actual Toolbox-based functions to Part 2 dependencies.

    Pass this object as both ``triage`` and ``verify`` in Dependencies. Baseline
    remains owned by the toolbox; no second baseline is inferred from a snapshot.
    """
    toolbox: object
    alert: object
    verification_sleep: object = None

    def snapshot(self):
        from agent import triage
        return triage.snapshot(self.toolbox, self.alert)

    def check(self, alert, baseline, window_s=150):
        from agent import verify
        options = {'sleep': self.verification_sleep} if self.verification_sleep is not None else {}
        return verify.check(self.toolbox, alert, window_s=window_s, **options)

@dataclass
class Dependencies:
    ledger: object
    triage: object
    engine: object
    validate: object
    investigator: object
    remedy: object
    actions: object  # fresh Actions instance for this incident
    approval: object
    verify: object
    report: object
    memory: object

async def run(alert, d):
    timeline = []
    def transition(state):
        timeline.append((datetime.now(timezone.utc), state))
    transition('ALERT')
    incident = None
    diagnoses, actions = [], []
    outcome = 'escalated'
    snapshot = None
    try:
        for _ in range(2):
            transition('TRIAGE')
            snapshot = d.triage.snapshot()
            transition('DIAGNOSE')
            diagnosis = d.engine.diagnose(snapshot, d.ledger, d.memory.similar(snapshot))
            if diagnosis.state == 'UNCERTAIN':
                transition('LLM')
                diagnosis = await d.investigator.investigate(snapshot, d.ledger, diagnosis)
            diagnoses.append(diagnosis)
            transition('VALIDATE')
            if diagnosis.state != 'CONFIDENT' or not d.validate.check(diagnosis, d.ledger).ok:
                break
            h = next(h for h in diagnosis.hypotheses if h.id == diagnosis.selected_id)
            transition('REMEDY')
            p = d.remedy.propose(h, diagnosis)
            if p is None:
                break
            transition('DRY_RUN')
            dry = d.actions.dry_run(p)
            transition('APPROVAL')
            approval = d.approval.request(p, diagnosis, dry, d.ledger)
            actions.append((p, approval, None, None))
            if not approval.approved:
                outcome = 'rejected'
                break
            d.actions.bind_approval(p, approval)
            transition('EXECUTE')
            result = d.actions.execute(p, approval)
            actions[-1] = (p, approval, result, None)
            if not result.ok:
                # Failure can be partial. Never retry an uncertain mutation.
                break
            transition('VERIFY')
            previous_evidence = {item.id for item in d.ledger.all()}
            verified = d.verify.check(alert, snapshot, window_s=150)
            if not verified.evidence_ids:
                raise ValueError('Recovery verification requires ledger evidence')
            for evidence_id in verified.evidence_ids:
                evidence = d.ledger.get(evidence_id)
                if evidence_id in previous_evidence or not evidence.redacted or not evidence.facts:
                    raise ValueError('Recovery requires fresh, redacted, typed ledger evidence')
            actions[-1] = (p, approval, result, verified)
            d.actions.verified(p, verified)
            if not verified.recovered:
                if d.actions.adapter.supports_rollback:
                    transition('ROLLBACK_DRY_RUN')
                    dry = d.actions.rollback_dry_run(p)
                    transition('ROLLBACK_APPROVAL')
                    rollback_approval = d.approval.request(p, diagnosis, dry, d.ledger)
                    d.actions.bind_approval(p, rollback_approval)
                    transition('ROLLBACK')
                    d.actions.approved_rollback(p, rollback_approval)
                break
            if not verified.remaining_symptoms:
                outcome = 'recovered'
                break
    except Exception as exc:
        transition('ERROR:' + type(exc).__name__)
    if snapshot is None:
        # Cannot fabricate the required shared Incident.snapshot.
        raise RuntimeError('Triage unavailable; cannot build a truthful Incident')
    if outcome != 'recovered':
        transition('ESCALATE' if outcome != 'rejected' else 'REJECTED')
    transition('REPORT')
    incident = Incident(alert=alert, snapshot=snapshot, diagnoses=diagnoses,
                        actions=actions, outcome=outcome, timeline=timeline)
    report = d.report.build(incident, d.ledger)
    if outcome == 'recovered':
        d.memory.store(incident)
    return incident, report

if __name__ == '__main__':
    print('Person C core ready. Supply Part 2 dependencies to run(); real Action API disabled.')
