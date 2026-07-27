"""Static safety contracts for the offline conversion recovery overlay."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_COMPOSE_FILE = REPO_ROOT / "docker" / "compose.yml"
RECOVERY_COMPOSE_FILE = REPO_ROOT / "docker" / "compose.conversion-recovery.yml"
APP_DOCKERFILE = REPO_ROOT / "docker" / "ui" / "Dockerfile.app"
RECOVERY_WRAPPER = REPO_ROOT / "scripts" / "run_conversion_recovery.sh"
DATA_ROOT = "${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}"


def _overlay() -> dict:
    return yaml.safe_load(RECOVERY_COMPOSE_FILE.read_text(encoding="utf-8"))


def _merged_config() -> dict:
    environment = os.environ.copy()
    environment.setdefault("POSTGRES_PASSWORD", "compose-contract-test")
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE_FILE),
            "-f",
            str(RECOVERY_COMPOSE_FILE),
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_normal_services_are_mutation_disabled_and_mount_nas_read_only():
    overlay = _overlay()

    for service_name in ("app", "curation-worker", "converter"):
        service = overlay["services"][service_name]
        assert service["environment"]["CURATION_CONVERSION_MUTATIONS_ENABLED"] == (
            "false"
        )
        assert service["volumes"] == [
            {
                "type": "bind",
                "source": DATA_ROOT,
                "target": DATA_ROOT,
                "read_only": True,
            }
        ]


def test_recovery_is_an_offline_one_shot_with_the_only_writable_nas_bind():
    recovery = _overlay()["services"]["conversion-recovery"]

    assert recovery["profiles"] == ["recovery"]
    assert recovery["restart"] == "no"
    assert recovery["network_mode"] == "none"
    assert recovery["environment"]["CURATION_RECOVERY_ISOLATED"] == "true"
    assert "depends_on" not in recovery
    assert recovery["volumes"] == [
        {
            "type": "bind",
            "source": DATA_ROOT,
            "target": DATA_ROOT,
            "read_only": False,
        }
    ]
    assert recovery["entrypoint"] == [
        "python",
        "-m",
        "scripts.recover_conversion",
    ]


def test_recovery_reuses_app_image_build_and_image_contains_cli():
    base = yaml.safe_load(BASE_COMPOSE_FILE.read_text(encoding="utf-8"))
    recovery = _overlay()["services"]["conversion-recovery"]

    assert recovery["build"] == base["services"]["app"]["build"]
    dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "COPY scripts/recover_conversion.py "
        "/app/scripts/recover_conversion.py"
    ) in dockerfile


def test_recovery_wrapper_stops_and_verifies_all_mutation_services():
    wrapper = RECOVERY_WRAPPER.read_text(encoding="utf-8")

    assert '--profile "*" stop app curation-worker converter' in wrapper
    assert '--profile "*" ps --status running --services' in wrapper
    assert "for mutation_service in app curation-worker converter" in wrapper
    assert "run --rm --no-deps conversion-recovery" in wrapper


def test_merged_compose_has_exactly_one_rw_nas_consumer():
    services = _merged_config()["services"]
    resolved_root = "/mnt/synology/data/data_div/2026_1"

    for service_name in ("app", "curation-worker", "converter"):
        service = services[service_name]
        mounts = [
            volume
            for volume in service["volumes"]
            if volume["target"] == resolved_root
        ]
        assert len(mounts) == 1
        assert mounts[0]["read_only"] is True
        assert service["environment"]["CURATION_CONVERSION_MUTATIONS_ENABLED"] == (
            "false"
        )

    recovery = services["conversion-recovery"]
    recovery_mounts = [
        volume
        for volume in recovery["volumes"]
        if volume["target"] == resolved_root
    ]
    assert len(recovery_mounts) == 1
    assert recovery_mounts[0].get("read_only", False) is False
    assert recovery.get("depends_on") is None
    assert recovery["environment"]["CURATION_RECOVERY_ISOLATED"] == "true"
