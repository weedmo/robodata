-- init.sql — runs on first boot of the Postgres container only.
-- (It does NOT run on subsequent restarts; schema migrations are applied
--  at app startup by backend/core/db.py::init_db().)

-- ---- Create a dedicated test database ---------------------------------
-- The compose-provided database is already created by POSTGRES_DB.
-- We additionally create curation_test for pytest fixtures.
CREATE DATABASE curation_test OWNER curation;

-- ---- schema_versions (replaces SQLite PRAGMA user_version) ------------
\connect curation

CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- Application tables (Postgres dialect) ----------------------------
CREATE TABLE IF NOT EXISTS datasets (
    id              SERIAL PRIMARY KEY,
    path            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    cell_name       TEXT,
    fps             INTEGER NOT NULL DEFAULT 0,
    total_episodes  INTEGER NOT NULL DEFAULT 0,
    robot_type      TEXT,
    features        JSONB,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    synced_at       TIMESTAMPTZ,
    info_json_mtime DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS dataset_stats (
    dataset_id          INTEGER PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    graded_count        INTEGER NOT NULL DEFAULT 0,
    good_count          INTEGER NOT NULL DEFAULT 0,
    normal_count        INTEGER NOT NULL DEFAULT 0,
    bad_count           INTEGER NOT NULL DEFAULT 0,
    total_duration_sec  DOUBLE PRECISION NOT NULL DEFAULT 0,
    good_duration_sec   DOUBLE PRECISION NOT NULL DEFAULT 0,
    normal_duration_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
    bad_duration_sec    DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS episode_serials (
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    episode_index   INTEGER NOT NULL,
    serial_number   TEXT NOT NULL,
    PRIMARY KEY (dataset_id, episode_index)
);
CREATE INDEX IF NOT EXISTS idx_episode_serials_serial
    ON episode_serials(serial_number);

CREATE TABLE IF NOT EXISTS annotations (
    serial_number   TEXT PRIMARY KEY,
    grade           TEXT CHECK(grade IN ('good','normal','bad')),
    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason          TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- jobs queue (used by Spec-2 converter and Spec-3 curation-worker) -
DO $$ BEGIN
    CREATE TYPE job_type AS ENUM (
        'convert',
        'split',
        'merge',
        'delete',
        'sync_good_episodes',
        'stamp_cycles',
        'auto_bad_camera_flicker'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM (
        'queued',
        'running',
        'complete',
        'failed',
        'cancel_requested',
        'cancelled'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS jobs (
    id                  BIGSERIAL PRIMARY KEY,
    external_id         TEXT       NOT NULL DEFAULT gen_random_uuid()::text,
    type                job_type   NOT NULL,
    status              job_status NOT NULL DEFAULT 'queued',
    payload             JSONB      NOT NULL DEFAULT '{}'::jsonb,
    progress            JSONB      NOT NULL DEFAULT '{}'::jsonb,
    result              JSONB,
    error               TEXT,
    attempts            INTEGER    NOT NULL DEFAULT 0,
    worker_id           TEXT,
    dedupe_key          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    heartbeat_at        TIMESTAMPTZ,
    cancel_requested_at TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_jobs_queued
    ON jobs(type, created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_jobs_running
    ON jobs(type, worker_id) WHERE status IN ('running', 'cancel_requested');
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_external_id
    ON jobs(external_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_dedupe
    ON jobs(type, dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'cancel_requested');

-- Worker control plane (API-only Worker Control 보강안 §4)
DO $$ BEGIN
  CREATE TYPE worker_state AS ENUM ('running', 'paused', 'draining', 'stopped');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS worker_controls (
    worker_id     TEXT PRIMARY KEY,
    desired_state worker_state NOT NULL DEFAULT 'running',
    updated_by    TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note          TEXT
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id        TEXT PRIMARY KEY,
    actual_state     worker_state NOT NULL,
    pid              INTEGER,
    container_id     TEXT,
    last_beat_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    in_flight_job_id BIGINT REFERENCES jobs(id),
    detail           JSONB
);
CREATE INDEX IF NOT EXISTS idx_worker_heartbeats_recent
    ON worker_heartbeats(last_beat_at);

INSERT INTO worker_controls (worker_id, desired_state)
VALUES ('converter', 'running'), ('curation-worker', 'running')
ON CONFLICT (worker_id) DO NOTHING;

-- Record initial schema version.
INSERT INTO schema_versions(version) VALUES (1)
    ON CONFLICT (version) DO NOTHING;

-- ---- Mirror the schema into curation_test too -------------------------
\connect curation_test

CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Test DB intentionally starts empty of application tables;
-- pytest fixtures run init_db() to create them per-session.
