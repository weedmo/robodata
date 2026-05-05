# ADR 0001 — Keep Job Handler Registry Manual

- **Status**: Accepted
- **Date**: 2026-05-05
- **Context**: architecture review surfaced "automate the curation-worker
  HANDLERS dict" as a deepening candidate.

## Decision

Keep `backend/workers/curation_worker.py::HANDLERS` and
`backend/jobs/router.py::EnqueueBody.type` (`Literal[...]`) as **hand-rolled
registries**. Do not introduce a decorator-based or auto-discovered
JobHandler registry.

## Reasons future reviewers should not re-suggest this

1. **`Literal[...]` is the API contract.** The Operation enum on
   `EnqueueBody.type` is consumed by FastAPI's OpenAPI schema and downstream
   typed clients. A dynamically-generated Literal loses static-typing benefit
   and complicates schema-first tooling. The cost of typing one new string in
   a Literal when adding an Operation is smaller than the cost of debugging
   when typed clients silently widen.

2. **Workers intentionally pick different subsets.** `converter` and
   `curation-worker` each register a partial map of Operations. Auto-discover
   would either force a single-worker model (wrong — the workers run in
   different containers with different runtimes) or require a per-worker
   filter expression that recreates the manual mapping in another form.

3. **Scale.** 6 Operations today (`convert`, `split`, `merge`, `delete`,
   `sync_good_episodes`, `stamp_cycles`). Manual registration boilerplate is
   ~3 lines per Operation; the import-time magic of a registry pattern adds
   debug surface that exceeds the boilerplate it removes at this scale.

4. **Deletion test.** Adding then removing a registry would leave the same
   per-handler registration calls inlined. Complexity moves rather than
   concentrates — failing the deepening criterion in `LANGUAGE.md`.

## Threshold for revisiting

Revisit if **any** of the following becomes true:
- Operations exceed ~10 distinct types.
- A third Worker kind appears (beyond `converter` and `curation-worker`).
- Multiple Operations need to be enabled/disabled at runtime per environment.
