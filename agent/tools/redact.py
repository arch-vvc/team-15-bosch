"""Flag redaction. Every tool passes its raw backend data through here before building
evidence, and the ledger runs scrub() again as a last line of defence.

Rules (fault-signatures.md, section 2, patterns in config/redaction.yaml):
  1. drop span events named feature_flag (and any event or attribute key containing it),
  2. drop attributes whose key contains feature_flag,
  3. exclude the flagd and flagd-ui services and any call to them,
  4. mask flag wording and the 20 flag key names in all text,
  5. find_leaks() lets tests and the ledger prove nothing survived.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from agent import config

# Span tags that name the other side of a call. If one of them is the flag service,
# the span is a flag lookup and is dropped whole.
PEER_TAGS = ("peer.service", "server.address", "net.peer.name", "rpc.service", "url.full",
             "http.url", "url.path", "http.target", "db.system")


@lru_cache(maxsize=None)
def _rules() -> dict:
    cfg = config.redaction()
    keys = "|".join(re.escape(k) for k in cfg["flag_keys"])
    return {
        "excluded": set(cfg["excluded_services"]),
        "drop_keys": [k.lower() for k in cfg["drop_keys_containing"]],
        "system": re.compile(cfg["flag_system_pattern"]),
        "masks": [re.compile(p) for p in cfg["mask_patterns"]]
        + [re.compile(rf"(?<![A-Za-z])(?:{keys})(?![A-Za-z])", re.IGNORECASE)],
        "mask_with": cfg["mask_with"],
    }


def mask(text: str) -> str:
    r = _rules()
    for pattern in r["masks"]:
        text = pattern.sub(r["mask_with"], text)
    return text


def is_flag_system(name: str | None) -> bool:
    """True for the flag services themselves or anything naming them (an operation, a peer)."""
    if not name:
        return False
    r = _rules()
    # a name already masked to the marker (in a scrubbed recording) was a flag name too
    return name in r["excluded"] or bool(r["system"].search(name)) or r["mask_with"] in name


def is_flag_key(key: str) -> bool:
    """A key or event name that is flag data. Recordings are scrubbed when saved, so a
    key that was already masked to the marker counts too."""
    k = key.lower()
    return any(d in k for d in _rules()["drop_keys"]) or _rules()["mask_with"].lower() in k


def scrub(obj: Any) -> Any:
    """Recursively mask every string, and drop dict entries whose key is flag data."""
    if isinstance(obj, str):
        return mask(obj)
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(scrub(x) for x in obj)
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if not (isinstance(k, str) and is_flag_key(k))}
    return obj


def find_leaks(obj: Any) -> list[str]:
    """Every piece of flag text still present anywhere in obj. Empty means clean."""
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    r = _rules()
    leaks = [m.group(0) for p in r["masks"] for m in p.finditer(text)]
    leaks += [k for k in r["drop_keys"] if k in text.lower()]
    leaks += [s for s in r["excluded"] if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text)]
    return leaks


# --- Prometheus -------------------------------------------------------------------------

def clean_prom_rows(rows: list[dict]) -> list[dict]:
    """Drop series that belong to, or are calls to, the flag service."""
    return [row for row in rows
            if not any(is_flag_system(str(v)) for v in row.get("metric", {}).values())]


# --- Jaeger -----------------------------------------------------------------------------

def _tag_value(tags: list[dict], key: str) -> Any:
    for t in tags or []:
        if t.get("key") == key:
            return t.get("value")
    return None


def _clean_kv(items: list[dict]) -> list[dict]:
    out = []
    for kv in items or []:
        if is_flag_key(str(kv.get("key", ""))):
            continue
        value = kv.get("value")
        out.append({**kv, "value": mask(value) if isinstance(value, str) else value})
    return out


def _is_flag_event(log: dict) -> bool:
    for f in log.get("fields", []):
        if f.get("key") in ("event", "name") and isinstance(f.get("value"), str):
            if is_flag_key(f["value"]) or is_flag_system(f["value"]):
                return True
    return False


def clean_trace(trace: dict) -> dict:
    """A Jaeger trace with flag services, flag calls, flag events and flag attributes removed."""
    processes = {pid: p for pid, p in (trace.get("processes") or {}).items()
                 if not is_flag_system(p.get("serviceName"))}
    spans = []
    for span in trace.get("spans") or []:
        if span.get("processID") not in processes:
            continue
        if is_flag_system(span.get("operationName")):
            continue
        if any(is_flag_system(str(_tag_value(span.get("tags"), k) or "")) for k in PEER_TAGS):
            continue
        spans.append({
            **span,
            "operationName": mask(span.get("operationName", "")),
            "tags": _clean_kv(span.get("tags")),
            "logs": [{**log, "fields": _clean_kv(log.get("fields"))}
                     for log in span.get("logs") or [] if not _is_flag_event(log)],
        })
    kept = {s["spanID"] for s in spans}
    for s in spans:
        s["references"] = [r for r in s.get("references") or [] if r.get("spanID") in kept]
    clean_processes = {pid: {**p, "tags": _clean_kv(p.get("tags"))} for pid, p in processes.items()}
    return {**trace, "spans": spans, "processes": clean_processes}


def clean_edges(edges: list[dict]) -> list[dict]:
    return [e for e in edges
            if not is_flag_system(e.get("parent")) and not is_flag_system(e.get("child"))]


# --- logs and docker events -------------------------------------------------------------

def clean_log_lines(lines: list[str]) -> list[str]:
    return [mask(line) for line in lines]
