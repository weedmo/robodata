#!/usr/bin/env python3
"""Inspect or recover one interrupted conversion transaction."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence


DEFAULT_DATA_ROOT = Path("/mnt/synology/data/data_div/2026_1")
MODES = (
    "inspect",
    "rollback",
    "adopt-finalization",
    "quarantine-restart",
    "commit-verified",
)
_LOWERCASE_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _recovery_service_class() -> Any:
    from backend.converter.recovery_service import RecoveryService

    return RecoveryService


def _default_root(environment_name: str, child: str) -> Path:
    configured = os.environ.get(environment_name)
    if configured:
        return Path(configured)
    return Path(os.environ.get("CURATION_DATA_ROOT", DEFAULT_DATA_ROOT)) / child


def _lowercase_sha256(value: str) -> str:
    if _LOWERCASE_SHA256_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "authorization must be a lowercase SHA-256 digest"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or recover one interrupted conversion transaction."
    )
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("cell_task", help="Relative cell/task identifier")
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=_default_root("RAW_BASE", "raw"),
    )
    parser.add_argument(
        "--lerobot-root",
        type=Path,
        default=_default_root("LEROBOT_BASE", "lerobot"),
    )
    parser.add_argument("--state-file", type=Path)
    parser.add_argument(
        "--authorize-legacy-marker-sha256",
        action="append",
        default=[],
        metavar="SHA256",
        help=(
            "Authorize one opaque legacy marker by an independently verified "
            "full-file SHA-256; repeat for multiple markers."
        ),
    )
    parser.add_argument(
        "--contract-manifest",
        type=Path,
        help=(
            "Private exact-once contract manifest used to validate legacy "
            "output semantics."
        ),
    )
    parser.add_argument(
        "--authorize-contract-manifest-sha256",
        type=_lowercase_sha256,
        metavar="SHA256",
        help=(
            "Independently verified full-file lowercase SHA-256 for "
            "--contract-manifest."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.contract_manifest is None) != (
        args.authorize_contract_manifest_sha256 is None
    ):
        parser.error(
            "--contract-manifest and "
            "--authorize-contract-manifest-sha256 must be provided together"
        )
    try:
        service = _recovery_service_class()(
            raw_root=args.raw_root,
            lerobot_root=args.lerobot_root,
            state_file=args.state_file,
            authorized_legacy_marker_sha256s=set(
                args.authorize_legacy_marker_sha256
            ),
            contract_manifest_path=args.contract_manifest,
            authorized_contract_manifest_sha256=(
                args.authorize_contract_manifest_sha256
            ),
        )
        if args.mode == "inspect":
            result = service.inspect(args.cell_task)
        else:
            result = service.recover(args.cell_task, args.mode)
    except Exception as exc:
        print(f"recover_conversion: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
