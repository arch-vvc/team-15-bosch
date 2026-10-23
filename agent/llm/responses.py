"""Model adapters for agent.llm.investigator.Investigator, which calls
    await model.complete(history=..., system=..., tools={name: schema}, output_schema=..., temperature=0)
and expects {"tool_calls": [{"name", "arguments"}]} or {"output": <diagnosis>}.

ResponsesAdapter reuses incident_zero's OpenAI Responses client (key and model from .env,
official endpoint or a loopback server only). The final answer is a `submit_diagnosis`
function call whose parameters are the investigator's output schema. NoModel answers
UNKNOWN straight away, so a missing key escalates instead of guessing.
"""
from __future__ import annotations

import asyncio
import json

SUBMIT = "submit_diagnosis"


class NoModel:
    label = "none"

    async def complete(self, **_):
        return {"output": {"hypotheses": [], "selected_diagnosis": "UNKNOWN",
                           "reason": "No LLM is configured; escalating with the evidence collected."}}


class ResponsesAdapter:
    label = "openai-responses"

    def __init__(self, client, max_output_tokens: int = 2000, timeout_s: float = 30):
        self.client, self.max_tokens, self.timeout = client, max_output_tokens, timeout_s

    @classmethod
    def from_env(cls, model: str | None = None):
        """The adapter if .env holds usable settings, otherwise NoModel."""
        from incident_zero.model import ResponsesModel
        try:
            return cls(ResponsesModel(model))
        except ValueError:
            return NoModel()

    @staticmethod
    def functions(tools: dict, output_schema: dict) -> list[dict]:
        defs = [{"type": "function", "name": name, "description": f"Read-only telemetry tool {name}.",
                 "parameters": schema, "strict": False} for name, schema in tools.items()]
        defs.append({"type": "function", "name": SUBMIT, "strict": False, "parameters": output_schema,
                     "description": "Finish: hypotheses citing ledger evidence IDs, and selected_diagnosis "
                                    "(a hypothesis id or UNKNOWN)."})
        return defs

    async def complete(self, history, system, tools, output_schema, temperature=0):
        conversation = [{"role": "user", "content": json.dumps(history, default=str)}]
        data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self.client.respond(system, conversation, self.functions(tools, output_schema),
                                              self.max_tokens, self.timeout))
        return self.parse(data)

    @staticmethod
    def parse(data: dict) -> dict:
        calls = [item for item in data.get("output", []) if item.get("type") == "function_call"]
        for call in calls:
            if call.get("name") == SUBMIT:
                return {"output": json.loads(call.get("arguments") or "{}")}
        if calls:
            return {"tool_calls": [{"name": c["name"], "arguments": json.loads(c.get("arguments") or "{}")}
                                   for c in calls]}
        text = "".join(part.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                       for part in item.get("content", []) if isinstance(part, dict))
        return {"output": text}      # the investigator validates it and retries once if it is not a diagnosis
