"""The merged system: the Eyes tools inside the incident_zero pipeline, the new rules that use
them, flag redaction on the pipeline's own trace projection and evidence store, and the
Jaeger v3 fallback in the Eyes backends."""
import json

import httpx
import pytest

from agent import baseline as baseline_mod
from agent.tools import redact
from agent.tools.backends import Jaeger, otlp_to_traces
from incident_zero.eyes_tools import EyesTools, observation, origin_services, to_records
from incident_zero.pipeline import Pipeline
from incident_zero.store import Store
from incident_zero.telemetry import TelemetryError, trace_spans
from incident_zero.tools import LiveTools
from tests import fakes


class OfflineLiveTools(LiveTools):
    """LiveTools whose own HTTP and Docker readers are offline; only the Eyes tools answer,
    from the simulated demo."""

    def __init__(self, backends, baseline_path):
        super().__init__(container_health=True)
        self.eyes = EyesTools(backends=backends, baseline_path=baseline_path, docker_enabled=True)

    def _offline(self, *args):
        raise TelemetryError("offline in tests")

    inspect_service = read_logs = read_container_health = read_resources = _offline


@pytest.fixture
def merged(tmp_path):
    b, world = fakes.backends()
    path = tmp_path / "baseline.json"
    baseline_mod.compute(b, end_offset_min=10).save(path)
    return OfflineLiveTools(b, path), Store(tmp_path / "incidents.sqlite3"), world


def test_payment_failure_is_diagnosed_by_rules_from_eyes_evidence(merged):
    tools, store, _ = merged
    incident = Pipeline(store, tools, actions=[]).run({"service": "checkout", "summary": "checkout errors"})
    state = store.incident(incident)
    assert state["diagnosis"]["component"] == "payment"
    assert state["diagnosis"]["signature"] == "errors_originate_in_service"
    assert state["diagnosis"]["source"] == "rules"
    assert state["state"] == "escalated"          # no remedy is configured for it, so a human takes over
    kinds = {row["kind"] for row in store.evidence(incident)}
    assert {"service_health", "trace_errors", "container_limits", "deploy_history"} <= kinds
    assert redact.find_leaks(store.evidence(incident)) == []


def test_matching_remedy_goes_to_approval_not_execution(merged):
    tools, store, _ = merged
    action = {"id": "restart_payment", "service": "payment", "signatures": ["errors_originate_in_service"], "tier": 1}
    incident = Pipeline(store, tools, actions=[action]).run({"service": "checkout"})
    assert store.incident(incident)["state"] == "awaiting_approval"


def test_cpu_starvation_rule(merged):
    tools, store, world = merged
    world.services = fakes.healthy_services()
    world.services["ad"]["p95"] = 900.0
    world.containers["ad"]["HostConfig"]["NanoCpus"] = int(0.05e9)
    incident = Pipeline(store, tools, actions=[]).run({"service": "ad"})
    diagnosis = store.incident(incident)["diagnosis"]
    assert (diagnosis["component"], diagnosis["signature"]) == ("ad", "cpu_limit_lowered")


def test_unknown_pattern_still_escalates_without_a_model(merged):
    tools, store, world = merged
    world.services = fakes.healthy_services()
    world.services["shipping"]["p95"] = 900.0      # slow, but no rule explains it
    incident = Pipeline(store, tools, actions=[]).run({"service": "shipping"})
    assert store.incident(incident)["state"] == "escalated"
    assert store.incident(incident)["diagnosis"] is None


def test_observation_shape_lets_one_condition_match_one_fact():
    from agent.contracts import Fact

    obs = observation(Fact(service="payment", signal="error_ratio", value=0.5, baseline=0.0, direction="up"))
    assert obs == {"signal": "error_ratio", "error_ratio": 0.5, "scope": None, "baseline": 0.0,
                   "direction": "up", "error_ratio_up": True}


def test_trace_errors_are_filed_under_the_service_they_belong_to(merged):
    tools, _, _ = merged
    rows = tools.call("read_trace_errors", "checkout", 900, fakes.T0)
    assert {r["service"] for r in rows} == {"checkout", "frontend", "payment"}
    assert origin_services(rows) == ["payment"]


def test_eyes_tools_respect_kill_switch_and_docker_opt_in(merged):
    tools, _, _ = merged
    tools.eyes.guard = lambda: True
    with pytest.raises(TelemetryError):
        tools.call("read_service_health", "payment", 300, fakes.T0)
    tools.eyes.guard, tools.eyes.docker_enabled = (lambda: False), False
    with pytest.raises(TelemetryError):
        tools.call("read_deploy_history", "payment", 300, fakes.T0)
    with pytest.raises(ValueError):
        tools.call("read_trace_errors", "flagd", 300, fakes.T0)


