"""Policy-neutral path normalization and containment checks for datasets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class PathPolicyError(ValueError):
    """Base error for path-policy failures."""


class PathOutsideAllowedRoots(PathPolicyError):
    """Raised when a candidate path does not resolve inside any allowed root."""

    def __init__(self, candidate: Path, roots: list[Path]) -> None:
        self.candidate = candidate
        self.roots = roots
        super().__init__(f"Path outside allowed roots: {candidate}")


@dataclass(frozen=True)
class ContainedPath:
    """Resolved candidate path and the allowed root that contains it."""

    path: Path
    root: Path


def normalize_path(path: str | Path) -> Path:
    """Resolve a path without applying caller-specific policy."""
    return Path(path).resolve()


def normalize_roots(roots: Iterable[str | Path]) -> list[Path]:
    """Resolve root paths while preserving caller-provided order."""
    return [Path(root).resolve() for root in roots]


def is_contained_in(candidate: str | Path, root: str | Path) -> bool:
    """Return whether candidate resolves inside root, including root itself."""
    resolved_candidate = normalize_path(candidate)
    resolved_root = normalize_path(root)
    return resolved_candidate == resolved_root or resolved_candidate.is_relative_to(resolved_root)


def require_contained_in_roots(
    path: str | Path,
    roots: Iterable[str | Path],
) -> ContainedPath:
    """Resolve path and require it to be inside one of the resolved roots."""
    candidate = normalize_path(path)
    allowed = normalize_roots(roots)
    for root in allowed:
        if candidate == root or candidate.is_relative_to(root):
            return ContainedPath(path=candidate, root=root)
    raise PathOutsideAllowedRoots(candidate, allowed)

