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


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.unlink(missing_ok=True)
    live, _ = fakes.backends()
    b = Backends(prom=Recorder(live.prom, "prom", OUT), jaeger=Recorder(live.jaeger, "jaeger", OUT),
                 docker=Recorder(live.docker, "docker", OUT), opensearch=None, clock=Recorder(live.clock, "clock", OUT))
    base = baseline_mod.compute(b, end_offset_min=20)
    alert = detect.pick(detect.poll(b, base))
    tb = Toolbox(b, Ledger(), base)
    triage.snapshot(tb, alert)
    # one call of every tool, so `eyes tool NAME` works on the replay too
    for name, args in EXAMPLE_CALLS:
        registry.call(tb, name, args)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
