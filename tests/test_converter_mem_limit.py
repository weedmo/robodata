"""Regression tests for converter settings in the unified compose stack."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker" / "compose.yml"


def _environment_map(environment: dict[str, str] | list[str]) -> dict[str, str]:
    if isinstance(environment, dict):
        return environment
    return dict(item.split("=", 1) for item in environment)


def test_converter_service_memory_limits_and_guard_threshold():
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    service = compose["services"]["converter"]
    environment = _environment_map(service["environment"])

    assert "convert" in service["profiles"]
    assert service["container_name"] == "convert-server"
    assert service["mem_limit"] == "24g"
    assert service["memswap_limit"] == "24g"
    assert environment["MEMORY_THRESHOLD_PCT"] == "80"
