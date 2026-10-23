"""Eyes command line: the Checkpoint 1 demo, and a way to poke each tool by hand.

  python -m agent.eyes baseline            record "normal" (run with no fault active)
  python -m agent.eyes watch               wait for an alert, then triage and summarise it
  python -m agent.eyes snapshot            triage and summarise right now, no waiting
  python -m agent.eyes tool NAME '{json}'  run one read-only tool and print its evidence
  python -m agent.eyes verify SERVICE SIGNAL   wait for one signal to recover

Add --record FILE to save every backend response, or --replay FILE to run from one.
Results go to runs/<time>/: alert.json, snapshot.json, ledger.json, summary.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent import baseline as baseline_mod
from agent import detect, summarize, triage, verify
from agent.contracts import Alert
from agent.ledger import Ledger
from agent.tools import Toolbox, registry
from agent.tools.backends import Backends


def _backends(args) -> Backends:
    if args.replay:
        return Backends.replay(args.replay)
    if args.record:
        return Backends.recording(args.record)
    return Backends.live()


def _baseline(b: Backends, args, offset_if_missing: int) -> baseline_mod.Baseline:
    path = Path(args.baseline)
    if path.exists() and not args.replay:
        return baseline_mod.Baseline.load(path)
    print(f"no baseline at {path}; computing one from the {offset_if_missing} min before now ...", file=sys.stderr)
    base = baseline_mod.compute(b, end_offset_min=offset_if_missing)
    _warn(base)
    if not args.replay:
        base.save(path)
    return base


def _warn(base: baseline_mod.Baseline) -> None:
    for w in base.warnings:
        print(f"  warning: {w}", file=sys.stderr)


def _write_run(args, alert: Alert | None, snap, ledger: Ledger, summary: str) -> Path:
    out = Path(args.out) / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    if alert:
        (out / "alert.json").write_text(alert.model_dump_json(indent=2))
    (out / "snapshot.json").write_text(snap.model_dump_json(indent=2))
    ledger.save(out / "ledger.json")
    (out / "summary.md").write_text(summary)
    return out


def _triage_and_report(b: Backends, base, args, alert: Alert | None) -> None:
    ledger = Ledger()
    tb = Toolbox(b, ledger, base)
    print("triaging ...", file=sys.stderr)
    snap = triage.snapshot(tb, alert)
    text = summarize.build(alert, snap, ledger)
    out = _write_run(args, alert, snap, ledger, text)
    print(text)
    print(f"saved to {out}/", file=sys.stderr)


def cmd_baseline(args) -> None:
    b = _backends(args)
    base = baseline_mod.compute(b, end_offset_min=args.offset_min, window_min=args.window_min)
    base.save(args.baseline)
    print(f"baseline over {base.window_min} min saved to {args.baseline}")
    for svc, row in sorted(base.services.items()):
        er, p95, rate = row.get("error_ratio"), row.get("p95_ms"), row.get("req_rate")
        print(f"  {svc:16} {rate or 0:7.2f} req/s  errors {er or 0:6.1%}  p95 {p95 or 0:8.1f} ms")
    print(f"  {len(base.containers)} containers, {len(base.edges)} call edges")
    _warn(base)


def cmd_watch(args) -> None:
    b = _backends(args)
    base = _baseline(b, args, offset_if_missing=0)

    def show(ts, candidates, streak):
        if "error" in streak:
            print(f"{ts:%H:%M:%S} backend error: {streak['error']}", file=sys.stderr)
        elif not candidates:
            print(f"{ts:%H:%M:%S} all normal", file=sys.stderr)
        else:
            print(f"{ts:%H:%M:%S} " + "; ".join(f"{a.service} {a.signal} {a.value:.3g} ({streak[(a.service, a.signal)]})"
                                               for a in candidates[:4]), file=sys.stderr)

    max_min = args.max_min or (30 if args.replay else None)  # a replay's clock stands still, so bound it
    max_polls = int(max_min * 60 / 15) if max_min else None
    alert = detect.watch(b, base, max_polls=max_polls, on_poll=show,
                         sleep=(lambda s: None) if args.replay else __import__("time").sleep)
    if alert is None:
        print("no alert before the time limit")
        return
    print(f"ALERT: {detect.fmt_alert(alert)}", file=sys.stderr)
    _triage_and_report(b, base, args, alert)


def cmd_snapshot(args) -> None:
    b = _backends(args)
    base = _baseline(b, args, offset_if_missing=args.offset_min)
    candidates = detect.poll(b, base)
    alert = detect.pick(candidates) if candidates else None
    _triage_and_report(b, base, args, alert)


def cmd_tool(args) -> None:
    b = _backends(args)
    base = baseline_mod.Baseline.load(args.baseline) if Path(args.baseline).exists() else None
    tb = Toolbox(b, Ledger(), base)
    result = registry.call(tb, args.name, json.loads(args.args or "{}"))
    print(json.dumps(result, indent=2, default=str))


def cmd_verify(args) -> None:
    b = _backends(args)
    base = _baseline(b, args, offset_if_missing=0)
    tb = Toolbox(b, Ledger(), base)
    alert = Alert(service=args.service, signal=args.signal, value=0, baseline=0, ts=b.now_dt())
    result = verify.check(tb, alert, sleep=(lambda s: None) if args.replay else __import__("time").sleep,
                          on_poll=lambda ok, last: print("healthy" if ok else "still abnormal", file=sys.stderr))
    print(result.model_dump_json(indent=2))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m agent.eyes", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--record", help="save every backend response to this JSONL file")
    p.add_argument("--replay", help="run from a recorded JSONL file instead of the live demo")
    p.add_argument("--baseline", default=str(baseline_mod.DEFAULT_PATH))
    p.add_argument("--out", default="runs")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("baseline", help="record normal behaviour (no fault active)")
    s.add_argument("--offset-min", type=int, default=0, help="end the window this many minutes ago")
    s.add_argument("--window-min", type=int, default=None)
    s.set_defaults(fn=cmd_baseline)

    s = sub.add_parser("watch", help="wait for an alert, then triage and summarise")
    s.add_argument("--max-min", type=float, default=None, help="give up after this many minutes")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("snapshot", help="triage and summarise now")
    s.add_argument("--offset-min", type=int, default=20,
                   help="if there is no saved baseline, take one ending this many minutes ago")
    s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("tool", help="run one read-only tool")
    s.add_argument("name", choices=sorted(registry.READ_ONLY_TOOLS))
    s.add_argument("args", nargs="?", help='JSON arguments, e.g. \'{"service": "payment"}\'')
    s.set_defaults(fn=cmd_tool)

    s = sub.add_parser("verify", help="wait for one signal of one service to recover")
    s.add_argument("service")
    s.add_argument("signal")
    s.set_defaults(fn=cmd_verify)

    args = p.parse_args(argv)
    registry.assert_read_only()
    args.fn(args)


if __name__ == "__main__":
    main()
