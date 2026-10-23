"""Transparent deterministic test driver. This is not an LLM or scored practice case."""

import json


def example_bundle(service="payment", recover=True, missing=False):
    before = [] if missing else [
        {"kind": "server_calls_per_second", "observations": [{"value": 10.0}]},
        {"kind": "server_errors_per_second", "observations": [{"value": 10.0}]},
        {"kind": "server_p95_milliseconds", "observations": [{"value": 3000.0}]},
        {"kind": "recent_traces", "observations": [{"trace_id": "synthetic-error", "error": True}]},
        {"kind": "container_health", "observations": [{"running": False, "status": "exited", "oom_killed": False}]},
        {"kind": "recent_logs", "observations": [{"body": "Synthetic process exited unexpectedly"}]},
    ]
    after = [
        {"kind": "server_calls_per_second", "observations": [{"value": 10.0}]},
        {"kind": "server_errors_per_second", "observations": [{"value": 0.0 if recover else 10.0}]},
        {"kind": "server_p95_milliseconds", "observations": [{"value": 100.0 if recover else 3000.0}]},
    ]
    return {"synthetic": True, "description": "Hand-authored synthetic test data, not an organizer incident", "before": {service: before}, "after": {service: after}}


class SyntheticModel:
    label = "deterministic-synthetic-driver-not-llm"

    def __init__(self, service):
        self.service = service
        self.turn = 0

    def respond(self, instructions, conversation, tools, max_output_tokens, timeout):
        self.turn += 1
        if self.turn <= 3:
            name = ("inspect_service", "read_container_health", "read_logs")[self.turn - 1]
            args = {"service": self.service, "lookback": 300}
        else:
            evidence = []
            for item in conversation:
                if item.get("type") == "function_call_output":
                    evidence.extend(json.loads(item["output"]).get("evidence", []))
            health = next((item for item in evidence if item["kind"] == "container_health" and item["observations"]), None)
            stopped = health is not None and health["observations"][0].get("running") is False
            claims = [] if not stopped else [{"evidence_id": health["evidence_id"], "observation_index": 0, "field": "running", "value": False}]
            name = "finish_investigation"
            args = {"component": self.service if stopped else "unknown",
                    "root_cause": "Synthetic container is stopped; the underlying reason for exit is unproven." if stopped else "Insufficient evidence to identify the cause.",
                    "confidence": 0.8 if stopped else 0.1, "claims": claims,
                    "action_id": "synthetic_restart" if stopped else None, "escalate": not stopped,
                    "uncertainty": "Deterministic synthetic exercise; does not establish real model accuracy.",
                    "prevention": "Review process exit causes and add monitoring for unexpected exits."}
        return {"output": [{"type": "function_call", "call_id": f"synthetic_call_{self.turn}", "name": name, "arguments": json.dumps(args)}], "usage": {"total_tokens": 0}}
