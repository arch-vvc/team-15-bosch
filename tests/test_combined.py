"""The combined agent: Eyes -> rules engine and validator -> Aryan's orchestrator and action
safety -> approval, report and memory. Runs on the simulated demo, no Docker or LLM."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent import baseline as baseline_mod
from agent import detect, validate
from agent.act.actions import Actions
from agent.approval.cli import AutoReject, CliApproval
from agent.contracts import ActionResult, Diagnosis, DryRunResult, Hypothesis
from agent.ledger import Ledger
from agent.llm.investigator import Investigator
from agent.llm.responses import NoModel, ResponsesAdapter
from agent.main import EyesContext
from agent.memory import Memory, fingerprint
from agent.rules.engine import Engine
from agent.run import dependencies, handle
from agent.tools import Toolbox
from agent import triage
from tests import fakes
from tests.make_fixture import CONTAINER_DOWN, OUT as PAYMENT_FAILURE, container_down_world, stop_payment

NOW = datetime.fromtimestamp(fakes.T0, timezone.utc)


def snapshot_for(world=None, after_baseline=None):
    b, w = fakes.backends(world)
    base = baseline_mod.compute(b, end_offset_min=10)
    if after_baseline:
        after_baseline(w)
    tb = Toolbox(b, Ledger(), base)
    found = detect.poll(b, base)
    return tb, triage.snapshot(tb, detect.pick(found) if found else None), w


# --- engine --------------------------------------------------------------------------------

def test_engine_names_payment_for_payment_failure():
    tb, snap, _ = snapshot_for()
    d = Engine().diagnose(snap, tb.ledger)
    assert d.state == "CONFIDENT"
    h = d.hypotheses[0]
    assert (h.component, h.cause, h.source) == ("payment", "service_errors", "rules")
    assert all(tb.ledger.has(e) for e in h.supporting_evidence)
    assert validate.check(d, tb.ledger).ok


def test_engine_names_a_stopped_container():
    tb, snap, _ = snapshot_for(container_down_world(), after_baseline=stop_payment)
    d = Engine().diagnose(snap, tb.ledger)
    assert d.state == "CONFIDENT" and (d.hypotheses[0].component, d.hypotheses[0].cause) == ("payment", "container_down")


def test_engine_cpu_starvation():
    def starve(w):
        w.services = fakes.healthy_services()
        w.services["ad"]["p95"] = 900.0
        w.containers["ad"]["HostConfig"]["NanoCpus"] = int(0.05e9)   # usage 0.05 cores: pinned at the cap
    tb, snap, _ = snapshot_for(after_baseline=starve)
    d = Engine().diagnose(snap, tb.ledger)
    assert (d.state, d.hypotheses[0].component, d.hypotheses[0].cause) == ("CONFIDENT", "ad", "cpu_starvation")
    assert "high_cpu" not in {h.cause for h in d.hypotheses}           # ruled out by the lowered limit


def test_engine_high_cpu_without_a_limit():
    def busy(w):
        w.services = fakes.healthy_services()
        w.services["ad"]["p95"] = 900.0
        w.usage["ad"] = (1.8, 120.0)          # 1.8 cores against a 0.05-core baseline, no CPU limit set
    tb, snap, _ = snapshot_for(after_baseline=busy)
    d = Engine().diagnose(snap, tb.ledger)
    assert (d.state, d.hypotheses[0].component, d.hypotheses[0].cause) == ("CONFIDENT", "ad", "high_cpu")


def test_engine_hands_unexplained_symptoms_to_the_llm():
    def slow(w):
        w.services = fakes.healthy_services()
        w.services["shipping"]["p95"] = 900.0       # slow, but no rule explains it
    tb, snap, _ = snapshot_for(after_baseline=slow)
    d = Engine().diagnose(snap, tb.ledger)
    assert d.state == "UNCERTAIN" and "shipping:p95_ms" in d.reason


def test_engine_unknown_when_nothing_is_abnormal():
    tb, snap, _ = snapshot_for(after_baseline=lambda w: setattr(w, "services", fakes.healthy_services()))
    assert Engine().diagnose(snap, tb.ledger).state == "UNKNOWN"


def _facts_engine(tmp_path, signatures):
    path = tmp_path / "sig.yaml"
    path.write_text(json.dumps({"minimum_confidence": 0.7, "minimum_margin": 0.15, "unknown_below": 0.3,
                                "signatures": signatures}))
    return Engine(path)


def test_engine_rivals_on_one_call_path_are_uncertain_unrelated_are_not(tmp_path):
    tb, snap, _ = snapshot_for()
    sig = lambda i, cause, conf, svc: {"id": i, "cause": cause, "confidence": conf, "summary": i,
                                       "all": [{"signal": "error_ratio", "direction": "up"}]}
    engine = _facts_engine(tmp_path, [sig("a", "x", 0.8, None)])
    d = engine.diagnose(snap, tb.ledger)      # payment, checkout, frontend all match, all on one path
    assert d.state == "UNCERTAIN" and d.selected_id is None
    snap2 = snap.model_copy(update={"dependencies": []})
    assert engine.diagnose(snap2, tb.ledger).state == "CONFIDENT"


def test_memory_hint_only_reorders(tmp_path):
    tb, snap, _ = snapshot_for()
    engine = _facts_engine(tmp_path, [{"id": "a", "cause": "x", "confidence": 0.8, "summary": "a",
                                       "all": [{"signal": "error_ratio", "direction": "up"}]}])
    snap2 = snap.model_copy(update={"dependencies": []})
    plain = engine.diagnose(snap2, tb.ledger)
    hinted = engine.diagnose(snap2, tb.ledger, hints=[("M1", 0.9, "frontend", "x")])
    assert hinted.hypotheses[0].component == "frontend"
    assert [h.confidence for h in hinted.hypotheses] == [h.confidence for h in plain.hypotheses]


# --- validator -----------------------------------------------------------------------------

def test_validator_rejects_invented_and_unrelated_evidence():
    tb, snap, _ = snapshot_for()
    d = Engine().diagnose(snap, tb.ledger)
    h = d.hypotheses[0]
    bad = d.model_copy(update={"hypotheses": [h.model_copy(update={"supporting_evidence": ["E999"]})]})
    assert "E999 is not in the ledger" in validate.check(bad, tb.ledger).problems
    overview_only = [e for e in snap.evidence_ids if tb.ledger.get(e).tool == "deploy_history"]
    unrelated = d.model_copy(update={"hypotheses": [h.model_copy(update={"supporting_evidence": overview_only})]})
    assert not validate.check(unrelated, tb.ledger).ok
    not_a_service = d.model_copy(update={"hypotheses": [h.model_copy(update={"component": "network"})]})
    assert not validate.check(not_a_service, tb.ledger).ok


# --- approval, report, memory --------------------------------------------------------------

def _proposal(tier=1, target="payment"):
    from agent.contracts import ActionProposal
    return ActionProposal(action="restart_service", target=target, tier=tier, reason="container_down", hypothesis_id="H1")


def _diag():
    h = Hypothesis(id="H1", component="payment", cause="container_down", confidence=0.9, supporting_evidence=[],
                   contradicting_evidence=[], missing_evidence=[], source="rules")
    return Diagnosis(state="CONFIDENT", hypotheses=[h], selected_id="H1", reason="test")


@pytest.mark.parametrize("tier,answer,approved", [(1, "y", True), (1, "", False), (1, "yes please", False),
                                                  (2, "payment", True), (2, "y", False)])
def test_cli_approval(tier, answer, approved):
    p = _proposal(tier)
    dry = DryRunResult(proposal=p, would_do="restart", checks_passed=[], checks_failed=[])
    shown = []
    a = CliApproval(input_fn=lambda prompt: answer, print_fn=shown.append, operator="archit").request(p, _diag(), dry, Ledger())
    assert a.approved is approved and a.operator == "archit"
    assert "APPROVAL NEEDED" in shown[0]


def test_cli_approval_eof_rejects():
    def eof(prompt):
        raise EOFError
    p = _proposal()
    dry = DryRunResult(proposal=p, would_do="restart", checks_passed=[], checks_failed=[])
    assert CliApproval(input_fn=eof, print_fn=lambda s: None, operator="x").request(p, _diag(), dry, Ledger()).approved is False


def test_memory_stores_only_recovered(tmp_path):
    tb, snap, _ = snapshot_for()
    from agent.contracts import Alert, Incident
    alert = Alert(service="payment", signal="error_ratio", value=0.5, baseline=0, ts=NOW)
    d = Engine().diagnose(snap, tb.ledger)
    mem = Memory(tmp_path / "m.json")
    mem.store(Incident(alert=alert, snapshot=snap, diagnoses=[d], actions=[], outcome="escalated"))
    assert mem.similar(snap) == []
    mem.store(Incident(alert=alert, snapshot=snap, diagnoses=[d], actions=[], outcome="recovered"))
    [(mid, score, comp, cause)] = mem.similar(snap)
    assert (score, comp, cause) == (1.0, "payment", "service_errors")
    assert "payment:error_ratio:up" in fingerprint(snap)


# --- LLM adapter ---------------------------------------------------------------------------

def test_responses_adapter_parses_tool_calls_and_final_answer():
    tool = {"output": [{"type": "function_call", "name": "search_traces", "arguments": '{"service": "payment"}'}]}
    final = {"output": [{"type": "function_call", "name": "submit_diagnosis",
                         "arguments": '{"hypotheses": [], "selected_diagnosis": "UNKNOWN", "reason": "r"}'}]}
    assert ResponsesAdapter.parse(tool) == {"tool_calls": [{"name": "search_traces", "arguments": {"service": "payment"}}]}
    assert ResponsesAdapter.parse(final)["output"]["selected_diagnosis"] == "UNKNOWN"


def test_responses_adapter_round_trip_through_investigator():
    tb, snap, _ = snapshot_for()

    class Client:
        def __init__(self):
            self.turn = 0

        def respond(self, instructions, conversation, tools, max_tokens, timeout):
            self.turn += 1
            assert any(t["name"] == "submit_diagnosis" for t in tools)
            assert not any(word in t["name"] for t in tools for word in ("restart", "toggle", "throttle", "scale"))
            if self.turn == 1:
                return {"output": [{"type": "function_call", "name": "search_traces", "arguments": '{"service": "payment"}'}]}
            eid = [e for e in snap.evidence_ids if tb.ledger.get(e).tool == "search_traces"][0]
            h = {"id": "H1", "component": "payment", "cause": "service_errors", "confidence": 0.8,
                 "supporting_evidence": [eid], "contradicting_evidence": [], "missing_evidence": [], "source": "llm"}
            return {"output": [{"type": "function_call", "name": "submit_diagnosis",
                                "arguments": json.dumps({"hypotheses": [h], "selected_diagnosis": "H1", "reason": "r"})}]}

    inv = Investigator.from_eyes(ResponsesAdapter(Client()), tb)
    d = asyncio.run(inv.investigate(snap, tb.ledger, Engine().diagnose(snap, tb.ledger)))
    assert d.state == "CONFIDENT" and d.hypotheses[0].component == "payment"


def test_no_model_means_unknown():
    tb, snap, _ = snapshot_for()
    d = asyncio.run(Investigator.from_eyes(NoModel(), tb).investigate(snap, tb.ledger, Engine().diagnose(snap, tb.ledger)))
    assert d.state == "UNKNOWN"


# --- the whole agent -----------------------------------------------------------------------

class FixingAdapter:
    """A stand-in Action API that really fixes the simulated outage when asked to restart payment."""
    supports_rollback = False

    def __init__(self, world):
        self.world, self.calls = world, []

    def dry_run(self, p):
        return DryRunResult(proposal=p, would_do=f"{p.action} {p.target}", checks_passed=["fake"], checks_failed=[])

    def execute(self, p):
        self.calls.append(p.target)
        self.world.containers["payment"] = fakes.container("payment")
        self.world.services = fakes.healthy_services()
        self.world.edges = list(self.world.baseline_edges)
        return ActionResult(ok=True, response={"accepted": True}, ts=datetime.now(timezone.utc))


def test_full_loop_recovers_and_learns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b, world = fakes.backends(container_down_world())
    base = baseline_mod.compute(b, end_offset_min=10)
    stop_payment(world)
    alert = detect.pick(detect.poll(b, base))
    adapter = FixingAdapter(world)
    approval = CliApproval(input_fn=lambda prompt: "y", print_fn=lambda s: None, operator="archit")
    d = dependencies(b, base, alert, approval, adapter=adapter, state_dir=tmp_path / "state",
                     verification_sleep=lambda s: None)
    from agent.main import run
    incident, (report, md) = asyncio.run(run(alert, d))
    assert incident.outcome == "recovered", [t[1] for t in incident.timeline]
    assert adapter.calls == ["payment"]
    assert report["root_cause"]["cause"] == "container_down"
    assert "recovered" in md
    assert d.memory.similar(incident.snapshot)                       # verified incident is remembered
    log = [json.loads(line) for line in (tmp_path / "state/action_log.jsonl").read_text().splitlines()]
    assert [e["event"] for e in log][:2] == ["intent", "result"]     # dry run logged before anything else


@pytest.mark.parametrize("fixture,answer,outcome,cause", [
    (PAYMENT_FAILURE, "y", "escalated", "service_errors"),   # no remedy for app errors: escalate with diagnosis
    (CONTAINER_DOWN, "y", "escalated", "container_down"),    # approved, but no Action API yet: refused
    (CONTAINER_DOWN, "n", "rejected", "container_down"),
])
def test_replay_runs_of_the_combined_agent(tmp_path, monkeypatch, fixture, answer, outcome, cause):
    monkeypatch.chdir(tmp_path)
    from agent.tools.backends import Backends
    b = Backends.replay(fixture)
    base = baseline_mod.compute(b, end_offset_min=20)
    alert = detect.pick(detect.poll(b, base))
    approval = CliApproval(input_fn=lambda prompt: answer, print_fn=lambda s: None, operator="t")
    incident, md, out = handle(b, base, alert, approval, out_dir=tmp_path / "runs", verification_sleep=lambda s: None)
    assert incident.outcome == outcome
    report = json.loads((out / "report.json").read_text())
    assert report["root_cause"]["cause"] == cause
    assert {p.name for p in out.iterdir()} == {"report.json", "report.md", "ledger.json"}   # no LLM turn here


def test_kill_file_stops_the_action_before_the_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "KILL").write_text("")
    from agent.tools.backends import Backends
    b = Backends.replay(CONTAINER_DOWN)
    base = baseline_mod.compute(b, end_offset_min=20)
    alert = detect.pick(detect.poll(b, base))
    asked = []
    approval = CliApproval(input_fn=lambda prompt: asked.append(prompt) or "y", print_fn=lambda s: None, operator="t")
    incident, _, _ = handle(b, base, alert, approval, out_dir=tmp_path / "runs", verification_sleep=lambda s: None)
    assert incident.outcome == "escalated" and asked == []


# --- incident_zero gaps closed with Person C's rules ---------------------------------------

def test_incident_zero_tier1_needs_a_dry_run(tmp_path):
    from incident_zero.store import Store
    store = Store(tmp_path / "s.sqlite3")
    incident = store.create({"service": "payment"})
    plan_id = store.propose(incident, {"action": {"id": "a", "service": "payment"}, "tier": 1,
                                       "diagnosis": {"confidence": 0.9}})
    plan = store.proposal(plan_id)
    with pytest.raises(ValueError, match="dry run"):
        store.decide(plan_id, plan["digest"], "archit", True)


def test_incident_zero_honours_the_shared_kill_file(tmp_path, monkeypatch):
    from incident_zero.store import Store
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / "s.sqlite3")
    assert not store.killed()
    (tmp_path / "KILL").write_text("")
    assert store.killed()
    (tmp_path / "KILL").unlink()
    monkeypatch.setenv("INCIDENT_ZERO_KILL", "1")
    assert store.killed()


# --- Gemini --------------------------------------------------------------------------------

def _gemini(handler, model="gemini-3.5-flash-lite"):
    import httpx
    from agent.llm.gemini import GeminiAdapter
    return GeminiAdapter("test-key", model, http=httpx.Client(transport=httpx.MockTransport(handler)), fallbacks=[])


def test_gemini_schema_is_reduced_to_what_gemini_accepts():
    from agent.llm.gemini import to_gemini_schema
    from agent.llm.investigator import Output
    from agent.tools import registry
    banned = {"$ref", "$defs", "additionalProperties", "title", "pattern", "anyOf", "default"}

    def keywords(node):
        """Schema keywords in use, skipping property names (a property may be called "pattern")."""
        if not isinstance(node, dict):
            return set()
        found = set(node) - {"properties"}
        for key, value in node.items():
            children = value.values() if key == "properties" else [value] if isinstance(value, dict) else \
                value if isinstance(value, list) else []
            for child in children:
                found |= keywords(child)
        return found

    for schema in [Output.model_json_schema()] + [s["input_schema"] for s in registry.schemas()]:
        converted = to_gemini_schema(schema)
        assert not keywords(converted) & banned, converted
    hyp = to_gemini_schema(Output.model_json_schema())["properties"]["hypotheses"]["items"]
    assert hyp["properties"]["source"]["enum"] == ["rules", "llm"]
    assert hyp["properties"]["confidence"]["maximum"] == 1


def test_gemini_round_trip_through_the_investigator():
    tb, snap, _ = snapshot_for()
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
        assert "generationConfig" not in body                       # gemini 3: default temperature
        names = {d["name"] for d in body["tools"][0]["functionDeclarations"]}
        assert "submit_diagnosis" in names and not names & {"restart_service", "toggle_flag"}
        if len(seen) == 1:
            part = {"functionCall": {"name": "search_traces", "args": {"service": "payment"}}}
        else:
            eid = [e for e in snap.evidence_ids if tb.ledger.get(e).tool == "search_traces"][0]
            h = {"id": "H1", "component": "payment", "cause": "service_errors", "confidence": 0.8,
                 "supporting_evidence": [eid], "contradicting_evidence": [], "missing_evidence": [], "source": "llm"}
            part = {"functionCall": {"name": "submit_diagnosis",
                                     "args": {"hypotheses": [h], "selected_diagnosis": "H1", "reason": "r"}}}
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [part]}}]})

    import httpx
    d = asyncio.run(Investigator.from_eyes(_gemini(handler), tb).investigate(snap, tb.ledger, Engine().diagnose(snap, tb.ledger)))
    assert d.state == "CONFIDENT" and d.hypotheses[0].component == "payment"
    assert all(r.headers["x-goog-api-key"] == "test-key" and "test-key" not in str(r.url) for r in seen)
    assert seen[0].url.path.endswith("/models/gemini-3.5-flash-lite:generateContent")


def test_gemini_errors_escalate_without_leaking_the_key():
    import httpx
    tb, snap, _ = snapshot_for()
    calls = []
    adapter = _gemini(lambda r: calls.append(1) or httpx.Response(429, text="quota test-key"))
    with pytest.raises(RuntimeError) as err:
        adapter._post({})
    assert "429" in str(err.value) and "test-key" not in str(err.value)
    assert len(calls) == 2                                           # one wait-and-retry, then gave up
    d = asyncio.run(Investigator.from_eyes(adapter, tb).investigate(snap, tb.ledger, Engine().diagnose(snap, tb.ledger)))
    assert d.state == "UNKNOWN"


def test_gemini_2_gets_temperature_zero():
    body = _gemini(lambda r: None, model="gemini-2.5-flash").body([], "s", {}, {"type": "object"}, 0)
    assert body["generationConfig"] == {"temperature": 0}


def test_model_choice_from_env(tmp_path, monkeypatch):
    from agent.run import choose_model
    monkeypatch.chdir(tmp_path)
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    assert isinstance(choose_model("auto"), NoModel)
    (tmp_path / ".env").write_text("GEMINI_API_KEY='abc'\nGEMINI_MODEL=gemini-2.5-flash\n")
    model = choose_model("auto")
    assert (model.label, model.model, model.key) == ("gemini", "gemini-2.5-flash", "abc")
    assert isinstance(choose_model("none"), NoModel)


def test_gemini_moves_to_the_next_model_when_one_is_busy(monkeypatch):
    import httpx
    from agent.llm import gemini
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    asked = []

    def handler(request):
        model = request.url.path.split("/")[-1].split(":")[0]
        asked.append(model)
        if model == "gemini-3.5-flash-lite":
            return httpx.Response(503)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    adapter = gemini.GeminiAdapter("k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter._post({})
    assert asked == ["gemini-3.5-flash-lite", "gemini-flash-lite-latest"] and adapter.last_model == "gemini-flash-lite-latest"


def test_memory_that_was_already_high_at_baseline_is_not_a_leak():
    def slow_everything(w):
        for svc in w.services.values():
            svc["p95"] = 900.0
    w = fakes.World()
    w.services = fakes.healthy_services()
    w.usage["ad"] = (0.05, 275.0)            # ad fills its 300 MB heap: normal for the JVM
    tb, snap, _ = snapshot_for(w, after_baseline=slow_everything)
    d = Engine().diagnose(snap, tb.ledger)
    assert "memory_leak" not in {h.cause for h in d.hypotheses}

    def grow(w):
        slow_everything(w)
        w.usage["ad"] = (0.05, 295.0)
    w2 = fakes.World()
    w2.services = fakes.healthy_services()
    w2.usage["ad"] = (0.05, 200.0)
    tb, snap, _ = snapshot_for(w2, after_baseline=grow)
    assert ("ad", "memory_leak") in {(h.component, h.cause) for h in Engine().diagnose(snap, tb.ledger).hypotheses}


def test_escalation_report_explains_itself_and_keeps_the_llm_transcript(tmp_path, monkeypatch):
    """Unexplained slowness -> rules unsure -> the model fails -> the report still says why and
    shows the evidence, and the model turn is saved."""
    monkeypatch.chdir(tmp_path)
    b, world = fakes.backends()
    world.services = fakes.healthy_services()
    base = baseline_mod.compute(b, end_offset_min=10)
    world.services["shipping"]["p95"] = 900.0
    alert = detect.watch(b, base, max_polls=6, sleep=lambda s: None)

    class Broken:
        label = "broken"

        async def complete(self, **kw):
            raise RuntimeError("Gemini API failed: gemini-3.5-flash-lite:503")

    incident, md, out = handle(b, base, alert, AutoReject(), model=Broken(), out_dir=tmp_path / "runs",
                               verification_sleep=lambda s: None)
    report = json.loads((out / "report.json").read_text())
    assert incident.outcome == "escalated" and report["root_cause"] is None
    assert "503" in report["diagnosis_reason"]
    assert [p["state"] for p in report["diagnosis_path"]] == ["UNKNOWN"]
    assert report["evidence"] and "shipping p95_ms" in md
    assert "503" in json.loads((out / "llm.json").read_text())[0]["error"]


def test_checkout_script_reports_what_a_customer_sees():
    import httpx
    import importlib.util
    spec = importlib.util.spec_from_file_location("checkouts", Path(__file__).parents[1] / "scripts" / "checkouts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    failing = {"on": False}

    def shop(request):
        body = json.loads(request.content)
        if request.url.path == "/api/cart":
            assert body["item"]["quantity"] == 1 and body["userId"]
            return httpx.Response(200, json={})
        assert request.url.path == "/api/checkout" and body["creditCard"] and body["userId"]
        if failing["on"]:
            return httpx.Response(500, text="failed to charge card: Payment request failed. Invalid token.")
        return httpx.Response(200, json={"orderId": "abc"})

    http = httpx.Client(transport=httpx.MockTransport(shop))
    assert mod.checkout(http, "http://shop", "0PUK6V6EV0") == (True, "order abc")
    failing["on"] = True
    good, detail = mod.checkout(http, "http://shop", "0PUK6V6EV0")
    assert not good and "HTTP 500" in detail and "Payment request failed" in detail
