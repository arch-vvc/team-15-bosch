"""Safety and real Eyes interface tests with synthetic backends; no organiser API."""
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
from agent.main import Dependencies, EyesContext, run


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
    client.verified(p,VerifyResult(recovered=False,evidence_ids=['E1'],remaining_symptoms=['payment']))
    a=authorize(client,p)
    with pytest.raises(ValueError): client.approved_rollback(p,a)
    client.rollback_dry_run(p); a=approval(typed_confirmation=p.target); client.bind_approval(p,a)
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
        self.extra={}
        self.ev=Evidence(id='E1',tool='container_health',query={},ts=now(),onset_ts=None,
                         facts=[Fact(service='payment',signal='status',value='exited')])
    def get(self,eid):
        if eid=='E1': return self.ev
        return self.extra[eid]
    def all(self): return [self.ev,*self.extra.values()]
    def add(self,ev):
        eid=f'E{len(self.extra)+2}'
        self.extra[eid]=ev.model_copy(update={'id':eid},deep=True)
        return eid

def fresh_verification(ledger,recovered):
    eid=ledger.add(Evidence(tool='verification',query={},ts=now(),facts=[
        Fact(service='payment',signal='status',value='running' if recovered else 'exited')]))
    return VerifyResult(recovered=recovered,evidence_ids=[eid],remaining_symptoms=[])

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
    ledger=Ledger()
    d=Dependencies(ledger=ledger,triage=NS(snapshot=snapshot),engine=NS(diagnose=lambda *a:diagnosis()),
        validate=NS(check=lambda *a:ValidationResult(ok=True,problems=[])),investigator=None,remedy=Remedy(),actions=client,
        approval=NS(request=lambda *a:approval(approved=approved,typed_confirmation=a[0].target)),
        verify=NS(check=lambda *a,**kw:fresh_verification(ledger,recovered)),
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


def test_remedy_rejects_hypothesis_substituted_after_diagnosis():
    h=hypothesis(confidence=.69)
    assert Remedy().propose(h.model_copy(update={'confidence':.99}),diagnosis(h)) is None


def test_contract_eyes_compatibility_defaults():
    from agent.contracts import SIGNALS
    assert {'error_count','error_origin','error_message','log_count','deploy_event','uptime_s'} <= set(SIGNALS)
    e=Evidence(tool='test',query={},ts=now(),facts=[])
    assert e.id=='' and e.onset_ts is None and e.examples==[]


def test_rollback_cannot_precede_verification(client):
    p=proposal(); client.execute(p,authorize(client,p))
    with pytest.raises(ValueError): client.rollback_dry_run(p)


def test_rollback_always_requires_typed_service(client):
    p=proposal(); client.execute(p,authorize(client,p))
    client.verified(p,VerifyResult(recovered=False,evidence_ids=['E1'],remaining_symptoms=['payment']))
    client.rollback_dry_run(p)
    with pytest.raises(ValueError): client.bind_approval(p,approval())
    client.bind_approval(p,approval(typed_confirmation=p.target))


def test_second_forward_action_exhausts_rollback_budget(client):
    p=proposal(); client.execute(p,authorize(client,p))
    client.verified(p,VerifyResult(recovered=True,evidence_ids=['E1'],remaining_symptoms=['email']))
    q=p.model_copy(update={'target':'email'})
    client.execute(q,authorize(client,q))
    client.verified(q,VerifyResult(recovered=False,evidence_ids=['E1'],remaining_symptoms=['email']))
    with pytest.raises(ValueError): client.rollback_dry_run(q)
    assert 'rollback' not in client.adapter.calls


@pytest.mark.parametrize('raises', [True,False])
def test_ambiguous_or_rejected_mutation_closes_client(client,raises):
    p=proposal(); a=authorize(client,p)
    if raises:
        client.adapter.fail=True
        with pytest.raises(TimeoutError): client.execute(p,a)
    else:
        client.adapter.execute=lambda p:ActionResult(ok=False,response={},ts=now())
        assert not client.execute(p,a).ok
    with pytest.raises(ValueError): client.verified(p,VerifyResult(recovered=True,evidence_ids=['E1'],remaining_symptoms=[]))
    with pytest.raises(ValueError): client.rollback_dry_run(p)
    with pytest.raises(ValueError): client.dry_run(p)


def test_eyes_registry_adapter_uses_actual_tools_and_redaction(tb):
    from agent.tools import redact
    h=hypothesis(source='llm',cause='service_errors')
    class EvidenceModel:
        calls=0
        async def complete(self,**kw):
            self.calls+=1
            assert not redact.find_leaks(kw['history'])
            assert 'deploy_history' in kw['tools']
            if self.calls==1:
                return {'tool_calls':[{'name':'search_traces','arguments':{'service':'checkout'}}]}
            h.supporting_evidence=[kw['history'][-1]['evidence'][0]['id']]
            return {'output':{'hypotheses':[h.model_dump()],'selected_diagnosis':h.id,'reason':'Synthetic evidence-backed diagnosis'}}
    model=EvidenceModel()
    inv=Investigator.from_eyes(model,tb)
    snap=EyesContext(tb,None).snapshot()
    result=asyncio.run(inv.investigate(snap,tb.ledger,diagnosis()))
    assert result.state=='CONFIDENT' and model.calls==2
    assert not redact.find_leaks(tb.ledger.to_json())
    assert tb.ledger.has(result.hypotheses[0].supporting_evidence[0])


def test_eyes_registry_adapter_rejects_invalid_arguments(tb):
    model=Model([{'tool_calls':[{'name':'service_detail','arguments':{'service':'payment','extra':'bad'}}]}])
    inv=Investigator.from_eyes(model,tb)
    assert asyncio.run(inv.investigate(snapshot(),tb.ledger,diagnosis())).state=='UNKNOWN'
    assert len(tb.ledger)==0


def test_eyes_context_integrates_real_triage_and_verification(tb,world_backends):
    from tests import fakes
    from agent import detect
    _,world=world_backends
    alert=detect.watch(tb.b,tb.baseline,max_polls=3,sleep=lambda s:None)
    eyes=EyesContext(tb,alert,verification_sleep=lambda s:None)
    snap=eyes.snapshot()
    assert snap.evidence_ids and any(s.service=='payment' and s.error_ratio==.5 for s in snap.services)
    world.services=fakes.healthy_services()
    verified=eyes.check(alert,snap,window_s=150)
    assert verified.recovered and verified.evidence_ids
    assert all(tb.ledger.has(eid) for eid in verified.evidence_ids)


@pytest.mark.parametrize('llm_fallback',[False,True])
def test_orchestrator_with_real_eyes_pipeline(client,tb,world_backends,llm_fallback):
    """Production Eyes interfaces; fake world, rules/model, action API and E UI only."""
    from tests import fakes
    from agent import detect
    _,world=world_backends
    world.services=fakes.healthy_services()
    world.containers['payment']=fakes.container('payment',status='exited')
    alert=detect.watch(tb.b,tb.baseline,max_polls=3,sleep=lambda s:None)
    eyes=EyesContext(tb,alert,verification_sleep=lambda s:None)
    selected=[]
    def diagnose(snap,ledger,hints):
        eid=next(ev.id for ev in ledger.all() if any(
            f.service=='payment' and f.signal=='status' and f.value=='exited' for f in ev.facts))
        h=hypothesis(supporting_evidence=[eid]); selected.append(h)
        result=diagnosis(h)
        if llm_fallback: result.state='UNCERTAIN'
        return result
    class ModelDouble:
        async def complete(self,**kw):
            h=selected[-1].model_copy(update={'source':'llm'})
            return {'output':{'hypotheses':[h.model_dump()],'selected_diagnosis':h.id,'reason':'Synthetic stopped-container check'}}
    original=client.adapter.execute
    def fake_execute(p):
        result=original(p)
        world.containers[p.target]=fakes.container(p.target)
        return result
    client.adapter.execute=fake_execute
    stored=[]
    def validate(diagnosis,ledger):
        for h in diagnosis.hypotheses:
            for eid in h.supporting_evidence: ledger.get(eid)
        return ValidationResult(ok=True,problems=[])
    d=Dependencies(ledger=tb.ledger,triage=eyes,engine=NS(diagnose=diagnose),
        validate=NS(check=validate),investigator=Investigator.from_eyes(ModelDouble(),tb),
        remedy=Remedy(),actions=client,approval=NS(request=lambda *args:approval()),verify=eyes,
        report=NS(build=lambda i,l:(i.model_dump(mode='json'),'synthetic integration report')),
        memory=NS(similar=lambda s:[],store=stored.append))
    incident,report=asyncio.run(run(alert,d))
    assert incident.outcome=='recovered'
    assert len(incident.actions)==1 and incident.actions[0][3].recovered
    assert client.adapter.calls==['dry_run','execute'] and stored==[incident]
    assert ('LLM' in [s for _,s in incident.timeline])==llm_fallback


@pytest.mark.parametrize('evidence_id',['E999','E1'])
def test_missing_or_stale_verification_evidence_cannot_enter_memory(client,evidence_id):
    stored=[]
    d=Dependencies(ledger=Ledger(),triage=NS(snapshot=snapshot),engine=NS(diagnose=lambda *a:diagnosis()),
        validate=NS(check=lambda *a:ValidationResult(ok=True,problems=[])),investigator=None,
        remedy=Remedy(),actions=client,approval=NS(request=lambda *args:approval()),
        verify=NS(check=lambda *a,**kw:VerifyResult(recovered=True,evidence_ids=[evidence_id],remaining_symptoms=[])),
        report=NS(build=lambda i,l:(i.model_dump(mode='json'),'synthetic report')),
        memory=NS(similar=lambda s:[],store=stored.append))
    alert=Alert(service='payment',signal='error_ratio',value=1,baseline=0,ts=now())
    incident,_=asyncio.run(run(alert,d))
    assert incident.outcome=='escalated' and not stored


def test_log_failure_after_execution_cannot_be_cleared_by_verification(client):
    p=proposal(); a=authorize(client,p)
    original=client.log.append
    def fail_result(**record):
        if record.get('event')=='result': raise OSError('simulated audit failure')
        original(**record)
    client.log.append=fail_result
    with pytest.raises(OSError): client.execute(p,a)
    with pytest.raises(ValueError): client.verified(p,VerifyResult(recovered=True,evidence_ids=['E1'],remaining_symptoms=[]))
    with pytest.raises(ValueError): client.rollback_dry_run(p)


def test_unreadable_kill_marker_blocks_call(client):
    def unavailable(): raise PermissionError('simulated marker access failure')
    client.kill.path=NS(lstat=unavailable)
    with pytest.raises(PermissionError): client.dry_run(proposal())
    assert client.adapter.calls==[]


def test_dangling_kill_marker_blocks_call(client):
    client.kill.path.symlink_to(client.kill.path.parent/'missing-target')
    with pytest.raises(RuntimeError): client.dry_run(proposal())
    assert client.adapter.calls==[]


def test_audit_rejects_nonfinite_values_before_append(client):
    with pytest.raises(ValueError): client.log.append(event='synthetic',response={'value':float('nan')})
    from pathlib import Path
    assert not Path(client.log.path).exists()


def test_shared_ledger_rejects_concurrent_investigation():
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        class BlockingModel:
            async def complete(self,**kwargs):
                entered.set()
                await release.wait()
                return {'output':{'hypotheses':[],'selected_diagnosis':'UNKNOWN','reason':'No conclusion'}}
        ledger=Ledger()
        first=Investigator(BlockingModel(),{},lambda x:x)
        other_model=Model([{'output':{'hypotheses':[],'selected_diagnosis':'UNKNOWN','reason':'No conclusion'}}])
        other=Investigator(other_model,{},lambda x:x)
        task=asyncio.create_task(first.investigate(snapshot(),ledger,diagnosis()))
        try:
            await asyncio.wait_for(entered.wait(),1)
            result=await other.investigate(snapshot(),ledger,diagnosis())
            assert result.state=='UNKNOWN' and other_model.calls==0
        finally:
            release.set()
            await task
        await other.investigate(snapshot(),ledger,diagnosis())
        assert other_model.calls==1
    asyncio.run(scenario())


def test_timed_out_eyes_worker_keeps_ledger_guard(tb,monkeypatch):
    from threading import Event
    from agent.tools import registry
    entered,release=Event(),Event()
    def blocked_registry(*args):
        entered.set()
        if not release.wait(2): raise TimeoutError('synthetic worker release timed out')
        return {'evidence':[]}
    monkeypatch.setattr(registry,'call',blocked_registry)
    async def scenario():
        model=Model([{'tool_calls':[{'name':'container_health','arguments':{}}]}])
        inv=Investigator.from_eyes(model,tb,timeout_s=.02)
        task=asyncio.create_task(inv.investigate(snapshot(),tb.ledger,diagnosis()))
        other_model=Model([{'output':{'hypotheses':[],'selected_diagnosis':'UNKNOWN','reason':'No conclusion'}}])
        other=Investigator.from_eyes(other_model,tb)
        try:
            assert await asyncio.to_thread(entered.wait,1)
            assert (await task).state=='UNKNOWN'
            assert (await other.investigate(snapshot(),tb.ledger,diagnosis())).state=='UNKNOWN'
            assert other_model.calls==0
        finally:
            release.set()
            await task
        async def retry_after_worker_exit():
            while other_model.calls==0:
                await other.investigate(snapshot(),tb.ledger,diagnosis())
                await asyncio.sleep(.001)
        await asyncio.wait_for(retry_after_worker_exit(),1)
        assert other_model.calls==1
    asyncio.run(scenario())


def test_eyes_investigator_rejects_mismatched_ledger(tb):
    model=Model([])
    inv=Investigator.from_eyes(model,tb)
    assert asyncio.run(inv.investigate(snapshot(),Ledger(),diagnosis())).state=='UNKNOWN'
    assert model.calls==0


def test_overlapping_action_calls_cannot_share_approval(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    p=proposal(); a=authorize(client,p)
    entered,release=Event(),Event()
    def blocking_clock():
        entered.set()
        if not release.wait(2): raise TimeoutError('synthetic release timed out')
        return now()
    client.clock=blocking_clock
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(client.execute,p,a)
        try:
            assert entered.wait(1)
            with pytest.raises(ValueError,match='Another operation'):
                client.execute(p,a)
            assert 'execute' not in client.adapter.calls
        finally:
            release.set()
        assert future.result(timeout=2).ok
    assert client.adapter.calls.count('execute')==1


def test_fsync_failure_prevents_mutation_dispatch(client,monkeypatch):
    p=proposal(); a=authorize(client,p)
    def failed_fsync(fd): raise OSError('synthetic fsync failure')
    monkeypatch.setattr('agent.act.action_log.os.fsync',failed_fsync)
    with pytest.raises(OSError): client.execute(p,a)
    assert client.adapter.calls==['dry_run'] and client.faulted


@pytest.mark.parametrize('failed',[False,True])
def test_rollback_never_allows_recovery_claim(client,failed):
    p=proposal(); client.execute(p,authorize(client,p))
    client.verified(p,VerifyResult(recovered=False,evidence_ids=['E1'],remaining_symptoms=['payment']))
    client.rollback_dry_run(p)
    a=approval(typed_confirmation=p.target); client.bind_approval(p,a)
    if failed:
        def ambiguous(p): raise TimeoutError('synthetic ambiguous rollback')
        client.adapter.rollback=ambiguous
        with pytest.raises(TimeoutError): client.approved_rollback(p,a)
        assert client.faulted
    else:
        assert client.approved_rollback(p,a).ok
    with pytest.raises(ValueError): client.verified(p,VerifyResult(recovered=True,evidence_ids=['E2'],remaining_symptoms=[]))
    assert client.rolled_back and client.attempts==2


def test_kill_switch_rechecked_before_rollback(client):
    p=proposal(); client.execute(p,authorize(client,p))
    client.verified(p,VerifyResult(recovered=False,evidence_ids=['E1'],remaining_symptoms=['payment']))
    client.rollback_dry_run(p)
    a=approval(typed_confirmation=p.target); client.bind_approval(p,a)
    client.kill.path.touch()
    with pytest.raises(RuntimeError): client.approved_rollback(p,a)
    assert client.adapter.calls.count('rollback')==0


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-float('inf')])
def test_fact_rejects_nonfinite_values(value):
    with pytest.raises(ValidationError): Fact(service='payment',signal='error_ratio',value=value)
