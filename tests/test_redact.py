"""Redaction is a disqualification risk, so these tests feed in every kind of flag leak the
demo produces (fault-signatures.md, section 2) and assert that none survives."""
import json

from agent.tools import redact
from tests import fakes


def test_mask_flag_wording_and_keys():
    text = ("Error: Product Catalog Fail Feature Flag Enabled; Feature Flag adManualGc enabled; "
            "due to intlShippingSlowdown feature flag; flagd provider; OpenFeature hook; feature_flag_key=X")
    masked = redact.mask(text)
    assert redact.find_leaks(masked) == []
    assert "Product Catalog Fail" in masked          # the useful part stays


def test_flag_key_mask_respects_word_boundaries():
    assert redact.mask("loadFailure happened") == "loadFailure happened"
    assert redact.mask("PAYMENTFAILURE on") == "[redacted] on"


def test_clean_trace_drops_flag_events_attributes_and_spans():
    trace = redact.clean_trace(fakes.payment_trace(1, fakes.FAULT_AT))
    assert redact.find_leaks(trace) == []
    ops = [s["operationName"] for s in trace["spans"]]
    assert not any("flagd" in op for op in ops)                     # the flag lookup span is gone
    assert "flagd" not in json.dumps(trace["processes"])            # and the flagd process
    payment = next(s for s in trace["spans"] if s["spanID"] == "c2")
    assert {t["key"] for t in payment["tags"]} >= {"demo.user_context.loyalty_level", "error"}
    assert [f["value"] for log in payment["logs"] for f in log["fields"] if f["key"] == "event"] == ["exception"]


def test_clean_trace_keeps_references_consistent():
    trace = redact.clean_trace(fakes.payment_trace(1, fakes.FAULT_AT))
    ids = {s["spanID"] for s in trace["spans"]}
    assert all(r["spanID"] in ids for s in trace["spans"] for r in s["references"])


def test_status_text_is_masked_not_dropped():
    trace = redact.clean_trace(fakes.catalog_trace(1, fakes.FAULT_AT))
    tags = {t["key"]: t["value"] for t in trace["spans"][0]["tags"]}
    assert tags["otel.status_description"] == "Error: Product Catalog Fail [redacted] Enabled"


def test_prom_rows_and_edges_drop_flag_services():
    rows = [{"metric": {"service_name": "flagd"}}, {"metric": {"span_name": "flagd.evaluation.v1.Service/ResolveFloat"}},
            {"metric": {"service_name": "flagd-ui"}}, {"metric": {"service_name": "payment"}}]
    assert redact.clean_prom_rows(rows) == [{"metric": {"service_name": "payment"}}]
    edges = [{"parent": "checkout", "child": "flagd"}, {"parent": "flagd-ui", "child": "flagd"},
             {"parent": "frontend", "child": "checkout"}]
    assert redact.clean_edges(edges) == [{"parent": "frontend", "child": "checkout"}]


def test_scrub_drops_flag_keys_and_masks_values():
    out = redact.scrub({"feature_flag.key": "x", "msg": ["paymentFailure on"], "n": 3})
    assert out == {"msg": ["[redacted] on"], "n": 3}


def test_already_masked_recordings_still_drop_events():
    """Recordings are scrubbed when saved, which turns the event name into the marker."""
    trace = redact.scrub(fakes.payment_trace(1, fakes.FAULT_AT))
    cleaned = redact.clean_trace(trace)
    payment = next(s for s in cleaned["spans"] if s["spanID"] == "c2")
    assert len(payment["logs"]) == 1
