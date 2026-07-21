#!/usr/bin/env python3
"""Split one raw task directory into conversion-compatible metadata groups."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _mapping_keys(value: Any) -> list[str]:
    return sorted(str(key) for key in value) if isinstance(value, dict) else []


def _normalized_action_joint_order(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): [str(joint) for joint in joints]
        for key, joints in sorted(value.items())
        if isinstance(joints, list)
    }


def conversion_signature(metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return a stable digest and the fields that shape LeRobot output."""
    payload = {
        "robot_type": str(metadata.get("robot_type", "")),
        "fps": int(metadata.get("fps") or 0),
        "joint_names": [str(joint) for joint in (metadata.get("joint_names") or [])],
        "action_order": [str(name) for name in (metadata.get("action_order") or [])],
        "action_joint_order": _normalized_action_joint_order(
            metadata.get("action_joint_order")
        ),
        "action_topic_names": _mapping_keys(metadata.get("action_topics_map")),
        "camera_names": _mapping_keys(metadata.get("camera_topic_map")),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:8], payload


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return normalized or "unknown"


def build_split_plan(source_task: Path, keep_signature: str | None = None) -> dict:
    source_task = source_task.resolve()
    if not source_task.is_dir():
        raise ValueError(f"raw task directory not found: {source_task}")

    grouped: dict[str, dict[str, Any]] = {}
    invalid: list[dict[str, str]] = []
    for recording_dir in sorted(path for path in source_task.iterdir() if path.is_dir()):
        metacard = recording_dir / "metacard.json"
        try:
            metadata = json.loads(metacard.read_text(encoding="utf-8"))
        except Exception as exc:
            invalid.append({"recording": recording_dir.name, "error": str(exc)})
            continue
        digest, signature = conversion_signature(metadata)
        group = grouped.setdefault(
            digest,
            {"signature": signature, "recordings": []},
        )
        if group["signature"] != signature:
            raise RuntimeError(f"signature hash collision: {digest}")
        group["recordings"].append(recording_dir.name)

    if not grouped:
        raise ValueError(f"no valid metacards found under {source_task}")
    if keep_signature is None:
        keep_signature = max(grouped, key=lambda key: len(grouped[key]["recordings"]))
    if keep_signature not in grouped:
        raise ValueError(f"keep signature not found: {keep_signature}")

    groups = []
    for digest, group in sorted(
        grouped.items(), key=lambda item: (-len(item[1]["recordings"]), item[0])
    ):
        robot_type = group["signature"]["robot_type"]
        destination = (
            source_task
            if digest == keep_signature
            else source_task.with_name(
                f"{source_task.name}__{_slug(robot_type)}__{digest}"
            )
        )
        groups.append({
            "signature_id": digest,
            "signature": group["signature"],
            "count": len(group["recordings"]),
            "destination": str(destination),
            "keep_in_source": digest == keep_signature,
            "recordings": group["recordings"],
        })

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_task": str(source_task),
        "keep_signature": keep_signature,
        "groups": groups,
        "invalid": invalid,
    }


