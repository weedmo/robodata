# Rerun-only Curation Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the multi-video React player and the OpenCV-based Rerun visualization with a Rerun-only playback path driven by `rr.AssetVideo` + `rr.VideoFrameReference`, eliminating parent↔iframe drift and backend decoding cost.

**Architecture:** Backend `visualize_episode` logs an MP4 asset per camera once (static) and per-frame references along the episode timeline; the browser Rerun viewer handles decoding via WebCodecs. The frontend mounts a `RerunViewer` iframe in place of `VideoPlayer`, keeps `ScalarChart` as a static divergence overview with clickable bands that seek the Rerun time cursor. A `/api/rerun/seek/{ep}/{frame}` endpoint re-sends the blueprint with a time cursor hint.

**Tech Stack:** `rerun-sdk` 0.31.2, FastAPI, React 19, Vite, pytest + pytest-asyncio, `_FakeRR` hand-crafted spy (existing pattern in `tests/test_rerun_service.py`).

**Spec:** `docs/superpowers/specs/2026-04-21-rerun-only-curation-viewer-design.md`

---

## File Structure

**Backend (Stage 1):**
- Modify `backend/datasets/services/rerun_service.py` — replace `visualize_episode`, add `seek_episode_frame`, add `_send_blueprint`, delete `_extract_video_frames`.
- Modify `backend/datasets/routers/rerun.py` — add `POST /seek/{episode_index}/{frame}`.
- Modify `backend/core/config.py` line 36 — flip `enable_rerun` default to `True`.
- Modify `tests/test_rerun_service.py` — extend `_FakeRR`, rewrite two existing tests, add four new ones.
- Modify `tests/test_rerun_router.py` — add seek-endpoint tests.
- Modify `README.md:49` — mark `rerun-sdk` required.

**Frontend (Stage 2):**
- Create `frontend/src/utils/throttle.ts` — 250 ms leading-edge throttle.
- Modify `frontend/src/components/RerunViewer.tsx` — focus-sink wrapper, `iframeReady` tracking, `onBusyChange` callback.
- Modify `frontend/src/components/ScalarChart.tsx` — remove `currentFrame` prop & cursor, add `busy` prop, make bands clickable.
- Modify `frontend/src/components/DatasetPage.tsx` — swap `VideoPlayer` → `RerunViewer`, remove frame-coupling state + keyboard shortcuts, add focus-reclaim effect, restore terminal-chip click.

**Cleanup (Stage 3):**
- Delete `frontend/src/components/VideoPlayer.tsx`.
- Modify `AGENTS.md` — one-line playback note.

---

## Stage 1 — Backend landing (VideoPlayer still mounted)

### Task 1: Extend `_FakeRR` spy for new Rerun API surface

**Files:**
- Modify: `tests/test_rerun_service.py:13-35`

Existing `_FakeRR` covers `Clear`, `Scalar`, `Image`. New tests need `AssetVideo`, `VideoFrameReference`, `TextLog`, `TextLogLevel`, `components.VideoTimestamp`, and a `send_blueprint` method. `log()` must accept a `static` kwarg.

- [ ] **Step 1: Replace `_FakeRR` with extended spy**

```python
class _FakeComponents:
    class VideoTimestamp:
        def __init__(self, seconds: float) -> None:
            self.seconds_value = seconds

        @classmethod
        def seconds(cls, value: float) -> "_FakeComponents.VideoTimestamp":
            return cls(value)


class _FakeRR:
    def __init__(self) -> None:
        self.logged: list[tuple[str, object, dict]] = []
        self.times: list[tuple[str, int]] = []
        self.blueprints: list[object] = []
        self.components = _FakeComponents

    def log(self, entity: str, value: object, *, static: bool = False) -> None:
        self.logged.append((entity, value, {"static": static}))

    def set_time(self, timeline: str, *, sequence: int) -> None:
        self.times.append((timeline, sequence))

    def send_blueprint(self, blueprint: object) -> None:
        self.blueprints.append(blueprint)

    class Clear:
        def __init__(self, recursive: bool) -> None:
            self.recursive = recursive

    class Scalar:
        def __init__(self, value: float) -> None:
            self.value = value

    class Image:
        def __init__(self, value: object) -> None:
            self.value = value

    class AssetVideo:
        def __init__(self, path: str) -> None:
            self.path = path

    class VideoFrameReference:
        def __init__(self, timestamp: object) -> None:
            self.timestamp = timestamp

    class TextLog:
        def __init__(self, text: str, level: str | None = None) -> None:
            self.text = text
            self.level = level

    class TextLogLevel:
        WARN = "warn"
        WARNING = "warn"
```

- [ ] **Step 2: Update existing `_install_fake_rerun` to monkeypatch `rrb`**

Add below `monkeypatch.setattr(rerun_service, "rr", fake_rr)`:

```python
    fake_rrb = SimpleNamespace(
        Blueprint=lambda *args, **kwargs: {"children": args, "kw": kwargs},
        Horizontal=lambda *args, **kwargs: ("Horizontal", args, kwargs),
        Vertical=lambda *args, **kwargs: ("Vertical", args, kwargs),
        Grid=lambda *args, **kwargs: ("Grid", args, kwargs),
        Spatial2DView=lambda **kwargs: ("Spatial2DView", kwargs),
        TimeSeriesView=lambda **kwargs: ("TimeSeriesView", kwargs),
        TimePanel=lambda **kwargs: ("TimePanel", kwargs),
    )
    monkeypatch.setattr(rerun_service, "rrb", fake_rrb, raising=False)
```

- [ ] **Step 3: Run existing tests to confirm no breakage from Task 1 alone**

