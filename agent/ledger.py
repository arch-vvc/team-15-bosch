"""Evidence ledger: append-only, IDs E1, E2, ... in the order evidence arrives.

Everything the agent may cite lives here. The validator (B) checks claims against it and the
report (E) copies facts from it, so nothing is ever edited or removed once added.
"""
from __future__ import annotations

import json
from pathlib import Path

from agent.contracts import Evidence, Fact
from agent.tools import redact


class RedactionError(RuntimeError):
    pass


class Ledger:
    def __init__(self):
        self._items: list[Evidence] = []

    def add(self, ev: Evidence) -> str:
        """Store a copy of ev under the next ID and return the ID.

        Tools already redact; this scrubs once more and refuses anything still carrying
        flag data, so a tool bug cannot leak flag text to the LLM or the report.
        """
        clean = Evidence.model_validate(redact.scrub(ev.model_dump(mode="json")))
        leaks = redact.find_leaks(clean.model_dump(mode="json"))
        if leaks:
            raise RedactionError(f"flag data in evidence from {ev.tool}: {sorted(set(leaks))}")
        clean.id = f"E{len(self._items) + 1}"
        clean.redacted = True
        self._items.append(clean)
        ev.id = clean.id
        return clean.id

    def get(self, evidence_id: str) -> Evidence:
        for ev in self._items:
            if ev.id == evidence_id:
                return ev.model_copy(deep=True)
        raise KeyError(evidence_id)

    def has(self, evidence_id: str) -> bool:
        return any(ev.id == evidence_id for ev in self._items)

    def all(self) -> list[Evidence]:
        return [ev.model_copy(deep=True) for ev in self._items]

    def facts(self, service: str | None = None, signal: str | None = None) -> list[tuple[str, Fact]]:
        """(evidence id, fact) pairs, optionally filtered. Handy for rules and the validator."""
        return [(ev.id, f) for ev in self._items for f in ev.facts
                if (service is None or f.service == service) and (signal is None or f.signal == signal)]

    def to_json(self) -> list[dict]:
        return [ev.model_dump(mode="json") for ev in self._items]

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "Ledger":
        ledger = cls()
        ledger._items = [Evidence.model_validate(e) for e in json.loads(Path(path).read_text())]
        return ledger

    def __len__(self) -> int:
        return len(self._items)
