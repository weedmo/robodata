# CONTEXT — Domain Glossary

This file names the load-bearing concepts in curation-tools. Use these terms in
code, commits, and discussion. Update when concepts shift or new ones land.

## Job

A persistent record of an asynchronous long-running operation, stored in the
`jobs` Postgres table.

- **Lifecycle**: `queued → running → (complete | failed | cancelled)`.
- **Identity**: `id BIGSERIAL` (internal) and `external_id TEXT` (UUID returned
  to API clients and the UI). Internal callers query by `id`; external callers
  see only `external_id`.
- **Dedupe**: `dedupe_key TEXT` is a domain-level uniqueness key — e.g.
  `"split:/path/to/source"` to prevent two concurrent splits of the same
  dataset. Distinct from `external_id`, which is just an identifier.
- **Result**: `result JSONB` carries operation-specific output. The API façade
  flattens it into a typed response shape; UI never reads the raw jsonb.
- **Cancel**: a worker observes `status='cancel_requested'` via its
  `check_cancel` poll. Wired only for `convert` today; rolling cancel out to
  other Operations is a separate piece of work.

## Operation

The domain action a Job performs, identified by `jobs.type`. Current set:

| type | meaning |
|---|---|
| `convert` | run rosbag2lerobot-svt against a cell/task target |
| `split` | split episodes from one dataset into a derived dataset |
| `merge` | merge multiple datasets |
| `delete` | delete episodes (in-place or to a derived path) |
| `sync_good_episodes` | sync selected episodes to an absolute destination |
| `stamp_cycles` | write cycle stamps onto episodes |

Adding a new Operation = define a JobHandler, register it under a new key,
extend `EnqueueBody.type`.

## JobHandler

An async function that processes one Job of a specific Operation.

```python
async def handler(
    job: Mapping[str, Any],
    *, check_cancel: Callable[[], Awaitable[bool]],
) -> CancelledNormally | None: ...
```

JobHandlers live next to their Operation's domain code, not next to the worker
runtime. Each handler owns a single Operation's invariants — payload shape,
idempotency, result jsonb keys.

## Worker

A long-running process that owns a `Mapping[str, JobHandler]` and runs the
`tick()` loop from `backend/workers/runtime.py`. Two Workers exist in
production:

- **`converter`** — handles `{"convert": _handler}`. Lives in the `converter`
  Docker container; entry point `backend/converter/queue_adapter.py`.
- **`curation-worker`** — handles the five dataset-ops Operations (`split`,
  `merge`, `delete`, `sync_good_episodes`, `stamp_cycles`). Lives in the
  `curation-worker` Docker container; entry point
  `backend/workers/curation_worker.py` (replacing
  `docker/curation-worker/placeholder.py`).

A Worker selects work via `SELECT … WHERE type = ANY($supported) FOR UPDATE
SKIP LOCKED`, so multiple replicas of the same Worker kind are safe.

## In-place rollback helper

`backend/jobs/runner_helpers.py::run_in_place_with_rollback` is the shared
implementation of "rename target to `.bak`, attempt operation, restore on
failure." Used by Operations that mutate a dataset directory in place
(`stamp_cycles`, in-place `delete`). Lives outside vendored engine code so
its invariant (no half-applied state on failure) stays testable.
