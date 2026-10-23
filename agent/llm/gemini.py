"""Gemini adapter for agent.llm.investigator.Investigator (same contract as ResponsesAdapter).

Key and model come from .env (GEMINI_API_KEY, GEMINI_MODEL) or the environment; the key is
sent only in the x-goog-api-key header to generativelanguage.googleapis.com and never logged.
Every turn must be a function call (toolConfig mode ANY): a read-only tool, or
submit_diagnosis with the investigator's output schema. Tool schemas are reduced to the
OpenAPI subset Gemini accepts ($ref inlined, unsupported keywords dropped).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from agent.llm.responses import SUBMIT, NoModel

API = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.5-flash-lite"   # lightest, least demand; ~1 s on 23 Oct 2026 (3.8-flash took 70 s+)
FALLBACKS = ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.1-flash-lite"]
KEEP = {"type", "format", "description", "nullable", "enum", "properties", "required", "items",
        "minimum", "maximum", "minItems", "maxItems"}


def settings(path: Path = Path(".env")) -> dict:
    """GEMINI_* values from .env, overridden by the environment. Never evaluates the file."""
    keys = {"GEMINI_API_KEY", "GEMINI_MODEL"}
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() in keys:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                values[key.strip()] = value
    values.update({k: os.environ[k] for k in keys if os.environ.get(k)})
    return values


def to_gemini_schema(schema: dict, defs: dict | None = None) -> dict:
    """JSON Schema (as pydantic and the Eyes registry write it) -> Gemini's OpenAPI subset."""
    defs = defs if defs is not None else schema.get("$defs", {})
    if "$ref" in schema:
        return to_gemini_schema(defs[schema["$ref"].split("/")[-1]], defs)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"]
            out = to_gemini_schema(options[0], defs) if options else {"type": "string"}
            if len(options) < len(schema[key]):
                out["nullable"] = True
            return out
    out = {}
    for key, value in schema.items():
        if key not in KEEP:
            continue
        if key == "type" and isinstance(value, list):
            kinds = [t for t in value if t != "null"]
            out["type"] = kinds[0] if kinds else "string"
            if len(kinds) < len(value):
                out["nullable"] = True
        elif key == "properties":
            out["properties"] = {k: to_gemini_schema(v, defs) for k, v in value.items()}
        elif key == "items":
            out["items"] = to_gemini_schema(value, defs)
        else:
            out[key] = value
    if "enum" in out and "type" not in out:
        out["type"] = "string"
    if "properties" in out and "type" not in out:
        out["type"] = "object"
    return out


class GeminiAdapter:
    label = "gemini"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, http: httpx.Client | None = None,
                 timeout_s: float = 40, fallbacks: list[str] | None = None):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        self.key, self.model = api_key, model
        self.fallbacks = FALLBACKS if fallbacks is None else fallbacks
        self.last_model = None
        self.http = http or httpx.Client(timeout=timeout_s)

    @classmethod
    def from_env(cls, model: str | None = None):
        s = settings()
        if not s.get("GEMINI_API_KEY"):
            return NoModel()
        return cls(s["GEMINI_API_KEY"], model or s.get("GEMINI_MODEL") or DEFAULT_MODEL)

    def declarations(self, tools: dict, output_schema: dict) -> list[dict]:
        decls = []
        for name, schema in tools.items():
            d = {"name": name, "description": f"Read-only telemetry tool {name}."}
            params = to_gemini_schema(schema)
            if params.get("properties"):
                d["parameters"] = params
            decls.append(d)
        decls.append({"name": SUBMIT, "parameters": to_gemini_schema(output_schema),
                      "description": "Finish: hypotheses citing ledger evidence IDs (E1, E2, ...) and "
                                     "selected_diagnosis (a hypothesis id, or UNKNOWN when unsure)."})
        return decls

    def body(self, history, system, tools, output_schema, temperature) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(history, default=str)}]}],
            "tools": [{"functionDeclarations": self.declarations(tools, output_schema)}],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
        }
        # Gemini 3 models are tuned for their default temperature; older ones take 0
        if self.model.startswith("gemini-2"):
            body["generationConfig"] = {"temperature": temperature}
        return body

    def _post(self, body: dict) -> dict:
        """Try the configured model, then the fallbacks, when Gemini answers 429 or 503
        ("high demand" is common and brief). A second pass waits 2 s first."""
        tried = []
        for attempt in (1, 2):
            for model in [self.model] + [m for m in self.fallbacks if m != self.model]:
                r = self.http.post(f"{API}/{model}:generateContent", json=body,
                                   headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
                tried.append(f"{model}:{r.status_code}")
                if r.status_code == 200:
                    self.last_model = model
                    return r.json()
                if r.status_code not in (429, 503):
                    break
            else:
                if attempt == 1:
                    time.sleep(2)
                    continue
            break
        # status codes only: the body can echo the request, and the key must never reach a log
        raise RuntimeError("Gemini API failed: " + ", ".join(tried))

    async def complete(self, history, system, tools, output_schema, temperature=0):
        body = self.body(history, system, tools, output_schema, temperature)
        data = await asyncio.get_running_loop().run_in_executor(None, self._post, body)
        return self.parse(data)

    @staticmethod
    def parse(data: dict) -> dict:
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        for call in calls:
            if call.get("name") == SUBMIT:
                return {"output": call.get("args") or {}}
        if calls:
            return {"tool_calls": [{"name": c["name"], "arguments": c.get("args") or {}} for c in calls]}
        return {"output": "".join(p.get("text", "") for p in parts)}
