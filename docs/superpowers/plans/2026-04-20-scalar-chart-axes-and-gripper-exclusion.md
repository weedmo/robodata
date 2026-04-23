# ScalarChart Axes + Gripper Band Exclusion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add x (timestamp) / y (shared min-max) axis labels to the curation right-panel joint mini charts, exclude gripper joints from divergence-band (red/yellow overlay) computation, and align the backend auto-grade severity calculation to the same gripper exclusion.

**Architecture:** Backend `scalars.py` router gains a per-frame `timestamps` array in its response so the frontend can label the x axis. Backend `auto_grade_service.py` and frontend `ScalarChart.tsx` share a single convention for gripper identification (unified keys `[7]` and `[15]`, matching `cycle_stamp_service.LEFT_GRIPPER_IDX=7` / `RIGHT_GRIPPER_IDX=15`) and both skip those joints when classifying severity. In the UI, paired observation/action series for the same joint are rendered with a shared y-axis scale so visual comparison is meaningful; min/max labels sit in a narrow column to the left of each canvas, and timestamp-based x labels appear only under the final chart of each section to reduce visual noise.

**Tech Stack:** FastAPI + pyarrow (backend), React + TypeScript + HTML Canvas 2D (frontend), pytest/pytest-asyncio (backend tests), Node-script assertion tests (frontend, matching `frontend/tests/appChrome.test.ts`).

---

## File Structure

**Backend — modify:**
- `backend/datasets/routers/scalars.py` — add `timestamps` array to response.
- `backend/datasets/services/auto_grade_service.py` — add `GRIPPER_INDICES` constant, skip those keys in `_episode_severity`.

**Backend — create:**
- `tests/test_scalars_router_timestamps.py` — verifies the router exposes a per-frame `timestamps` list aligned with scalar series.
- `tests/test_auto_grade_gripper_exclusion.py` — verifies gripper joints (`[7]`, `[15]`) are skipped by `_episode_severity`.

**Frontend — modify:**
- `frontend/src/components/ScalarChart.tsx` — add gripper-exclusion constant, shared y-range map, y/x axis label rendering, extend `ScalarData` with `timestamps`.

**Frontend — create:**
- `frontend/src/components/scalarChartHelpers.ts` — pure helpers: `GRIPPER_INDICES`, `isGripperKey`, `computeSharedYRanges`, `formatSeconds`. Kept in a separate file so they are testable from Node without a DOM.
- `frontend/tests/scalarChartHelpers.test.ts` — Node-script assertion tests for the helpers above (mirrors `appChrome.test.ts` style).

Files that change together stay together; the canvas draw routine and React tree stay in `ScalarChart.tsx`. Pure logic that benefits from unit tests moves to `scalarChartHelpers.ts`.

---

## Task 1: Backend — add `timestamps` array to the scalars response

**Files:**
- Create: `tests/test_scalars_router_timestamps.py`
- Modify: `backend/datasets/routers/scalars.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_scalars_router_timestamps.py`:

