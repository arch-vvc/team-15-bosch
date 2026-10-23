"""HTTP contract test using synthetic data, not a real demo incident."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from incident_zero.telemetry import LocalJsonClient, observe


class Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        self.requests.append((parsed.path, parse_qs(parsed.query)))
        if parsed.path == "/api/v1/query":
            payload = {"status": "success", "data": {"resultType": "vector", "result": [{"value": [1000, "2.5"]}]}}
        elif parsed.path == "/jaeger/ui/api/traces":
            payload = {"data": [{"traceID": "synthetic-trace", "processes": {"p1": {"serviceName": "checkout"}}, "spans": [
                {"processID": "p1", "spanID": "synthetic-span", "operationName": "checkout", "duration": 1500, "startTime": 999000000},
            ]}]}
        else:
            self.send_error(503)
            return
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class HttpContractTests(unittest.TestCase):
    def test_real_http_transport_and_backend_error(self):
        Handler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            report = observe("checkout", 300, LocalJsonClient(base), LocalJsonClient(base + "/jaeger/ui"), end=1000)
            self.assertEqual(len(Handler.requests), 4)
            self.assertEqual(Handler.requests[3][1]["start"], ["700000000"])
            self.assertEqual(Handler.requests[3][1]["service"], ["checkout"])
            self.assertTrue(all(item["status"] == "observed" for item in report["evidence"]))
            self.assertEqual(report["evidence"][0]["observations"][0]["value"], 2.5)
            self.assertEqual(report["evidence"][3]["observations"][0]["trace_id"], "synthetic-trace")
            failed = observe("checkout", 300, LocalJsonClient(base), LocalJsonClient(base + "/missing"), end=1000)
            self.assertEqual(failed["evidence"][3]["status"], "unavailable")
            self.assertIn("503", failed["evidence"][3]["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
