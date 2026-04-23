"""Regression tests for converter container memory limits."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker" / "converter" / "docker-compose.yml"


def _environment_map(environment: list[str]) -> dict[str, str]:
    return dict(item.split("=", 1) for item in environment)


def test_converter_compose_memory_limits_and_guard_threshold():
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    service = compose["services"]["convert-server"]
    environment = _environment_map(service["environment"])

    assert service["mem_limit"] == "48g"
    assert service["memswap_limit"] == "48g"
    assert environment["MEMORY_THRESHOLD_PCT"] == "60"