Run: `uv run pytest tests/test_rerun_service.py -v`
Expected: existing 2 tests still PASS (they don't use the new fakes yet).

- [ ] **Step 4: Commit**

```bash
git add tests/test_rerun_service.py
git commit -m "test(rerun): extend _FakeRR spy for AssetVideo/VideoFrameReference/blueprint"
```

---

### Task 2: Replace OpenCV decode path with AssetVideo + VideoFrameReference

**Files:**
- Modify: `tests/test_rerun_service.py` — rewrite `test_visualize_episode_starts_shared_video_at_episode_offset`; add a new `test_visualize_episode_logs_asset_video_and_frame_references`.
- Modify: `backend/datasets/services/rerun_service.py:56-74` — delete `_extract_video_frames`.
- Modify: `backend/datasets/services/rerun_service.py:125-211` — rewrite `visualize_episode` body per spec §4.1.

- [ ] **Step 1: Rewrite `test_visualize_episode_starts_shared_video_at_episode_offset` to assert AssetVideo + frame refs**

Replace the body (current asserts `extract_calls == [(video_path, 45, 3)]`) with:

```python
@pytest.mark.asyncio
async def test_visualize_episode_starts_shared_video_at_episode_offset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset"
    data_path = dataset_path / "data" / "chunk-000" / "file-000.parquet"
    video_path = dataset_path / "videos" / "observation.images.cam_top" / "chunk-000" / "file-000.mp4"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"parquet")
    video_path.write_bytes(b"mp4")

    table = pa.table({"timestamp": [0.0, 1 / 30, 2 / 30]})
    fake_rr = _FakeRR()

    _install_fake_rerun(monkeypatch, table, fake_rr)
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_episode_file_location",
        lambda _episode_index: {
            "dataset_from_index": 10,
            "dataset_to_index": 13,
            "data_chunk_index": 0,
            "data_file_index": 0,
            "videos": {
                "observation.images.cam_top": {
                    "chunk_index": 0,
                    "file_index": 0,
                    "from_timestamp": 1.5,
                    "to_timestamp": 1.6,
                }
            },
        },
    )
    monkeypatch.setattr(rerun_service.dataset_service, "get_dataset_path", lambda: str(dataset_path))
    monkeypatch.setattr(rerun_service.dataset_service, "get_info", lambda: {"fps": 30})
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_features",
        lambda: {
            "observation.images.cam_top": {
                "dtype": "video",
                "video_info": {"video.fps": 30},
            }
        },
    )

    await rerun_service.visualize_episode(12)

    asset_logs = [
        (entity, value, meta) for entity, value, meta in fake_rr.logged
        if isinstance(value, _FakeRR.AssetVideo)
    ]
    assert len(asset_logs) == 1
    entity, asset, meta = asset_logs[0]
    assert entity == "camera/observation/images/cam_top"
    assert asset.path == str(video_path)
    assert meta["static"] is True

    frame_refs = [
        (entity, value) for entity, value, _meta in fake_rr.logged
        if isinstance(value, _FakeRR.VideoFrameReference)
    ]
    assert len(frame_refs) == 3
    # Episode starts at from_timestamp=1.5s; each frame = 1/30s
    expected_ts = [1.5, 1.5 + 1 / 30, 1.5 + 2 / 30]
    actual_ts = [ref.timestamp.seconds_value for _e, ref in frame_refs]
    for a, e in zip(actual_ts, expected_ts):
        assert abs(a - e) < 1e-9
```

- [ ] **Step 2: Run the rewritten test to confirm it FAILS against current code**

Run: `uv run pytest tests/test_rerun_service.py::test_visualize_episode_starts_shared_video_at_episode_offset -v`
Expected: FAIL (current code calls `_extract_video_frames`, doesn't log `AssetVideo`).

- [ ] **Step 3: Rewrite `visualize_episode` in `backend/datasets/services/rerun_service.py`**

Replace the current body (lines 125–211 — everything after the signature and docstring) with:

```python
    ensure_rerun_ready()
    await asyncio.to_thread(_visualize_episode_sync, episode_index)


def _visualize_episode_sync(episode_index: int) -> None:
    loc = dataset_service.get_episode_file_location(episode_index)
    dataset_path = Path(dataset_service.get_dataset_path())
    dataset_info = dataset_service.get_info()
    features = dataset_service.get_features()
    dataset_fps = float(dataset_info.get("fps") or 30.0)

    from_idx = loc["dataset_from_index"]
    to_idx = loc["dataset_to_index"]
    chunk_idx = loc["data_chunk_index"]
    file_idx = loc["data_file_index"]

    rr.log("/", rr.Clear(recursive=True))

    data_path = dataset_path / f"data/chunk-{chunk_idx:03d}/file-{file_idx:03d}.parquet"
    if not data_path.exists():
        raise FileNotFoundError(f"Data parquet not found: {data_path}")

    table = pq.read_table(data_path)
    df = table.to_pydict()
    all_columns = list(df.keys())
    row_positions = _resolve_episode_rows(df, from_idx, to_idx, all_columns)
    num_frames = len(row_positions) if row_positions else max(0, to_idx - from_idx)

    image_columns: list[str] = []
    state_columns: list[str] = []
    action_columns: list[str] = []
    for col, feature in features.items():
        dtype = feature.get("dtype", "")
        if dtype in ("image", "video") or col.startswith("observation.image"):
            image_columns.append(col)
        elif col.startswith("observation.") and col in all_columns:
            state_columns.append(col)
        elif col.startswith("action") and col in all_columns:
            action_columns.append(col)

    video_features = {
        col: meta for col, meta in features.items()
        if meta.get("dtype") == "video"
    }
    for vkey, meta in video_features.items():
        vid_info = loc.get("videos", {}).get(vkey, {})
        video_start_ts = float(vid_info.get("from_timestamp") or 0.0)
        video_fps = _resolve_video_fps(meta, dataset_fps)
        video_path = dataset_path / (
            f"videos/{vkey}/chunk-{vid_info.get('chunk_index', chunk_idx):03d}"
            f"/file-{vid_info.get('file_index', file_idx):03d}.mp4"
        )
        if not video_path.exists():
            logger.warning("Video not found, skipping %s: %s", vkey, video_path)
            continue

        entity = f"camera/{vkey.replace('.', '/')}"
        rr.log(entity, rr.AssetVideo(path=str(video_path)), static=True)
        for sequence in range(num_frames):
            rr.set_time("frame", sequence=sequence)
            video_ts = video_start_ts + sequence / video_fps
            rr.log(
                entity,
                rr.VideoFrameReference(
                    timestamp=rr.components.VideoTimestamp.seconds(video_ts)
                ),
            )

    for sequence, row_position in enumerate(row_positions):
        rr.set_time("frame", sequence=sequence)
        row = {col: df[col][row_position] for col in all_columns if row_position < len(df[col])}
        _log_scalar_columns("observation", row, state_columns)
        _log_scalar_columns("action", row, action_columns)

    logger.info(
        "visualize ep=%d n_frames=%d n_cams=%d",
        episode_index, num_frames, len(video_features),
    )
```

Also add to the top of the file:
- Import `import asyncio` (remove the inline `import asyncio` at current line 127).
- Add `import rerun.blueprint as rrb` to the try/except block (line 8-17):

```python
try:
    import numpy as np
    import pyarrow.parquet as pq
    import rerun as rr
    import rerun.blueprint as rrb
    HAS_RERUN = True
except ImportError:
    HAS_RERUN = False
    rr = None  # type: ignore[assignment]
    rrb = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    pq = None  # type: ignore[assignment]
```

- [ ] **Step 4: Delete `_extract_video_frames` (lines 56-74)**

Remove the entire function body. Grep first to confirm no other callers: `grep -rn "_extract_video_frames" backend tests` should show only the function definition (now deleted) and the old test monkeypatch (remove from tests too — see step 5).

- [ ] **Step 5: Remove `_extract_video_frames` monkeypatch line from `test_visualize_episode_uses_file_local_rows_for_global_dataset_indices`**

Delete line `monkeypatch.setattr(rerun_service, "_extract_video_frames", lambda *_args, **_kwargs: [])` (currently at `tests/test_rerun_service.py:151`).

- [ ] **Step 6: Run all rerun tests to confirm GREEN**

Run: `uv run pytest tests/test_rerun_service.py -v`
Expected: both tests PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/datasets/services/rerun_service.py tests/test_rerun_service.py
git commit -m "refactor(rerun): replace OpenCV decoding with AssetVideo + VideoFrameReference"
```

---

### Task 3: Wrap handler in `asyncio.to_thread` and verify event loop stays responsive

**Files:**
- Modify: `tests/test_rerun_service.py` — add one test.

Task 2 already split into `_visualize_episode_sync` + async wrapper. This task adds the concurrency test that proves the wrapper actually yields the event loop.

- [ ] **Step 1: Add concurrency test**

Append to `tests/test_rerun_service.py`:

```python
@pytest.mark.asyncio
async def test_visualize_runs_in_thread_pool_yields_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset"
    data_path = dataset_path / "data" / "chunk-000" / "file-000.parquet"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"parquet")

    import time
    table = pa.table({"timestamp": [0.0]})
    fake_rr = _FakeRR()

    _install_fake_rerun(monkeypatch, table, fake_rr)
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_episode_file_location",
        lambda _idx: {
            "dataset_from_index": 0,
            "dataset_to_index": 1,
            "data_chunk_index": 0,
            "data_file_index": 0,
            "videos": {},
        },
    )
    monkeypatch.setattr(rerun_service.dataset_service, "get_dataset_path", lambda: str(dataset_path))
    monkeypatch.setattr(rerun_service.dataset_service, "get_info", lambda: {"fps": 30})
    monkeypatch.setattr(rerun_service.dataset_service, "get_features", lambda: {})

    # Force the sync worker to block for 200ms to prove the loop is free
    original_sync = rerun_service._visualize_episode_sync

    def slow_sync(episode_index: int) -> None:
        time.sleep(0.2)
        original_sync(episode_index)

    monkeypatch.setattr(rerun_service, "_visualize_episode_sync", slow_sync)

    async def other_task() -> str:
        await asyncio.sleep(0.05)
        return "ran"

    import asyncio
    results = await asyncio.gather(
        rerun_service.visualize_episode(0),
        other_task(),
    )
    assert results[1] == "ran"
