"""Behavioral regression tests for the hybrid host-dev launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
START_SH = REPO_ROOT / "start.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(0o755)


def _build_fake_project(tmp_path: Path) -> tuple[Path, Path]:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    shutil.copy(START_SH, project_dir / "start.sh")
    (project_dir / "start.sh").chmod(0o755)

    (project_dir / ".venv" / "bin").mkdir(parents=True)
    (project_dir / ".venv" / "bin" / "activate").write_text(
        "export VIRTUAL_ENV=fake-venv\n",
        encoding="utf-8",
    )
    (project_dir / "frontend" / "node_modules").mkdir(parents=True)
    docker_dir = project_dir / "docker"
    docker_dir.mkdir()
    docker_dir.joinpath(".env").write_text(
        "POSTGRES_DB=curation\nPOSTGRES_USER=curation\nPOSTGRES_PASSWORD=test-password\n",
        encoding="utf-8",
    )
    docker_dir.joinpath(".env.example").write_text(
        "POSTGRES_DB=curation\nPOSTGRES_USER=curation\nPOSTGRES_PASSWORD=dev-only-change-me\n",
        encoding="utf-8",
    )
    docker_dir.joinpath("compose.yml").write_text("services:\n  db:\n    image: postgres:16-alpine\n", encoding="utf-8")

    log_path = tmp_path / "launcher.log"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
        set -euo pipefail
        printf 'docker:%s\\n' "$*" >> "$FAKE_LOG"

        if [[ "$1" == "compose" && "$2" == "version" ]]; then
            exit 0
        fi

        if [[ "$1" == "compose" ]]; then
            if [[ " $* " == *" ps -q db "* ]]; then
                printf 'fake-db-container\\n'
                exit 0
            fi
            if [[ " $* " == *" up -d db "* ]]; then
                exit 0
            fi
        fi

        if [[ "$1" == "inspect" ]]; then
            count_file="$FAKE_STATE_DIR/inspect-count"
            count=0
            if [[ -f "$count_file" ]]; then
                count="$(cat "$count_file")"
            fi
            count="$((count + 1))"
            printf '%s' "$count" > "$count_file"
            printf 'inspect:%s\\n' "$count" >> "$FAKE_LOG"

            if (( count < ${FAKE_DB_READY_AFTER:-1} )); then
                printf '%s\\n' "${FAKE_DB_STATUS_BEFORE:-starting}"
            else
                printf '%s\\n' "${FAKE_DB_STATUS_AFTER:-healthy}"
            fi
            exit 0
        fi

        printf 'unexpected docker args: %s\\n' "$*" >&2
        exit 1
        """,
    )
    _write_executable(
        bin_dir / "uvicorn",
        """#!/usr/bin/env bash
        set -euo pipefail
        printf 'uvicorn:%s|%s|%s\\n' "$CURATION_DB_URL" "$CURATION_DATASET_ROOT_BASE" "$CURATION_DATASET_PATH" >> "$FAKE_LOG"
        trap 'printf "uvicorn:term\\n" >> "$FAKE_LOG"; exit 0' TERM INT

        if [[ -n "${UVICORN_EXIT_CODE:-}" ]]; then
            exit "$UVICORN_EXIT_CODE"
        fi

        while true; do
            sleep 0.1
        done
        """,
    )
    _write_executable(
        bin_dir / "npm",
        """#!/usr/bin/env bash
        set -euo pipefail
        printf 'npm:%s|%s\\n' "$*" "${VITE_BACKEND_URL:-}" >> "$FAKE_LOG"
        trap 'printf "npm:term\\n" >> "$FAKE_LOG"; exit 0' TERM INT

        if [[ "$1" == "install" ]]; then
            exit 0
        fi

        if [[ "$1" == "run" && "$2" == "dev" && -n "${NPM_DEV_EXIT_CODE:-}" ]]; then
            exit "$NPM_DEV_EXIT_CODE"
        fi

        while true; do
            sleep 0.1
        done
        """,
    )

    env = {
        "FAKE_LOG": str(log_path),
        "FAKE_STATE_DIR": str(state_dir),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    return project_dir, log_path, env


def _run_start_sh(tmp_path: Path, **extra_env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    project_dir, log_path, env = _build_fake_project(tmp_path)
    full_env = os.environ.copy()
    full_env.update(env)
    full_env.update(extra_env)

    result = subprocess.run(
        ["bash", "start.sh"],
        cwd=project_dir,
        env=full_env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    lines = log_path.read_text(encoding="utf-8").splitlines()
    return result, lines


def test_start_sh_waits_for_db_health_before_launching_host_processes(tmp_path: Path):
    result, lines = _run_start_sh(
        tmp_path,
        PROJECT_NAME="task20-test",
        BACKEND_PORT="8123",
        FRONTEND_PORT="4312",
        CURATION_PG_HOST_PORT="55433",
        CURATION_DATA_ROOT="/tmp/curation-root",
        FAKE_DB_READY_AFTER="2",
        NPM_DEV_EXIT_CODE="0",
    )

    assert result.returncode == 0, result.stderr
    assert any(
        '--env-file' in line and '-p task20-test' in line and 'up -d db' in line
        for line in lines
        if line.startswith('docker:')
    )
    assert not any('up -d db rerun' in line for line in lines)
    assert lines.count("inspect:1") == 1
    assert lines.count("inspect:2") == 1
    assert lines.index("inspect:2") < lines.index(
        "uvicorn:postgresql://curation:test-password@127.0.0.1:55433/curation|/tmp/curation-root|/tmp/curation-root/lerobot"
    )
    assert "npm:run dev|http://localhost:8123" in lines


def test_start_sh_stops_remaining_child_when_one_process_fails(tmp_path: Path):
    result, lines = _run_start_sh(
        tmp_path,
        FAKE_DB_READY_AFTER="1",
        NPM_DEV_EXIT_CODE="7",
    )

    assert result.returncode == 7, result.stderr
    assert any(line.startswith("uvicorn:postgresql://") for line in lines)
    assert "uvicorn:term" in lines
