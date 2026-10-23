"""Telemetry signatures, dependency localization and verified-memory fingerprints."""
import hashlib
import json
from incident_zero.telemetry import SERVICES
from incident_zero.tools import HEALTH_SERVICES


def claim(row, index, field):
    return {"evidence_id": row["evidence_id"], "observation_index": index,
            "field": field, "value": row["observations"][index][field]}


def dependencies(rows):
    edges = set()
    for row in rows:
        if row["status"] != "observed" or row["kind"] != "recent_traces":
            continue
        for span in row["observations"]:
            for peer in [span.get("peer_service"), *span.get("child_services", [])]:
                if peer in HEALTH_SERVICES and peer != row["service"]:
                    edges.add((row["service"], peer))
    return sorted(edges)


def current_rows(rows):
    latest = {}
    for row in rows:
        key = (row["service"], row["kind"])
        if row.get("window_end_unix", 0) >= latest.get(key, {}).get("window_end_unix", 0):
            latest[key] = row
    return list(latest.values())


def signatures(rows):
    """Only direct health failures get confident rules; symptoms stay hypotheses."""
    found = []
    for row in current_rows(rows):
        if row["kind"] != "container_health" or row["status"] != "observed":
            continue
        for index, health in enumerate(row["observations"]):
            running = health.get("running")
            if health.get("oom_killed") is True and running is False:
                code, confidence, fields = "oom_killed", .92, ["oom_killed", "running"]
                text = "Container was killed for memory exhaustion and is currently stopped."
            elif health.get("restarting") is True:
                code, confidence, fields = "restart_loop", .90, ["restarting"]
                text = "Container is currently restarting; the reason for repeated exits needs investigation."
            elif running is False and health.get("status") in {"exited", "dead"}:
                code, confidence, fields = "container_stopped", .90, ["running", "status"]
                text = "Container is stopped; the underlying reason for its exit is not established."
            else:
                continue
            found.append({"component": row["service"], "signature": code, "root_cause": text,
                          "confidence": confidence, "claims": [claim(row, index, field) for field in fields],
                          "action_id": None, "escalate": False,
                          "uncertainty": "Rule confidence is a starting value, not calibrated against organizer incidents.",
                          "prevention": "Investigate exit history and resource trends before changing capacity or restart policy."})
    # Multiple samples cannot manufacture multiple independent causes.
    best = {}
    for candidate in found:
        if candidate["confidence"] > best.get(candidate["component"], {}).get("confidence", 0):
            best[candidate["component"]] = candidate
    return list(best.values())


def localize(candidates, edges):
    """Deepest failing components first; cycles are bounded and kept visible."""
    graph = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
    def depth(node, seen):
        if node in seen:
            return 0
        return 1 + max((depth(child, seen | {node}) for child in graph.get(node, ())), default=0)
    return sorted(candidates, key=lambda item: (depth(item["component"], set()), -item["confidence"], item["component"]))


def fingerprint(rows):
    features = []
    for row in rows:
        if row["status"] != "observed":
            continue
        kind = row["kind"]
        for value in row["observations"]:
            if kind == "container_health":
                features.append((row["service"], kind, value.get("running"), value.get("oom_killed"), value.get("restarting")))
            elif kind in {"server_errors_per_second", "server_p95_milliseconds"}:
                number = value.get("value")
                if type(number) in (int, float):
                    features.append((row["service"], kind, number > (2000 if "p95" in kind else 0)))
    return hashlib.sha256(json.dumps(sorted(features, key=str)).encode()).hexdigest()


def checked(candidate, evidence):
    from incident_zero.agent import validate_diagnosis
    base = {key: value for key, value in candidate.items() if key != "signature"}
    return {**validate_diagnosis(base, evidence, []), "signature": candidate.get("signature", "unknown")}


def configured_signatures(rows, policy):
    """Trusted declarative rules. Never evaluate code or read incident identifiers."""
    rows = current_rows(rows)
    found = []
    for rule in policy.get("signatures", []):
        conditions = rule.get("conditions", [])
        if not 2 <= len(conditions) <= 8 or len({c["kind"] for c in conditions}) < 2:
            raise ValueError("Configured rules require 2-8 conditions from at least two evidence kinds")
        if type(rule.get("confidence")) not in (float, int) or not 0 <= rule["confidence"] <= .95:
            raise ValueError("Rule confidence must be finite and at most .95")
        for service in sorted({r["service"] for r in rows}):
            claims = []
            for condition in conditions:
                matched = None
                if condition["operator"] not in {"gte", "eq", "contains"}:
                    raise ValueError("Unsupported rule operator")
                for row in rows:
                    if row["service"] != service or row["kind"] != condition["kind"] or row["status"] != "observed":
                        continue
                    for index, observation in enumerate(row["observations"]):
                        field, expected = condition["field"], condition["value"]
                        actual = observation.get(field)
                        op = condition["operator"]
                        valid = ((op == "gte" and type(actual) in (int, float) and type(expected) in (int, float) and actual >= expected)
                            or (op == "eq" and type(actual) is type(expected) and actual == expected)
                            or (op == "contains" and isinstance(actual, str) and isinstance(expected, str) and expected.lower() in actual.lower()))
                        if valid:
                            matched = claim(row, index, field)
                if matched is None:
                    break
                claims.append(matched)
            if len(claims) == len(conditions):
                found.append({"component": service, "signature": rule["id"], "root_cause": rule["summary"],
                    "confidence": rule["confidence"], "claims": claims, "action_id": None, "escalate": False,
                    "uncertainty": "Correlated evidence supports this hypothesis; confidence requires calibration.",
                    "prevention": "Review historical trends and validate the causal hypothesis before permanent changes."})
    return found


def rank_candidates(candidates, policy):
    selected, ambiguous = [], []
    for component in sorted({item["component"] for item in candidates}):
        group = sorted([c for c in candidates if c["component"] == component], key=lambda item: -item["confidence"])
        if group[0]["confidence"] < policy.get("minimum_confidence", .7) or (len(group) > 1 and group[0]["confidence"] - group[1]["confidence"] < policy.get("minimum_margin", .1)):
            ambiguous.append(component)
        else:
            selected.append(group[0])
    return selected, ambiguous
