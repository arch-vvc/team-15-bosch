"""Each read-only tool against the fake demo: typed facts from the fixed vocabulary, the
right directions against baseline, and no flag data anywhere in the output."""
import pytest

from agent import config
from agent.contracts import SIGNALS
from agent.tools import deploy_history, docker_state, logs, prometheus, redact
from tests import fakes


def facts(evidence, **match):
    return [f for ev in evidence for f in ev.facts if all(getattr(f, k) == v for k, v in match.items())]


def assert_clean(evidence):
    for ev in evidence:
        assert redact.find_leaks(ev.model_dump(mode="json")) == [], ev.tool
        assert all(f.signal in SIGNALS for f in ev.facts), ev.tool


def test_baseline_is_healthy_and_skips_flagd(base):
    assert base.services["payment"]["error_ratio"] == 0
    assert "flagd" not in base.services
    assert ("checkout", "payment") in [tuple(e) for e in base.edges]
    assert all("flagd" not in e for e in map(tuple, base.edges) for e in e)
    assert base.containers["payment"]["status"] == "running"


def test_overview_flags_payment_and_checkout(tb):
    ev = tb.service_health_overview()
    assert_clean(ev)
    up = {(f.service, f.signal) for f in facts(ev, direction="up")}
    assert ("payment", "error_ratio") in up and ("checkout", "error_ratio") in up
    assert ("frontend", "error_ratio") in up          # 8 % > baseline + 5 %
    assert ("cart", "error_ratio") not in up
    assert facts(ev, service="payment", signal="error_ratio")[0].value == 0.5
    assert ev[0].id == "E1"


def test_overview_reports_missing_telemetry(world_backends, base, tb):
    _, w = world_backends
    w.services["payment"]["rate"] = 0
    ev = tb.service_health_overview()
    assert facts(ev, service="payment", signal="missing_telemetry", direction="absent")


def test_service_detail_names_the_failing_operation(tb):
    ev = tb.service_detail("checkout")
    assert_clean(ev)
    bad = {f.scope for f in facts(ev, signal="error_ratio", direction="up")}
    assert bad == {"oteldemo.CheckoutService/PlaceOrder", "oteldemo.PaymentService/Charge"}
    assert not any("flagd" in (f.scope or "") for f in facts(ev))


def test_search_traces_finds_origin_concentration_and_onset(tb):
    ev = tb.search_traces("checkout")
    assert_clean(ev)
    payment = next(e for e in ev if e.facts[0].service == "payment")
    assert facts([payment], signal="error_origin")[0].scope == "oteldemo.PaymentService/Charge"
    assert facts([payment], signal="error_concentration")[0].value == "gold"
    assert "Invalid token" in facts([payment], signal="error_message")[0].value
    assert payment.onset_ts.timestamp() == pytest.approx(fakes.FAULT_AT + 10 + 0.006, abs=0.01)
    assert len(payment.examples) == 3
    # frontend and checkout fail too, but only because payment did
    assert not facts(ev, service="checkout", signal="error_origin")
    assert all(e.facts[0].service != "flagd" for e in ev)


def test_search_traces_masks_flag_text_in_messages(tb):
    ev = tb.search_traces("product-catalog")
    assert_clean(ev)
    assert facts(ev, signal="error_message")[0].value == "Error: Product Catalog Fail [redacted] Enabled"


def test_hop_latency_compares_client_and_server(tb):
    ev = tb.hop_latency("checkout", "payment")
    gap = facts(ev, signal="hop_gap_ms")[0]
    assert gap.value == 10.0 and gap.scope == "checkout->payment"
    assert gap.baseline == 10.0 and gap.direction == "same"


def test_container_health_all_running(tb):
    ev = tb.container_health()
    assert_clean(ev)
    statuses = facts(ev, signal="status")
    assert len(statuses) == len(config.all_containers())
    assert {f.value for f in statuses} == {"running"}
    assert not any(f.service == "flagd" for f in facts(ev))


def test_container_health_spots_starvation_oom_and_stop(world_backends, base, tb):
    _, w = world_backends
    w.containers["ad"]["HostConfig"]["NanoCpus"] = int(0.05e9)
    w.containers["recommendation"] = fakes.container("recommendation", mem_mb=60, restarts=2, oom=True)
    w.containers["payment"] = fakes.container("payment", status="exited")
    ev = tb.container_health()
    assert facts(ev, service="ad", signal="cpu_limit", direction="down")[0].value == 0.05
    assert facts(ev, service="recommendation", signal="mem_limit", direction="down")[0].value == 60
    assert facts(ev, service="recommendation", signal="oom_killed")[0].value is True
    assert facts(ev, service="recommendation", signal="restarts", direction="up")[0].value == 2
    assert facts(ev, service="payment", signal="status")[0].value == "exited"


