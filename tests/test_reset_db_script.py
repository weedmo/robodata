"""Tests for scripts/reset_db.py — backup and init flow."""

import asyncio
from pathlib import Path


def test_dry_run_does_not_touch_files(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "metadata.db"
    db_path.write_bytes(b"dummy")
    monkeypatch.setattr("backend.core.config.settings.db_path", str(db_path))

    from scripts.reset_db import run
    run(dry_run=True, assume_yes=True)

    assert db_path.exists()
    assert db_path.read_bytes() == b"dummy"
    out = capsys.readouterr().out
    assert str(db_path) in out


def test_backup_and_init_creates_empty_postgres_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "metadata.db"
    db_path.write_bytes(b"old")
    (tmp_path / "metadata.db-wal").write_bytes(b"wal")
    monkeypatch.setattr("backend.core.config.settings.db_path", str(db_path))
    # Reset the db singleton so the override takes effect
    from backend.core import db as dbmod
    dbmod._reset()
    dbmod._db_path_override = str(db_path)

    from scripts.reset_db import run
    run(dry_run=False, assume_yes=True)

    # The legacy SQLite file is backed up and removed; the fresh schema lives in Postgres.
    assert not db_path.exists()
    # At least one backup created
    backups = sorted(tmp_path.glob("metadata.db.bak-*"))
    assert backups, f"no backup created in {list(tmp_path.iterdir())}"
    assert backups[0].read_bytes() == b"old"
    wal_backups = sorted(tmp_path.glob("metadata.db-wal.bak-*"))
    assert wal_backups
    assert wal_backups[0].read_bytes() == b"wal"

    # New Postgres schema is initialized.
    async def _check():
        conn = await dbmod.get_db()
        async with conn.execute("SELECT version FROM schema_versions ORDER BY version DESC LIMIT 1") as cur:
            return (await cur.fetchone())[0]

    version = asyncio.run(_check())
    assert version == 1

    asyncio.run(dbmod.close_db())
    dbmod._reset()


def test_backup_name_collision_suffix(tmp_path, monkeypatch):
    """Two resets under a forced-identical timestamp must not clobber the first backup."""
    db_path = tmp_path / "metadata.db"
    monkeypatch.setattr("backend.core.config.settings.db_path", str(db_path))

    from backend.core import db as dbmod
    import scripts.reset_db as resetmod

    monkeypatch.setattr(resetmod, "_utc_timestamp", lambda: "20260420T000000Z")

    # First reset with "alpha" contents
    dbmod._reset()
    dbmod._db_path_override = str(db_path)
    db_path.write_bytes(b"alpha")
    resetmod.run(dry_run=False, assume_yes=True)
    asyncio.run(dbmod.close_db())

    # Second reset with "beta" contents at the same forced timestamp
    dbmod._reset()
    dbmod._db_path_override = str(db_path)
    db_path.write_bytes(b"beta")
    resetmod.run(dry_run=False, assume_yes=True)
    asyncio.run(dbmod.close_db())

    names = sorted(p.name for p in tmp_path.glob("metadata.db.bak-*"))
    assert "metadata.db.bak-20260420T000000Z" in names
    assert any(n.endswith(".1") for n in names), names

    dbmod._reset()
