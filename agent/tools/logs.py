"""Log tool (query-cookbook.md tool 6): docker logs always, OpenSearch when reachable.

Many demo services log only through OTLP, so OpenSearch catches what docker logs miss.
Each source becomes its own Evidence. Lines are masked before they are counted or quoted.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone

from agent import config
from agent.contracts import Evidence, Fact
from agent.tools import redact
from agent.tools.backends import Backends, BackendError

LEVEL = re.compile(r"\b(fatal|panic|critical|error|err|exception|warn|warning)\b", re.IGNORECASE)
TS_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+")
VOLATILE = re.compile(r"\b[0-9a-f]{8,}\b|\b\d+(?:\.\d+)?\b|\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?", re.IGNORECASE)


LEVEL_FIELD = re.compile(r'"?\b(?:level|severity|lvl|loglevel)"?\s*[:=]\s*"?([A-Za-z]+)', re.IGNORECASE)
LEADING = re.compile(r"^\W{0,3}(trace|debug|info|information|notice|warn|warning|error|fatal|critical)\b", re.IGNORECASE)


def level_of(text: str) -> str:
    """ERROR, WARN or INFO. An explicit level field or leading level word wins; otherwise
    any error-ish word in the line counts."""
    m = LEVEL_FIELD.search(text) or LEADING.search(text)
    word = m.group(1).lower() if m else None
    if word is None:
        m = LEVEL.search(text)
        word = m.group(1).lower() if m else "info"
    if word.startswith("warn"):
        return "WARN"
    if word in ("fatal", "panic", "critical", "error", "err", "exception", "crit"):
        return "ERROR"
    return "INFO"


def _ts(s: str) -> float | None:
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def summarise(service: str, entries: list[tuple[float | None, str, str]], top: int) -> tuple[list[Fact], float | None]:
    """entries are (ts, level, masked text). Returns facts and the first ERROR time."""
    counts = Counter(level for _, level, _ in entries)
    facts = [Fact(service=service, signal="log_count", scope=level, value=counts.get(level, 0))
             for level in ("ERROR", "WARN", "INFO")]
    groups: dict[tuple[str, str], list[str]] = {}
    for _, level, text in entries:
        if level != "INFO":
            groups.setdefault((level, VOLATILE.sub("#", text)), []).append(text)
    ranked = sorted(groups.items(), key=lambda kv: (kv[0][0] != "ERROR", -len(kv[1])))
    facts += [Fact(service=service, signal="error_message", scope=f"log:{level}", value=texts[0][:200])
              for (level, _), texts in ranked[:top]]
    first_error = min((ts for ts, level, _ in entries if level == "ERROR" and ts), default=None)
    return facts, first_error


def parse_line(text: str) -> tuple[str, str]:
    """(level, message). JSON log lines are unpacked to their level and message fields."""
    text = text.strip()
    if text.startswith("{"):
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if isinstance(doc, dict):
            msg = next((str(doc[k]) for k in ("msg", "message", "body", "Body") if doc.get(k)), text)
            lvl = next((str(doc[k]) for k in ("level", "severity", "lvl", "levelname") if doc.get(k)), None)
            return (level_of(f"level={lvl}") if lvl else level_of(msg)), msg
    return level_of(text), text


def _docker_entries(b: Backends, service: str, since: float, tail: int) -> list[tuple[float | None, str, str]]:
    out = []
    for line in redact.clean_log_lines(b.docker.logs(name=service, since=since, tail=tail)):
        m = TS_PREFIX.match(line)
        ts, text = (_ts(m.group(1)), line[m.end():]) if m else (None, line)
        level, msg = parse_line(text)
        out.append((ts, level, msg))
    return out


def _opensearch_entries(b: Backends, service: str, since_min: int) -> list[tuple[float | None, str, str]]:
    ep = config.endpoints()
    f = ep["opensearch_fields"]
    body = {"size": 200, "sort": [{f["timestamp"]: "desc"}],
            "query": {"bool": {"filter": [
                {"match_phrase": {f["service"]: service}},
                {"range": {f["timestamp"]: {"gte": f"now-{since_min}m"}}}]}}}
    hits = b.opensearch.search(index=ep["opensearch_index"], body=body).get("hits", {}).get("hits", [])
    out = []
    for h in hits:
        src = h.get("_source", {})
        text = src.get(f["body"])
        text = text if isinstance(text, str) else str(text)
        sev = _get(src, f["severity"]) or ""
        level = "ERROR" if str(sev).upper().startswith(("ERR", "FATAL", "CRIT")) else \
            "WARN" if str(sev).upper().startswith("WARN") else level_of(text)
        out.append((_ts(str(_get(src, f["timestamp"]) or "")), level, redact.mask(text).strip()))
    return out


def _get(doc: dict, dotted: str):
    """Read "a.b.c" from a document whose keys may be nested or literally dotted."""
    if dotted in doc:
        return doc[dotted]
    head, _, rest = dotted.partition(".")
    if rest and isinstance(doc.get(head), dict):
        return _get(doc[head], rest)
    return None


def search_logs(b: Backends, service: str, pattern: str | None = None, level: str | None = None,
                since_min: int | None = None) -> list[Evidence]:
    """Tool 6: log counts by level, the top distinct WARN/ERROR messages (masked and
    de-duplicated), and the first ERROR time as onset_ts."""
    cfg = config.thresholds()["logs"]
    since_min = since_min or cfg["since_min"]
    query = {"service": service, "pattern": pattern, "level": level, "since_min": since_min}
    sources = {"docker": lambda: _docker_entries(b, service, b.now() - since_min * 60, cfg["tail"])}
    if b.opensearch is not None:
        sources["opensearch"] = lambda: _opensearch_entries(b, service, since_min)
    out = []
    for source, fetch in sources.items():
        try:
            entries = fetch()
        except BackendError as e:
            out.append(Evidence(tool="search_logs", query=query | {"source": source, "error": str(e)},
                                ts=b.now_dt(), facts=[]))
            continue
        if pattern:
            rx = re.compile(pattern, re.IGNORECASE)
            entries = [e for e in entries if rx.search(e[2])]
        if level:
            entries = [e for e in entries if e[1] == level.upper()]
        if source == "opensearch" and not entries:
            continue
        facts, first_error = summarise(service, entries, cfg["top_messages"])
        out.append(Evidence(tool="search_logs", query=query | {"source": source}, ts=b.now_dt(),
                            onset_ts=datetime.fromtimestamp(first_error, timezone.utc) if first_error else None,
                            facts=facts, examples=[t[:200] for _, lv, t in entries if lv == "ERROR"][:2]))
    return out
