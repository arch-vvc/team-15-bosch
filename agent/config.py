"""Loads the YAML files in agent/config. Thresholds and patterns live there, not in code."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_DIR = Path(os.environ.get("AGENT_CONFIG_DIR", Path(__file__).parent / "config"))


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    with open(CONFIG_DIR / f"{name}.yaml") as f:
        return yaml.safe_load(f) or {}


def endpoints() -> dict:
    cfg = dict(load("endpoints"))
    for key in ("prometheus_url", "jaeger_url", "opensearch_url", "opensearch_index"):
        if os.environ.get(key.upper()) is not None:
            cfg[key] = os.environ[key.upper()]
    return cfg


def thresholds() -> dict:
    return load("thresholds")


def redaction() -> dict:
    return load("redaction")


def services() -> dict:
    return load("services")


def shop_services() -> list[str]:
    return [n for n, c in services()["containers"].items() if c["role"] == "shop"]


def all_containers() -> list[str]:
    return list(services()["containers"])