def test_unavailable_eyes_tool_is_recorded_not_raised():
    from datetime import datetime, timezone

    from agent.contracts import Evidence

    ev = Evidence(tool="search_traces", query={"error": "jaeger down"}, ts=datetime.now(timezone.utc), facts=[])
    rows = to_records([ev], "trace_errors", "payment", 0, 1)
    assert rows[0]["status"] == "unavailable" and rows[0]["error"] == "jaeger down"


# --- redaction in the pipeline's own readers -----------------------------------------------

def test_pipeline_trace_projection_drops_flag_service_calls():
    trace = fakes.payment_trace(1, fakes.FAULT_AT)
    spans = trace_spans({"data": [trace]}, "checkout")
    assert spans and not any(redact.is_flag_system(s["operation"]) or s["peer_service"] == "flagd" for s in spans)


def test_store_masks_and_refuses_flag_data(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    incident = store.create({"service": "payment"})
    row = store.add_evidence(incident, {"service": "payment", "kind": "recent_logs", "status": "observed",
                                        "observations": [{"body": "Feature Flag adManualGc enabled", "feature_flag.key": "x"}]})
    assert row["observations"] == [{"body": "[redacted] [redacted] enabled"}]
    assert redact.find_leaks(store.evidence(incident)) == []


# --- Jaeger v3 fallback --------------------------------------------------------------------

def _v3_frame():
    return {"result": {"resourceSpans": [
        {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "checkout"}}]},
         "scopeSpans": [{"spans": [{"traceId": "t1", "spanId": "a", "name": "PlaceOrder", "kind": 2,
                                    "startTimeUnixNano": "1000000", "endTimeUnixNano": "5000000",
                                    "status": {"code": 2, "message": "boom"}}]}]},
        {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "payment"}}]},
         "scopeSpans": [{"spans": [{"traceId": "t1", "spanId": "b", "parentSpanId": "a", "name": "Charge",
                                    "kind": "SPAN_KIND_SERVER", "startTimeUnixNano": "2000000",
                                    "endTimeUnixNano": "3000000", "status": {"code": 2},
                                    "events": [{"name": "exception", "timeUnixNano": "2500000", "attributes": [
                                        {"key": "exception.message", "value": {"stringValue": "Invalid token"}}]}]}]}]},
    ]}}


def test_otlp_frames_become_classic_traces():
    [trace] = otlp_to_traces([_v3_frame()])
    services = {p["serviceName"] for p in trace["processes"].values()}
    assert services == {"checkout", "payment"}
    child = next(s for s in trace["spans"] if s["spanID"] == "b")
    assert child["references"][0]["spanID"] == "a" and child["duration"] == 1000
    assert {"key": "error", "value": True} in child["tags"]
    assert child["logs"][0]["fields"][1] == {"key": "exception.message", "value": "Invalid token"}


def test_jaeger_falls_back_to_v3_on_404():
    def handler(request):
        if request.url.path.endswith("/api/traces"):
            return httpx.Response(404)
        assert request.url.params["query.serviceName"] == "checkout"
        return httpx.Response(200, text=json.dumps(_v3_frame()) + "\n")

    jaeger = Jaeger("http://localhost:8080/jaeger/ui", httpx.Client(transport=httpx.MockTransport(handler)))
    traces = jaeger.traces(service="checkout", tags={"error": "true"})
    assert len(traces) == 1 and len(traces[0]["spans"]) == 2


def test_watch_everything_fires_after_two_polls_and_runs_the_pipeline(merged, tmp_path, monkeypatch, capsys):
    import argparse

    from incident_zero import __main__ as cli

    tools, store, _ = merged
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    args = argparse.Namespace(cycles=3, interval=10, container_health=True, action_contract=None,
                              reports=tmp_path / "reports")
    assert cli.watch_everything(args, store, lambda: None, tools) == 0
    out = capsys.readouterr().out
    assert '"cycle": 1' in out and "State: escalated" in out
    [incident] = [row["id"] for row in store.db.execute("SELECT id FROM incidents")]
    assert store.incident(incident)["alert"]["source"] == "eyes_watcher"
    assert store.incident(incident)["diagnosis"]["component"] == "payment"
