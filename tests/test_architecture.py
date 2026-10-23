import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from incident_zero.actions import execute_plan
from incident_zero.demo import ScenarioTools, ScenarioBackend, UnknownDriver
from incident_zero.pipeline import Pipeline
from incident_zero.rules import rank_candidates
from incident_zero.store import Store


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "incidents.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_case(self, scenario="recovered", factory=None):
        tools = ScenarioTools(scenario)
        backend = ScenarioBackend(tools)
        incident = Pipeline(self.store, tools, backend.actions, factory).run({"service": tools.entry})
        return incident, tools, backend

    def plan(self, incident):
        return [p for p in self.store.report(incident)["proposals"] if p["status"] == "pending"][0]

    def execute(self, incident, tools, backend):
        plan = self.plan(incident)
        execute_plan(self.store, plan["id"], backend, tools)
        self.store.decide(plan["id"], plan["digest"], "test", True, confirm_service=plan["body"]["action"]["service"])
        return execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=0)

    def test_confident_rule_never_constructs_model(self):
        factory = Mock(side_effect=AssertionError("LLM must not run"))
        incident, tools, backend = self.run_case(factory=factory)
        factory.assert_not_called()
        self.assertEqual(self.store.incident(incident)["state"], "awaiting_approval")
        self.assertEqual(backend.dispatched, [])

    def test_unknown_enters_fallback_and_escalates_without_action(self):
        factory = Mock(return_value=UnknownDriver())
        incident, _, backend = self.run_case("unknown", factory)
        factory.assert_called_once()
        self.assertEqual(self.store.incident(incident)["state"], "escalated")
        self.assertEqual(backend.dispatched, [])
        self.assertIn("llm_fallback", [e["kind"] for e in self.store.report(incident)["timeline"]])

    def test_missing_model_preserves_unknown_evidence(self):
        incident, _, _ = self.run_case("unknown")
        self.assertEqual(self.store.incident(incident)["state"], "escalated")
        self.assertTrue(self.store.evidence(incident))

    def test_two_causes_require_two_separate_approvals(self):
        incident, tools, backend = self.run_case("combined")
        first = self.execute(incident, tools, backend)
        self.assertTrue(first["requires_new_approval"])
        self.assertEqual(len(backend.dispatched), 1)
        self.assertEqual(self.store.incident(incident)["state"], "awaiting_approval")
        second = self.plan(incident)
        with self.assertRaises(ValueError):
            execute_plan(self.store, second["id"], backend, tools, dry_run=False, interval=0)
        self.execute(incident, tools, backend)
        self.assertEqual(self.store.incident(incident)["state"], "resolved")
        self.assertEqual(len(backend.dispatched), 2)

    def test_second_cause_is_rechecked_before_proposal(self):
        incident, tools, backend = self.run_case("combined")
        plan = self.plan(incident)
        second = plan["body"]["remaining_causes"][0]["component"]
        # It becomes unavailable between triage and verification; never execute the stale diagnosis.
        tools.bundle["before"][second] = []
        self.execute(incident, tools, backend)
        self.assertEqual(self.store.incident(incident)["state"], "escalated")
        self.assertEqual(len(backend.dispatched), 1)

    def test_three_causes_escalate_before_mutation(self):
        incident, _, backend = self.run_case("three-causes")
        self.assertEqual(self.store.incident(incident)["state"], "escalated")
        self.assertEqual(backend.dispatched, [])

    def test_tier2_needs_dry_run_and_typed_service(self):
        incident, tools, backend = self.run_case("tier2")
        plan = self.plan(incident)
        with self.assertRaises(ValueError):
            self.store.decide(plan["id"], plan["digest"], "test", True, confirm_service="payment")
        execute_plan(self.store, plan["id"], backend, tools)
        with self.assertRaises(ValueError):
            self.store.decide(plan["id"], plan["digest"], "test", True, confirm_service="cart")
        self.store.decide(plan["id"], plan["digest"], "test", True, confirm_service="payment")
        self.assertEqual(self.store.proposal(plan["id"])["status"], "approved")

    def test_rollback_stops_for_separate_strict_approval(self):
        incident, tools, backend = self.run_case("rollback")
        result = self.execute(incident, tools, backend)
        self.assertTrue(result["requires_new_approval"])
        rollback = self.plan(incident)
        self.assertTrue(rollback["body"]["rollback"])
        self.assertEqual(rollback["body"]["tier"], 2)
        self.assertEqual(len(backend.dispatched), 1)
        self.execute(incident, tools, backend)
        self.assertEqual(len(backend.dispatched), 2)
        self.assertEqual(self.store.incident(incident)["state"], "escalated")

    def test_append_only_evidence_and_events(self):
        incident, _, _ = self.run_case()
        for table in ("events", "evidence"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.db:
                    self.store.db.execute("DELETE FROM " + table)

    def test_memory_only_after_verified_resolution(self):
        incident, tools, backend = self.run_case()
        fingerprint = self.store.incident(incident)["diagnosis"]["fingerprint"]
        current = self.store.create({"service": "payment"}, "synthetic")
        self.assertEqual(self.store.verified_memories(current, fingerprint), [])
        self.execute(incident, tools, backend)
        self.assertEqual(len(self.store.verified_memories(current, fingerprint)), 1)
        live = self.store.create({"service": "payment"}, "live")
        self.assertEqual(self.store.verified_memories(live, fingerprint), [])

    def test_tied_rules_require_fallback(self):
        selected, ambiguous = rank_candidates([{"component": "payment", "confidence": .8}, {"component": "payment", "confidence": .8}], {})
        self.assertEqual(selected, [])
        self.assertEqual(ambiguous, ["payment"])

    def test_kill_prevents_all_triage_tools(self):
        tools = ScenarioTools("recovered")
        tools.call = Mock(side_effect=AssertionError("Must not call"))
        self.store.kill(True)
        incident = Pipeline(self.store, tools).run({"service": "payment"})
        tools.call.assert_not_called()
        self.assertEqual(self.store.incident(incident)["state"], "escalated")

    def test_model_cannot_choose_mutation_or_invent_evidence(self):
        class BadModel:
            label = "synthetic-malicious-output"
            def respond(self, *args):
                return {"output": [{"type": "function_call", "name": "finish_investigation", "call_id": "bad", "arguments": json.dumps({
                    "component": "payment", "root_cause": "Invented", "confidence": 1, "claims": [{"evidence_id": "fake", "observation_index": 0, "field": "running", "value": False}],
                    "action_id": "simulate_restart_payment", "escalate": False, "uncertainty": "", "prevention": ""})}]}
        incident, _, backend = self.run_case("unknown", BadModel)
        self.assertEqual(self.store.incident(incident)["state"], "escalated")
        self.assertEqual(backend.dispatched, [])
        turns = [event for event in self.store.report(incident)["timeline"] if event["kind"] == "model_turn"]
        self.assertEqual(len(turns), 8)

    def test_live_rule_without_documented_remedy_escalates(self):
        tools = ScenarioTools("recovered")
        incident = Pipeline(self.store, tools, actions=[]).run({"service": "payment"})
        self.assertEqual(self.store.incident(incident)["state"], "escalated")
        self.assertEqual(self.store.report(incident)["proposals"], [])

    def test_repeated_action_claim_is_blocked(self):
        incident, tools, backend = self.run_case("combined")
        first = copy.deepcopy(self.plan(incident))
        self.execute(incident, tools, backend)
        # Verify the durable guard independently from the normal coordinator.
        second = self.plan(incident)
        body = {**second["body"], "action": first["body"]["action"]}
        import hashlib
        from incident_zero.store import canonical
        digest = hashlib.sha256(canonical(body).encode()).hexdigest()
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET body=?, digest=?, status='approved' WHERE id=?", (canonical(body), digest, second["id"]))
        with self.assertRaisesRegex(ValueError, "Repeated"):
            self.store.claim(second["id"], body)


    def test_rule_generalizes_to_other_services_without_model(self):
        for service in ("email", "currency", "shipping"):
            tools = ScenarioTools("recovered", service)
            backend = ScenarioBackend(tools)
            incident = Pipeline(self.store, tools, backend.actions).run({"service": service})
            self.assertEqual(self.store.incident(incident)["diagnosis"]["component"], service)
            self.execute(incident, tools, backend)
            self.assertEqual(self.store.incident(incident)["state"], "resolved")

    def test_conflicting_resource_signatures_call_fallback(self):
        tools = ScenarioTools("unknown")
        tools.bundle["before"]["payment"].append({"kind": "container_resources", "observations": [{"cpu_percent": 98, "memory_percent": 98}]})
        factory = Mock(return_value=UnknownDriver())
        incident = Pipeline(self.store, tools, [], factory).run({"service": "payment"})
        factory.assert_called_once()
        self.assertEqual(self.store.incident(incident)["state"], "escalated")

    def test_resource_rule_requires_independent_latency_evidence(self):
        tools = ScenarioTools("unknown")
        tools.bundle["before"]["payment"].append({"kind": "container_resources", "observations": [{"cpu_percent": 98, "memory_percent": 20}]})
        backend = ScenarioBackend(tools)
        backend.actions[0]["signatures"] = ["cpu_saturation"]
        incident = Pipeline(self.store, tools, backend.actions).run({"service": "payment"})
        diagnosis = self.store.incident(incident)["diagnosis"]
        self.assertEqual(diagnosis["signature"], "cpu_saturation")
        self.assertEqual(len(diagnosis["claims"]), 2)

    def test_llm_fallback_collects_new_evidence_but_code_selects_action(self):
        tools = ScenarioTools("recovered")
        original = tools.call
        health_calls = []
        def delayed_health(name, *args):
            if name == "read_container_health":
                health_calls.append(name)
                if len(health_calls) == 1:
                    return []
            return original(name, *args)
        tools.call = delayed_health
        class DiscoveryModel:
            label = "synthetic-protocol-test"
            turn = 0
            def respond(self, instructions, conversation, definitions, *args):
                self.turn += 1
                finish = next(t for t in definitions if t["name"] == "finish_investigation")
                assert finish["parameters"]["properties"]["action_id"] == {"type": "null"}
                if self.turn == 1:
                    name, arguments = "read_container_health", {"service": "payment", "lookback": 300}
                else:
                    rows = json.loads(conversation[-1]["output"])["evidence"]
                    arguments = {"component": "payment", "root_cause": "Container is stopped.", "confidence": .9,
                        "claims": [{"evidence_id": rows[0]["evidence_id"], "observation_index": 0, "field": "running", "value": False}],
                        "action_id": None, "escalate": False, "uncertainty": "Exit reason unproven", "prevention": "Review exits"}
                    name = "finish_investigation"
                return {"output": [{"type": "function_call", "name": name, "call_id": str(self.turn), "arguments": json.dumps(arguments)}]}
        backend = ScenarioBackend(tools)
        incident = Pipeline(self.store, tools, backend.actions, DiscoveryModel).run({"service": "payment"})
        self.assertEqual(self.store.incident(incident)["diagnosis"]["source"], "llm")
        self.assertEqual(self.store.incident(incident)["state"], "awaiting_approval")
        self.assertEqual(self.plan(incident)["body"]["action"]["id"], "simulate_restart_payment")
        self.assertEqual(backend.dispatched, [])
