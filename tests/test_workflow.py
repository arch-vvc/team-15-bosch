import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from incident_zero.actions import DisabledBackend, HttpActionBackend, SyntheticBackend, execute_plan, healthy_sample
from incident_zero.agent import Budget, Investigator, validate_diagnosis
from incident_zero.alerts import detect
from incident_zero.evaluation import evaluate
from incident_zero.model import ModelError
from incident_zero.store import Store
from incident_zero.synthetic import SyntheticModel, example_bundle
from incident_zero.tools import BundleTools, LiveTools


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "test.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def proposed(self, recover=True):
        tools = BundleTools(example_bundle(recover=recover))
        backend = SyntheticBackend(tools, "payment")
        incident = Investigator(self.store, SyntheticModel("payment"), tools, actions=backend.actions).run({"service": "payment", "summary": "test"})
        plan = self.store.report(incident)["proposals"][0]
        return incident, plan, backend, tools

    def approve(self, plan):
        self.store.decide(plan["id"], plan["digest"], "synthetic test operator", True)

    def test_investigator_stops_before_mutation(self):
        incident, plan, backend, tools = self.proposed()
        self.assertEqual(self.store.incident(incident)["state"], "awaiting_approval")
        self.assertFalse(tools.recovered)
        self.assertEqual(plan["status"], "pending")

    def test_no_approval_no_dispatch(self):
        incident, plan, backend, tools = self.proposed()
        with self.assertRaises(ValueError):
            execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=0)
        self.assertFalse(tools.recovered)

    def test_rejected_plan_cannot_execute(self):
        incident, plan, backend, tools = self.proposed()
        self.store.decide(plan["id"], plan["digest"], "test operator", False)
        with self.assertRaises(ValueError):
            execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=0)
        self.assertEqual(self.store.incident(incident)["state"], "escalated")

    def test_successful_execution_is_single_use_and_persistent(self):
        incident, plan, backend, tools = self.proposed()
        self.approve(plan)
        result = execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=0)
        self.assertTrue(result["all_passed"])
        self.assertEqual(self.store.incident(incident)["state"], "resolved")
        with self.assertRaises(ValueError):
            execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=0)
        other = Store(self.root / "test.sqlite3")
        try:
            self.assertEqual(other.proposal(plan["id"])["status"], "consumed")
        finally:
            other.close()

    def test_failed_verification_escalates_without_unauthorized_rollback(self):
        incident, plan, backend, tools = self.proposed(recover=False)
        self.approve(plan)
        result = execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=0)
        self.assertFalse(result["all_passed"])
        self.assertIn("No rollback", result["rollback"])
        self.assertEqual(self.store.incident(incident)["state"], "escalated")

    def test_kill_blocks_dispatch_without_consuming_approval(self):
        incident, plan, backend, tools = self.proposed()
        self.approve(plan)
        self.store.kill(True)
        with self.assertRaises(ValueError):
            execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=0)
        self.assertFalse(tools.recovered)
        self.assertEqual(self.store.proposal(plan["id"])["status"], "approved")

    def test_expired_approval_and_wrong_digest_rejected(self):
        incident, plan, backend, tools = self.proposed()
        with self.assertRaises(ValueError):
            self.store.decide(plan["id"], "wrong", "test", True)
        with patch("incident_zero.store.time.time", return_value=plan["expires"] + 1), self.assertRaises(ValueError):
            self.store.decide(plan["id"], plan["digest"], "test", True)
        self.approve(plan)
        with patch("incident_zero.store.time.time", return_value=plan["expires"] + 1), self.assertRaises(ValueError):
            self.store.claim(plan["id"], plan["body"])

    def test_modified_proposal_rejected(self):
        incident, plan, backend, tools = self.proposed()
        self.approve(plan)
        body = copy.deepcopy(plan["body"])
        body["action"]["service"] = "checkout"
        with self.assertRaises(ValueError):
            self.store.claim(plan["id"], body)

    def test_dry_run_does_not_consume_approval_or_dispatch(self):
        incident, plan, backend, tools = self.proposed()
        self.approve(plan)
        execute_plan(self.store, plan["id"], backend, tools, dry_run=True)
        self.assertEqual(self.store.proposal(plan["id"])["status"], "approved")
        self.assertFalse(tools.recovered)

    def test_ambiguous_mutation_is_never_retried(self):
        incident, plan, backend, tools = self.proposed()
        self.approve(plan)
        backend.execute = Mock(side_effect=TimeoutError("uncertain dispatch"))
        result = execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=0)
        self.assertFalse(result["all_passed"])
        self.assertEqual(self.store.proposal(plan["id"])["status"], "consumed")
        backend.execute.assert_called_once()

    def test_disabled_live_backend_never_claims(self):
        incident, plan, backend, tools = self.proposed()
        self.approve(plan)
        with self.assertRaises(ValueError):
            execute_plan(self.store, plan["id"], DisabledBackend(), tools, dry_run=False, interval=0)
        self.assertEqual(self.store.proposal(plan["id"])["status"], "approved")

    def test_live_and_synthetic_cannot_mix(self):
        incident, plan, backend, tools = self.proposed()
        self.approve(plan)
        backend.mode = "live"
        with self.assertRaises(ValueError):
            execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=60)

    def test_model_failure_escalates_and_preserves_report(self):
        model = Mock(label="test", respond=Mock(side_effect=ModelError("rate limited")))
        incident = Investigator(self.store, model, BundleTools(example_bundle())).run({"service": "payment"})
        self.assertEqual(self.store.incident(incident)["state"], "escalated")
        self.assertIn("rate limited", json.dumps(self.store.report(incident)))

    def test_budget_exhaustion_escalates(self):
        incident = Investigator(self.store, SyntheticModel("payment"), BundleTools(example_bundle()), Budget(model_turns=1)).run({"service": "payment"})
        self.assertEqual(self.store.incident(incident)["state"], "escalated")
        self.assertGreater(len(self.store.evidence(incident)), 0)

    def test_invented_evidence_and_altered_values_rejected(self):
        incident, plan, backend, tools = self.proposed()
        diagnosis = {key: value for key, value in self.store.incident(incident)["diagnosis"].items() if key not in {"confidence_calibrated", "root_cause_is_hypothesis", "review"}}
        for changed in ("invented", "altered"):
            value = copy.deepcopy(diagnosis)
            if changed == "invented":
                value["claims"][0]["evidence_id"] = "fake"
            else:
                value["claims"][0]["value"] = True
            with self.assertRaises(ValueError):
                validate_diagnosis(value, self.store.evidence(incident), backend.actions)

    def test_action_not_in_runbook_rejected(self):
        incident, plan, backend, tools = self.proposed()
        diagnosis = {key: value for key, value in self.store.incident(incident)["diagnosis"].items() if key not in {"confidence_calibrated", "root_cause_is_hypothesis", "review"}}
        diagnosis["action_id"] = "arbitrary-shell"
        with self.assertRaises(ValueError):
            validate_diagnosis(diagnosis, self.store.evidence(incident), backend.actions)

    def test_missing_samples_and_zero_traffic_never_verify_recovery(self):
        self.assertFalse(healthy_sample([]))
        rows = BundleTools(example_bundle()).call("inspect_service", "payment", 300, 1000)
        rows[0]["observations"][0]["value"] = 0
        self.assertFalse(healthy_sample(rows))

    def test_alert_detection_uses_values_not_missing_as_zero(self):
        tools = BundleTools(example_bundle())
        self.assertIsNotNone(detect(tools.call("inspect_service", "payment", 300, 1000), "payment"))
        self.assertIsNone(detect([], "payment"))
        tools.recovered = True
        self.assertIsNone(detect(tools.call("inspect_service", "payment", 300, 1000), "payment"))

    def test_projected_health_does_not_expose_environment_or_logs(self):
        tools = LiveTools(container_health=True)
        state = {"state": {"Status": "exited", "Running": False, "OOMKilled": True, "Error": "secret", "Health": {"Status": "unhealthy", "Log": [{"Output": "secret"}]}}, "restart_count": 2}
        with patch("incident_zero.tools.subprocess.run", return_value=Mock(returncode=0, stdout=json.dumps(state))):
            rows = tools.call("read_container_health", "payment", 300, 1000)
        self.assertNotIn("secret", json.dumps(rows))
        self.assertTrue(rows[0]["observations"][0]["oom_killed"])

    def test_flag_tools_and_raw_shell_are_unavailable(self):
        tools = LiveTools(container_health=True)
        for name, service in (("read_container_health", "flagd"), ("shell", "payment"), ("read_logs", "flagd-ui")):
            with self.assertRaises(ValueError):
                tools.call(name, service, 300, 1000)

    def test_all_synthetic_workflow_cases(self):
        result = evaluate(self.store, self.root / "reports")
        self.assertTrue(all(row["passed"] for row in result["results"]))
        self.assertFalse(result["measures_model_accuracy"])

    def test_action_contract_requires_review_and_loopback(self):
        for contract in ({}, {"operator_confirmed": True, "reviewed_by": "test", "base_url": "http://example.com", "actions": []}):
            with self.assertRaises(ValueError):
                HttpActionBackend(contract)

    def test_memory_does_not_mix_live_and_synthetic_incidents(self):
        incident, plan, backend, tools = self.proposed()
        self.approve(plan)
        execute_plan(self.store, plan["id"], backend, tools, dry_run=False, interval=0)
        synthetic = self.store.create({"service": "payment"}, "synthetic")
        live = self.store.create({"service": "payment"}, "live")
        self.assertEqual(len(self.store.memories(synthetic, "payment", 3600)), 1)
        self.assertEqual(self.store.memories(live, "payment", 3600), [])

    def test_direct_resolution_without_verification_is_rejected(self):
        incident = self.store.create({"service": "payment"})
        with self.assertRaises(ValueError):
            self.store.transition(incident, "resolved", {})

    def test_concurrent_claims_dispatch_at_most_once(self):
        incident, plan, backend, tools = self.proposed()
        self.approve(plan)
        barrier = threading.Barrier(2)
        outcomes = []

        def claim():
            local = Store(self.root / "test.sqlite3")
            try:
                barrier.wait(timeout=3)
                try:
                    local.claim(plan["id"], plan["body"])
                    outcomes.append("claimed")
                except ValueError:
                    outcomes.append("blocked")
            finally:
                local.close()

        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertEqual(sorted(outcomes), ["blocked", "claimed"])

    def test_logs_are_bounded_filtered_and_sensitive_records_removed(self):
        tools = LiveTools(opensearch="http://localhost:9200")
        tools.logs.get = Mock(return_value={"hits": {"hits": [
            {"_id": "safe", "_source": {"body": "request failed", "severity": {"text": "ERROR"}}},
            {"_id": "skip", "_source": {"body": "feature_flag variant enabled"}},
            {"_id": "secret", "_source": {"body": "password=do-not-export"}},
        ]}})
        rows = tools.call("read_logs", "payment", 300, 1000)
        self.assertEqual(len(rows[0]["observations"]), 1)
        self.assertNotIn("do-not-export", json.dumps(rows))
        query = json.loads(tools.logs.get.call_args.args[1]["source"])
        self.assertEqual(query["size"], 20)
        self.assertEqual(query["query"]["bool"]["filter"][0]["term"]["resource.service.name.keyword"], "payment")


if __name__ == "__main__":
    unittest.main()
