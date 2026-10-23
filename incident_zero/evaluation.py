"""Synthetic architecture evaluation; never presented as hidden-incident accuracy."""
import json
from pathlib import Path
from incident_zero.actions import execute_plan
from incident_zero.demo import ScenarioTools, ScenarioBackend, UnknownDriver, SCENARIOS
from incident_zero.pipeline import Pipeline
from incident_zero.reporting import export_report


def exercise(store, directory, scenario="recovered", service="payment", automatic=True):
    tools = ScenarioTools(scenario, service)
    backend = ScenarioBackend(tools)
    incident = Pipeline(store, tools, backend.actions, model_factory=UnknownDriver).run(
        {"service": tools.entry, "summary": "Synthetic architecture exercise", "synthetic": True})
    if automatic:
        for _ in range(3):
            plans = [p for p in store.report(incident)["proposals"] if p["status"] == "pending"]
            if not plans:
                break
            plan = plans[0]
            execute_plan(store, plan["id"], backend, tools, dry_run=True)
            store.decide(plan["id"], plan["digest"], "automated synthetic test (not a human)", scenario != "rejected",
                         confirm_service=plan["body"]["action"]["service"])
            if scenario == "rejected":
                break
            if scenario == "killed":
                store.kill(True)
            try:
                execute_plan(store, plan["id"], backend, tools, dry_run=False, interval=0)
            except ValueError as exc:
                store.transition(incident, "escalated", {"reason": str(exc), "synthetic": True})
            finally:
                if scenario == "killed":
                    store.kill(False)
    target = export_report(store, incident, directory)
    return store.report(incident), target


def evaluate(store, directory):
    directory = Path(directory)
    expected = {name: ("resolved" if name in {"recovered", "combined", "oom", "restart-loop", "tier2"} else "escalated") for name in SCENARIOS}
    results = []
    for scenario, state in expected.items():
        report, path = exercise(store, directory, scenario)
        consumed = sum(p["status"] == "consumed" for p in report["proposals"])
        results.append({"scenario": scenario, "expected_state": state, "actual_state": report["state"],
            "passed": report["state"] == state and consumed <= 2, "mutations": consumed, "report": str(path)})
    summary = {"synthetic": True, "uses_real_llm": False, "measures_model_accuracy": False, "results": results,
        "limitations": "Hand-authored synthetic workflow cases, including a protocol test double for fallback. No organizer practice/hidden cases or live LLM evaluated. Escalation is the expected safe outcome for unsupported unknown causes, not a successful diagnosis."}
    (directory / "evaluation.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = ["# Synthetic architecture evaluation", "", summary["limitations"], "", "| Scenario | Expected | Actual | Mutations | Pass |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {r['scenario']} | {r['expected_state']} | {r['actual_state']} | {r['mutations']} | {r['passed']} |" for r in results]
    (directory / "evaluation.md").write_text("\n".join(lines) + "\n")
    return summary