```

- [ ] **Step 2: Run and confirm PASS (Task 2 already wrapped with to_thread)**

Run: `uv run pytest tests/test_rerun_service.py::test_visualize_runs_in_thread_pool_yields_event_loop -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_rerun_service.py
git commit -m "test(rerun): verify visualize yields event loop under asyncio.to_thread"
```

---

### Task 4: Log `is_terminal` frames as text markers

**Files:**
- Modify: `tests/test_rerun_service.py` — add test.
- Modify: `backend/datasets/services/rerun_service.py` inside `_visualize_episode_sync` — add terminal log loop.

- [ ] **Step 1: Add failing test**

Append to `tests/test_rerun_service.py`:

```python
@pytest.mark.asyncio
async def test_visualize_logs_terminal_markers_for_is_terminal_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset"
    data_path = dataset_path / "data" / "chunk-000" / "file-000.parquet"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"parquet")

    table = pa.table(
        {
            "index": [0, 1, 2, 3],
            "frame_index": [0, 1, 2, 3],
            "timestamp": [0.0, 1 / 30, 2 / 30, 3 / 30],
            "is_terminal": [False, False, True, False],
        }
    )
    fake_rr = _FakeRR()

    _install_fake_rerun(monkeypatch, table, fake_rr)
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_episode_file_location",
        lambda _idx: {
            "dataset_from_index": 0,
            "dataset_to_index": 4,
            "data_chunk_index": 0,
            "data_file_index": 0,
            "videos": {},
        },
    )
    monkeypatch.setattr(rerun_service.dataset_service, "get_dataset_path", lambda: str(dataset_path))
    monkeypatch.setattr(rerun_service.dataset_service, "get_info", lambda: {"fps": 30})
    monkeypatch.setattr(rerun_service.dataset_service, "get_features", lambda: {})

    await rerun_service.visualize_episode(0)

    terminal_logs = [
        (entity, value) for entity, value, _meta in fake_rr.logged
        if isinstance(value, _FakeRR.TextLog)
    ]
    assert len(terminal_logs) == 1
    entity, log = terminal_logs[0]
    assert entity == "markers/terminal"
    assert log.level in ("warn", "warning")
