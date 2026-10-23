"""Incident report (step 13): JSON plus readable markdown, built only from the Incident and
the ledger (no free text that the evidence does not hold). Field names are a development
layout until the organisers publish their schema; adapt `build` to it then.
"""
from __future__ import annotations

from agent.contracts import Incident

PREVENTION = {
    "container_down": "Find why the container exited (exit code, last logs) and alert on container state, not only on errors.",
    "crash_loop": "Fix the cause of repeated exits; alert on restart count and OOM kills.",
    "memory_leak": "A restart only mitigates a leak; find the growing structure and alert on memory growth rate.",
    "service_errors": "Fix the failing operation in this service; alert on its error ratio at the source, not on its callers.",
    "cpu_starvation": "Restore the CPU limit and alert when a container's limits change.",
    "memory_starvation": "Restore the memory limit and alert when a container's limits change.",
    "high_cpu": "Profile the CPU hot path; alert on CPU near the limit together with latency.",
    "traffic_flood": "Rate-limit the entry point and alert on request-rate surges.",
}


def build(incident: Incident, ledger) -> tuple[dict, str]:
    final = incident.diagnoses[-1] if incident.diagnoses else None
    chosen = next((h for h in final.hypotheses if h.id == final.selected_id), None) if final else None
    if chosen:
        cited = set(chosen.supporting_evidence + chosen.contradicting_evidence)
    else:
        # nothing selected: show what was seen about the alerting service, so an escalation
        # still hands the human the evidence
        svc = incident.alert.service
        cited = {eid for eid in incident.snapshot.evidence_ids if ledger.has(eid)
                 and any(f.service == svc and f.direction in ("up", "down", "absent") for f in ledger.get(eid).facts)}
        cited |= {eid for h in (final.hypotheses if final else []) for eid in h.supporting_evidence}
    cited = sorted(cited, key=lambda e: int(e[1:]) if e[1:].isdigit() else 0)
    evidence = [ledger.get(eid).model_dump(mode="json") for eid in cited if ledger.has(eid)]
    actions = []
    for proposal, approval, result, verified in incident.actions:
        actions.append({"proposal": proposal.model_dump(mode="json"),
                        "approval": approval.model_dump(mode="json") if approval else None,
                        "result": result.model_dump(mode="json") if result else None,
                        "verification": verified.model_dump(mode="json") if verified else None})
    report = {
        "alert": incident.alert.model_dump(mode="json"),
        "outcome": incident.outcome,
        "root_cause": None if chosen is None else {
            "component": chosen.component, "cause": chosen.cause, "confidence": chosen.confidence,
            "source": chosen.source, "reason": final.reason},
        "diagnosis_state": final.state if final else None,
        "diagnosis_reason": final.reason if final else None,
        "diagnosis_path": [{"state": d.state, "reason": d.reason,
                            "source": d.hypotheses[0].source if d.hypotheses else None} for d in incident.diagnoses],
        "hypotheses": [h.model_dump(mode="json") for h in (final.hypotheses if final else [])],
        "evidence": evidence,
        "actions": actions,
        "timeline": [{"ts": ts.isoformat(), "step": step} for ts, step in incident.timeline],
        "prevention": PREVENTION.get(chosen.cause) if chosen else None,
        "limitations": "Confidence values are uncalibrated starting points. Evidence is a sample of telemetry.",
    }
    return report, markdown(report)


def markdown(r: dict) -> str:
    a = r["alert"]
    lines = [f"# Incident report: {a['service']} {a['signal']}", "",
             f"**Outcome:** {r['outcome']}  ", f"**Alert:** {a['service']} {a['signal']} = {a['value']:.3g} "
             f"(baseline {a['baseline']:.3g}) at {a['ts']}", ""]
    rc = r["root_cause"]
    lines += ["## Root cause", ""]
    if rc:
        lines += [f"**{rc['component']}**: {rc['cause']} (confidence {rc['confidence']:.2f}, from {rc['source']})", "",
                  rc["reason"], ""]
    else:
        lines += [f"Not established (diagnosis state {r['diagnosis_state']}); escalated with the evidence below.", "",
                  f"Why: {r['diagnosis_reason']}", ""]
    others = [h for h in r["hypotheses"] if not rc or h["component"] != rc["component"] or h["cause"] != rc["cause"]]
    if others:
        lines += ["Other hypotheses considered: " + ", ".join(
            f"{h['id']} {h['component']}/{h['cause']} {h['confidence']:.2f}" for h in others), ""]
    focus = rc["component"] if rc else a["service"]
    lines += ["## Evidence", ""]
    for ev in r["evidence"]:
        about = [f for f in ev["facts"] if f["service"] == focus]
        for f in (about or ev["facts"])[:4]:
            base = f" (baseline {f['baseline']})" if f["baseline"] is not None else ""
            scope = f" [{f['scope']}]" if f["scope"] else ""
            lines.append(f"- `{ev['id']}` {ev['tool']}: {f['service']} {f['signal']}{scope} = {f['value']}{base}")
    if not r["evidence"]:
        lines.append("- none cited")
    lines += ["", "## Actions", ""]
    for act in r["actions"]:
        p, ap, res, ver = act["proposal"], act["approval"], act["result"], act["verification"]
        lines.append(f"- {p['action']} on {p['target']} (tier {p['tier']}): "
                     f"{'approved by ' + ap['operator'] if ap and ap['approved'] else 'not approved'}; "
                     f"{'executed ok' if res and res['ok'] else 'not executed' if res is None else 'execution failed'}; "
                     f"{'recovered' if ver and ver['recovered'] else 'not verified' if ver is None else 'did not recover'}")
    if not r["actions"]:
        lines.append("- none")
    lines += ["", "## Timeline", ""] + [f"- {t['ts']} {t['step']}" for t in r["timeline"]]
    if r["prevention"]:
        lines += ["", "## Prevention", "", r["prevention"]]
    lines += ["", "## Limitations", "", r["limitations"], ""]
    return "\n".join(lines)
