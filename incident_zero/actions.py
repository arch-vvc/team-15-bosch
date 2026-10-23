"""Approval-gated execution; live adapter disabled until its contract is supplied."""

import hashlib
import json
import math
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request

from incident_zero.tools import BundleTools, HEALTH_SERVICES
from incident_zero.store import canonical
from incident_zero.telemetry import LocalJsonClient, MAX_RESPONSE_BYTES
from incident_zero.telemetry import SERVICES


class DisabledBackend:
    mode = "live"
    configured = False
    actions = []

    def execute(self, action, incident, approval=None):
        raise ValueError("Official Action API contract is missing; live execution is disabled")


class SyntheticBackend:
    mode = "synthetic"
    configured = True

    def __init__(self, tools: BundleTools, service):
        self.tools = tools
        self.actions = [{"id": "synthetic_restart", "service": service,
                         "description": "Simulate a restart in an in-memory synthetic exercise; no Docker or HTTP mutation",
                         "parameters": {}, "tier": 1, "signatures": ["container_stopped"]}]

    def execute(self, action, incident, approval=None):
        if action not in self.actions:
            raise ValueError("Unknown synthetic action")
        self.tools.recovered = True
        return {"accepted": True, "synthetic": True}


class HttpActionBackend:
    """Operator maps documented Action API endpoints; the model cannot supply URLs."""

    mode = "live"
    configured = True

    def __init__(self, contract):
        if contract.get("operator_confirmed") is not True or not contract.get("reviewed_by"):
            raise ValueError("Action contract requires operator confirmation and reviewer attribution")
        self.client = LocalJsonClient(contract["base_url"])
        self.specs = {}
        self.actions = []
        for spec in contract.get("actions", []):
            path = spec["path"]
            if not path.startswith("/") or path.startswith("//") or any(word in path for word in ("..", "?", "REPLACE")):
                raise ValueError("Invalid documented action path")
            if spec["id"] in self.specs or spec["service"] not in HEALTH_SERVICES:
                raise ValueError("Duplicate action ID or unknown target service")
            if not isinstance(spec["body"], dict) or not isinstance(spec["accepted_field"], str) or not spec["accepted_field"] or spec.get("accepted_value") is None:
                raise ValueError("Action needs a fixed body and documented acceptance field")
            contexts = spec.get("context_fields", {})
            if set(contexts) - {"incident_id", "proposal_id", "approval_digest", "approver"}:
                raise ValueError("Unknown approval context field")
            if any(not isinstance(field, str) or field in spec["body"] for field in contexts.values()):
                raise ValueError("Approval context must not overwrite fixed action parameters")
            fingerprint = hashlib.sha256(canonical({"base_url": contract["base_url"], "spec": spec}).encode()).hexdigest()
            action = {"id": spec["id"], "service": spec["service"], "description": spec["description"],
                      "parameters": spec["body"], "contract_digest": fingerprint}
            if "tier" in spec:
                if type(spec["tier"]) is not int or spec["tier"] not in (1, 2, 3):
                    raise ValueError("Invalid action tier")
                if not isinstance(spec.get("signatures", []), list) or any(not isinstance(v, str) for v in spec.get("signatures", [])):
                    raise ValueError("Invalid rule signature mapping")
                action.update(tier=spec["tier"], signatures=spec.get("signatures", []), rollback_action_id=spec.get("rollback_action_id"))
            self.actions.append(action)
            self.specs[spec["id"]] = spec
        if not self.actions:
            raise ValueError("Verified action contract has no allowlisted actions")

    def execute(self, action, incident, approval=None):
        if action not in self.actions or not approval:
            raise ValueError("Unknown action or missing approval context")
        spec = self.specs[action["id"]]
        body = dict(spec["body"])
        context = {"incident_id": incident, **approval}
        for key, field in spec.get("context_fields", {}).items():
            body[field] = context[key]
        headers = {"Content-Type": "application/json"}
        for header, variable in spec.get("header_env", {}).items():
            if header.lower() not in {"authorization", "x-api-key"}:
                raise ValueError("Unsupported Action API credential header")
            secret = os.environ.get(variable)
            if not secret:
                raise ValueError("Missing Action API credential environment variable")
            headers[header] = secret
        request = Request(self.client.base_url + spec["path"], data=canonical(body).encode(), headers=headers, method="POST")
        try:
            with self.client.opener.open(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Action response exceeded size limit; outcome is uncertain")
            result = json.loads(raw)
            accepted = spec["accepted_field"] in result and type(result.get(spec["accepted_field"])) is type(spec["accepted_value"]) and result.get(spec["accepted_field"]) == spec["accepted_value"]
            return {"accepted": accepted, "contract_action_id": action["id"]}
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise ValueError("Action request or acceptance check failed; do not retry without inspecting its outcome") from exc


def healthy_sample(records, max_error_ratio=0.01, max_latency_ms=2000):
    values = {}
    for item in records:
        if item["status"] != "observed" or not item["observations"]:
            continue
        if item["kind"] in {"server_calls_per_second", "server_errors_per_second", "server_p95_milliseconds"}:
            value = item["observations"][0].get("value")
            if type(value) in (int, float) and math.isfinite(value):
                values[item["kind"]] = value
    if len(values) != 3:
        return False
    calls = values["server_calls_per_second"]
    errors = values["server_errors_per_second"]
    latency = values["server_p95_milliseconds"]
    return calls > 0 and 0 <= errors <= calls and errors / calls <= max_error_ratio and 0 <= latency <= max_latency_ms


def recovered_service(records, target):
    rows = [item for item in records if item.get("service") == target]
    if target in SERVICES:
        return healthy_sample(rows)
    return any(item["kind"] == "container_health" and item["status"] == "observed"
               and any(row.get("running") is True and row.get("health") == "healthy" for row in item["observations"]) for item in rows)


def execute_plan(store, proposal_id, backend, tools, dry_run=True, samples=3, interval=60):
    if tools is not None and hasattr(tools, "set_guard"):
        tools.set_guard(store.killed)
    plan = store.proposal(proposal_id)
    incident = plan["incident"]
    if store.killed():
        raise ValueError("Kill switch is active")
    if dry_run:
        with store.db:
            store.event(incident, "dry_run", {"proposal_id": proposal_id, "mutation": False})
        return {"dry_run": True, "proposal": plan}
    if not backend.configured:
        raise ValueError("Official Action API contract is missing; live execution is disabled")
    if backend.mode != store.incident(incident)["mode"] or tools.mode != backend.mode:
        raise ValueError("Synthetic and live execution modes cannot be mixed")
    if plan["body"]["action"] not in backend.actions:
        raise ValueError("Approved action no longer matches the trusted backend allowlist")
    if samples < 3 or interval < 0 or (backend.mode == "live" and interval < 60):
        raise ValueError("Verification requires at least three samples and live intervals of at least sixty seconds")
    baseline = [item for item in store.evidence(incident) if item.get("service") == plan["body"]["action"]["service"]]
    if not baseline:
        raise ValueError("Missing baseline evidence for action target")
    store.claim(proposal_id, plan["body"])
    try:
        # The claim is committed before dispatch. Never retry an ambiguous mutation automatically.
        if store.killed():
            raise ValueError("Kill switch activated before dispatch")
        result = backend.execute(plan["body"]["action"], incident, {
            "proposal_id": proposal_id, "approval_digest": plan["digest"], "approver": plan["approver"]})
        if result.get("accepted") is not True:
            raise ValueError("Action was not accepted")
        store.transition(incident, "verifying", {"action_result": result})
        passed = []
        target_passed = []
        targets = [plan["body"]["action"]["service"]]
        alerted_service = store.incident(incident)["alert"].get("service")
        if alerted_service and alerted_service not in targets:
            targets.append(alerted_service)
        if plan["body"].get("architecture") and not plan["body"].get("remaining_causes"):
            for cause in (store.incident(incident)["diagnosis"] or {}).get("causes", []):
                if cause["component"] not in targets:
                    targets.append(cause["component"])
        for index in range(samples):
            if store.killed():
                raise ValueError("Kill switch active during recovery verification")
            if interval:
                time.sleep(interval)
            end = time.time()
            fresh = []
            for target in targets:
                if store.killed():
                    raise ValueError("Kill switch active before verification tool call")
                fresh.extend(tools.call("inspect_service" if target in SERVICES else "read_container_health", target, 60, end))
            saved = [store.add_evidence(incident, item) for item in fresh]
            passed.append(all(recovered_service(saved, target) for target in targets))
            target_passed.append(recovered_service(saved, targets[0]))
            with store.db:
                store.event(incident, "verification_sample", {"sample": index + 1, "passed": passed[-1], "evidence_ids": [item["evidence_id"] for item in saved]})
        verified = all(passed)
        detail = {"samples": samples, "all_passed": verified, "verified_services": targets,
                  "baseline_evidence_ids": [item["evidence_id"] for item in baseline],
                  "criteria": "positive server traffic, error ratio <= 1%, p95 <= 2000 ms in every sample",
                  "limitations": "Service-level telemetry checks do not replace an end-to-end customer transaction test. Synthetic verification uses replay data."}
        remaining = plan["body"].get("remaining_causes", [])
        if plan["body"].get("architecture") == "rules-first-v2":
            if plan["body"].get("rollback"):
                store.transition(incident, "escalated", {**detail, "reason": "Rollback attempted; human review required"})
                return detail
            if all(target_passed) and remaining:
                # Verify the first target before collecting fresh evidence for the second.
                from incident_zero.rules import signatures, checked, configured_signatures, rank_candidates
                from incident_zero.pipeline import propose_next
                candidate = remaining[0]
                if store.killed():
                    raise ValueError("Kill switch active before next-cause check")
                rows = []
                names = ["read_container_health", "read_resources"]
                if candidate["component"] in SERVICES:
                    names += ["inspect_service", "read_logs"]
                for name in names:
                    if store.killed():
                        raise ValueError("Kill switch active before next-cause tool call")
                    try:
                        rows.extend(tools.call(name, candidate["component"], 60, time.time()))
                    except Exception:
                        continue
                saved = [store.add_evidence(incident, row) for row in rows]
                policy = (store.incident(incident)["diagnosis"] or {}).get("rule_policy", {})
                ranked, ambiguous = rank_candidates(signatures(saved) + configured_signatures(saved, policy), policy)
                matches = [item for item in ranked if item["component"] == candidate["component"] and item["signature"] == candidate["signature"] and item["component"] not in ambiguous]
                if matches:
                    store.transition(incident, "diagnosed", {"reason": "First target verified; second cause reconfirmed", **detail})
                    next_id = propose_next(store, incident, backend.actions, [checked(matches[0], store.evidence(incident))])
                    return {**detail, "next_proposal": next_id, "requires_new_approval": True}
                if not verified:
                    store.transition(incident, "escalated", {"reason": "Second cause no longer confirmed but alerted service remains unhealthy", **detail})
                    return detail
            if not verified:
                rollback_id = plan["body"]["action"].get("rollback_action_id")
                rollback = next((a for a in backend.actions if a["id"] == rollback_id and a["service"] == targets[0] and a.get("tier") in (1, 2)), None)
                consumed = [p for p in store.report(incident)["proposals"] if p["status"] == "consumed"]
                if rollback and len(consumed) < 2 and rollback["id"] != plan["body"]["action"]["id"] and plan["body"]["diagnosis"]["confidence"] >= .85:
                    store.transition(incident, "diagnosed", {"reason": "Recovery failed; rollback requires separate strict approval", **detail})
                    next_id = store.propose(incident, {"architecture": "rules-first-v2", "action": rollback,
                        "diagnosis": plan["body"]["diagnosis"], "remaining_causes": [], "rollback": True, "tier": 2})
                    return {**detail, "rollback_proposal": next_id, "requires_new_approval": True}
        if not verified:
            detail["rollback"] = "No rollback was executed. A specific allowed rollback requires a separate explicit approval. Escalated for human recovery."
        store.transition(incident, "resolved" if verified else "escalated", detail)
        return detail
    except Exception as exc:
        store.transition(incident, "escalated", {"reason": str(exc), "action_may_have_executed": True,
                                              "retry": "Do not retry automatically; inspect action status and request a new plan"})
        return {"all_passed": False, "error": str(exc)}