```

- [ ] **Step 2: Run and confirm FAIL**

Run: `uv run pytest tests/test_rerun_service.py::test_visualize_logs_terminal_markers_for_is_terminal_rows -v`
Expected: FAIL (no terminal logging yet).

- [ ] **Step 3: Add terminal marker loop to `_visualize_episode_sync`**

Insert after the scalar-logging loop, before the trailing `logger.info` line:

```python
    terminal_series = df.get("is_terminal") or []
    for sequence, row_position in enumerate(row_positions):
        flag = terminal_series[row_position] if row_position < len(terminal_series) else False
        if bool(flag):
            rr.set_time("frame", sequence=sequence)
            rr.log(
                "markers/terminal",
                rr.TextLog("terminal frame", level=rr.TextLogLevel.WARN),
            )
```

- [ ] **Step 4: Run and confirm PASS**

Run: `uv run pytest tests/test_rerun_service.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/datasets/services/rerun_service.py tests/test_rerun_service.py
git commit -m "feat(rerun): log is_terminal rows as warn-level text markers"
```

---

### Task 5: Send blueprint at end of visualize

**Files:**
- Modify: `backend/datasets/services/rerun_service.py` — add `_send_blueprint`, call it at end.
- Modify: `tests/test_rerun_service.py` — add test.

- [ ] **Step 1: Add failing test**

Append to `tests/test_rerun_service.py`:

```python
@pytest.mark.asyncio
async def test_visualize_sends_blueprint_with_camera_views(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset"
    data_path = dataset_path / "data" / "chunk-000" / "file-000.parquet"
    video_path = dataset_path / "videos" / "observation.images.cam_top" / "chunk-000" / "file-000.mp4"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_bytes(b"parquet")
    video_path.write_bytes(b"mp4")

    table = pa.table({"timestamp": [0.0, 1 / 30]})
    fake_rr = _FakeRR()

    _install_fake_rerun(monkeypatch, table, fake_rr)
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_episode_file_location",
        lambda _idx: {
            "dataset_from_index": 0,
            "dataset_to_index": 2,
            "data_chunk_index": 0,
            "data_file_index": 0,
            "videos": {"observation.images.cam_top": {"chunk_index": 0, "file_index": 0, "from_timestamp": 0.0}},
        },
    )
    monkeypatch.setattr(rerun_service.dataset_service, "get_dataset_path", lambda: str(dataset_path))
    monkeypatch.setattr(rerun_service.dataset_service, "get_info", lambda: {"fps": 30})
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_features",
        lambda: {"observation.images.cam_top": {"dtype": "video"}},
    )

    await rerun_service.visualize_episode(0)

    assert len(fake_rr.blueprints) == 1
```

- [ ] **Step 2: Run and confirm FAIL**

Run: `uv run pytest tests/test_rerun_service.py::test_visualize_sends_blueprint_with_camera_views -v`
Expected: FAIL.

- [ ] **Step 3: Add `_send_blueprint` function and call site**

Add near the other helpers (after `_log_scalar_columns`):

```python
def _send_blueprint(camera_keys: list[str], *, time_cursor_frame: int | None = None) -> None:
    """Send the layout blueprint for the current Rerun session.

    `time_cursor_frame` is threaded through for the seek endpoint; the exact
    blueprint field used to position the runtime cursor is verified during
    O7 spike (see spec §10). Until that lands this argument is accepted but
    not threaded into the blueprint payload, and the escalation path in
    `_seek_sync` falls back to re-logging `rr.set_time` before blueprint.
    """
    cam_views = [
        rrb.Spatial2DView(origin=f"camera/{k.replace('.', '/')}", name=k)
        for k in camera_keys
    ]
    body = rrb.Horizontal(
        rrb.Grid(*cam_views),
        rrb.Vertical(
            rrb.TimeSeriesView(origin="observation", name="observation"),
            rrb.TimeSeriesView(origin="action", name="action"),
        ),
        column_shares=[3, 1],
    )
    panel = rrb.TimePanel(state="collapsed")
    blueprint = rrb.Blueprint(body, panel)
    rr.send_blueprint(blueprint)
