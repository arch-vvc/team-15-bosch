"""Writes tests/fixtures/payment_failure.jsonl: the fake demo's responses to one
`eyes snapshot`, so the CLI can be tried without Docker:

    python -m tests.make_fixture
    python -m agent.eyes --replay tests/fixtures/payment_failure.jsonl snapshot
"""
from pathlib import Path

from agent import baseline as baseline_mod
from agent import detect, triage
from agent.ledger import Ledger
from agent.tools import Toolbox
from agent.tools.backends import Backends, Recorder
from tests import fakes

OUT = Path(__file__).parent / "fixtures" / "payment_failure.jsonl"


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.unlink(missing_ok=True)
    live, _ = fakes.backends()
    b = Backends(prom=Recorder(live.prom, "prom", OUT), jaeger=Recorder(live.jaeger, "jaeger", OUT),
                 docker=Recorder(live.docker, "docker", OUT), opensearch=None, clock=Recorder(live.clock, "clock", OUT))
    base = baseline_mod.compute(b, end_offset_min=20)
    alert = detect.pick(detect.poll(b, base))
    triage.snapshot(Toolbox(b, Ledger(), base), alert)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
