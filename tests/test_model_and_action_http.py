import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from incident_zero.actions import HttpActionBackend, SyntheticBackend
from incident_zero.agent import Investigator
from incident_zero.model import ResponsesModel
from incident_zero.store import Store
from incident_zero.synthetic import SyntheticModel, example_bundle
from incident_zero.tools import BundleTools


class ProtocolHandler(BaseHTTPRequestHandler):
    requests = []
    driver = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append({"path": self.path, "body": body, "authorization": self.headers.get("Authorization")})
        if self.path == "/responses":
            result = self.driver.respond(body["instructions"], body["input"], body["tools"], body["max_output_tokens"], 5)
        elif self.path == "/documented-action":
            result = {"accepted": True}
        else:
            self.send_error(404)
            return
        raw = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ModelAndActionHttpTests(unittest.TestCase):
    def setUp(self):
        ProtocolHandler.requests = []
        ProtocolHandler.driver = SyntheticModel("payment")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ProtocolHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_responses_function_call_roundtrip_without_leaking_cloud_key(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"OPENAI_API_KEY": "never-send-this-to-local-provider"}):
            store = Store(Path(folder) / "test.sqlite3")
            try:
                model = ResponsesModel("synthetic-protocol-test", self.base)
                tools = BundleTools(example_bundle())
                backend = SyntheticBackend(tools, "payment")
                incident = Investigator(store, model, tools, actions=backend.actions).run({"service": "payment", "summary": "synthetic protocol contract test"})
                self.assertEqual(store.incident(incident)["state"], "awaiting_approval")
                self.assertGreater(len(store.evidence(incident)), 0)
                self.assertTrue(all(row["authorization"] is None for row in ProtocolHandler.requests))
                first = ProtocolHandler.requests[0]["body"]
                self.assertFalse(first["store"])
                self.assertFalse(first["parallel_tool_calls"])
                self.assertTrue(all(tool["strict"] for tool in first["tools"]))
                self.assertTrue(any(item.get("type") == "function_call_output" for item in ProtocolHandler.requests[1]["body"]["input"]))
            finally:
                store.close()

    def contract(self):
        return {"operator_confirmed": True, "reviewed_by": "test fixture", "base_url": self.base, "actions": [{
            "id": "test_restart", "service": "payment", "description": "Synthetic API contract test", "path": "/documented-action",
            "body": {"service": "payment", "action": "restart"}, "accepted_field": "accepted", "accepted_value": True,
            "context_fields": {"incident_id": "incident", "proposal_id": "plan", "approval_digest": "digest", "approver": "approved_by"},
        }]}

    def test_action_dispatch_uses_fixed_contract_and_exact_approval_context(self):
        backend = HttpActionBackend(self.contract())
        result = backend.execute(backend.actions[0], "test-incident", {"proposal_id": "test-plan", "approval_digest": "test-digest", "approver": "test-fixture"})
        self.assertTrue(result["accepted"])
        sent = ProtocolHandler.requests[0]
        self.assertEqual(sent["path"], "/documented-action")
        self.assertEqual(sent["body"]["service"], "payment")
        self.assertEqual(sent["body"]["digest"], "test-digest")
        self.assertEqual(sent["body"]["approved_by"], "test-fixture")

    def test_action_contract_change_alters_approval_bound_fingerprint(self):
        contract = self.contract()
        first = HttpActionBackend(contract).actions[0]["contract_digest"]
        contract["actions"][0]["path"] = "/different-action"
        self.assertNotEqual(first, HttpActionBackend(contract).actions[0]["contract_digest"])

    def test_absent_approval_prevents_http_request(self):
        backend = HttpActionBackend(self.contract())
        with self.assertRaises(ValueError):
            backend.execute(backend.actions[0], "test-incident")
        self.assertEqual(ProtocolHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