```

At end of `_visualize_episode_sync`, before the trailing `logger.info`:

```python
    _send_blueprint(list(video_features.keys()))
```

- [ ] **Step 4: Run and confirm PASS**

Run: `uv run pytest tests/test_rerun_service.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/datasets/services/rerun_service.py tests/test_rerun_service.py
git commit -m "feat(rerun): send blueprint with per-camera views and scalar panels"
```

---

### Task 6: Add `seek_episode_frame` service function

**Files:**
- Modify: `backend/datasets/services/rerun_service.py` — add `seek_episode_frame` + `_seek_sync`.
- Modify: `tests/test_rerun_service.py` — add test.

- [ ] **Step 1: Add failing test**

Append to `tests/test_rerun_service.py`:

```python
@pytest.mark.asyncio
async def test_seek_episode_frame_resends_blueprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    table = pa.table({"timestamp": [0.0]})
    fake_rr = _FakeRR()
    _install_fake_rerun(monkeypatch, table, fake_rr)
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_episode_file_location",
        lambda _idx: {
            "dataset_from_index": 0,
            "dataset_to_index": 1,
            "data_chunk_index": 0,
            "data_file_index": 0,
            "videos": {},
        },
    )
    monkeypatch.setattr(
        rerun_service.dataset_service,
        "get_features",
        lambda: {"observation.images.cam_top": {"dtype": "video"}},
    )

    await rerun_service.seek_episode_frame(7, 42)

    assert len(fake_rr.blueprints) == 1
```

- [ ] **Step 2: Run and confirm FAIL**

Run: `uv run pytest tests/test_rerun_service.py::test_seek_episode_frame_resends_blueprint -v`
Expected: FAIL (function does not exist).

- [ ] **Step 3: Add `seek_episode_frame` + `_seek_sync`**

Add below `visualize_episode`:

```python
async def seek_episode_frame(episode_index: int, frame: int) -> None:
    """Re-send blueprint with the time cursor pointed at the given frame."""
    ensure_rerun_ready()
    await asyncio.to_thread(_seek_sync, episode_index, frame)


def _seek_sync(episode_index: int, frame: int) -> None:
    # Resolve cameras for the requested episode so the blueprint matches the
    # current visualization. We rely on the AssetVideo logged by a prior
    # `visualize_episode` call — this function does not re-log video assets.
    dataset_service.get_episode_file_location(episode_index)  # KeyError/RuntimeError → caller
    features = dataset_service.get_features()
    camera_keys = [k for k, m in features.items() if m.get("dtype") == "video"]
    _send_blueprint(camera_keys, time_cursor_frame=frame)
