"""Static contract tests for the conversion recovery hold overlay."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_COMPOSE_FILE = REPO_ROOT / "docker" / "compose.yml"
HOLD_COMPOSE_FILE = REPO_ROOT / "docker" / "compose.conversion-hold.yml"
DATA_ROOT = "${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}"


def test_conversion_hold_disables_mutations_for_all_conversion_services():
    hold = yaml.safe_load(HOLD_COMPOSE_FILE.read_text(encoding="utf-8"))

    for service_name in ("app", "curation-worker", "converter"):
        assert (
            hold["services"][service_name]["environment"][
                "CURATION_CONVERSION_MUTATIONS_ENABLED"
            ]
            == "false"
        )


def test_base_compose_requires_explicit_conversion_enablement():
    base = yaml.safe_load(BASE_COMPOSE_FILE.read_text(encoding="utf-8"))

    for service_name in ("app", "curation-worker", "converter"):
        assert (
            base["services"][service_name]["environment"][
                "CURATION_CONVERSION_MUTATIONS_ENABLED"
            ]
            == "${CURATION_CONVERSION_MUTATIONS_ENABLED:-false}"
        )


def test_conversion_hold_makes_converter_nas_bind_read_only():
    hold = yaml.safe_load(HOLD_COMPOSE_FILE.read_text(encoding="utf-8"))

    assert hold["services"]["converter"]["volumes"] == [
        {
            "type": "bind",
            "source": DATA_ROOT,
            "target": DATA_ROOT,
            "read_only": True,
        }
    ]


def test_conversion_hold_preserves_base_app_docker_status_socket():
    base = yaml.safe_load(BASE_COMPOSE_FILE.read_text(encoding="utf-8"))
    hold = yaml.safe_load(HOLD_COMPOSE_FILE.read_text(encoding="utf-8"))

    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in base["services"]["app"][
        "volumes"
    ]
    assert "volumes" not in hold["services"]["app"]