def write_manifest(path: Path, plan: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def apply_split(plan: dict) -> int:
    source_task = Path(plan["source_task"])
    source_device = source_task.stat().st_dev
    moves: list[tuple[Path, Path]] = []
    for group in plan["groups"]:
        if group["keep_in_source"]:
            continue
        destination = Path(group["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.stat().st_dev != source_device:
            raise RuntimeError(f"destination is on a different filesystem: {destination}")
        destination.mkdir(exist_ok=True)
        for serial in group["recordings"]:
            source = source_task / serial
            target = destination / serial
            if not source.is_dir():
                raise FileNotFoundError(f"recording disappeared before move: {source}")
            if target.exists():
                raise FileExistsError(f"destination recording already exists: {target}")
            moves.append((source, target))

    for source, target in moves:
        source.rename(target)
    return len(moves)


def apply_split_as_symlink_view(plan: dict, backing_source: Path | None = None) -> int:
    """Expose metadata groups as symlink directories without rewriting source data.

    This mode is intended for read-only or UID-mapped NAS exports where the task
    directory itself cannot be modified, but its writable parent allows an atomic
    rename.  The original task becomes one hidden backing directory and each
    visible group contains relative symlinks to its recordings.
    """
    source_task = Path(plan["source_task"])
    if backing_source is None:
        backing_source = source_task.with_name(
            f".{source_task.name}__metadata_source"
        )
    backing_source = backing_source.resolve(strict=False)

    if not source_task.is_dir() or source_task.is_symlink():
        raise ValueError(f"source must be a real task directory: {source_task}")
    if backing_source.exists() or backing_source.is_symlink():
        raise FileExistsError(f"backing source already exists: {backing_source}")
    if backing_source.parent != source_task.parent:
        raise ValueError("backing source must be a sibling of the source task")

    source_device = source_task.stat().st_dev
    destinations: list[Path] = []
    recordings: list[tuple[Path, str]] = []
    seen_serials: set[str] = set()
    preexisting_destinations: set[Path] = set()
    for group in plan["groups"]:
        destination = Path(group["destination"])
        if destination.parent.stat().st_dev != source_device:
            raise RuntimeError(f"destination is on a different filesystem: {destination}")
        if destination != source_task:
            if destination.exists():
                if not destination.is_dir() or any(destination.iterdir()):
                    raise FileExistsError(
                        f"destination must be absent or an empty directory: {destination}"
                    )
                preexisting_destinations.add(destination)
            elif destination.is_symlink():
                raise FileExistsError(f"destination symlink already exists: {destination}")
        destinations.append(destination)
        for serial in group["recordings"]:
            if serial in seen_serials:
                raise RuntimeError(f"recording appears in multiple groups: {serial}")
            seen_serials.add(serial)
            recording = source_task / serial
            if not recording.is_dir():
                raise FileNotFoundError(f"recording disappeared before split: {recording}")
            recordings.append((destination, serial))

    created_links: list[Path] = []
    created_destinations: list[Path] = []
    source_task.rename(backing_source)
    try:
        for destination in destinations:
            if destination not in preexisting_destinations:
                destination.mkdir()
                created_destinations.append(destination)
        for destination, serial in recordings:
            link = destination / serial
            if link.exists() or link.is_symlink():
                raise FileExistsError(f"destination recording already exists: {link}")
            relative_target = os.path.relpath(backing_source / serial, destination)
            link.symlink_to(relative_target, target_is_directory=True)
            created_links.append(link)
    except Exception:
        for link in reversed(created_links):
            link.unlink(missing_ok=True)
        for destination in reversed(created_destinations):
            destination.rmdir()
        backing_source.rename(source_task)
        raise
    return len(created_links)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_task", type=Path)
    parser.add_argument("--keep-signature")
    parser.add_argument("--manifest", type=Path, required=True)
    apply_mode = parser.add_mutually_exclusive_group()
    apply_mode.add_argument("--apply", action="store_true")
    apply_mode.add_argument("--link-view", action="store_true")
    parser.add_argument("--backing-source", type=Path)
    args = parser.parse_args()

    plan = build_split_plan(args.source_task, args.keep_signature)
    write_manifest(args.manifest, plan)
    for group in plan["groups"]:
        marker = "keep" if group["keep_in_source"] else "move"
        print(
            f"{marker:4} {group['count']:5d} {group['signature_id']} "
            f"{group['signature']['robot_type']} -> {group['destination']}"
        )
    if plan["invalid"]:
        print(f"invalid metacards: {len(plan['invalid'])}")
    if args.apply:
        moved = apply_split(plan)
        print(f"moved recordings: {moved}")
    elif args.link_view:
        linked = apply_split_as_symlink_view(plan, args.backing_source)
        print(f"linked recordings: {linked}")
    else:
        print("dry-run only; pass --apply or --link-view to split recordings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
