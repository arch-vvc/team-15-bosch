"""Development incident reports and evaluation notes; no fabricated official schema."""

import json
from datetime import datetime, timezone
from pathlib import Path


def markdown(report):
    diagnosis = report.get("diagnosis") or {}
    lines = [f"# Incident {report['id']}", "", f"Mode: **{report['mode']}** · State: **{report['state']}**", "",
             "Development report; not the official organizer JSON schema.", "", "## Alert", "", json.dumps(report["alert"], ensure_ascii=False), "",
             "## Diagnosis", "", diagnosis.get("root_cause", "No validated diagnosis; escalated with collected evidence."), "",
             f"Component: {diagnosis.get('component', 'unknown')}. Confidence estimate: {diagnosis.get('confidence', 'unavailable')} (not calibrated).",
             "", "Root cause is a hypothesis. Exact evidence references are checked; causality still needs review.", "",
             f"Uncertainty: {diagnosis.get('uncertainty', 'See timeline.')}", "", "## Evidence", ""]
    if diagnosis.get("causes"):
        lines += ["| Component | Signature | Confidence |", "| --- | --- | --- |"]
        lines += [f"| {c['component']} | {c.get('signature', 'unknown')} | {c['confidence']} |" for c in diagnosis["causes"]]
        lines += ["", f"Investigation route: {diagnosis.get('source', 'unknown')}. Reports are rendered from the evidence ledger; no model invents report facts.", ""]
    for item in report["evidence"]:
        lines.append(f"- `{item['evidence_id']}` — {item.get('service', '')} / {item['kind']}: {item['status']}; {len(item['observations'])} observations. Source: `{item['source_url']}`.")
    lines += ["", "## Timeline", ""]
    for event in report["timeline"]:
        at = datetime.fromtimestamp(event["created"], timezone.utc).isoformat()
        lines.append(f"- {at} — **{event['kind']}**: {json.dumps(event['detail'], ensure_ascii=False)}")
    lines += ["", "## Prevention suggestion", "", diagnosis.get("prevention", "Investigate before recommending a preventive change."), "",
              "## Limitations", "", "Synthetic runs are development exercises. Live Action API, official scoring compliance, and hidden-incident performance require organizer interfaces and real evaluation.", ""]
    return "\n".join(lines)


def export_report(store, incident, directory):
    report = store.report(incident)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / incident
    target.with_suffix(".json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    target.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    return target.with_suffix(".md")
