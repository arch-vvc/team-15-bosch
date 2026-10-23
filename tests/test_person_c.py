"""Local doubles only: these do not claim to implement the organiser API."""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
import pytest
from pydantic import ValidationError
from agent.contracts import *
from agent.act.actions import Actions, DisabledAdapter
from agent.act.remedy import Remedy
from agent.act.killswitch import KillSwitch
from agent.act.action_log import ActionLog
from agent.llm.investigator import Investigator, ReadOnlyTool
from agent.main import Dependencies, run


def now(): return datetime.now(timezone.utc)
def hypothesis(**kw):
    return Hypothesis(**dict(id='H1', component='payment', cause='container_down', confidence=.9,
        supporting_evidence=['E1'], contradicting_evidence=[], missing_evidence=[], source='rules') | kw)
def diagnosis(h=None):
    return Diagnosis(state='CONFIDENT', hypotheses=[h or hypothesis()], selected_id='H1', reason='Observed stopped container')
def proposal(): return Remedy().propose(hypothesis(), diagnosis())
def approval(**kw):
    return Approval(**dict(approved=True, operator='tester', typed_confirmation=None, ts=now()) | kw)

class FakeAdapter:
    supports_rollback = True
    def __init__(self): self.calls = []; self.fail = False
    def dry_run(self, p):
        self.calls.append('dry_run')
        return DryRunResult(proposal=p, would_do='TEST ONLY', checks_passed=['fake'], checks_failed=[])
    def rollback_dry_run(self, p): return self.dry_run(p)
    def execute(self, p):
        self.calls.append('execute')
        if self.fail: raise TimeoutError('unknown result')
        return ActionResult(ok=True, response={'test_only': True}, ts=now())
    def rollback(self, p):
        self.calls.append('rollback')
        return ActionResult(ok=True, response={'test_only': True}, ts=now())

@pytest.fixture
def client(tmp_path):
    return Actions(FakeAdapter(), KillSwitch(tmp_path/'KILL'), ActionLog(tmp_path/'audit.jsonl'))

def authorize(c, p):
    c.dry_run(p); a = approval(); c.bind_approval(p,a); return a

@pytest.mark.parametrize('value', [-.1, 1.1, float('nan')])
def test_contract_confidence(value):
    with pytest.raises(ValidationError): hypothesis(confidence=value)

@pytest.mark.parametrize('confidence,target,expected',[(.69,'payment',False),(.7,'payment',True),(.84,'astronomy-db',False),(.85,'astronomy-db',True),(.99,'unknown',False)])
def test_tiers(confidence,target,expected):
    h=hypothesis(component=target,confidence=confidence)
    assert bool(Remedy().propose(h,diagnosis(h))) == expected

def test_unknown_cause():
    h=hypothesis(cause='unmapped'); assert Remedy().propose(h,diagnosis(h)) is None

@pytest.mark.parametrize('change',[{'target':'flagd'}, {'params':{'other':'cart'}}, {'tier':3}, {'action':'toggle_flag'}])
def test_policy_blocks(client,change):
    p=proposal().model_copy(update=change)
    with pytest.raises(ValueError): client.dry_run(p)
    assert client.adapter.calls == []

def test_no_approval_or_wrong_proposal(client):
    p=proposal()
    with pytest.raises(ValueError): client.execute(p,approval())
    a=authorize(client,p)
    with pytest.raises(ValueError): client.execute(p.model_copy(update={'reason':'different'}),a)

def test_stale_and_rejected(client):
    p=proposal(); client.dry_run(p)
    for a in [approval(ts=now()-timedelta(minutes=5)),approval(approved=False),approval(operator='')]:
        with pytest.raises(ValueError): client.bind_approval(p,a)

def test_t2_typed(client):
    h=hypothesis(component='astronomy-db'); p=Remedy().propose(h,diagnosis(h)); client.dry_run(p)
    with pytest.raises(ValueError): client.bind_approval(p,approval())
    client.bind_approval(p,approval(typed_confirmation='astronomy-db'))

