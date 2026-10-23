"""Ledger, alert watcher, triage, symptom summary, verify, registry and record/replay."""
import json
from datetime import datetime, timezone

import pytest

from agent import baseline as baseline_mod
from agent import config, detect, summarize, triage, verify
from agent.contracts import Alert, Evidence, Fact
from agent.ledger import Ledger
from agent.tools import Toolbox, redact, registry
from agent.tools.backends import Backends, Recorder
from tests import fakes

NOW = datetime.fromtimestamp(fakes.T0, timezone.utc)
no_sleep = lambda s: None


# --- ledger --------------------------------------------------------------------------------

def test_ledger_assigns_ids_and_is_append_only():
    ledger = Ledger()
    ev = Evidence(tool="t", query={}, ts=NOW, facts=[Fact(service="payment", signal="error_ratio", value=0.5)])
    assert ledger.add(ev) == "E1" and ledger.add(ev.model_copy()) == "E2"
    got = ledger.get("E1")
    got.facts[0].value = 0.0
    assert ledger.get("E1").facts[0].value == 0.5          # callers get copies
    assert [i for i, _ in ledger.facts(service="payment")] == ["E1", "E2"]
    with pytest.raises(KeyError):
        ledger.get("E9")


def test_ledger_scrubs_anything_a_tool_missed(tmp_path):
    ledger = Ledger()
    ev = Evidence(tool="t", query={"feature_flag": "x"}, ts=NOW, examples=["paymentFailure is on via flagd"],
                  facts=[Fact(service="payment", signal="error_message", value="Feature Flag enabled")])
    ledger.add(ev)
    stored = ledger.to_json()
    assert redact.find_leaks(stored) == []
    ledger.save(tmp_path / "l.json")
    assert Ledger.load(tmp_path / "l.json").to_json() == stored


# --- alert watcher -------------------------------------------------------------------------

def test_watch_needs_two_consecutive_polls(world_backends, base):
    b, w = world_backends
    w.services = fakes.healthy_services()
    seen = []

    def on_poll(ts, candidates, streak):
        seen.append(len(candidates))
        if len(seen) == 2:      # the fault starts after the second poll
            w.services = fakes.payment_failure_services()

    alert = detect.watch(b, base, max_polls=10, sleep=no_sleep, on_poll=on_poll)
    assert seen[:2] == [0, 0] and len(seen) == 4
    assert alert.service in ("payment", "checkout") and alert.signal == "error_ratio"


def test_watch_returns_none_when_quiet(world_backends, base):
    b, w = world_backends
    w.services = fakes.healthy_services()
    assert detect.watch(b, base, max_polls=3, sleep=no_sleep) is None


def test_container_down_outranks_errors(world_backends, base):
    b, w = world_backends
    w.containers["payment"] = fakes.container("payment", status="exited")
    alert = detect.watch(b, base, max_polls=3, sleep=no_sleep)
    assert (alert.service, alert.signal) == ("payment", "status")


def test_container_missing_at_baseline_is_not_an_alert(world_backends, base):
    b, w = world_backends
    w.services = fakes.healthy_services()
    base.containers["opensearch"]["status"] = "missing"
    w.containers["opensearch"] = None
    assert detect.poll(b, base) == []


def test_restart_and_limit_alerts(world_backends, base):
    b, w = world_backends
    w.services = fakes.healthy_services()
    w.containers["email"]["RestartCount"] = 1
    w.containers["ad"]["HostConfig"]["NanoCpus"] = int(0.05e9)
    got = {(a.service, a.signal) for a in detect.poll(b, base)}
    assert got == {("email", "restarts"), ("ad", "cpu_limit")}


# --- triage and summary --------------------------------------------------------------------

def test_triage_snapshot_and_summary(tb):
    alert = detect.watch(tb.b, tb.baseline, max_polls=3, sleep=no_sleep)
    snap = triage.snapshot(tb, alert)
    assert snap.evidence_ids == [ev.id for ev in tb.ledger.all()]
    payment = next(s for s in snap.services if s.service == "payment")
    assert payment.error_ratio == 0.5 and payment.baseline["error_ratio"] == 0
    assert {"error_ratio", "first_error_span"} <= set(payment.onset_ts)
    assert ("checkout", "payment") in snap.dependencies
    assert all(c.status == "running" for c in snap.containers)
    assert "flagd" not in json.dumps(snap.model_dump(mode="json"))

    text = summarize.build(alert, snap, tb.ledger)
    assert redact.find_leaks(text) == []
    assert "**payment**" in text and "errors originate here: oteldemo.PaymentService/Charge" in text
    assert "every error shares demo.user_context.loyalty_level = gold" in text
    assert "no container was created" in text
    for cited in set(__import__("re").findall(r"\[(E\d+)\]", text)):
        assert tb.ledger.has(cited)
    assert redact.find_leaks(tb.ledger.to_json()) == []


