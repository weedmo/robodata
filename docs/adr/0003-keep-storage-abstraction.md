# ADR 0003 — Keep `backend/core/storage/` Abstraction Despite Zero Consumers

- **Status**: Accepted
- **Date**: 2026-05-06
- **Context**: An architecture review surfaced
  `backend/core/storage/{base,local}.py` as YAGNI — a `StorageBackend` ABC
  with a single `LocalStorage` implementation and zero production consumers.

## Decision

Keep the abstraction. Do **not** delete `backend/core/storage/` even though
nothing currently imports `StorageBackend` or `LocalStorage` from outside the
folder.

## Reasons future reviewers should not re-suggest deletion

1. **Aspirational by design.** The abstraction was introduced by
   `docs/superpowers/plans/2026-04-15-domain-restructure.md` Task 2 as the
   future home for `HFHubStorage`, `S3Storage`, etc. — not as a layer that
   was supposed to have multiple adapters on day one.

2. **Cost is zero.** No runtime overhead, no test boilerplate, no API
   surface. The same logic as `docs/adr/0002-keep-compat-shims.md`: the only
   "cost" is visual.

3. **Deletion-test framing.** Removing the abstraction now would require
   re-introducing it the moment a second backend (HF Hub / object store)
   appears in any plan — and re-introducing an interface after the first
   adapter is harder than evolving an interface that already exists.

## Threshold for revisiting

Revisit if **any** of the following becomes true:
- Two years have passed with no second storage backend appearing in any
  plan and no plan referencing the abstraction.
- Storage operations move into a different abstraction (e.g. a unified
  `Dataset` repository) that subsumes this layer.

Until then, the abstraction stays — even though it currently has one
implementation.
