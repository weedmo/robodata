# ADR 0002 — Keep `backend/routers/` and `backend/services/` Compat Shims

- **Status**: Accepted
- **Date**: 2026-05-05
- **Context**: An architecture review surfaced both folders as "dead façade
  layer — delete." The proposal was reverted by code review.

## Decision

Keep the re-export shims under `backend/routers/` and `backend/services/`.
Each shim file is 1–2 lines (`from backend.datasets.X import *` or similar)
and is intentionally cheap.

## Reasons future reviewers should not re-suggest deletion

1. **Documented "keep" decision.** Promoted from
   `docs/superpowers/plans/2026-04-15-domain-restructure.md` Task 8: *"Keep
   shims for now — they cost nothing and prevent breakage from any external
   scripts or tools that import from old paths."* This ADR exists to surface
   that decision where architecture reviews look (`docs/adr/`) instead of
   leaving it buried in a migration plan.

2. **In-flight plans still reference legacy paths.**
   `docs/superpowers/plans/2026-04-14-robodata-studio-plan-b.md` and
   `docs/superpowers/plans/2026-04-12-conversion-pipeline.md` reference
   `backend.services.*` and `backend.routers.*` import paths. Deleting the
   shims breaks those plans mid-execution.

3. **Cost is genuinely zero.** No runtime cost, no test cost. The only
   "cost" is visual — a developer might think there are two parallel
   layers. That cost is paid by reading this ADR or by the shim file's own
   `Backwards-compatibility shim` docstring.

4. **What the architecture review got right.** Internal callers (e.g.
   tests under `tests/`) **may** be migrated to the canonical
   `backend.datasets.services.*` / `backend.datasets.routers.*` paths.
   That migration was performed and kept. Only the **folder deletion** is
   forbidden by this ADR.

## Threshold for revisiting

Revisit if **all** of the following become true:
- No superpowers plan references the legacy paths.
- A grep across the entire org's tooling (CI, ops scripts, downstream
  packages) confirms zero external consumers.
- The migration plan that introduced the shims is fully retired.

Until then, both folders stay.
