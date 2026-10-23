"""The combined agent: Eyes (A) -> rules engine and validator (B) -> LLM fallback, remedy,
dry run, action safety (C) -> approval, report, memory (E), driven by agent.main.run.

  python -m agent.run once      triage what is abnormal right now and handle it
  python -m agent.run watch     wait for an alert (2 abnormal polls), then handle it

  --replay FILE   run from a recorded JSONL file instead of the live demo
  --record FILE   save every backend response while running live
  --auto-reject   never approve (evaluation runs score the diagnosis only)

Needs a baseline first: python -m agent.eyes baseline (no fault active).
Actions use a preview-only adapter until the organisers' Action API exists: the approval
gate is real, but execute refuses and the incident escalates.
Output goes to runs/<time>/: report.json, report.md, ledger.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agent import baseline as baseline_mod
from agent import detect, report, validate
from agent.act.action_log import ActionLog
from agent.act.actions import Actions
from agent.act.killswitch import KillSwitch
from agent.act.preview import PreviewOnlyAdapter
from agent.act.remedy import Remedy
from agent.approval.cli import AutoReject, CliApproval
from agent.ledger import Ledger
from agent.llm.investigator import Investigator
from agent.llm.responses import NoModel, ResponsesAdapter
from agent.main import Dependencies, EyesContext, run
from agent.memory import Memory
from agent.rules.engine import Engine
from agent.tools import Toolbox, registry
from agent.tools.backends import BackendError, Backends


def dependencies(b: Backends, base, alert, approval, model=None, adapter=None, state_dir=Path("state"),
                 verification_sleep=None) -> Dependencies:
    """One fresh set of collaborators per incident (Actions must not be reused)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger()
    tb = Toolbox(b, ledger, base)
    eyes = EyesContext(tb, alert, verification_sleep=verification_sleep)
    return Dependencies(
        ledger=ledger, triage=eyes, engine=Engine(), validate=validate,
        investigator=Investigator.from_eyes(model or NoModel(), tb),
        remedy=Remedy(),
        actions=Actions(adapter or PreviewOnlyAdapter(), KillSwitch(), ActionLog(str(state_dir / "action_log.jsonl"))),
        approval=approval, verify=eyes, report=report, memory=Memory(state_dir / "memory.json"))


def handle(b, base, alert, approval, model=None, out_dir=Path("runs"), verification_sleep=None):
    d = dependencies(b, base, alert, approval, model=model, verification_sleep=verification_sleep)
    incident, (report_json, report_md) = asyncio.run(run(alert, d))
    out = out_dir / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report_json, indent=2))
    (out / "report.md").write_text(report_md)
    d.ledger.save(out / "ledger.json")
    return incident, report_md, out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m agent.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["once", "watch"])
    p.add_argument("--replay")
    p.add_argument("--record")
    p.add_argument("--baseline", default=str(baseline_mod.DEFAULT_PATH))
    p.add_argument("--auto-reject", action="store_true")
    p.add_argument("--model", help="model name for the LLM fallback (key and default in .env)")
    p.add_argument("--max-min", type=float, help="watch: give up after this many minutes")
    p.add_argument("--out", default="runs")
    args = p.parse_args(argv)
    registry.assert_read_only()

    b = Backends.replay(args.replay) if args.replay else (Backends.recording(args.record) if args.record else Backends.live())
    replay = bool(args.replay)
    try:
        if Path(args.baseline).exists() and not replay:
            base = baseline_mod.Baseline.load(args.baseline)
        elif replay:
            base = baseline_mod.compute(b, end_offset_min=20)
        else:
            print("No baseline: run `python -m agent.eyes baseline` with no fault active first.", file=sys.stderr)
            return 1
        if args.command == "once":
            found = detect.poll(b, base)
            alert = detect.pick(found) if found else None
        else:
            max_min = args.max_min or (30 if replay else None)
            alert = detect.watch(b, base, max_polls=int(max_min * 4) if max_min else None,
                                 sleep=(lambda s: None) if replay else time.sleep,
                                 on_poll=lambda ts, c, s: print(f"{ts:%H:%M:%S} " + ("; ".join(
                                     f"{a.service} {a.signal} ({s.get((a.service, a.signal))})" for a in c[:4]) or "all normal"),
                                     file=sys.stderr))
        if alert is None:
            print("Nothing abnormal.")
            return 0
        print(f"ALERT: {detect.fmt_alert(alert)}", file=sys.stderr)
        model = NoModel() if replay else ResponsesAdapter.from_env(args.model)
        approval = AutoReject() if args.auto_reject else CliApproval()
        incident, md, out = handle(b, base, alert, approval, model=model, out_dir=Path(args.out),
                                   verification_sleep=(lambda s: None) if replay else None)
    except BackendError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(md)
    print(f"outcome: {incident.outcome}; saved to {out}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
