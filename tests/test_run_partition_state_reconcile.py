from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "scripts" / "run_partition_state_reconcile.sh"
DOCKERFILE = REPO_ROOT / "docker" / "converter" / "Dockerfile"
DIGEST = "a" * 64


def _fixture(tmp_path: Path) -> tuple[dict[str, str], list[str], Path]:
    data_root = tmp_path / "data"
    raw_root = data_root / "raw"
    lerobot_root = data_root / "lerobot"
    source = raw_root / "cell004" / "task"
    source.mkdir(parents=True)
    lerobot_root.mkdir()
    state = lerobot_root / "convert_state.json"
    state.write_text("{}", encoding="utf-8")
    state.chmod(0o640)
    private = data_root / "private"
    private.mkdir()
    journal = private / "partition.json"
    journal.write_text("{}", encoding="utf-8")
    journal.chmod(0o600)
    state_log = private / ".partition.json.state-log"
    state_log.write_text("{}\n", encoding="utf-8")
    state_log.chmod(0o600)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runtime.chmod(0o700)
    docker_log = tmp_path / "docker.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_CALL_LOG"
case " $* " in
  *" psql "*) printf '%s\n' "${FAKE_ACTIVE_CONVERTS:-0}" ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["DOCKER_CALL_LOG"] = str(docker_log)
    environment["CURATION_DATA_ROOT"] = str(data_root)
    environment["XDG_RUNTIME_DIR"] = str(runtime)
    args = [
        "apply",
        str(raw_root),
        str(lerobot_root),
        str(journal),
        "cell004/task",
        f"{DIGEST}=cell004/task__move",
    ]
    return environment, args, docker_log


def test_wrapper_enforces_isolation_and_forwards_exact_reconcile_command(
    tmp_path: Path,
):
    environment, args, docker_log = _fixture(tmp_path)

    completed = subprocess.run(
        ["bash", str(WRAPPER), *args],
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
    assert any(" stop app curation-worker converter" in call for call in calls)
    assert any("ps --status running --services" in call for call in calls)
    assert any(" psql " in f" {call} " for call in calls)
    assert "default_transaction_read_only=on" in " ".join(calls)
    assert "--no-deps --build" in recovery_call
    assert "--entrypoint /entrypoint.sh" in recovery_call
    assert "-m scripts.reconcile_partition_convert_state apply" in recovery_call
    assert f"--destination {DIGEST}=cell004/task__move" in recovery_call
    assert ":ro" in recovery_call
    assert ":rw" in recovery_call
    assert next(i for i, call in enumerate(calls) if " psql " in f" {call} ") < (
        calls.index(recovery_call)
    )


def test_wrapper_blocks_recovery_container_when_active_convert_exists(
    tmp_path: Path,
):
    environment, args, docker_log = _fixture(tmp_path)
    environment["FAKE_ACTIVE_CONVERTS"] = "1"

    completed = subprocess.run(
        ["bash", str(WRAPPER), *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert completed.returncode != 0
    assert "active convert" in completed.stderr.lower()
    assert not any("conversion-recovery" in call for call in calls)


def test_wrapper_allows_raw_source_owner_to_differ_from_state_artifacts(
    tmp_path: Path,
):
    environment, args, docker_log = _fixture(tmp_path)
    source = Path(args[1]) / args[4]
    state = Path(args[2]) / "convert_state.json"
    real_stat = shutil.which("stat")
    assert real_stat is not None
    fake_stat = tmp_path / "stat"
    fake_stat.write_text(
        f"""#!/bin/sh
if [ "$1" = "-c" ] && [ "$4" = "$CROSS_OWNER_SOURCE" ]; then
  case "$2" in
    "%u") printf '%s\\n' "$CROSS_OWNER_UID"; exit 0 ;;
    "%g") printf '%s\\n' "$CROSS_OWNER_GID"; exit 0 ;;
  esac
fi
exec {real_stat} "$@"
""",
        encoding="utf-8",
    )
    fake_stat.chmod(0o700)
    environment["CROSS_OWNER_SOURCE"] = str(source)
    environment["CROSS_OWNER_UID"] = str(state.stat().st_uid + 2)
    environment["CROSS_OWNER_GID"] = str(state.stat().st_gid)

    completed = subprocess.run(
        ["bash", str(WRAPPER), *args],
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
    assert f"--user {state.stat().st_uid}:{state.stat().st_gid}" in recovery_call


def test_wrapper_rejects_incompatible_state_and_journal_owners_before_docker(
    tmp_path: Path,
):
    environment, args, docker_log = _fixture(tmp_path)
    journal = Path(args[3])
    real_stat = shutil.which("stat")
    assert real_stat is not None
    fake_stat = tmp_path / "stat"
    fake_stat.write_text(
        f"""#!/bin/sh
if [ "$1" = "-c" ] && [ "$2" = "%u" ] && [ "$4" = "$FOREIGN_JOURNAL" ]; then
  printf '65534\\n'
  exit 0
fi
exec {real_stat} "$@"
""",
        encoding="utf-8",
    )
    fake_stat.chmod(0o700)
    environment["FOREIGN_JOURNAL"] = str(journal)

    completed = subprocess.run(
        ["bash", str(WRAPPER), *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "owners are incompatible" in completed.stderr.lower()
    assert not docker_log.exists()


def test_converter_image_copies_partition_state_reconcile_module():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "COPY --chmod=0755 scripts/reconcile_partition_convert_state.py "
        "/app/scripts/reconcile_partition_convert_state.py"
    ) in dockerfile
