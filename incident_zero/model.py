"""OpenAI Responses function calling; credentials never enter reports."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from incident_zero.telemetry import MAX_RESPONSE_BYTES, NoRedirects, TelemetryError
from incident_zero.config import model_settings


class ModelError(Exception):
    pass


class ResponsesModel:
    label = "openai-responses"

    def __init__(self, model=None, base_url=None):
        settings = model_settings()
        self.model = model or settings.get("INCIDENT_MODEL", "gpt-6.1-sol")
        self.key = settings.get("OPENAI_API_KEY", "")
        self.base = (base_url or settings.get("INCIDENT_MODEL_URL", "https://api.openai.com/v1")).rstrip("/")
        parsed = urlsplit(self.base)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.scheme == "http"
        if not local and self.base != "https://api.openai.com/v1":
            raise ValueError("Model endpoint must be the official OpenAI API or a loopback HTTP server")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid model endpoint")
        if not self.model or (not local and not self.key):
            raise ValueError("Set INCIDENT_MODEL and OPENAI_API_KEY locally, or configure a local Responses-compatible server")
        self.local = local
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def respond(self, instructions, conversation, tools, max_output_tokens, timeout):
        body = {"model": self.model, "instructions": instructions, "input": conversation,
                "tools": tools, "parallel_tool_calls": False, "store": False,
                "max_output_tokens": max_output_tokens}
        if not self.local:
            body["include"] = ["reasoning.encrypted_content"]
        headers = {"Content-Type": "application/json"}
        if not self.local and self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = Request(self.base + "/responses", data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ModelError("Model response exceeded size limit")
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("output"), list) or not all(isinstance(item, dict) for item in data["output"]):
                raise ModelError("Invalid model response")
            return data
        except HTTPError as exc:
            raise ModelError(f"Model API returned HTTP {exc.code}; check account, model access and limits") from exc
        except (URLError, OSError, TimeoutError, ValueError, TelemetryError) as exc:
            raise ModelError("Model transport or JSON response failed") from exc
