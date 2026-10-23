"""Writes tests/fixtures/payment_failure.jsonl: the fake demo's responses to one
`eyes snapshot`, so the CLI can be tried without Docker:

    python -m tests.make_fixture
    python -m agent.eyes --replay tests/fixtures/payment_failure.jsonl snapshot
    python -m agent.eyes --replay tests/fixtures/payment_failure.jsonl tool service_detail '{"service": "checkout"}'
"""
from pathlib import Path

from agent import baseline as baseline_mod
from agent import detect, triage
from agent.ledger import Ledger
from agent.tools import Toolbox, registry
from agent.tools.backends import Backends, Recorder
from tests import fakes

OUT = Path(__file__).parent / "fixtures" / "payment_failure.jsonl"

EXAMPLE_CALLS = [
    ("service_health_overview", {}),
    ("service_detail", {"service": "checkout"}),
    ("service_detail", {"service": "payment"}),
    ("hop_latency", {"caller": "checkout", "callee": "payment"}),
    ("search_traces", {"service": "payment"}),
    ("container_health", {"service": "payment"}),
    ("search_logs", {"service": "payment"}),
    ("onset", {"service": "payment", "signal": "error_ratio"}),
    ("service_dependencies", {}),
    ("deploy_history", {}),
]


CONTAINER_DOWN = Path(__file__).parent / "fixtures" / "container_down.jsonl"


def container_down_world() -> fakes.World:
    """payment's container is stopped: no telemetry from it, checkout fails every charge."""
    w = fakes.World()
    w.services = fakes.healthy_services()
    w.services["payment"]["rate"] = 0
    w.services["checkout"]["err"] = 1.0
    w.services["checkout"]["ops"]["oteldemo.CheckoutService/PlaceOrder"]["err"] = 1.0
    w.containers["payment"] = fakes.container("payment", status="exited")
    w.edges = [e for e in w.edges if e["child"] != "payment"]
    w.traces = {"checkout": [], "frontend": []}
    w.events = [fakes.docker_event("payment", "kill", fakes.T0 - 200, signal="15"),
                fakes.docker_event("payment", "die", fakes.T0 - 199, exitCode="143"),
                fakes.docker_event("payment", "stop", fakes.T0 - 199)]
    return w


def record(out: Path, world: fakes.World | None, every_tool: bool) -> None:
    out.parent.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    live, _ = fakes.backends(world)
    b = Backends(prom=Recorder(live.prom, "prom", out), jaeger=Recorder(live.jaeger, "jaeger", out),
                 docker=Recorder(live.docker, "docker", out), opensearch=None, clock=Recorder(live.clock, "clock", out))
    base = baseline_mod.compute(b, end_offset_min=20)
    alert = detect.pick(detect.poll(b, base))
    tb = Toolbox(b, Ledger(), base)
    triage.snapshot(tb, alert)
    if every_tool:   # one call of every tool, so `eyes tool NAME` works on the replay too
        for name, args in EXAMPLE_CALLS:
            registry.call(tb, name, args)
    print(f"wrote {out}")


def main() -> None:
    record(OUT, None, every_tool=True)
    record(CONTAINER_DOWN, container_down_world(), every_tool=False)


if __name__ == "__main__":
    main()