def test_kill_after_approval(client):
    p=proposal(); a=authorize(client,p); client.kill.path.touch()
    with pytest.raises(RuntimeError): client.execute(p,a)
    assert 'execute' not in client.adapter.calls

def test_env_kill(client,monkeypatch):
    monkeypatch.setenv('INCIDENT_ZERO_KILL','true')
    with pytest.raises(RuntimeError): client.dry_run(proposal())

def test_log_failure_blocks_call(client):
    client.log=NS(append=lambda **kw: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError): client.dry_run(proposal())
    assert not client.adapter.calls

def test_action_limits_and_verification(client):
    p=proposal(); client.execute(p,authorize(client,p))
    q=p.model_copy(update={'target':'email'})
    with pytest.raises(ValueError): client.execute(q,authorize(client,q))
    client.verified(p,VerifyResult(recovered=True,evidence_ids=['E1'],remaining_symptoms=['email']))
    with pytest.raises(ValueError): client.execute(p,authorize(client,p))
    client.execute(q,authorize(client,q))
    client.verified(q,VerifyResult(recovered=True,evidence_ids=['E1'],remaining_symptoms=[]))
    r=p.model_copy(update={'target':'cart'})
    with pytest.raises(ValueError): client.execute(r,authorize(client,r))

def test_api_failure_consumes_attempt(client):
    p=proposal(); a=authorize(client,p); client.adapter.fail=True
    with pytest.raises(TimeoutError): client.execute(p,a)
    assert client.attempts==1 and client.pending
    with pytest.raises(ValueError): client.execute(p,a)

def test_rollback_needs_separate_preview_and_only_once(client):
    p=proposal(); client.execute(p,authorize(client,p))
    a=authorize(client,p)
    with pytest.raises(ValueError): client.approved_rollback(p,a)
    client.rollback_dry_run(p); a=approval(); client.bind_approval(p,a)
    assert client.approved_rollback(p,a).ok
    with pytest.raises(ValueError): client.approved_rollback(p,a)

def test_disabled_adapter(client):
    client.adapter=DisabledAdapter()
    with pytest.raises(RuntimeError): client.dry_run(proposal())

def test_append_only(client):
    client.dry_run(proposal()); before=open(client.log.path).read()
    client.dry_run(proposal()); assert open(client.log.path).read().startswith(before)

class Ledger:
    def __init__(self):
        self.ev=Evidence(id='E1',tool='container_health',query={},ts=now(),onset_ts=None,
                         facts=[Fact(service='payment',signal='status',value='exited')])
    def get(self,eid):
        if eid!='E1': raise KeyError(eid)
        return self.ev
    def all(self): return [self.ev]

def snapshot():
    return TriageSnapshot(ts=now(),services=[ServiceStatus(service='payment',req_rate=1,error_ratio=1,p95_ms=10,baseline={})],containers=[],dependencies=[],evidence_ids=['E1'])

class Model:
    def __init__(self,replies): self.replies=iter(replies); self.calls=0
    async def complete(self,**kw): self.calls+=1; return next(self.replies)

def test_llm_valid_and_bad_citations():
    h=hypothesis(source='llm')
    def output(h): return {'output':{'hypotheses':[h.model_dump()], 'selected_diagnosis':'H1','reason':'Observed'}}
    good=Model([output(h)])
    assert asyncio.run(Investigator(good,{},lambda x:x).investigate(snapshot(),Ledger(),diagnosis())).state=='CONFIDENT'
    h.supporting_evidence=['E999']; bad=Model([output(h),output(h)])
    assert asyncio.run(Investigator(bad,{},lambda x:x).investigate(snapshot(),Ledger(),diagnosis())).state=='UNKNOWN'
    assert bad.calls==2