```python
"""The /api/scalars/{episode_index} response must expose per-frame timestamps.

The frontend labels the x-axis with `timestamps[0]` and `timestamps[-1]`, so the
router must return a list of floats with the same length as each scalar series
(or an empty list if the parquet file has no `timestamp` column).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException

from backend.datasets.routers import scalars as scalars_router


class _StubDatasetService:
    def __init__(self, dataset_path: Path, features: dict):
        self._dataset_path = dataset_path
        self._features = features

    def get_episode_file_location(self, episode_index: int):
        return {
            "dataset_from_index": 0,
            "dataset_to_index": 4,
            "data_chunk_index": 0,
            "data_file_index": 0,
        }

    def get_dataset_path(self):
        return str(self._dataset_path)

    def get_features(self):
        return self._features


def _write_parquet(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data" / "chunk-000"
    data_dir.mkdir(parents=True)
    out = data_dir / "file-000.parquet"
    table = pa.table({
        "observation.state": [[0.0, 0.1], [0.0, 0.2], [0.0, 0.3], [0.0, 0.4]],
        "action": [[0.0, 0.1], [0.0, 0.2], [0.0, 0.3], [0.0, 0.4]],
        "timestamp": [0.0, 0.1, 0.2, 0.3],
    })
    pq.write_table(table, out)
    return out


def test_scalars_response_includes_per_frame_timestamps(tmp_path, monkeypatch):
    _write_parquet(tmp_path)
    stub = _StubDatasetService(
        dataset_path=tmp_path,
        features={
            "observation.state": {"dtype": "float32"},
            "action": {"dtype": "float32"},
        },
    )
    monkeypatch.setattr(scalars_router, "dataset_service", stub)

    result = asyncio.run(scalars_router.get_scalars(episode_index=0))

    assert "timestamps" in result
    assert result["timestamps"] == [0.0, 0.1, 0.2, 0.3]
    # Timestamps length must match the per-frame series length.
    any_series = next(iter(result["observations"].values()))
    assert len(result["timestamps"]) == len(any_series)


def test_scalars_response_timestamps_empty_when_column_missing(tmp_path, monkeypatch):
    data_dir = tmp_path / "data" / "chunk-000"
    data_dir.mkdir(parents=True)
    table = pa.table({
        "observation.state": [[0.0, 0.1], [0.0, 0.2]],
        "action": [[0.0, 0.1], [0.0, 0.2]],
    })
    pq.write_table(table, data_dir / "file-000.parquet")

    class _Stub(_StubDatasetService):
        def get_episode_file_location(self, episode_index: int):
            return {
                "dataset_from_index": 0,
                "dataset_to_index": 2,
                "data_chunk_index": 0,
                "data_file_index": 0,
            }

    stub = _Stub(
        dataset_path=tmp_path,
        features={
            "observation.state": {"dtype": "float32"},
            "action": {"dtype": "float32"},
        },
    )
    monkeypatch.setattr(scalars_router, "dataset_service", stub)

    result = asyncio.run(scalars_router.get_scalars(episode_index=0))
    assert result["timestamps"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scalars_router_timestamps.py -v`
Expected: FAIL with `KeyError: 'timestamps'` or `assert 'timestamps' in result`.

- [ ] **Step 3: Modify `backend/datasets/routers/scalars.py`**

Replace the final `return` block (currently at `scalars.py:112-119`) with a version that includes `timestamps`. Also widen the early-return path at `scalars.py:59-67` to include an empty `timestamps` key so the shape is stable.

In the early return:

```python
    if not needed_columns and not flag_col:
        return {
            "episode_index": episode_index,
            "num_frames": to_idx - from_idx,
            "observations": {},
            "actions": {},
            "terminal_frames": [],
            "terminal_timestamps": [],
            "timestamps": [],
        }
```

After the `terminal_timestamps` block, add a per-frame `timestamps` extractor before the final return:

```python
    timestamps: list[float] = []
    if ts_col and ts_col in df:
        timestamps = [float(v) for v in df[ts_col]]
```

Final return:

```python
    return {
        "episode_index": episode_index,
        "num_frames": to_idx - from_idx,
        "observations": observations,
        "actions": actions,
        "terminal_frames": terminal_frames,
        "terminal_timestamps": terminal_timestamps,
        "timestamps": timestamps,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scalars_router_timestamps.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_scalars_router_timestamps.py backend/datasets/routers/scalars.py
git commit -m "feat(scalars): return per-frame timestamps for chart x-axis"
```

---

## Task 2: Backend — exclude gripper joints from auto-grade severity

**Files:**
- Create: `tests/test_auto_grade_gripper_exclusion.py`
- Modify: `backend/datasets/services/auto_grade_service.py`

Gripper index convention: `LEFT_GRIPPER_IDX = 7`, `RIGHT_GRIPPER_IDX = 15` (see `backend/datasets/services/cycle_stamp_service.py:14-15`). After `unify_key`, those columns become `[7]` and `[15]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auto_grade_gripper_exclusion.py`:

