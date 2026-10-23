"""Incident Zero local command-line workflow."""

import argparse
import json
import time
from pathlib import Path

from incident_zero.actions import DisabledBackend, HttpActionBackend, execute_plan
from incident_zero.pipeline import Pipeline
from incident_zero.alerts import detect
from incident_zero.doctor import as_report, run_checks
from incident_zero.evaluation import evaluate, exercise
from incident_zero.demo import SCENARIOS
from incident_zero.model import ResponsesModel
from incident_zero.reporting import export_report
from incident_zero.store import Store
from incident_zero.telemetry import SERVICES, LocalJsonClient, observe
from incident_zero.tools import BundleTools, LiveTools


def endpoints(parser):
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--jaeger-url", default="http://localhost:8080/jaeger/ui")
    parser.add_argument("--opensearch-url")
    parser.add_argument("--container-health", action="store_true", help="Enable projected read-only Docker state inspection")


def live_tools(args):
    return LiveTools(args.prometheus_url, args.jaeger_url, args.opensearch_url, args.container_health)


def action_backend(args):
    return HttpActionBackend(json.loads(args.action_contract.read_text())) if args.action_contract else DisabledBackend()


def watch_everything(args, store, model, tools):
    """Eyes alert watcher: every service and container against the recorded baseline, two
    consecutive abnormal polls to fire, then the full pipeline on the most severe alert."""
    from agent import detect as eyes
    from agent.tools.backends import BackendError
    from incident_zero.tools import HEALTH_SERVICES

    base = tools.eyes.baseline
    if base is None:
        raise ValueError("No baseline at state/baseline.json: run `python -m agent.eyes baseline` with no fault active")
    streak, last_incident = {}, -float("inf")
    for cycle in range(args.cycles):
        if store.killed():
            print("Watch stopped by kill switch")
            break
        try:
            candidates = [a for a in eyes.poll(tools.eyes.backends, base, containers=args.container_health)
                          if a.service in HEALTH_SERVICES]
        except BackendError as exc:
            print(json.dumps({"cycle": cycle + 1, "error": str(exc)}))
            candidates = []
        current = {(a.service, a.signal): a for a in candidates}
        streak = {key: streak.get(key, 0) + 1 for key in current}
        firing = [current[key] for key, n in streak.items() if n >= 2]
        if firing and time.monotonic() - last_incident >= 300:
            alert = eyes.pick(firing)
            incident = Pipeline(store, tools, actions=action_backend(args).actions, model_factory=model).run(
                {"service": alert.service, "summary": eyes.fmt_alert(alert), "source": "eyes_watcher"})
            print(export_report(store, incident, args.reports).resolve())
            print("State:", store.incident(incident)["state"])
            last_incident, streak = time.monotonic(), {}
        else:
            print(json.dumps({"cycle": cycle + 1, "abnormal": [f"{a.service}:{a.signal}" for a in candidates]}))
        if cycle + 1 < args.cycles:
            time.sleep(args.interval)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Incident Zero: investigate with evidence; require approval for actions")
    parser.add_argument("--db", type=Path, default=Path("output/incidents.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Read-only prerequisite checks")
    doctor.add_argument("--json", action="store_true")
    observation = commands.add_parser("observe", help="Collect metric/trace evidence without a model")
    observation.add_argument("--service", choices=SERVICES, default="checkout")
    observation.add_argument("--lookback", type=int, default=300)
    observation.add_argument("--output", type=Path)
    endpoints(observation)
    investigation = commands.add_parser("investigate", help="Rules-first investigation with bounded LLM fallback; never executes mutations")
    investigation.add_argument("--service", choices=SERVICES, required=True)
    investigation.add_argument("--alert", required=True)
    investigation.add_argument("--model")
    investigation.add_argument("--action-contract", type=Path)
    investigation.add_argument("--bundle", type=Path, help="Synthetic development bundle; investigation still uses a real model")
    investigation.add_argument("--reports", type=Path, default=Path("output/reports"))
    endpoints(investigation)
    watch = commands.add_parser("watch", help="Poll thresholds and investigate sustained alerts")
    watch.add_argument("--service", choices=SERVICES,
                       help="Watch one service with fixed thresholds. Omit to watch every service and container "
                            "against the Eyes baseline (python -m agent.eyes baseline)")
    watch.add_argument("--cycles", type=int, default=10)
    watch.add_argument("--interval", type=int, default=30)
    watch.add_argument("--model")
    watch.add_argument("--action-contract", type=Path)
    watch.add_argument("--reports", type=Path, default=Path("output/reports"))
    endpoints(watch)
    simulation = commands.add_parser("simulate", help="Synthetic deterministic exercise; no LLM or Docker changes")
    simulation.add_argument("--scenario", choices=SCENARIOS, default="recovered")
    simulation.add_argument("--service", choices=SERVICES, default="payment")
    simulation.add_argument("--exercise", action="store_true", help="Simulate approval and execution entirely in test data")
    simulation.add_argument("--reports", type=Path, default=Path("output/synthetic-reports"))
    evaluation = commands.add_parser("evaluate", help="Twelve synthetic architecture cases, not model accuracy")
    evaluation.add_argument("--reports", type=Path, default=Path("output/evaluation"))
    report = commands.add_parser("report", help="Export incident JSON and Markdown")
    report.add_argument("incident")
    report.add_argument("--reports", type=Path, default=Path("output/reports"))
    plan = commands.add_parser("plan", help="Display exact proposal and approval digest")
    plan.add_argument("proposal")
    decision = commands.add_parser("decide", help="Human-only local approval/rejection entry point")
    decision.add_argument("proposal")
    decision.add_argument("--confirm-service", help="Tier 2: type the target service after a dry run")
    decision.add_argument("--digest", required=True)
    decision.add_argument("--by", required=True)
    decision.add_argument("--decision", choices=["approve", "reject"], required=True)
    execution = commands.add_parser("execute", help="Dry-run a plan; live dispatch needs the official Action API adapter")
    execution.add_argument("proposal")
    execution.add_argument("--live", action="store_true")
    execution.add_argument("--action-contract", type=Path)
    endpoints(execution)
    kill = commands.add_parser("kill", help="Persistent kill switch")
    kill.add_argument("state", choices=["on", "off"])
    args = parser.parse_args()
    if args.command == "doctor":
        checks = run_checks(Path.cwd())
        if args.json:
            print(json.dumps(as_report(checks), indent=2))
        else:
            for check in checks:
                print(f"[{check.status.upper()}] {check.name}: {check.detail}")
        return int(any(check.status == "fail" for check in checks))
    if args.command == "observe":
        try:
            result = observe(args.service, args.lookback, LocalJsonClient(args.prometheus_url), LocalJsonClient(args.jaeger_url))
        except ValueError as exc:
            parser.error(str(exc))
        rendered = json.dumps(result, indent=2, allow_nan=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
            print(args.output.resolve())
        else:
            print(rendered)
        return int(any(item["status"] == "unavailable" for item in result["evidence"]))
    store = Store(args.db)
    try:
        if args.command == "kill":
            store.kill(args.state == "on")
            print("Kill switch " + args.state)
        elif args.command == "plan":
            print(json.dumps(store.proposal(args.proposal), indent=2))
        elif args.command == "decide":
            store.decide(args.proposal, args.digest, args.by, args.decision == "approve", confirm_service=args.confirm_service)
            print("Decision recorded; no action has been executed.")
        elif args.command == "execute":
            try:
                print(json.dumps(execute_plan(store, args.proposal, action_backend(args), live_tools(args) if args.live else None, dry_run=not args.live, interval=60), indent=2))
            finally:
                print(export_report(store, store.proposal(args.proposal)["incident"], Path("output/reports")).resolve())
        elif args.command == "report":
            print(export_report(store, args.incident, args.reports).resolve())
        elif args.command == "evaluate":
            result = evaluate(store, args.reports)
            print(json.dumps(result, indent=2))
            return int(not all(item["passed"] for item in result["results"]))
        elif args.command == "simulate":
            result, target = exercise(store, args.reports, args.scenario, args.service, automatic=args.exercise)
            print(f"SYNTHETIC TEST ONLY: {result['id']} -> {result['state']}\n{target.resolve()}")
            if result["state"] == "awaiting_approval":
                print(json.dumps(result["proposals"], indent=2))
        elif args.command == "investigate":
            model = lambda: ResponsesModel(args.model)
            tools = BundleTools(json.loads(args.bundle.read_text())) if args.bundle else live_tools(args)
            incident = Pipeline(store, tools, actions=action_backend(args).actions, model_factory=model).run({"service": args.service, "summary": args.alert, "source": "manual_alert"})
            print(export_report(store, incident, args.reports).resolve())
            print("State:", store.incident(incident)["state"])
        elif args.command == "watch":
            if not 1 <= args.cycles <= 120 or not 10 <= args.interval <= 300:
                raise ValueError("Watch supports 1-120 cycles and 10-300 second intervals")
            model, tools = lambda: ResponsesModel(args.model), live_tools(args)
            tools.set_guard(store.killed)
            if args.service is None:
                return watch_everything(args, store, model, tools)
            streak = 0
            last_incident = -float("inf")
            for cycle in range(args.cycles):
                if store.killed():
                    print("Watch stopped by kill switch")
                    break
                rows = tools.call("inspect_service", args.service, 300, time.time())
                if args.container_health:
                    try:
                        rows.extend(tools.call("read_container_health", args.service, 300, time.time()))
                    except Exception:
                        pass  # Failed health reads are not proof of a failure or health.
                alert = detect(rows, args.service)
                streak = streak + 1 if alert else 0
                if streak >= 2 and time.monotonic() - last_incident >= 300:
                    incident = Pipeline(store, tools, actions=action_backend(args).actions, model_factory=model).run(alert)
                    print(export_report(store, incident, args.reports).resolve())
                    last_incident = time.monotonic()
                    streak = 0
                else:
                    print(json.dumps({"cycle": cycle + 1, "threshold_breached": bool(alert), "query_statuses": [row["status"] for row in rows]}))
                if cycle + 1 < args.cycles:
                    time.sleep(args.interval)
    except (ValueError, OSError, KeyError) as exc:
        print("Error:", str(exc))
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
