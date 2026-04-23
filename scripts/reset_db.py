"""Backup and reinitialize the curation-tools metadata DB.

Usage:
    python -m scripts.reset_db --dry-run          # preview
    python -m scripts.reset_db                    # interactive
    python -m scripts.reset_db --yes              # non-interactive
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import shutil
import sys
from pathlib import Path


def _utc_timestamp() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _unique_backup(target: Path) -> Path:
    candidate = target
    n = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.name}.{n}")
        n += 1
    return candidate


def _resolve_db_path() -> Path:
    from backend.core.config import settings
    if settings.db_path:
        return Path(settings.db_path)
    return Path.home() / ".local" / "share" / "curation-tools" / "metadata.db"


def run(*, dry_run: bool, assume_yes: bool) -> None:
    db_path = _resolve_db_path()
    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    ts = _utc_timestamp()

    targets: list[Path] = [p for p in (db_path, wal, shm) if p.exists()]
    backups: list[tuple[Path, Path]] = []
    for src in targets:
        desired = src.with_name(f"{src.name}.bak-{ts}")
        backups.append((src, _unique_backup(desired)))

    print(f"[reset_db] DB path: {db_path}")
    if not targets:
        print("[reset_db] no existing DB files; will create fresh v4 DB")
    else:
        for src, dst in backups:
            print(f"[reset_db] backup  {src}  ->  {dst}")

    if dry_run:
        print("[reset_db] dry-run; no files modified")
        return

    if not assume_yes:
        resp = input("Proceed? [y/N] ").strip().lower()
        if resp != "y":
            print("[reset_db] aborted")
            sys.exit(1)

    for src, dst in backups:
        shutil.copy2(src, dst)
        src.unlink()

    # Reset the singleton in case the test harness (or a prior call) left one
    from backend.core import db as dbmod
    asyncio.run(dbmod.close_db())
    dbmod._reset()
    dbmod._db_path_override = str(db_path)

    asyncio.run(dbmod.init_db())
    print(f"[reset_db] fresh DB initialized at {db_path} (schema v4)")
    for _src, dst in backups:
        print(f"[reset_db] backup retained: {dst}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset curation-tools metadata DB.")
    parser.add_argument("--dry-run", action="store_true", help="show actions without modifying files")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    args = parser.parse_args()
    run(dry_run=args.dry_run, assume_yes=args.yes)


if __name__ == "__main__":
    main()