def test_summary_for_a_stopped_container(world_backends, base):
    b, w = world_backends
    w.services = fakes.healthy_services()
    w.services["payment"]["rate"] = 0
    w.services["checkout"]["err"] = 1.0
    w.containers["payment"] = fakes.container("payment", status="exited")
    w.edges = [e for e in w.edges if e["child"] != "payment"]
    w.events = [fakes.docker_event("payment", "kill", fakes.T0 - 200, signal="15"),
                fakes.docker_event("payment", "die", fakes.T0 - 199, exitCode="143"),
                fakes.docker_event("payment", "stop", fakes.T0 - 199)]
    tb = Toolbox(b, Ledger(), base)
    alert = detect.watch(b, base, max_polls=3, sleep=no_sleep)
    assert (alert.service, alert.signal) == ("payment", "status")
    text = summarize.build(alert, triage.snapshot(tb, alert), tb.ledger)
    assert "**payment**: status exited" in text
    assert "checkout->payment: present at baseline, no calls now" in text
    assert "die exitCode=143" in text
    assert "no telemetry" in text


# --- verify --------------------------------------------------------------------------------

def test_verify_recovers_after_fix(tb, world_backends):
    _, w = world_backends
    alert = Alert(service="payment", signal="error_ratio", value=0.5, baseline=0, ts=NOW)
    polls = []

    def on_poll(ok, last):
        polls.append(ok)
        if len(polls) == 2:
            w.services = fakes.healthy_services()

    result = verify.check(tb, alert, sleep=no_sleep, on_poll=on_poll)
    assert result.recovered and polls == [False, False, True, True, True]
    assert result.remaining_symptoms == []
    assert all(tb.ledger.has(i) for i in result.evidence_ids)


def test_verify_reports_remaining_symptoms(tb, world_backends):
    _, w = world_backends
    w.services["payment"]["err"] = 0.0
    alert = Alert(service="payment", signal="error_ratio", value=0.5, baseline=0, ts=NOW)
    result = verify.check(tb, alert, sleep=no_sleep)
    assert result.recovered
    assert "checkout:error_ratio" in result.remaining_symptoms


def test_verify_gives_up(tb):
    alert = Alert(service="payment", signal="error_ratio", value=0.5, baseline=0, ts=NOW)
    result = verify.check(tb, alert, sleep=no_sleep)
    assert not result.recovered and "payment:error_ratio" in result.remaining_symptoms


def test_verify_ignores_the_restart_that_was_the_fix(tb, world_backends):
    _, w = world_backends
    w.services = fakes.healthy_services()
    w.containers["payment"]["RestartCount"] = 1
    alert = Alert(service="payment", signal="restarts", value=1, baseline=0, ts=NOW)
    assert verify.check(tb, alert, sleep=no_sleep).recovered


# --- registry ------------------------------------------------------------------------------

def test_registry_is_read_only_and_well_formed():
    registry.assert_read_only()
    names = {s["name"] for s in registry.schemas()}
    assert names == set(registry.READ_ONLY_TOOLS) and len(names) == 9
    assert not any(registry.ACTION_WORDS.search(n) for n in names)
    assert all(s["input_schema"]["additionalProperties"] is False for s in registry.schemas())
    assert set(registry.READ_ONLY_TOOLS) <= {m for m in dir(Toolbox) if not m.startswith("_")}


@pytest.mark.parametrize("name,args,error", [
    ("restart_service", {"service": "payment"}, "unknown tool"),
    ("service_detail", {}, "missing argument service"),
    ("service_detail", {"service": "flagd"}, "must be one of"),
    ("search_traces", {"service": "payment", "lookback": "1d"}, "must match"),
    ("search_logs", {"service": "payment", "pattern": "("}, "not a valid regex"),
    ("search_logs", {"service": "payment", "since_min": 500}, "between"),
    ("container_health", {"service": "payment", "force": True}, "unknown argument"),
])
def test_registry_rejects_bad_calls(tb, name, args, error):
    assert error in registry.call(tb, name, args)["error"]


def test_registry_call_returns_ledger_evidence(tb):
    out = registry.call(tb, "service_detail", {"service": "payment"})
    assert out["evidence"][0]["id"] == "E1" and tb.ledger.has("E1")


