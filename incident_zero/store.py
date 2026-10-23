"""Durable incident state and single-use, exact-proposal approvals."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from agent.act.killswitch import KillSwitch
from agent.tools import redact


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY, mode TEXT NOT NULL, alert TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, diagnosis TEXT);
        CREATE TABLE IF NOT EXISTS evidence(id TEXT NOT NULL, incident TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(incident,id));
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, incident TEXT, created REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, incident TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, approver TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT, 'Evidence ledger is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT, 'Evidence ledger is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'Action log is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'Action log is append-only'); END;
        """)

    def close(self):
        self.db.close()

    def event(self, incident, kind, detail):
        self.db.execute("INSERT INTO events(incident,created,kind,detail) VALUES(?,?,?,?)", (incident, time.time(), kind, canonical(detail)))

    def create(self, alert, mode="live"):
        if mode not in {"live", "synthetic"}:
            raise ValueError("Invalid incident mode")
        incident = "inc_" + uuid.uuid4().hex[:16]
        with self.db:
            self.db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,NULL)", (incident, mode, canonical(alert), "investigating", time.time()))
            self.event(incident, "detected", alert)
        return incident

    def incident(self, incident):
        row = self.db.execute("SELECT * FROM incidents WHERE id=?", (incident,)).fetchone()
        if row is None:
            raise ValueError("Unknown incident")
        return {**dict(row), "alert": json.loads(row["alert"]), "diagnosis": json.loads(row["diagnosis"]) if row["diagnosis"] else None}

    def add_evidence(self, incident, record):
        self.incident(incident)
        # last line of defence (agent/tools/redact.py): mask flag text, refuse anything left
        record = redact.scrub(dict(record))
        if leaks := redact.find_leaks(record):
            raise ValueError(f"Flag data in evidence refused: {sorted(set(leaks))}")
        record["evidence_id"] = "ev_" + hashlib.sha256(canonical(record).encode()).hexdigest()[:20]
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?)", (record["evidence_id"], incident, canonical(record)))
        return record

    def evidence(self, incident):
        return [json.loads(row[0]) for row in self.db.execute("SELECT record FROM evidence WHERE incident=? ORDER BY rowid", (incident,))]

    def transition(self, incident, state, detail):
        current = self.incident(incident)["state"]
        allowed = {"investigating": {"diagnosed", "escalated"}, "awaiting_approval": {"escalated"},
                   "executing": {"verifying", "escalated"}, "verifying": {"resolved", "escalated", "diagnosed"},
                   "diagnosed": {"escalated"}, "resolved": set(), "escalated": set()}
        if state not in allowed.get(current, set()):
            raise ValueError(f"Invalid incident transition: {current} to {state}")
        with self.db:
            self.db.execute("UPDATE incidents SET state=? WHERE id=?", (state, incident))
            self.event(incident, state, detail)

    def diagnose(self, incident, diagnosis):
        if self.incident(incident)["state"] != "investigating":
            raise ValueError("Only an active investigation may record a diagnosis")
        with self.db:
            self.db.execute("UPDATE incidents SET diagnosis=? WHERE id=?", (canonical(diagnosis), incident))
            self.event(incident, "diagnosed", diagnosis)

    def propose(self, incident, body, ttl=300):
        if not 1 <= ttl <= 3600:
            raise ValueError("Approval TTL must be 1–3600 seconds")
        if self.incident(incident)["state"] not in {"investigating", "diagnosed"}:
            raise ValueError("Incident is not eligible for a proposal")
        proposal = "plan_" + uuid.uuid4().hex[:16]
        digest = hashlib.sha256(canonical(body).encode()).hexdigest()
        with self.db:
            self.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,'pending',NULL)", (proposal, incident, canonical(body), digest, time.time() + ttl))
            self.db.execute("UPDATE incidents SET state='awaiting_approval' WHERE id=?", (incident,))
            self.event(incident, "proposed", {"proposal_id": proposal, "digest": digest, "body": body})
        return proposal

    def proposal(self, proposal):
        row = self.db.execute("SELECT * FROM proposals WHERE id=?", (proposal,)).fetchone()
        if row is None:
            raise ValueError("Unknown proposal")
        return {**dict(row), "body": json.loads(row["body"])}

    def decide(self, proposal, digest, approver, approve, confirm_service=None):
        if not approver.strip():
            raise ValueError("A human approver name is required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            plan = self.proposal(proposal)
            current_digest = hashlib.sha256(canonical(plan["body"]).encode()).hexdigest()
            if plan["digest"] != digest or current_digest != digest or plan["expires"] < time.time() or plan["status"] != "pending":
                raise ValueError("Proposal changed, expired, or already decided")
            tier = plan["body"].get("tier")
            if approve and tier is not None:
                if tier not in (1, 2) or plan["body"].get("diagnosis", {}).get("confidence", 0) < (.85 if tier == 2 else .7):
                    raise ValueError("Action is below tier confidence or outside allowed tiers")
            if approve:
                # every tier, not only tier 2: the dry run is what the human approves
                dry = self.db.execute("SELECT detail FROM events WHERE incident=? AND kind='dry_run'", (plan["incident"],))
                if not any(json.loads(row[0]).get("proposal_id") == proposal for row in dry):
                    raise ValueError("A dry run of this exact proposal is required before approval")
            if approve and tier == 2 and confirm_service != plan["body"]["action"]["service"]:
                raise ValueError("Tier 2 requires typing the exact service name")
            status = "approved" if approve else "rejected"
            self.db.execute("UPDATE proposals SET status=?,approver=? WHERE id=?", (status, approver, proposal))
            if not approve:
                self.db.execute("UPDATE incidents SET state='escalated' WHERE id=?", (plan["incident"],))
            self.event(plan["incident"], status, {"proposal_id": proposal, "approver": approver})

    def killed(self):
        """The stored switch (`kill on`), or the shared KILL file / INCIDENT_ZERO_KILL variable
        that agent/act/killswitch.py checks, so one switch stops both entry points."""
        row = self.db.execute("SELECT value FROM settings WHERE key='kill'", ()).fetchone()
        if row is not None and row[0] == "on":
            return True
        try:
            KillSwitch().check()
        except RuntimeError:
            return True
        except OSError:
            return True      # cannot tell: fail closed
        return False

    def kill(self, enabled):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES('kill',?)", ("on" if enabled else "off",))
            self.event(None, "kill_switch", {"enabled": enabled})

    def claim(self, proposal, body):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            plan = self.proposal(proposal)
            digest = hashlib.sha256(canonical(body).encode()).hexdigest()
            if self.killed():
                raise ValueError("Kill switch is active")
            if plan["status"] != "approved" or plan["expires"] < time.time() or plan["digest"] != digest:
                raise ValueError("Action lacks a current, unused approval for this exact proposal")
            if self.incident(plan["incident"])["state"] != "awaiting_approval":
                raise ValueError("Incident cannot execute in its current state")
            consumed = [self.proposal(row[0]) for row in self.db.execute("SELECT id FROM proposals WHERE incident=? AND status='consumed'", (plan["incident"],))]
            if len(consumed) >= 2:
                raise ValueError("At most two mutations per incident, including rollback")
            if any(p["body"]["action"]["id"] == body["action"]["id"] for p in consumed):
                raise ValueError("Repeated actions require human escalation")
            if not body.get("rollback") and any(p["body"]["action"]["service"] == body["action"]["service"]
                                                and not p["body"].get("rollback") for p in consumed):
                raise ValueError("A second corrective action on the same service requires human escalation")
            if body.get("rollback") and any(p["body"].get("rollback") for p in consumed):
                raise ValueError("Only one rollback per incident")
            self.db.execute("UPDATE proposals SET status='consumed' WHERE id=?", (proposal,))
            self.db.execute("UPDATE incidents SET state='executing' WHERE id=?", (plan["incident"],))
            self.event(plan["incident"], "executing", {"proposal_id": proposal})
        return plan

    def report(self, incident):
        item = self.incident(incident)
        plans = [self.proposal(row[0]) for row in self.db.execute("SELECT id FROM proposals WHERE incident=?", (incident,))]
        timeline = [{**dict(row), "detail": json.loads(row["detail"])} for row in self.db.execute("SELECT seq,created,kind,detail FROM events WHERE incident=? ORDER BY seq", (incident,))]
        return {"schema_version": "incident-zero-development-v1", "official_incident_schema": False, **item, "evidence": self.evidence(incident), "proposals": plans, "timeline": timeline}

    def memories(self, current, service, lookback):
        mode = self.incident(current)["mode"]
        rows = self.db.execute("SELECT * FROM incidents WHERE id != ? AND mode=? AND created>=? AND state='resolved' ORDER BY created DESC LIMIT 50", (current, mode, time.time() - lookback))
        found = []
        for row in rows:
            alert = json.loads(row["alert"])
            diagnosis = json.loads(row["diagnosis"]) if row["diagnosis"] else {}
            if alert.get("service") == service or diagnosis.get("component") == service:
                found.append({"incident": row["id"], "state": row["state"], "created": row["created"], "diagnosis": diagnosis,
                              "warning": "Historical hypothesis, not current evidence. Recheck against fresh telemetry."})
            if len(found) == 3:
                break
        return found

    def verified_memories(self, current, fingerprint):
        mode = self.incident(current)["mode"]
        rows = self.db.execute("SELECT id,diagnosis FROM incidents WHERE id!=? AND mode=? AND state='resolved' ORDER BY created DESC LIMIT 100", (current, mode))
        matches = []
        for row in rows:
            diagnosis = json.loads(row["diagnosis"] or "{}")
            if diagnosis.get("fingerprint") == fingerprint:
                matches.append({"incident": row["id"], "component": diagnosis.get("component"), "hint_only": True})
            if len(matches) == 3:
                break
        return matches
