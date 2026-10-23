"""Read only explicit model settings; never evaluate a dotenv file as code."""

import os
from pathlib import Path

KEYS = {"OPENAI_API_KEY", "INCIDENT_MODEL", "INCIDENT_MODEL_URL"}


def model_settings(path=Path(".env")):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key = key.strip()
            if separator and key in KEYS:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                values[key] = value
    values.update({key: os.environ[key] for key in KEYS if key in os.environ})
    return values