```python
"""Auto-grade severity must ignore gripper joints [7] and [15].

Gripper close/open transitions produce legitimate action↔observation divergence
spikes that are not data-quality problems. The chart UI paints no red/yellow
band for them, and the backend severity calculation must agree.
"""

from backend.datasets.services.auto_grade_service import (
    GRIPPER_INDICES,
    _episode_severity,
)


def test_gripper_indices_match_cycle_stamp_constants():
    from backend.datasets.services.cycle_stamp_service import (
        LEFT_GRIPPER_IDX,
        RIGHT_GRIPPER_IDX,
    )

    assert f"[{LEFT_GRIPPER_IDX}]" in GRIPPER_INDICES
    assert f"[{RIGHT_GRIPPER_IDX}]" in GRIPPER_INDICES


def test_gripper_joint_severe_divergence_is_excluded():
    # A severe (>30%) sustained band on joint [7] — gripper.
    # Without exclusion this would appear in the severity list.
    obs = [0.0] * 20
    act = [0.0] * 20
    for i in range(2, 12):
        act[i] = 0.5  # 50% of the 1.0 range, 10-frame run → severe

    observations = {"observation.state[7]": obs}
    actions = {"action[7]": act}

    sev = _episode_severity(observations, actions)
    assert sev == []


def test_non_gripper_severe_divergence_is_kept():
    obs = [0.0] * 20
    act = [0.0] * 20
    for i in range(2, 12):
        act[i] = 0.5

    observations = {"observation.state[3]": obs}
    actions = {"action[3]": act}

    sev = _episode_severity(observations, actions)
    assert len(sev) == 1
    assert sev[0]["joint"] == "[3]"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auto_grade_gripper_exclusion.py -v`
Expected: FAIL — `ImportError: cannot import name 'GRIPPER_INDICES'` on the first test.

- [ ] **Step 3: Modify `backend/datasets/services/auto_grade_service.py`**

Add the constant near the top of the file (below the existing `MIN_SEVERE_RUN = 5` tuning constants at line 26):

```python
# Gripper joints are open/close actuators whose action↔state lag produces
# transient spikes that are not data-quality problems. Skip them when
# classifying severity. Indices mirror cycle_stamp_service.LEFT_GRIPPER_IDX
# (7) and RIGHT_GRIPPER_IDX (15); unify_key maps observation.state[N] /
# action[N] → '[N]'.
GRIPPER_INDICES = frozenset({"[7]", "[15]"})
```

Then in `_episode_severity` (currently at lines 130-157), insert the skip after the `name = unify_key(k)` line and before `act = act_by_name.get(name)`:

```python
    for k, obs in observations.items():
        name = unify_key(k)
        if name in GRIPPER_INDICES:
            continue
        act = act_by_name.get(name)
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auto_grade_gripper_exclusion.py -v`
Expected: PASS (all three tests).

- [ ] **Step 5: Run full auto-grade test suite to check for regressions**

Run: `pytest tests/test_auto_grade_bands.py tests/test_auto_grade_service.py tests/test_auto_grade_gripper_exclusion.py -v`
Expected: PASS (all tests).

- [ ] **Step 6: Commit**

```bash
git add tests/test_auto_grade_gripper_exclusion.py backend/datasets/services/auto_grade_service.py
git commit -m "feat(auto-grade): exclude gripper joints [7] and [15] from severity"
```

---

## Task 3: Frontend helpers — gripper detection + shared y-range + time formatting

**Files:**
- Create: `frontend/src/components/scalarChartHelpers.ts`
- Create: `frontend/tests/scalarChartHelpers.test.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/scalarChartHelpers.test.ts`:

```typescript
import {
  GRIPPER_INDICES,
  isGripperKey,
  computeSharedYRanges,
  formatSeconds,
} from '../src/components/scalarChartHelpers'

function assertEqual(actual: unknown, expected: unknown, label: string) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) {
    throw new Error(`${label}: expected ${e}, got ${a}`)
  }
}

// GRIPPER_INDICES convention — must match cycle_stamp_service.py
assertEqual([...GRIPPER_INDICES].sort(), ['[15]', '[7]'], 'GRIPPER_INDICES')

// isGripperKey uses the unified key form
assertEqual(isGripperKey('observation.state[7]'), true, 'obs gripper 7')
assertEqual(isGripperKey('action[15]'), true, 'action gripper 15')
assertEqual(isGripperKey('observation.state[3]'), false, 'non-gripper 3')
assertEqual(isGripperKey('observation.state.joint1'), false, 'named joint')

// computeSharedYRanges pairs obs and action by unified key and returns
// { min: min(obs,act), max: max(obs,act) } for each paired joint.
{
  const obs = {
    'observation.state[0]': [0, 1, 2],
    'observation.state[1]': [10, 20, 30],
  }
  const act = {
    'action[0]': [-1, 1, 3],
    'action[1]': [15, 15, 35],
  }
  const ranges = computeSharedYRanges(obs, act)
  assertEqual(ranges['[0]'], { min: -1, max: 3 }, 'shared y [0]')
  assertEqual(ranges['[1]'], { min: 10, max: 35 }, 'shared y [1]')
}

// Unpaired keys are skipped from shared-range map.
{
  const obs = { 'observation.state[0]': [1, 2, 3] }
  const act = {}
  const ranges = computeSharedYRanges(obs, act)
  assertEqual(ranges['[0]'], undefined, 'unpaired has no shared range')
}

// formatSeconds — one decimal, always trailing 's'
assertEqual(formatSeconds(0), '0.0s', 'format 0')
assertEqual(formatSeconds(15.34), '15.3s', 'format 15.34')
assertEqual(formatSeconds(undefined), '', 'format undefined → empty')

console.log('scalarChartHelpers: all assertions passed')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx tsx tests/scalarChartHelpers.test.ts`
Expected: FAIL with `Cannot find module '../src/components/scalarChartHelpers'`.

