"""Regression tests for converter Docker image definitions."""

from __future__ import annotations

import re
from pathlib import Path

from backend.core.config import settings


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "docker" / "converter" / "Dockerfile"
COPY_SOURCE_RE = re.compile(
    r"^COPY\s+rosbag2lerobot-svt/(.+?)\s+/app(?:/.*)?$", re.MULTILINE
)
COPY_RE = re.compile(r"^COPY(?:\s+--from=\S+)?\s+(\S+)\s+(\S+)$", re.MULTILINE)


def _write_fake_rosbag_checkout(repo_root: Path) -> None:
    (repo_root / "conversion").mkdir(parents=True)
    (repo_root / "nas").mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "partition_recordings.py").write_text(
        "",
        encoding="utf-8",
    )
    (repo_root / "auto_converter.py").write_text("", encoding="utf-8")


def _copy_mappings(dockerfile: str) -> list[tuple[str, str]]:
    return COPY_RE.findall(dockerfile)


def test_converter_dockerfile_only_copies_existing_rosbag_sources(
    tmp_path, monkeypatch
):
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    rosbag_repo = tmp_path / "rosbag2lerobot-svt"
    _write_fake_rosbag_checkout(rosbag_repo)
    monkeypatch.setattr(settings, "rosbag_to_lerobot_repo_path", str(rosbag_repo))

    missing = [
        relative_path
        for relative_path in COPY_SOURCE_RE.findall(dockerfile)
        if not (Path(settings.rosbag_to_lerobot_repo_path) / relative_path).exists()
    ]

    assert missing == []


def test_converter_dockerfile_drops_sourceless_bootstrap():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "sourceless_pyc.py" not in dockerfile
    assert "materialize_sourceless_pyc" not in dockerfile


def test_converter_dockerfile_runtime_smoke_tests_imports():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert 'COPY --from=builder /app /app' in dockerfile
    assert 'PYTHONPATH=/app python3 -c "\\' not in dockerfile
    assert 'PYTHONPATH="/app:${PYTHONPATH}" python3 -c "\\' in dockerfile
    assert 'Path(\\"/app/conversion/data_creator.py\\").exists()' in dockerfile
    assert 'print(\\"conversion import OK\\")' in dockerfile
    assert 'print(\\"auto_converter import OK\\")' in dockerfile
    assert 'print(\\"queue_adapter import OK\\")' in dockerfile


def test_converter_dockerfile_defaults_to_queue_worker():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert 'CMD ["python3", "-m", "backend.converter.queue_adapter"]' in dockerfile


def test_converter_dockerfile_copies_robot_profiles_into_runtime_path():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert ("conversion_configs/robots", "/app/configs/robots") in _copy_mappings(
        dockerfile
    )


def test_converter_dockerfile_targets_submodule_sources():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY rosbag2lerobot-svt/conversion /app/conversion" in dockerfile
    assert "COPY rosbag2lerobot-svt/nas /app/nas" in dockerfile
    assert "COPY rosbag2lerobot-svt/auto_converter.py /app/auto_converter.py" in dockerfile


def test_converter_dockerfile_has_no_direct_rerun_dependency():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "rerun-sdk" not in dockerfile
