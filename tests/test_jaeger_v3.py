import unittest
from unittest.mock import Mock
from incident_zero.telemetry import observe, otlp_trace_spans, TelemetryHTTPError


def frame():
    return {'frames': [{'result': {'resourceSpans': [
        {'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'checkout'}}]},
         'scopeSpans': [{'spans': [{'traceId': 't', 'spanId': 'a', 'name': 'checkout', 'startTimeUnixNano': '1000', 'endTimeUnixNano': '9000', 'status': {'code': 2}, 'attributes': [{'key': 'feature_flag.key', 'value': {'stringValue': 'secret-flag'}}]}]}]},
        {'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'payment'}}]},
         'scopeSpans': [{'spans': [{'traceId': 't', 'spanId': 'b', 'parentSpanId': 'a'}]}]},
        {'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'flagd'}}]},
         'scopeSpans': [{'spans': [{'traceId': 't', 'spanId': 'c', 'parentSpanId': 'a'}]}]}
    ]}}]}


class JaegerV3Tests(unittest.TestCase):
    def test_projection_preserves_dependencies_and_excludes_flags(self):
        rows = otlp_trace_spans(frame(), 'checkout')
        self.assertEqual(rows[0]['child_services'], ['payment'])
        self.assertEqual(rows[0]['duration_us'], 8)
        self.assertTrue(rows[0]['error'])
        self.assertNotIn('secret-flag', str(rows))
        self.assertNotIn('flagd', str(rows))

    def test_404_falls_back_to_v3_with_correct_provenance(self):
        prom = Mock(base_url='http://localhost:9090')
        prom.get.return_value = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
        jaeger = Mock(base_url='http://localhost:8080/jaeger/ui')
        jaeger.get.side_effect = [TelemetryHTTPError(404), frame()]
        result = observe('checkout', 300, prom, jaeger, end=1000)
        traces = result['evidence'][-1]
        self.assertEqual(traces['status'], 'observed')
        self.assertTrue(traces['source_url'].endswith('/api/v3/traces'))
        self.assertEqual(traces['query']['query.serviceName'], 'checkout')
        self.assertEqual(jaeger.get.call_count, 2)

    def test_other_http_errors_do_not_trigger_fallback(self):
        prom = Mock(base_url='http://localhost:9090')
        prom.get.return_value = {'status': 'success', 'data': {'resultType': 'vector', 'result': []}}
        jaeger = Mock(base_url='http://localhost:8080/jaeger/ui')
        jaeger.get.side_effect = TelemetryHTTPError(503)
        self.assertEqual(observe('checkout', 300, prom, jaeger, end=1000)['evidence'][-1]['status'], 'unavailable')
        self.assertEqual(jaeger.get.call_count, 1)