- [ ] **Step 3: Create `frontend/src/components/scalarChartHelpers.ts`**

```typescript
// Gripper joint identifiers. Must match
// backend/datasets/services/cycle_stamp_service.py:
//   LEFT_GRIPPER_IDX  = 7
//   RIGHT_GRIPPER_IDX = 15
// Divergence bands and auto-grade severity both skip these joints because
// gripper open/close lag is not a data-quality problem.
export const GRIPPER_INDICES: ReadonlySet<string> = new Set(['[7]', '[15]'])

/** Reduce an observation/action key to its pair-matching identifier. */
export function unifyKey(key: string): string {
  const idxMatch = /\[(\d+)\]$/.exec(key)
  if (idxMatch) return idxMatch[0]
  return key
    .replace(/^observation\.state\.?/, '')
    .replace(/^observation\./, '')
    .replace(/^action\.?/, '')
}

export function isGripperKey(key: string): boolean {
  return GRIPPER_INDICES.has(unifyKey(key))
}

export interface YRange {
  min: number
  max: number
}

/**
 * Pair observation and action series by unified key and return the combined
 * {min, max} across both. Used so obs and action for the same joint render at
 * the same y-scale and can be visually compared.
 */
export function computeSharedYRanges(
  observations: Record<string, number[]>,
  actions: Record<string, number[]>,
): Record<string, YRange> {
  const actByName = new Map<string, number[]>()
  for (const k of Object.keys(actions)) {
    actByName.set(unifyKey(k), actions[k])
  }

  const ranges: Record<string, YRange> = {}
  for (const k of Object.keys(observations)) {
    const name = unifyKey(k)
    const act = actByName.get(name)
    if (!act) continue
    const obs = observations[k]
    if (obs.length === 0 && act.length === 0) continue

    let min = Infinity
    let max = -Infinity
    for (const v of obs) {
      if (v < min) min = v
      if (v > max) max = v
    }
    for (const v of act) {
      if (v < min) min = v
      if (v > max) max = v
    }
    if (min === Infinity) continue
    ranges[name] = { min, max }
  }
  return ranges
}

/** "15.34" → "15.3s", undefined → "". Used for x-axis labels. */
export function formatSeconds(value: number | undefined): string {
  if (value === undefined || Number.isNaN(value)) return ''
  return `${value.toFixed(1)}s`
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx tsx tests/scalarChartHelpers.test.ts`
Expected: prints `scalarChartHelpers: all assertions passed` and exits 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/scalarChartHelpers.ts frontend/tests/scalarChartHelpers.test.ts
git commit -m "feat(curation): helpers for gripper detection and shared y-range"
```

---

## Task 4: Frontend — wire helpers into `ScalarChart.tsx` (gripper exclusion + shared y-range)

**Files:**
- Modify: `frontend/src/components/ScalarChart.tsx`

This task threads the new helpers into the existing component without adding axis labels yet. Axis rendering comes in Task 5 to keep the diff reviewable.

- [ ] **Step 1: Replace the inline `unifyKey` and add gripper-exclusion + shared y-range**

At the top of `ScalarChart.tsx`, replace the local `unifyKey` function (currently at lines 105-118) with an import from the new helpers module:

```typescript
import {
  GRIPPER_INDICES,
  isGripperKey,
  unifyKey,
  computeSharedYRanges,
  type YRange,
} from './scalarChartHelpers'
```

Delete the existing `unifyKey` function body inside `ScalarChart.tsx` (it now lives in the helpers file). If `GRIPPER_INDICES` appears unused directly in this file after the edit, drop it from the import — keep only the names actually referenced.

Extend `ScalarData`:

```typescript
interface ScalarData {
  episode_index: number
  num_frames: number
  observations: Record<string, number[]>
  actions: Record<string, number[]>
  terminal_frames?: number[]
  terminal_timestamps?: number[]
  timestamps?: number[]
}
```

- [ ] **Step 2: Skip gripper joints in `bandsByName` memo**

In the `bandsByName` `useMemo` (currently lines 289-302), skip gripper keys so their chart shows no red/yellow overlay:

```typescript
  const bandsByName = useMemo(() => {
    const map = new Map<string, RatioBand[]>()
    if (!data) return map
    const actByName = new Map<string, number[]>()
    for (const k of Object.keys(data.actions)) actByName.set(unifyKey(k), data.actions[k])
    for (const k of Object.keys(data.observations)) {
      const name = unifyKey(k)
      if (GRIPPER_INDICES.has(name)) continue
      const act = actByName.get(name)
      if (!act) continue
      const bands = computeBands(data.observations[k], act)
      if (bands.length > 0) map.set(name, bands)
    }
    return map
  }, [data])
