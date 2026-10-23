"""Portable architecture demonstrations using explicitly synthetic evidence only."""
import copy
import json
from incident_zero.synthetic import example_bundle
from incident_zero.tools import BundleTools

SCENARIOS = ("recovered", "failed", "missing", "rejected", "killed", "unknown", "combined", "oom", "restart-loop", "tier2", "rollback", "three-causes")


class ScenarioTools(BundleTools):
    def __init__(self, scenario, service="payment"):
        self.scenario = scenario
        self.fixed = set()
        bundle = example_bundle(service, missing=scenario == "missing")
        self.targets = [service]
        self.entry = service
        if scenario in {"combined", "three-causes"}:
            self.targets = ["payment", "cart"] + (["ad"] if scenario == "three-causes" else [])
            self.entry = "frontend"
            bundle["before"] = {target: example_bundle(target)["before"][target] for target in self.targets}
            bundle["after"] = {target: example_bundle(target)["after"][target] for target in self.targets}
            bundle["before"][self.entry] = copy.deepcopy(example_bundle(self.entry)["before"][self.entry])
            bundle["before"][self.entry] = [row for row in bundle["before"][self.entry] if row["kind"] != "container_health"]
            bundle["before"][self.entry].append({"kind": "recent_traces", "observations": [{"peer_service": target, "error": True} for target in self.targets]})
            bundle["after"][self.entry] = example_bundle(self.entry)["after"][self.entry]
        for target in self.targets:
            for row in bundle["before"].get(target, []):
                if row["kind"] == "container_health":
                    health = row["observations"][0]
                    if scenario == "oom":
                        health["oom_killed"] = True
                    if scenario == "restart-loop":
                        health["restarting"] = True
                    if scenario == "unknown":
                        health.update(running=True, status="running")
            bundle["after"].setdefault(target, []).append({"kind": "container_health", "observations": [{"running": True, "status": "running", "health": "healthy", "oom_killed": False, "restarting": False}]})
        super().__init__(bundle)

    def call(self, name, service, lookback, end):
        self.recovered = service in self.fixed or (service == self.entry and all(target in self.fixed for target in self.targets))
        if self.scenario in {"failed", "rollback"}:
            self.recovered = False
        return super().call(name, service, lookback, end)


class ScenarioBackend:
    mode = "synthetic"
    configured = True

    def __init__(self, tools):
        self.tools, self.dispatched = tools, []
        self.actions = []
        for service in tools.targets:
            self.actions.append({"id": "simulate_restart_" + service, "service": service, "parameters": {},
                "description": "Synthetic in-memory change; never changes Docker", "tier": 2 if tools.scenario == "tier2" or service in {"cart"} else 1,
                "signatures": ["container_stopped", "oom_killed", "restart_loop"],
                "rollback_action_id": "simulate_rollback_" + service if tools.scenario == "rollback" else None})
            if tools.scenario == "rollback":
                self.actions.append({"id": "simulate_rollback_" + service, "service": service, "parameters": {},
                    "description": "Synthetic rollback", "tier": 2, "signatures": []})

    def execute(self, action, incident, approval=None):
        if action not in self.actions or not approval:
            raise ValueError("Unapproved synthetic action")
        self.dispatched.append(action["id"])
        self.tools.fixed.add(action["service"])
        return {"accepted": True, "synthetic": True}


class UnknownDriver:
    """Tests the LLM protocol boundary, not model reasoning or accuracy."""
    label = "synthetic-fallback-protocol-driver-not-llm"
    def respond(self, instructions, conversation, tools, max_output_tokens, timeout):
        value = {"component": "unknown", "root_cause": "Synthetic anomalous telemetry has no supported cause.",
            "confidence": .2, "claims": [], "action_id": None, "escalate": True,
            "uncertainty": "Unknown incidents need further investigation; no safe automated remedy.", "prevention": "Collect additional traces and resource trends."}
        return {"output": [{"type": "function_call", "call_id": "unknown", "name": "finish_investigation", "arguments": json.dumps(value)}]}
