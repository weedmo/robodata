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
CONVERTER_DOCKERFILE = REPO_ROOT / "docker" / "converter" / "Dockerfile"
RECOVERY_WRAPPER = REPO_ROOT / "scripts" / "run_conversion_recovery.sh"
RAW_MATERIALIZATION_WRAPPER = (
    REPO_ROOT / "scripts" / "run_raw_materialization.sh"
)
RAW_CONTRACT_PARTITION_WRAPPER = (
    REPO_ROOT / "scripts" / "run_raw_contract_partition.sh"
)
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
    assert recovery["environment"]["HF_HOME"] == "/tmp/huggingface"
    assert recovery["environment"]["XDG_CACHE_HOME"] == "/tmp/cache"
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
        "/entrypoint.sh",
        "python3",
        "-m",
        "scripts.recover_conversion",
    ]


def test_recovery_reuses_ros_converter_image_build_and_contains_cli():
    base = yaml.safe_load(BASE_COMPOSE_FILE.read_text(encoding="utf-8"))
    recovery = _overlay()["services"]["conversion-recovery"]

    assert recovery["build"] == base["services"]["converter"]["build"]
    dockerfile = CONVERTER_DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "COPY --chmod=0644 scripts/__init__.py /app/scripts/__init__.py"
        in dockerfile
    )
    assert (
        "COPY --chmod=0644 scripts/recover_conversion.py "
        "/app/scripts/recover_conversion.py"
    ) in dockerfile
    assert (
        "COPY --chmod=0644 scripts/split_raw_task_by_metadata.py "
        "/app/scripts/split_raw_task_by_metadata.py"
    ) in dockerfile
    assert (
        "COPY rosbag2lerobot-svt/scripts/partition_recordings.py "
        "/app/rosbag2lerobot-svt/scripts/partition_recordings.py"
    ) in dockerfile
    assert "_partition_manifest_builder" in dockerfile


def test_recovery_wrapper_stops_and_verifies_all_mutation_services():
    wrapper = RECOVERY_WRAPPER.read_text(encoding="utf-8")

    assert '--profile "*" stop app curation-worker converter' in wrapper
    assert '--profile "*" ps --status running --services' in wrapper
    assert "for mutation_service in app curation-worker converter" in wrapper
    assert "run --rm --no-deps conversion-recovery" in wrapper


def test_raw_materialization_wrapper_runs_cli_only_after_service_isolation():
    wrapper = RAW_MATERIALIZATION_WRAPPER.read_text(encoding="utf-8")
    normalized = " ".join(wrapper.split())

    assert '--profile "*" stop app curation-worker converter' in wrapper
    assert '--profile "*" ps --status running --services' in wrapper
    assert "for mutation_service in app curation-worker converter" in wrapper
    assert (
        "run --rm --no-deps --entrypoint /entrypoint.sh conversion-recovery"
        in normalized
    )
    assert "python3" in normalized
    assert "-m scripts.split_raw_task_by_metadata" in normalized
    assert "-m scripts.split_raw_task_by_metadata" in normalized
    assert "--materialize-link-view" in wrapper
    assert "--detached-destination" in wrapper
    assert "--backing-source" in wrapper
    assert "--manifest" in wrapper
    assert "if (( $# != 4 ))" in wrapper
    assert "replacement" not in wrapper.lower()


def test_raw_materialization_wrapper_checks_active_converts_read_only_before_rw_run():
    wrapper = RAW_MATERIALIZATION_WRAPPER.read_text(encoding="utf-8")
    normalized = " ".join(wrapper.split())

    assert "psql" in normalized
    assert "FROM jobs" in normalized
    assert "type = 'convert'" in normalized
    for active_status in ("queued", "running", "cancel_requested"):
        assert active_status in normalized
    assert any(
        read_only_marker in normalized
        for read_only_marker in (
            "BEGIN READ ONLY",
            "SET TRANSACTION READ ONLY",
            "default_transaction_read_only=on",
        )
    )
    assert normalized.index("psql") < normalized.index(
        "run --rm --no-deps --entrypoint /entrypoint.sh conversion-recovery"
    )


def test_raw_contract_partition_wrapper_isolates_services_and_db_before_rw_run():
    wrapper = RAW_CONTRACT_PARTITION_WRAPPER.read_text(encoding="utf-8")
    normalized = " ".join(wrapper.split())

    assert '--profile "*" stop app curation-worker converter' in wrapper
    assert '--profile "*" ps --status running --services' in wrapper
    assert "for mutation_service in app curation-worker converter" in wrapper
    assert "flock -n 9" in wrapper
    assert 'exec 9<"${runtime_dir}"' in wrapper
    assert "robodata-contract-partition.lock" not in wrapper
    assert "default_transaction_read_only=on" in normalized
    assert "type = 'convert'" in normalized
    for active_status in ("queued", "running", "cancel_requested"):
        assert active_status in normalized
    assert "-m scripts.partition_raw_by_contract" in normalized
    assert "--entrypoint /entrypoint.sh" in normalized
    assert "python3 -m scripts.partition_raw_by_contract" in normalized
    assert "/contract-manifest.json:ro" in wrapper
    assert normalized.index("psql") < normalized.index(
        '"${recovery_compose[@]}"'
    )
    assert normalized.index("flock -n 9") < normalized.index(
        '--profile "*" stop'
    )
    assert (
        "scripts/partition_raw_by_contract.py"
        in CONVERTER_DOCKERFILE.read_text(encoding="utf-8")
    )