def test_llm_tool_budget():
    calls=[]
    ledger=Ledger()
    async def tool(): calls.append(1); return [ledger.ev]
    request={'tool_calls':[{'name':'container_health','arguments':{}}]}
    model=Model([request]*9)
    inv=Investigator(model,{'container_health':ReadOnlyTool(tool,{})},lambda x:x)
    assert asyncio.run(inv.investigate(snapshot(),ledger,diagnosis())).state=='UNKNOWN'
    assert len(calls)==8

def test_llm_timeout_and_registry():
    with pytest.raises(ValueError): Investigator(None,{'execute':ReadOnlyTool(None,{})},lambda x:x)
    class Slow:
        async def complete(self,**kw): await asyncio.sleep(1)
    inv=Investigator(Slow(),{},lambda x:x,timeout_s=.001)
    assert asyncio.run(inv.investigate(snapshot(),Ledger(),diagnosis())).state=='UNKNOWN'

@pytest.mark.parametrize('recovered,approved,expected',[(True,True,'recovered'),(False,True,'escalated'),(True,False,'rejected')])
def test_vertical_slice(client,recovered,approved,expected):
    stored=[]
    d=Dependencies(ledger=Ledger(),triage=NS(snapshot=snapshot),engine=NS(diagnose=lambda *a:diagnosis()),
        validate=NS(check=lambda *a:ValidationResult(ok=True,problems=[])),investigator=None,remedy=Remedy(),actions=client,
        approval=NS(request=lambda *a:approval(approved=approved)),
        verify=NS(check=lambda *a,**kw:VerifyResult(recovered=recovered,evidence_ids=['E1'],remaining_symptoms=[])),
        report=NS(build=lambda i,l:(i.model_dump(mode='json'),'test report')),memory=NS(similar=lambda s:[],store=stored.append))
    alert=Alert(service='payment',signal='error_ratio',value=1,baseline=0,ts=now())
    incident,report=asyncio.run(run(alert,d))
    assert incident.outcome==expected
    states=[state for _,state in incident.timeline]
    assert states[-1]=='REPORT' and states[:3]==['ALERT','TRIAGE','DIAGNOSE']
    assert bool(stored)==(expected=='recovered')
    if not recovered and approved: assert client.adapter.calls.count('rollback')==1

def test_signal_and_mutable_defaults():
    with pytest.raises(ValidationError): Fact(service='payment',signal='made_up',value=1)
    first=proposal(); other=proposal(); first.params['x']=1
    assert other.params=={}

def test_dry_run_failure_and_mismatch(client):
    p=proposal()
    client.adapter.dry_run=lambda p: DryRunResult(proposal=p,would_do='blocked',checks_passed=[],checks_failed=['unsafe'])
    with pytest.raises(ValueError): client.dry_run(p)
    with pytest.raises(ValueError): client.bind_approval(p,approval())

def test_expired_approval_at_execution(client):
    p=proposal(); a=authorize(client,p)
    client.clock=lambda: now()+timedelta(minutes=5)
    with pytest.raises(ValueError): client.execute(p,a)

def test_post_call_log_failure_blocks_further_action(client):
    p=proposal(); a=authorize(client,p)
    old=client.log.append
    def log(**kw):
        if kw.get('event')=='result': raise OSError('audit failure')
        old(**kw)
    client.log.append=log
    with pytest.raises(OSError): client.execute(p,a)
    assert client.pending and client.attempts==1

def test_llm_forbidden_service_never_called():
    called=[]
    async def tool(**kw): called.append(kw); return []
    model=Model([{'tool_calls':[{'name':'container_health','arguments':{'service':'flagd'}}]}])
    inv=Investigator(model,{'container_health':ReadOnlyTool(tool,{})},lambda x:x)
    assert asyncio.run(inv.investigate(snapshot(),Ledger(),diagnosis())).state=='UNKNOWN'
    assert not called
