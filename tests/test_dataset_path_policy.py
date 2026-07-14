import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.datasets.services.path_policy import (
    PathOutsideAllowedRoots,
    is_contained_in,
    require_contained_in_roots,
)


def test_policy_accepts_absolute_path_under_allowed_root(tmp_path):
    root = tmp_path / "datasets"
    dataset = root / "cell001" / "ds"
    dataset.mkdir(parents=True)

    contained = require_contained_in_roots(f"{root}/cell001/./ds", [root])

    assert contained.path == dataset.resolve()
    assert contained.root == root.resolve()


def test_policy_rejects_traversal_outside_allowed_root(tmp_path):
    root = tmp_path / "datasets"
    sibling = tmp_path / "outside"
    root.mkdir()
    sibling.mkdir()

    with pytest.raises(PathOutsideAllowedRoots) as excinfo:
        require_contained_in_roots(root / ".." / "outside", [root])

    assert excinfo.value.candidate == sibling.resolve()


def test_policy_rejects_sibling_prefix_paths(tmp_path):
    root = tmp_path / "data"
    sibling_prefix = tmp_path / "data-other"
    root.mkdir()
    sibling_prefix.mkdir()

    assert not is_contained_in(sibling_prefix, root)
    with pytest.raises(PathOutsideAllowedRoots):
        require_contained_in_roots(sibling_prefix, [root])


def test_policy_rejects_symlink_resolved_outside_root(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathOutsideAllowedRoots):
        require_contained_in_roots(link, [root])


def test_dataset_ops_adapter_preserves_400_detail(tmp_path, monkeypatch):
    from backend.config import settings
    from backend.datasets.routers import dataset_ops

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(allowed)], raising=False)

    with pytest.raises(HTTPException) as excinfo:
        dataset_ops._validate_path(str(outside))

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == f"Path outside allowed roots: {outside}"


def test_fields_adapter_preserves_403_detail(tmp_path, monkeypatch):
    from backend.core.config import settings
    from backend.datasets.routers import fields

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(allowed)], raising=False)

    with pytest.raises(HTTPException) as excinfo:
        fields._validate_path(str(outside))

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Access denied: path outside allowed roots"


def test_distribution_adapter_preserves_403_detail(tmp_path, monkeypatch):
    from backend.core.config import settings
    from backend.datasets.routers import distribution

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(settings, "allowed_dataset_roots", [str(allowed)], raising=False)

    with pytest.raises(HTTPException) as excinfo:
        distribution._validate_dataset_path(str(outside))

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Access denied: path outside allowed roots"