# --- record and replay ---------------------------------------------------------------------

def test_record_then_replay_gives_the_same_snapshot(tmp_path, base):
    path = tmp_path / "rec.jsonl"
    live, _ = fakes.backends()
    rec = Backends(prom=Recorder(live.prom, "prom", path), jaeger=Recorder(live.jaeger, "jaeger", path),
                   docker=Recorder(live.docker, "docker", path), opensearch=None,
                   clock=Recorder(live.clock, "clock", path))
    first = Toolbox(rec, Ledger(), base)
    snap1 = triage.snapshot(first)
    recorded = [json.loads(line) for line in path.read_text().splitlines()]
    assert redact.find_leaks([r["result"] for r in recorded]) == []     # responses are scrubbed on disk

    replay = Backends.replay(path)
    second = Toolbox(replay, Ledger(), base)
    snap2 = triage.snapshot(second)
    assert snap1.model_dump() == snap2.model_dump()
    assert first.ledger.to_json() == second.ledger.to_json()


def test_replay_without_recording_is_a_clear_error(tmp_path):
    path = tmp_path / "clock-only.jsonl"
    path.write_text(json.dumps({"backend": "clock", "method": "now", "args": {}, "result": fakes.T0}) + "\n")
    b = Backends.replay(path)
    tb = Toolbox(b, Ledger())
    ev = tb.service_health_overview()
    assert "no recording" in ev[0].query["error"]


# --- config --------------------------------------------------------------------------------

def test_services_yaml_matches_compose():
    pytest.importorskip("yaml")
    import pathlib

    import yaml

    root = pathlib.Path(__file__).parent.parent / "opentelemetry-demo"
    if not (root / "compose.yaml").exists():
        pytest.skip("demo submodule not checked out")
    limits = {}
    for f in ("compose.yaml", "compose.observability.yaml"):
        for name, svc in yaml.safe_load((root / f).read_text())["services"].items():
            mem = (((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}).get("memory")
            if mem:
                limits[name] = int(mem[:-1]) * (1024 if mem.endswith("G") else 1)
    for name, c in config.services()["containers"].items():
        assert limits[name] == c["mem_limit_mb"], name
    shop = {n for n, s in yaml.safe_load((root / "compose.yaml").read_text())["services"].items()
            if any("OTEL_SERVICE_NAME" in str(e) for e in s.get("environment") or [])}
    assert shop - {"flagd", "flagd-ui", "telemetry-docs"} == set(config.shop_services())


def test_fixture_replays_every_tool():
    """The committed recording answers one call of every tool, for demos without Docker."""
    import pathlib

    from tests.make_fixture import EXAMPLE_CALLS, OUT

    assert {name for name, _ in EXAMPLE_CALLS} == set(registry.READ_ONLY_TOOLS)
    tb = Toolbox(Backends.replay(OUT), Ledger(), baseline_mod.Baseline.model_validate(
        {"taken_at": 0, "window_min": 10, "services": {}, "containers": {}, "edges": []}))
    for name, args in EXAMPLE_CALLS:
        out = registry.call(tb, name, args)
        assert all("error" not in ev["query"] for ev in out["evidence"]), (name, out)


def test_latency_needs_four_polls_errors_need_two(world_backends, base):
    b, w = world_backends
    w.services = fakes.healthy_services()
    w.services["recommendation"]["p95"] = 3700.0
    seen = []
    alert = detect.watch(b, base, max_polls=10, sleep=no_sleep, on_poll=lambda ts, c, s: seen.append(1))
    assert alert.signal == "p95_ms" and len(seen) == 4
    w.services = fakes.payment_failure_services()
    seen.clear()
    alert = detect.watch(b, base, max_polls=10, sleep=no_sleep, on_poll=lambda ts, c, s: seen.append(1))
    assert alert.signal == "error_ratio" and len(seen) == 2


def test_container_down_at_baseline_is_not_blamed(world_backends):
    from agent.rules.engine import Engine
    b, w = world_backends
    w.containers["opensearch"] = None                 # stopped on purpose before the baseline
    base = baseline_mod.compute(b, end_offset_min=10)
    tb = Toolbox(b, Ledger(), base)
    snap = triage.snapshot(tb, detect.watch(b, base, max_polls=3, sleep=no_sleep))
    d = Engine().diagnose(snap, tb.ledger)
    assert "opensearch" not in {h.component for h in d.hypotheses}
    assert d.hypotheses[0].component == "payment"