def test_raw_contract_partition_wrapper_active_convert_blocks_rw_container(
    tmp_path,
):
    docker_log = tmp_path / "docker.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_CALL_LOG"
case " $* " in
  *" psql "*) printf '1\n' ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["DOCKER_CALL_LOG"] = str(docker_log)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime_dir.chmod(0o700)
    environment["XDG_RUNTIME_DIR"] = str(runtime_dir)

    completed = subprocess.run(
        [
            "bash",
            str(RAW_CONTRACT_PARTITION_WRAPPER),
            "rollback",
            "/raw/task",
            "/raw/journal.json",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert completed.returncode != 0
    assert "active convert" in completed.stderr.lower()
    assert any(" psql " in f" {call} " for call in calls)
    assert not any("conversion-recovery" in call for call in calls)


def test_raw_contract_partition_wrapper_forwards_exact_apply_after_clear_gate(
    tmp_path,
):
    docker_log = tmp_path / "docker.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_CALL_LOG"
case " $* " in
  *" psql "*) printf '0\n' ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    source = tmp_path / "raw" / "task"
    source.mkdir(parents=True)
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    contract.chmod(0o600)
    journal = tmp_path / "journal.json"
    destination = source.parent / "high"
    digest = "a" * 64
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["DOCKER_CALL_LOG"] = str(docker_log)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime_dir.chmod(0o700)
    environment["XDG_RUNTIME_DIR"] = str(runtime_dir)

    completed = subprocess.run(
        [
            "bash",
            str(RAW_CONTRACT_PARTITION_WRAPPER),
            "apply",
            str(source),
            str(contract),
            str(journal),
            digest,
            f"{digest}={destination}",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    calls = docker_log.read_text(encoding="utf-8").splitlines()
    recovery_call = next(
        call for call in calls if "conversion-recovery" in call
    )
    assert completed.returncode == 0
    assert f"{contract}:/contract-manifest.json:ro" in recovery_call
    assert "-m scripts.partition_raw_by_contract apply" in recovery_call
    assert f"--destination {digest}={destination}" in recovery_call
    assert next(i for i, call in enumerate(calls) if " psql " in f" {call} ") < (
        calls.index(recovery_call)
    )


def test_raw_materialization_wrapper_active_convert_blocks_rw_container(tmp_path):
    docker_log = tmp_path / "docker.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALL_LOG"
case " $* " in
  *" psql "*) printf 't\\n' ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["DOCKER_CALL_LOG"] = str(docker_log)

    completed = subprocess.run(
        [
            "bash",
            str(RAW_MATERIALIZATION_WRAPPER),
            "/raw/task",
            "/raw/.backing",
            "/raw/task-detached",
            "/tmp/manifest.json",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert completed.returncode != 0
    assert "active convert" in completed.stderr.lower()
    assert any(" psql " in f" {call} " for call in calls)
    assert not any("conversion-recovery" in call for call in calls)


def test_raw_materialization_wrapper_forwards_detached_args_after_clear_db_gate(
    tmp_path,
):
    docker_log = tmp_path / "docker.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALL_LOG"
case " $* " in
  *" psql "*) printf '0\\n' ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["DOCKER_CALL_LOG"] = str(docker_log)

    completed = subprocess.run(
        [
            "bash",
            str(RAW_MATERIALIZATION_WRAPPER),
            "/raw/task",
            "/raw/.backing",
            "/raw/task-detached",
            "/tmp/manifest.json",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    calls = docker_log.read_text(encoding="utf-8").splitlines()
    recovery_call = next(
        call for call in calls if "conversion-recovery" in call
    )
    assert completed.returncode == 0
    assert "--detached-destination /raw/task-detached" in recovery_call
    assert "/raw/task --materialize-link-view" in recovery_call
    assert "--backing-source /raw/.backing" in recovery_call
    assert "--manifest /tmp/manifest.json" in recovery_call
    assert next(i for i, call in enumerate(calls) if " psql " in f" {call} ") < (
        calls.index(recovery_call)
    )


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
    assert recovery["environment"]["HF_HOME"] == "/tmp/huggingface"
    assert recovery["environment"]["XDG_CACHE_HOME"] == "/tmp/cache"
