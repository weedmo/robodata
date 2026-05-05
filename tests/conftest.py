"""Shared fixtures for curation-tools tests using real dataset data."""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REAL_DATASET_ROOT = Path("/tmp/hf-mounts/Phy-lab/dataset")
BASIC_AIC = REAL_DATASET_ROOT / "basic_aic_cheetcode_dataset"
HOJUN = REAL_DATASET_ROOT / "hojun"


@pytest.fixture(scope="module")
def _point_settings_at_test_db():
    url = os.environ.get(
        "CURATION_TEST_DB_URL",
        "postgresql://curation:dev-only-change-me@127.0.0.1:5433/curation_test",
    )
    previous_url = os.environ.get("CURATION_DB_URL")
    os.environ["CURATION_DB_URL"] = url
    from backend.core import config as _cfg

    _cfg.settings = _cfg.Settings()

    # backend.core.db imports the settings object directly, so keep that module in sync too.
    try:
        from backend.core import db as _db
    except Exception:
        _db = None
    if _db is not None:
        _db.settings = _cfg.settings
    yield
    if previous_url is None:
        os.environ.pop("CURATION_DB_URL", None)
    else:
        os.environ["CURATION_DB_URL"] = previous_url
    _cfg.settings = _cfg.Settings()
    if _db is not None:
        _db.settings = _cfg.settings


def _copy_dataset(src: Path) -> Path:
    """Copy a dataset to a temp directory so write tests don't mutate originals."""
    tmp = Path(tempfile.mkdtemp(prefix="curation_test_"))
    dest = tmp / src.name
    shutil.copytree(src, dest)
    # Ensure all files and dirs are writable (source may be read-only mount)
    import os, stat
    for root, dirs, files in os.walk(dest):
        for d in dirs:
            os.chmod(os.path.join(root, d), stat.S_IRWXU)
        for f in files:
            os.chmod(os.path.join(root, f), stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    return dest


@pytest.fixture
def basic_aic_path():
    if not BASIC_AIC.exists():
        pytest.skip(f"real dataset fixture is not available: {BASIC_AIC}")
    return BASIC_AIC


@pytest.fixture
def hojun_path():
    if not HOJUN.exists():
        pytest.skip(f"real dataset fixture is not available: {HOJUN}")
    return HOJUN


@pytest.fixture
def writable_basic_aic():
    """Writable copy of basic_aic dataset for mutation tests."""
    if not BASIC_AIC.exists():
        pytest.skip(f"real dataset fixture is not available: {BASIC_AIC}")
    dest = _copy_dataset(BASIC_AIC)
    from backend.config import settings
    original = settings.allowed_dataset_roots
    settings.allowed_dataset_roots = original + ["/tmp"]
    yield dest
    settings.allowed_dataset_roots = original
    shutil.rmtree(dest.parent, ignore_errors=True)


@pytest.fixture
def writable_hojun():
    """Writable copy of hojun dataset for mutation tests."""
    if not HOJUN.exists():
        pytest.skip(f"real dataset fixture is not available: {HOJUN}")
    dest = _copy_dataset(HOJUN)
    from backend.config import settings
    original = settings.allowed_dataset_roots
    settings.allowed_dataset_roots = original + ["/tmp"]
    yield dest
    settings.allowed_dataset_roots = original
    shutil.rmtree(dest.parent, ignore_errors=True)


@pytest.fixture
def fresh_episode_service():
    """Return a fresh EpisodeService instance."""
    from backend.datasets.services.episode_service import EpisodeService
    return EpisodeService()