```

- [ ] **Step 4: Run and confirm PASS**

Run: `uv run pytest tests/test_rerun_service.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/datasets/services/rerun_service.py tests/test_rerun_service.py
git commit -m "feat(rerun): add seek_episode_frame service resending blueprint"
```

---

### Task 7: Expose `POST /api/rerun/seek/{episode}/{frame}` endpoint

**Files:**
- Modify: `backend/datasets/routers/rerun.py`
- Modify: `tests/test_rerun_router.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_rerun_router.py`:

```python
    @pytest.mark.asyncio
    async def test_seek_returns_200_on_success(self, monkeypatch):
        app = FastAPI()
        app.include_router(rerun_router.router)
        monkeypatch.setattr(
            rerun_router.rerun_service,
            "seek_episode_frame",
            AsyncMock(return_value=None),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post("/api/rerun/seek/5/12")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_seek_rejects_negative_frame_with_400(self, monkeypatch):
        app = FastAPI()
        app.include_router(rerun_router.router)
        monkeypatch.setattr(
            rerun_router.rerun_service,
            "seek_episode_frame",
            AsyncMock(return_value=None),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post("/api/rerun/seek/5/-1")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_seek_returns_404_for_unknown_episode(self, monkeypatch):
        app = FastAPI()
        app.include_router(rerun_router.router)
        monkeypatch.setattr(
            rerun_router.rerun_service,
            "seek_episode_frame",
            AsyncMock(side_effect=KeyError("no such episode")),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post("/api/rerun/seek/9999/0")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_seek_returns_503_when_rerun_unavailable(self, monkeypatch):
        app = FastAPI()
        app.include_router(rerun_router.router)
        monkeypatch.setattr(
            rerun_router.rerun_service,
            "seek_episode_frame",
            AsyncMock(side_effect=RuntimeError("Rerun viewer is not available")),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post("/api/rerun/seek/5/0")
        assert response.status_code == 503
```

- [ ] **Step 2: Run and confirm all four FAIL**

Run: `uv run pytest tests/test_rerun_router.py -v`
Expected: 4 failures (endpoint does not exist).

- [ ] **Step 3: Add endpoint to `backend/datasets/routers/rerun.py`**

Append below `visualize_episode`:

```python
@router.post("/seek/{episode_index}/{frame}")
async def seek_to_frame(episode_index: int, frame: int):
    if frame < 0:
        raise HTTPException(status_code=400, detail="frame must be >= 0")
    try:
        await rerun_service.seek_episode_frame(episode_index, frame)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"status": "ok", "episode_index": episode_index, "frame": frame}
```

- [ ] **Step 4: Run and confirm PASS**

Run: `uv run pytest tests/test_rerun_router.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/datasets/routers/rerun.py tests/test_rerun_router.py
git commit -m "feat(rerun): add POST /api/rerun/seek/{ep}/{frame} endpoint"
```

---

### Task 8: Flip `enable_rerun` default to `True` and document in README

**Files:**
- Modify: `backend/core/config.py:36`
- Modify: `README.md:48-50`

- [ ] **Step 1: Edit `backend/core/config.py`**

Change line 36 from `enable_rerun: bool = False` to `enable_rerun: bool = True`.

- [ ] **Step 2: Run full backend test suite to confirm no test assumes default-off**

Run: `uv run pytest tests -v -x --ignore=tests/test_mockup.py`
Expected: all PASS. (If any test relies on `enable_rerun=False`, fix by monkeypatching `settings.enable_rerun=False` inside that test.)

- [ ] **Step 3: Update `README.md` install line**

Read the current block around line 48. Change `uv pip install fastapi uvicorn pyarrow pydantic-settings rerun-sdk numpy` to:

```markdown
uv pip install fastapi uvicorn pyarrow pydantic-settings rerun-sdk numpy
# Note: rerun-sdk is required — the curation UI uses the Rerun web viewer for all playback.
```

- [ ] **Step 4: Commit**

```bash
git add backend/core/config.py README.md
git commit -m "chore: enable Rerun by default and mark rerun-sdk required in README"
```

---

### Task 9: Manual verification of backend behavior (TA-1 … TA-4)

This is a checklist task; no code changes. Run before starting Stage 2.

- [ ] **Step 1: Start the server**

Run: `uv run python -m backend.main`
Expected: logs include `Rerun viewer available at http://localhost:9090`.

- [ ] **Step 2: Load a dataset and trigger visualize via curl**

```bash
# (replace dataset path as appropriate)
curl -X POST http://localhost:8001/api/datasets/load -H "Content-Type: application/json" -d '{"path": "/mnt/synology/data/data_div/2026_1/lerobot"}'
curl -X POST http://localhost:8001/api/rerun/visualize/0
```
Expected: 200 within ~1 s for a small episode; within a few seconds for a large one (first MP4 transfer). Server log shows `visualize ep=0 n_frames=... n_cams=...`.

- [ ] **Step 3: TA-4 — concurrent health call during visualize**

In a second shell during visualize:
```bash
curl -w "%{time_total}s\n" -o /dev/null -s http://localhost:8001/api/health
```
Expected: response well under 500 ms (handler is off the loop).

- [ ] **Step 4: TA-3 — measure episode-switch latency**

Click-through 10 episodes via frontend (VideoPlayer still mounted). In browser DevTools, filter `visualize` in Network tab; compute p95 manually. Record in notes.
Expected: re-switch p95 < 500 ms. First switch for a new MP4 may be higher per spec §9.

- [ ] **Step 5: TA-1 / TA-2 — Rerun viewer vs VLC**

Open `http://localhost:9090` in a separate browser window. Confirm:
- Video plays with multi-camera grid layout.
- Frame N in Rerun matches the same frame in VLC (visual check).
- 2x and 4x speed both pause cleanly with all cameras on the same frame.

- [ ] **Step 6: No code changes — no commit**

If any TA fails, open an issue and block Stage 2 until resolved. Otherwise proceed.

---

## Stage 2 — Frontend swap

### Task 10: Add throttle utility

**Files:**
- Create: `frontend/src/utils/throttle.ts`

- [ ] **Step 1: Write the util**

```typescript
// frontend/src/utils/throttle.ts
export function throttle<T extends (...args: never[]) => void>(
  fn: T,
  waitMs: number,
): (...args: Parameters<T>) => void {
  let lastCall = 0
  let pending: number | null = null
  return (...args: Parameters<T>) => {
    const now = Date.now()
    const since = now - lastCall
    if (since >= waitMs) {
      lastCall = now
      fn(...args)
    } else if (pending === null) {
      pending = window.setTimeout(() => {
        lastCall = Date.now()
        pending = null
        fn(...args)
      }, waitMs - since)
    }
  }
}
```

- [ ] **Step 2: tsc sanity check**

Run: `cd frontend && npx tsc -b`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/utils/throttle.ts
git commit -m "feat(frontend): add 250ms throttle utility for rerun seek"
```

---

### Task 11: Update `RerunViewer` with focus sink, iframeReady, onBusyChange

**Files:**
- Modify: `frontend/src/components/RerunViewer.tsx`

- [ ] **Step 1: Replace component body**

Replace the existing `RerunViewer` export with:

```tsx
import { useEffect, useState, useRef } from 'react'
import axios from 'axios'
import client from '../api/client'

interface RerunViewerProps {
  episodeIndex: number | null
  onBusyChange?: (busy: boolean) => void
}

export function RerunViewer({ episodeIndex, onBusyChange }: RerunViewerProps) {
  const [loading, setLoading] = useState(false)
  const [iframeReady, setIframeReady] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const prevIndexRef = useRef<number | null>(null)
  const rerunUrl = (
    import.meta as ImportMeta & { env?: { VITE_RERUN_URL?: string } }
  ).env?.VITE_RERUN_URL ?? 'http://localhost:9090'

  useEffect(() => {
    if (episodeIndex === null || episodeIndex === prevIndexRef.current) return
    prevIndexRef.current = episodeIndex
    setLoading(true)
    setError(null)

    client.post(`/rerun/visualize/${episodeIndex}`)
      .then(() => setLoading(false))
      .catch(err => {
        const message = axios.isAxiosError(err)
          ? (typeof err.response?.data?.detail === 'string' ? err.response.data.detail : err.message)
          : (err instanceof Error ? err.message : 'Visualization failed')
        setError(message)
        setLoading(false)
      })
  }, [episodeIndex])

  const busy = loading || !iframeReady
  useEffect(() => {
    onBusyChange?.(busy)
  }, [busy, onBusyChange])

  return (
    <div style={styles.container} tabIndex={0}>
      <div style={styles.toolbar}>
        <span style={styles.title}>Rerun Viewer</span>
        {episodeIndex !== null && (
          <span style={styles.epLabel}>Episode #{episodeIndex}</span>
        )}
        {busy && <span style={styles.loading}>Loading visualization...</span>}
        {error && <span style={styles.error}>{error}</span>}
      </div>
      <iframe
        style={styles.iframe}
        src={rerunUrl}
        title="Rerun Viewer"
        sandbox="allow-scripts allow-same-origin"
        allowFullScreen
        onLoad={() => setIframeReady(true)}
      />
    </div>
  )
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: 'flex',
    flexDirection: 'column',
    flex: 1,
    overflow: 'hidden',
    background: 'var(--panel3)',
    outline: 'none',
  },
  toolbar: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '8px 12px',
    background: 'var(--panel2)',
    borderBottom: '1px solid var(--border3)',
    flexShrink: 0,
  },
  title: {
    fontSize: '11px',
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.08em',
    color: 'var(--text-muted)',
  },
  epLabel: {
    fontSize: '12px',
    color: 'var(--interactive)',
    fontFamily: 'var(--font-mono)',
  },
  loading: {
    fontSize: '12px',
    color: 'var(--c-yellow)',
  },
  error: {
    fontSize: '12px',
    color: 'var(--c-red)',
  },
  iframe: {
    flex: 1,
    border: 'none',
    width: '100%',
  },
}
```

- [ ] **Step 2: tsc sanity check**

Run: `cd frontend && npx tsc -b`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/RerunViewer.tsx
git commit -m "feat(frontend): RerunViewer reports busy state via onBusyChange"
```

---

### Task 12: Convert `ScalarChart` to static overview with clickable bands

**Files:**
- Modify: `frontend/src/components/ScalarChart.tsx`

- [ ] **Step 1: Change `ScalarChartProps`**

Locate the `ScalarChartProps` interface (around line 14) and change:

```tsx
interface ScalarChartProps {
  episodeIndex: number | null
  busy: boolean
  onTerminalFrames?: (frames: number[], timestamps: number[]) => void
}
```

- [ ] **Step 2: Delete cursor logic**

Grep in the file for every use of `currentFrame`. Remove each one, including:
- any `<ReferenceLine>` / cursor line inside the recharts chart
- any tooltip anchored on `currentFrame`

The divergence band computation (`computeBands`, `MODERATE_RATIO`, `SEVERE_RATIO`, `MIN_SEVERE_RUN`) stays untouched.

- [ ] **Step 3: Add throttle + band click**

Near the top of the component body, add:

```tsx
import { throttle } from '../utils/throttle'

const seekThrottled = useMemo(
  () => throttle((f: number) => {
    if (episodeIndex == null || busy) return
    void client.post(`/rerun/seek/${episodeIndex}/${f}`)
  }, 250),
  [episodeIndex, busy],
)
```

Find where divergence band rectangles are rendered (the `.map(band => ...)` block). Wrap each rect so it's clickable:

```tsx
<rect
  x={xForFrame(band.start)}
  y={...}
  width={xForFrame(band.end + 1) - xForFrame(band.start)}
  height={...}
  fill={band.level === 'severe' ? 'var(--c-red-dim)' : 'var(--c-yellow-dim)'}
  style={{ cursor: busy ? 'wait' : 'pointer' }}
  onClick={() => seekThrottled(band.start)}
/>
```

(The exact prop names inside recharts `<Customized>` or SVG layer depend on the current render path — preserve whatever attributes are already there; only add `style.cursor` and `onClick`.)

- [ ] **Step 4: tsc sanity check**

Run: `cd frontend && npx tsc -b`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ScalarChart.tsx
git commit -m "feat(frontend): ScalarChart becomes static overview with clickable bands"
```

---

### Task 13: Swap `VideoPlayer` → `RerunViewer` inside `DatasetPage`

**Files:**
- Modify: `frontend/src/components/DatasetPage.tsx`

- [ ] **Step 1: Imports**

Delete:
```tsx
import { VideoPlayer, type VideoPlayerHandle } from './VideoPlayer'
```
Add:
```tsx
import { RerunViewer } from './RerunViewer'
import { throttle } from '../utils/throttle'
import client from '../api/client'
```
(Keep existing imports.)

- [ ] **Step 2: Remove frame-coupling state and refs**

Delete lines that declare:
```tsx
const [currentFrame, setCurrentFrame] = useState(0)
const videoRef = useRef<VideoPlayerHandle>(null)
```
(Keep `terminalFrames`, `terminalTimestamps`, `selectedEpisode` etc.)

Add:
```tsx
const [rerunBusy, setRerunBusy] = useState(false)
```

- [ ] **Step 3: Remove keyboard cases for video**

In the `handler` for `keydown`, delete the cases `ArrowLeft`, `ArrowRight`, ` ` (space), `q`, `Q`, `w`, `W`. Keep `ArrowUp`/`k`, `ArrowDown`/`j`, and `1`/`2`/`3`.

- [ ] **Step 4: Add focus-reclaim effect on episode change**

Near the other `useEffect` hooks:

```tsx
useEffect(() => {
  if (selectedEpisode) {
    ;(document.activeElement as HTMLElement | null)?.blur()
    document.body.focus()
  }
}, [selectedEpisode?.episode_index])
```

- [ ] **Step 5: Add seek throttle for terminal chips**

Near the other `useMemo`/`useCallback` hooks:

```tsx
const seekThrottled = useMemo(
  () => throttle((f: number) => {
    if (!selectedEpisode || rerunBusy) return
    void client.post(`/rerun/seek/${selectedEpisode.episode_index}/${f}`)
  }, 250),
  [selectedEpisode, rerunBusy],
)
```

- [ ] **Step 6: Replace `<VideoPlayer ... />` block**

Find the block that renders `<VideoPlayer ref={videoRef} ... onFrameChange={setCurrentFrame} terminalFrames={terminalFrames} />`. Replace it with:

```tsx
<RerunViewer
  episodeIndex={selectedEpisode?.episode_index ?? null}
  onBusyChange={setRerunBusy}
/>
```

- [ ] **Step 7: Update terminal chip click handler**

Find the `<button ... className="terminal-frame-chip" ...>` in the terminal-bar block. Replace its `onClick` with:

```tsx
onClick={() => {
  const f = ts != null && fps > 0 ? Math.round(ts * fps) : Number(label.replace(/^f/, ''))
  if (Number.isFinite(f)) seekThrottled(f)
}}
disabled={rerunBusy}
style={{ cursor: rerunBusy ? 'wait' : 'pointer' }}
```

(Preserve existing styling; just swap the handler.)

- [ ] **Step 8: Pass `busy` into ScalarChart**

Find the `<ScalarChart ... />` usage. Remove `currentFrame={currentFrame}`. Add `busy={rerunBusy}`.

- [ ] **Step 9: tsc sanity check**

Run: `cd frontend && npx tsc -b`
Expected: no errors.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/components/DatasetPage.tsx
git commit -m "feat(frontend): DatasetPage uses Rerun viewer and bands for navigation"
```

---

### Task 14: Manual trust verification (TA-1 … TA-5)

No code. Run after Task 13.

- [ ] **Step 1: Start backend and frontend dev**

In two shells:
```bash
uv run python -m backend.main
cd frontend && npm run dev
```

- [ ] **Step 2: Exercise the UI**

Open `http://localhost:5173`, load a dataset with ≥3 cameras, select an episode. Verify:
- Grid of N camera views + scalar panels appears in the center iframe.
- Playing at 2x then 4x, then pause, shows all cameras on the same frame (TA-2).
- Episode-switch latency is subjectively snappy (<0.5 s after the first switch).
- Clicking a red/yellow band in `ScalarChart` moves the Rerun time cursor to that frame (TA-5).
- Clicking a terminal chip jumps the cursor to that frame.
- While `rerunBusy`, band and chip clicks are disabled (cursor `wait`).
- `j/k/1/2/3` work immediately after clicking an episode in the sidebar. After interacting with the iframe, clicking the sidebar restores those shortcuts.

- [ ] **Step 3: Record measurements in scratch notes**

Capture p95 visualize latency from DevTools Network for 10 switches. If it exceeds 500 ms on re-switches (same video file), flag as a regression and do not proceed to Stage 3.

- [ ] **Step 4: No commit**

If every TA passes, continue to Stage 3. Otherwise fix and revisit before deleting `VideoPlayer`.

---

## Stage 3 — Cleanup

### Task 15: Delete `VideoPlayer.tsx`

**Files:**
- Delete: `frontend/src/components/VideoPlayer.tsx`
- Possibly modify: any lingering reference.

- [ ] **Step 1: Grep for remaining references**

Run: `grep -rn "VideoPlayer" frontend/src`
Expected: no matches. If any remain, remove them before deleting the file.

- [ ] **Step 2: Delete the file**

```bash
rm frontend/src/components/VideoPlayer.tsx
```

- [ ] **Step 3: tsc sanity check**

Run: `cd frontend && npx tsc -b`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add -A frontend/src/components/VideoPlayer.tsx
git commit -m "chore(frontend): remove VideoPlayer — replaced by RerunViewer"
```

---

### Task 16: Document the playback invariant in `AGENTS.md`

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Add one-line playback note**

Add a short section (or a bullet under an existing Playback/UI section) to `AGENTS.md`:

```markdown
## Playback

All video and multi-camera playback in the curation UI flows through the Rerun web viewer (`RerunViewer` iframe). There is no native `<video>` player. Backend uses `rr.AssetVideo` + `rr.VideoFrameReference`; do not reintroduce frame decoding in Python. See `docs/superpowers/specs/2026-04-21-rerun-only-curation-viewer-design.md`.
```

- [ ] **Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs(agents): document Rerun-only playback invariant"
```

---

## Self-review notes

Spec coverage check:
- §2 Goals 1–4 → Tasks 2, 3, 5, 10–13 (Rerun-only path, drift-free, non-blocking, ScalarChart preserved).
- §2 TA-1…TA-5 → Tasks 9 (TA-3, TA-4, TA-1/2 backend-side) and 14 (TA-2, TA-5 frontend-side).
- §4.1 visualize_episode rewrite → Tasks 2, 3, 4, 5.
- §4.2 blueprint → Task 5.
- §4.4 seek endpoint → Tasks 6, 7.
- §5 frontend changes → Tasks 10, 11, 12, 13, 15.
- §6 keyboard/focus → Task 13 step 4.
- §7 tests (backend) → Tasks 1–7. (Frontend automated tests skipped — no vitest in package.json; manual checks in Task 14.)
- §8 rollout order → Tasks grouped by Stage 1/2/3.
- §10 open items → threaded as comments + spec references inside Tasks 5 and 6 so implementer can address O6/O7 during work.

Known placeholders: `_send_blueprint`'s `time_cursor_frame` parameter is accepted but not threaded into the blueprint body in Task 5 because the exact Rerun API is O7 (spike during implementation). The docstring captures this; implementer must resolve during Task 6 (seek) and may need to iterate.
