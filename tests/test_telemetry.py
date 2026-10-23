import json
import unittest
from unittest.mock import Mock

from incident_zero.telemetry import LocalJsonClient, MAX_RESPONSE_BYTES, NoRedirects, TelemetryError, metric_values, observe, trace_spans


class TelemetryTests(unittest.TestCase):
    def test_remote_and_credential_urls_rejected(self):
        for url in ("http://example.com", "https://localhost", "http://user:secret@localhost", "http://localhost/?query=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalJsonClient(url)

    def test_redirects_are_not_followed(self):
        with self.assertRaises(TelemetryError):
            NoRedirects().redirect_request(None, None, 302, "", {}, "http://example.com")

    def test_client_gets_json_with_bounded_response_and_timeout(self):
        client = LocalJsonClient("http://127.0.0.1:9090")
        response = Mock()
        response.read.return_value = b'{"status":"success"}'
        client.opener = Mock()
        client.opener.open.return_value.__enter__ = Mock(return_value=response)
        client.opener.open.return_value.__exit__ = Mock(return_value=False)
        self.assertEqual(client.get("/api/v1/query", {"query": "up"})["status"], "success")
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 8)
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)
        response.read.return_value = b"x" * (MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(TelemetryError):
            client.get("/api/v1/query")

    def test_missing_or_nan_metric_is_not_a_zero(self):
        for samples in ([], [{"value": [1, "NaN"]}]):
            self.assertEqual(metric_values({"status": "success", "data": {"resultType": "vector", "result": samples}}), [])

    def test_partial_backend_failure_preserves_other_evidence(self):
        prometheus = Mock(base_url="http://localhost:9090")
        prometheus.get.return_value = {"status": "success", "data": {"resultType": "vector", "result": [{"value": [1000, "2"]}]}}
        jaeger = Mock(base_url="http://localhost:8080/jaeger/ui")
        jaeger.get.side_effect = TelemetryError("timeout")
        report = observe("checkout", 300, prometheus, jaeger, end=1000)
        self.assertEqual([item["status"] for item in report["evidence"]], ["observed"] * 3 + ["unavailable"])
        self.assertFalse(report["official_incident_schema"])
        self.assertEqual(report["window_start_unix"], 700)
        self.assertEqual(len({item["evidence_id"] for item in report["evidence"]}), 4)

    def test_empty_backends_report_no_data(self):
        prometheus = Mock(base_url="http://localhost:9090")
        prometheus.get.return_value = {"status": "success", "data": {"resultType": "vector", "result": []}}
        jaeger = Mock(base_url="http://localhost:8080/jaeger/ui")
        jaeger.get.return_value = {"data": []}
        report = observe("checkout", 300, prometheus, jaeger, end=1000)
        self.assertTrue(all(item["status"] == "no_data" for item in report["evidence"]))

    def test_trace_projection_excludes_flags_and_other_services(self):
        payload = {"data": [{"traceID": "t1", "processes": {"p1": {"serviceName": "checkout"}, "p2": {"serviceName": "flagd"}}, "spans": [
            {"processID": "p1", "spanID": "s1", "operationName": "charge", "tags": [{"key": "error", "value": True}, {"key": "feature_flag.variant", "value": "secret-fault"}]},
            {"processID": "p2", "spanID": "s2", "operationName": "evaluate"},
        ]}]}
        spans = trace_spans(payload, "checkout")
        self.assertEqual(len(spans), 1)
        self.assertTrue(spans[0]["error"])
        self.assertNotIn("secret-fault", json.dumps(spans))

    def test_forbidden_service_or_unbounded_window_rejected_before_query(self):
        backend = Mock()
        for service, lookback in (("flagd", 300), ("checkout", 999999), ("checkout", 0)):
            with self.assertRaises(ValueError):
                observe(service, lookback, backend, backend)
        backend.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