```

Add `GRIPPER_INDICES` to the import list from Step 1.

- [ ] **Step 3: Compute a shared y-range map**

Right after the `bandsByName` memo, add:

```typescript
  const yRangeByName = useMemo<Record<string, YRange>>(() => {
    if (!data) return {}
    return computeSharedYRanges(data.observations, data.actions)
  }, [data])
```

- [ ] **Step 4: Pass `yMin`/`yMax` into `MiniChart`**

Extend `MiniChart`'s props:

```typescript
const MiniChart = memo(function MiniChart({
  label, series, color, currentFrame, collapsed, themeVersion, bands,
  yMin, yMax,
}: {
  label: string
  series: number[]
  color: string
  currentFrame: number
  collapsed: boolean
  themeVersion: number
  bands?: RatioBand[]
  yMin?: number
  yMax?: number
}) {
```

In the canvas draw effect (`useEffect` starting at line 131), replace the per-series min/max with shared-first fallback. Replace this block:

```typescript
      const min = Math.min(...series)
      const max = Math.max(...series)
      const range = max - min || 1
```

with:

```typescript
      const min = yMin ?? Math.min(...series)
      const max = yMax ?? Math.max(...series)
      const range = max - min || 1
```

Add `yMin, yMax` to the effect's dependency array.

- [ ] **Step 5: Wire the shared y-range at each `MiniChart` call site**

In the obs column (currently lines 351-365):

```typescript
            {obsKeys.map(key => {
              const name = key.replace('observation.', '').replace('state.', '')
              const shared = yRangeByName[unifyKey(key)]
              return (
                <MiniChart
                  key={key}
                  label={name}
                  series={data.observations[key]}
                  color="var(--c-blue)"
                  currentFrame={currentFrame}
                  collapsed={obsCollapsed}
                  themeVersion={themeVersion}
                  bands={bandsByName.get(unifyKey(key))}
                  yMin={shared?.min}
                  yMax={shared?.max}
                />
              )
            })}
```

Mirror the same change in the action column (currently lines 388-402), using `data.actions[key]`.

- [ ] **Step 6: Build the frontend to verify no type errors**

Run: `cd frontend && npm run build`
Expected: build succeeds with no TypeScript errors.

- [ ] **Step 7: Re-run the helpers test to confirm nothing regressed**

Run: `cd frontend && npx tsx tests/scalarChartHelpers.test.ts`
Expected: `scalarChartHelpers: all assertions passed`.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/ScalarChart.tsx
git commit -m "feat(curation): shared y-scale and gripper band exclusion in ScalarChart"
```

---

## Task 5: Frontend — y-axis min/max labels and x-axis time labels

**Files:**
- Modify: `frontend/src/components/ScalarChart.tsx`

Layout plan (confirmed during brainstorming):
- Each `chartItem` becomes a 2-column grid: `[y-labels col (28px)] [canvas]`.
- The y-labels column shows `max` (top) and `min` (bottom) in 9px mono, `var(--text-dim)`.
- An x-axis row is appended *only* on the last chart of each section (obs and action), showing `formatSeconds(timestamps[0])` on the left and `formatSeconds(timestamps[timestamps.length - 1])` on the right.

- [ ] **Step 1: Add `showXAxis` and `xStartLabel`/`xEndLabel` props to `MiniChart`**

```typescript
const MiniChart = memo(function MiniChart({
  label, series, color, currentFrame, collapsed, themeVersion, bands,
  yMin, yMax, showXAxis, xStartLabel, xEndLabel,
}: {
  label: string
  series: number[]
  color: string
  currentFrame: number
  collapsed: boolean
  themeVersion: number
  bands?: RatioBand[]
  yMin?: number
  yMax?: number
  showXAxis?: boolean
  xStartLabel?: string
  xEndLabel?: string
}) {
```

- [ ] **Step 2: Compute the y-label strings**

Inside `MiniChart`, just before the `return`:

```typescript
  const effectiveMin = yMin ?? (series.length ? Math.min(...series) : 0)
  const effectiveMax = yMax ?? (series.length ? Math.max(...series) : 0)
  const yMaxLabel = effectiveMax.toFixed(2)
  const yMinLabel = effectiveMin.toFixed(2)
```

- [ ] **Step 3: Replace the `chartItem` JSX with a grid layout including y labels and optional x axis**

Replace the current return body (lines 243-256) with:

```tsx
  return (
    <div style={chartStyles.chartItem}>
      <div style={chartStyles.chartHeader}>
        <span style={{ ...chartStyles.chartLabel, color }}>{label}</span>
        <span style={chartStyles.chartValue}>{currentVal}</span>
      </div>
      {!collapsed && (
        <>
          <div style={chartStyles.canvasRow}>
            <div style={chartStyles.yAxis} aria-hidden>
              <span style={chartStyles.yAxisLabel}>{yMaxLabel}</span>
              <span style={chartStyles.yAxisLabel}>{yMinLabel}</span>
            </div>
            <canvas ref={canvasRef} style={chartStyles.canvas} />
          </div>
          {showXAxis && (
            <div style={chartStyles.xAxisRow} aria-hidden>
              <span style={chartStyles.xAxisSpacer} />
              <div style={chartStyles.xAxis}>
                <span style={chartStyles.xAxisLabel}>{xStartLabel ?? ''}</span>
                <span style={chartStyles.xAxisLabel}>{xEndLabel ?? ''}</span>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
```

- [ ] **Step 4: Extend `chartStyles`**

Append the following entries to the `chartStyles` object at the bottom of the file (currently `scalars.tsx:410-429`), and adjust `canvas` width to fill its grid column:

```typescript
  canvasRow: {
    display: 'grid',
    gridTemplateColumns: '28px 1fr',
    alignItems: 'stretch',
    gap: '4px',
  },
  yAxis: {
    display: 'flex',
    flexDirection: 'column',
    justifyContent: 'space-between',
    alignItems: 'flex-end',
    fontSize: '9px',
    fontFamily: 'var(--font-mono)',
    color: 'var(--text-dim)' as string,
    padding: '1px 0',
  },
  yAxisLabel: { lineHeight: 1 },
  xAxisRow: {
    display: 'grid',
    gridTemplateColumns: '28px 1fr',
    gap: '4px',
    marginTop: '2px',
  },
  xAxisSpacer: {},
  xAxis: {
    display: 'flex',
    justifyContent: 'space-between',
    fontSize: '9px',
    fontFamily: 'var(--font-mono)',
    color: 'var(--text-dim)' as string,
  },
  xAxisLabel: { lineHeight: 1 },
```

`canvas` already has `width: '100%'`, so no change there.

- [ ] **Step 5: Pass `showXAxis` and timestamp labels from `ScalarChart`**

Import `formatSeconds`:

```typescript
import {
  GRIPPER_INDICES,
  unifyKey,
  computeSharedYRanges,
  formatSeconds,
  type YRange,
} from './scalarChartHelpers'
```

Inside `ScalarChart`, just before the return, derive the axis bounds:

```typescript
  const timestamps = data.timestamps ?? []
  const xStartLabel = formatSeconds(timestamps[0])
  const xEndLabel = formatSeconds(timestamps[timestamps.length - 1])
  const lastObsKey = obsKeys.length > 0 ? obsKeys[obsKeys.length - 1] : null
  const lastActKey = actKeys.length > 0 ? actKeys[actKeys.length - 1] : null
```

In the obs `.map`, add:

```typescript
                  showXAxis={!obsCollapsed && key === lastObsKey}
                  xStartLabel={xStartLabel}
                  xEndLabel={xEndLabel}
```

Mirror the same three props in the action `.map`, using `lastActKey` and `!actCollapsed`.

- [ ] **Step 6: Build the frontend to verify no type errors**

Run: `cd frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 7: Manual UI verification (follows project CLAUDE.md rule on UI changes)**

Run: `cd frontend && npm run dev` in one terminal, backend in another (per project convention).

In the browser:
1. Open the curation view, pick an episode that loads the right-panel scalar charts.
2. Confirm each mini chart shows min/max numbers to the left of the canvas (two small numbers, max on top, min on bottom).
3. Confirm the last chart in the Observation section *and* the last chart in the Action section each show `0.0s` and a `{episode_length}s` label below the canvas.
4. Confirm no other chart in either section shows the x-axis row.
5. Confirm the paired `[0]` obs and `[0]` action have identical y-label values (shared scale).
6. Confirm `[7]` and `[15]` charts render the line but show **no** red or yellow band overlays, even on episodes that previously showed them. Sanity-check a non-gripper joint still paints bands where expected.

If any check fails, fix the underlying issue before committing. Per CLAUDE.md: type checks and tests alone do not certify feature correctness — if the UI cannot be verified, say so explicitly instead of claiming success.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/ScalarChart.tsx
git commit -m "feat(curation): y/x axis labels on joint mini charts"
```

---

## Task 6: End-to-end cross-check

**Files:**
- No code changes; verification only.

- [ ] **Step 1: Run the full backend test suite**

Run: `pytest tests/ -v`
Expected: all tests pass (pre-existing + the two new test files added in Tasks 1-2).

- [ ] **Step 2: Run the frontend helper test**

Run: `cd frontend && npx tsx tests/scalarChartHelpers.test.ts && npx tsx tests/appChrome.test.ts`
Expected: both exit 0.

- [ ] **Step 3: Lint the frontend**

Run: `cd frontend && npm run lint`
Expected: no errors.

- [ ] **Step 4: Confirm build still passes**

Run: `cd frontend && npm run build`
Expected: success.

- [ ] **Step 5: Request code review**

Invoke `/oh-my-claudecode:code-reviewer` on the branch diff with focus on:
- Backend: response-shape backward compatibility (new `timestamps` key must not break existing frontend consumers).
- Frontend: render correctness when `timestamps` is absent (pre-existing backend versions or parquet files without a `timestamp` column) — the UI must degrade to empty x-axis labels, not crash.
- Gripper exclusion consistency: `GRIPPER_INDICES` constant values match between `auto_grade_service.py`, `scalarChartHelpers.ts`, and `cycle_stamp_service.py` constants.

---

## Self-Review Notes (completed)

- **Spec coverage:** timestamps added to response (Task 1) → x-axis labeling (Task 5). Shared y-scale (Tasks 3-4) → visually comparable obs vs action. Gripper exclusion applied in both frontend (Task 4) and backend severity (Task 2). x-axis only under the last chart of each section (Task 5 Step 5). y-axis to the left of canvas (Task 5 Step 3).
- **Placeholder scan:** every code step includes the actual code. No "TODO"/"similar to" references. No unreferenced helpers.
- **Type consistency:** `YRange`, `GRIPPER_INDICES`, `unifyKey`, `formatSeconds`, `computeSharedYRanges` — same identifiers across Tasks 3-5 and tests. Props `yMin`, `yMax`, `showXAxis`, `xStartLabel`, `xEndLabel` defined in Task 5 Step 1 and consumed in Step 5 call sites.
- **Scope:** Single coherent feature (axes + gripper exclusion), single spec, no unrelated refactors. Stays within `ScalarChart.tsx` and its natural helpers, plus two backend files already owning the divergence logic.