def test_container_detail_has_usage_and_uptime(tb):
    ev = tb.container_health("payment")
    signals = {f.signal for f in facts(ev)}
    assert {"status", "restarts", "oom_killed", "mem_limit", "mem_used", "cpu_used", "uptime_s"} <= signals
    assert facts(ev, signal="cpu_used")[0].value == pytest.approx(0.05)
    assert facts(ev, signal="mem_used")[0].value == pytest.approx(0.4 * 140, abs=0.1)


def test_usage_from_stats_handles_missing_data():
    assert docker_state.usage_from_stats(None) == (None, None)
    assert docker_state.usage_from_stats({}) == (None, None)


def test_search_logs_counts_levels_and_masks(tb):
    ev = tb.search_logs("payment")
    assert facts(ev, signal="log_count", scope="ERROR")[0].value == 6
    assert facts(ev, signal="log_count", scope="INFO")[0].value == 1
    msgs = facts(ev, signal="error_message")
    assert len(msgs) == 1 and "Payment request failed" in msgs[0].value   # numbers de-duplicated
    assert ev[0].onset_ts is not None
    for svc in ("ad", "shipping"):
        assert_clean(tb.search_logs(svc))


@pytest.mark.parametrize("line,level", [
    ('{"level":"info","msg":"no error here"}', "INFO"),
    ("WARN something odd", "WARN"),
    ("level=error msg=boom", "ERROR"),
    ("panic: runtime error", "ERROR"),
    ("GET /api/products 200", "INFO"),
    ("(node:1) MetadataLookupWarning: received unexpected error = All promises were rejected", "WARN"),
    ('{"level":"error","msg":"DeprecationWarning ignored"}', "ERROR"),
])
def test_level_detection(line, level):
    assert logs.level_of(line) == level


def test_deploy_history_keeps_changes_and_drops_noise(world_backends, tb):
    _, w = world_backends
    w.events = [
        fakes.docker_event("payment", "die", fakes.T0 - 120, exitCode="137"),
        fakes.docker_event("payment", "start", fakes.T0 - 110),
        fakes.docker_event("payment", "exec_start: /bin/sh -c healthcheck", fakes.T0 - 100),
        fakes.docker_event("flagd", "restart", fakes.T0 - 90),
        fakes.docker_event("ad", "update", fakes.T0 - 80),
        fakes.docker_event("cart", "health_status: unhealthy", fakes.T0 - 70),
    ]
    ev = tb.deploy_history()
    assert_clean(ev)
    got = [(f.service, f.scope) for f in facts(ev)]
    assert got == [("payment", "die"), ("payment", "start"), ("ad", "update"), ("cart", "health_status")]
    assert facts(ev, service="payment")[0].value.startswith("die exitCode=137")
    assert ev[0].onset_ts.timestamp() == pytest.approx(fakes.T0 - 120)


def test_deploy_history_empty_window(tb):
    ev = tb.deploy_history()
    assert len(ev) == 1 and ev[0].facts == []


def test_onset_from_series_needs_two_points():
    base, step = 0.0, 15
    pts = [(0, 0.0), (15, 0.3), (30, 0.0), (45, 0.4), (60, 0.5), (75, 0.5)]
    assert prometheus.onset_from_series(pts, "error_ratio", base, "up", 0, 75, step) == (45, 0.4)
    assert prometheus.onset_from_series(pts[:3], "error_ratio", base, "up", 0, 30, step) is None
    gone = [(0, 2.0), (15, 2.0)]
    assert prometheus.onset_from_series(gone, "req_rate", 2.0, "absent", 0, 60, step) == (30, 0.0)


def test_onset_tool_finds_the_fault_time(tb):
    ev = tb.onset("payment", "error_ratio")
    assert ev[0].onset_ts is not None
    assert fakes.FAULT_AT + 45 <= ev[0].onset_ts.timestamp() <= fakes.FAULT_AT + 90


def test_dependencies_missing_edge(world_backends, tb):
    _, w = world_backends
    w.edges = [e for e in w.edges if e["child"] != "payment"]
    ev = tb.service_dependencies()
    assert facts(ev, signal="missing_edge")[0].scope == "checkout->payment"
    assert_clean(ev)


def test_backend_failure_becomes_empty_evidence(world_backends, tb):
    from agent.tools.backends import BackendError

    b, _ = world_backends

    def boom(**_):
        raise BackendError("prometheus down")

    b.prom.query = boom
    ev = tb.service_health_overview()
    assert ev[0].facts == [] and "prometheus down" in ev[0].query["error"]
