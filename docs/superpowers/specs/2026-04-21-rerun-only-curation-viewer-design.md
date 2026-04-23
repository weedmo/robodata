# Rerun-only Curation Viewer

Date: 2026-04-21
Scope: curation UI video playback + episode visualization
Status: design (approved scope/architecture; open items flagged in §10)

## 1. Problem

Two playback pain points are undermining curator trust:

1. **Episode switch stalls in Rerun path.** `backend/datasets/services/rerun_service.py::visualize_episode` reads a full parquet, materializes it via `to_pydict`, opens each camera MP4 with OpenCV, decodes every frame into memory, then `rr.log(rr.Image)` s them one by one — all on the FastAPI event loop (only `pq.read_table` is on `asyncio.to_thread`). Long or multi-camera episodes cost seconds, during which other API responses (e.g. `/api/videos/.../stream/...` range reads on the same worker) also shake.
2. **Multi-video playback stutter at 2x/4x.** `frontend/src/components/VideoPlayer.tsx` runs N independent `<video>` elements and synchronizes secondaries by **seek** (`video.currentTime = primary.currentTime`) whenever drift exceeds 0.1 s. Seeks flush decoder state; at higher rates the drift-correct cycle kicks more often, producing visible jumps. A requestAnimationFrame loop plus `onTimeUpdate` both call `setCurrentTime`, adding per-frame React renders.

These make it impossible for a curator to distinguish a real data defect from a rendering artifact. The tool needs to be **trust-grade**: when something looks wrong, it must be the data that is wrong.

## 2. Goals & non-goals

### Goals

1. **Rerun-only viewer.** Remove `VideoPlayer` entirely. `RerunViewer` iframe is the single source of truth for video, time axis, multi-camera playback.
2. **Drift-free by construction.** All playback goes through Rerun's single engine; no parent↔iframe time synchronization layer exists, so drift bugs cannot exist.
3. **Non-blocking backend.** `visualize_episode` drops OpenCV decoding and logs `rr.AssetVideo` + `rr.VideoFrameReference` only; the browser's WebCodecs does lazy decoding. The handler runs entirely on `asyncio.to_thread`.
4. **Preserve ScalarChart divergence overview.** The red/yellow `MODERATE_RATIO`/`SEVERE_RATIO` bands are themselves a trust tool (anomaly detection). They stay in the parent UI as a static overview; click-on-band seeks the Rerun time cursor.

### Non-goals

- Fixing `VideoPlayer`'s sync (deleted instead).
- Multi-user Rerun sessions (tool remains single-user).
- Large-scale Rerun viewer UX customization (accept the default web viewer).
- Changing the existing `/api/rerun/visualize/{episode_index}` contract — implementation swap only; the POST body and return shape are unchanged.

### Trust acceptance criteria

The design succeeds only when each of these is demonstrable:

| ID | Criterion |
|---|---|
| TA-1 | A given frame N in Rerun matches the same frame N played in VLC/ffprobe within one frame of tolerance. |
| TA-2 | At 2x and 4x playback, pausing leaves all N cameras on the same frame index (no post-pause frame skew). |
| TA-3 | Episode switch p95 < 500 ms for re-switches within a session (video assets already transferred). First switch of a video file may exceed this due to MP4 byte transfer (see O2). |
| TA-4 | During `/api/rerun/visualize`, concurrent `/api/health` and `/api/videos/.../stream/...` requests respond without user-visible delay. |
| TA-5 | Clicking a divergence band in ScalarChart moves Rerun's time cursor to that frame; the scalar panel values at that cursor match the band's start position. |

## 3. Architecture

```
[episode click]
    │
    ▼
DatasetPage (React)
    ├── POST /api/rerun/visualize/{episode_index}
    │       │
    │       ▼
    │   rerun_service.visualize_episode   (asyncio.to_thread, whole body)
    │       ├── rr.log("/", Clear(recursive=True))
    │       ├── rr.log(f"camera/{k}", AssetVideo(path=...), static=True)  × N_cams
    │       ├── rr.log(f"camera/{k}", VideoFrameReference(...))           × N_frames × N_cams
    │       ├── rr.log("observation/*" | "action/*", Scalar(...))         × N_frames
    │       ├── rr.log("markers/terminal", TextLog(...))                  × N_terminal
    │       └── rr.send_blueprint(grid(cams) + timeseries)
    │
    └── RerunViewer iframe → http://localhost:9090 → WebCodecs decode
                          ← clicks to /api/rerun/seek/{ep}/{frame}
```

### Frontend layout

```
┌──────────────────────────────────────────────────────────┐
│ DatasetPage                                              │
│ ┌──────────┐ ┌─────────────────────────┐ ┌─────────────┐ │
│ │ Episode  │ │ RerunViewer (iframe)    │ │ Grade bar   │ │
│ │ List     │ │ - N cams + timeseries   │ │ Reason      │ │
│ │          │ │ - timeline + speed      │ │ ScalarChart │ │
│ │          │ │ - terminal markers      │ │ (static)    │ │
│ │          │ │                         │ │ TrimPanel   │ │
│ └──────────┘ └─────────────────────────┘ └─────────────┘ │
└──────────────────────────────────────────────────────────┘
```

- Center: `RerunViewer` replaces `VideoPlayer`.
- Right pane: existing components unchanged in purpose; `ScalarChart` loses its frame cursor but keeps divergence bands and terminal markers. `TrimPanel` is unchanged (not frame-coupled).
- Parent-level keyboard shortcuts kept: `1/2/3` (grade), `j/k`, `↑/↓` (episode nav). Removed: `Space`, `←/→`, `Q/W` (delegated to Rerun's native controls).

## 4. Backend pipeline rewrite

### 4.1 `visualize_episode`

```python
# backend/datasets/services/rerun_service.py
async def visualize_episode(episode_index: int) -> None:
    ensure_rerun_ready()
    await asyncio.to_thread(_visualize_episode_sync, episode_index)


def _visualize_episode_sync(episode_index: int) -> None:
    loc = dataset_service.get_episode_file_location(episode_index)
    dataset_path = Path(dataset_service.get_dataset_path())
    dataset_info = dataset_service.get_info()
    features = dataset_service.get_features()
    dataset_fps = float(dataset_info.get("fps") or 30.0)

    rr.log("/", rr.Clear(recursive=True))

    data_path = dataset_path / (
        f"data/chunk-{loc['data_chunk_index']:03d}"
        f"/file-{loc['data_file_index']:03d}.parquet"
    )
    if not data_path.exists():
        raise FileNotFoundError(f"Data parquet not found: {data_path}")

    table = pq.read_table(data_path)
    df = table.to_pydict()
    all_columns = list(df.keys())
    row_positions = _resolve_episode_rows(
        df, loc["dataset_from_index"], loc["dataset_to_index"], all_columns
    )
    num_frames = len(row_positions)

    video_features = {k: m for k, m in features.items() if m.get("dtype") == "video"}
    for vkey, meta in video_features.items():
        vid_info = loc.get("videos", {}).get(vkey, {})
        video_start_ts = float(vid_info.get("from_timestamp") or 0.0)
        video_fps = _resolve_video_fps(meta, dataset_fps)
        video_path = dataset_path / (
            f"videos/{vkey}/chunk-{vid_info.get('chunk_index', loc['data_chunk_index']):03d}"
            f"/file-{vid_info.get('file_index', loc['data_file_index']):03d}.mp4"
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

    image_cols, state_cols, action_cols = _classify_feature_columns(features, all_columns)
    for sequence, row_position in enumerate(row_positions):
        rr.set_time("frame", sequence=sequence)
        row = {c: df[c][row_position] for c in all_columns if row_position < len(df[c])}
        _log_scalar_columns("observation", row, state_cols)
        _log_scalar_columns("action", row, action_cols)

    terminal_series = df.get("is_terminal") or []
    for sequence, row_position in enumerate(row_positions):
        flag = terminal_series[row_position] if row_position < len(terminal_series) else False
        if bool(flag):
            rr.set_time("frame", sequence=sequence)
            rr.log(
                "markers/terminal",
                rr.TextLog("terminal frame", level=rr.TextLogLevel.WARN),
            )

    _send_blueprint(list(video_features.keys()))
    logger.info(
        "visualize ep=%d elapsed_hint n_frames=%d n_cams=%d",
        episode_index, num_frames, len(video_features),
    )
```

### 4.2 Blueprint

```python
import rerun.blueprint as rrb

def _send_blueprint(camera_keys: list[str], *, time_cursor_frame: int | None = None) -> None:
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
    # time_cursor_frame: verified API lands during O7 spike; see §10.
    blueprint = rrb.Blueprint(body, panel)
    rr.send_blueprint(blueprint)
```

### 4.3 Cost delta

| Item | Current | New |
|---|---|---|
| Video decoding | `cv2.VideoCapture` × N_cams × N_frames | None (browser WebCodecs) |
| Python memory | all frames held as `list[np.ndarray]` | None |
| Image logging | `rr.log(Image)` × N_cams × N_frames | `rr.log(VideoFrameReference)` × same count (shallow) |
| Event loop | most of handler | zero (whole body in `asyncio.to_thread`) |
| Rerun stream bytes | raw RGB for every frame | MP4 once per camera + shallow refs |

### 4.4 Seek endpoint (light path + escalation)

```python
# backend/datasets/routers/rerun.py
@router.post("/seek/{episode_index}/{frame}")
async def seek_to_frame(episode_index: int, frame: int):
    if frame < 0:
        raise HTTPException(400, "frame must be >= 0")
    try:
        await rerun_service.seek_episode_frame(episode_index, frame)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except (KeyError, ValueError) as e:
        raise HTTPException(404, str(e))
    return {"status": "ok"}
```

```python
# backend/datasets/services/rerun_service.py
async def seek_episode_frame(episode_index: int, frame: int) -> None:
    ensure_rerun_ready()
    await asyncio.to_thread(_seek_sync, episode_index, frame)


def _seek_sync(episode_index: int, frame: int) -> None:
    loc = dataset_service.get_episode_file_location(episode_index)
    features = dataset_service.get_features()
    camera_keys = [k for k, m in features.items() if m.get("dtype") == "video"]
    # Light path: re-send blueprint with initial time cursor at `frame`.
    _send_blueprint(camera_keys, time_cursor_frame=frame)
```

Escalation (only if O7 spike shows blueprint cursor is not respected at runtime): the light path is replaced with a re-send that also re-logs `rr.set_time("frame", sequence=frame)` as the final write before blueprint. Final fallback: full `visualize_episode` re-call with initial cursor in blueprint — still cheaper than the current OpenCV path.

## 5. Frontend changes

### 5.1 `DatasetPage.tsx`

Remove:
- `import { VideoPlayer, type VideoPlayerHandle }`
- `const [currentFrame, setCurrentFrame] = useState(0)`
- `const videoRef = useRef<VideoPlayerHandle>(null)`
- Keyboard cases: `Space`, `ArrowLeft`, `ArrowRight`, `q`/`Q`, `w`/`W`
- The `<VideoPlayer ... />` block in the center pane
- `onFrameChange={setCurrentFrame}` plumbing

Add:
- `import { RerunViewer } from './RerunViewer'`
- `<RerunViewer episodeIndex={selectedEpisode?.episode_index ?? null} />` in the center pane
- Focus-reclaim effect on episode change (so `j/k/1/2/3` work again after iframe stole focus):
  ```tsx
  useEffect(() => {
    if (selectedEpisode) {
      ;(document.activeElement as HTMLElement | null)?.blur()
      document.body.focus()
    }
  }, [selectedEpisode?.episode_index])
  ```
- (No `Esc`-to-return-focus handler: a cross-origin iframe captures all keydowns, so a parent-level `window.keydown` listener cannot fire while focus is inside Rerun. Focus returns via sidebar click; see §6.)

### 5.2 `RerunViewer.tsx`

- Wrap iframe in `<div tabIndex={0}>` so a focus sink exists.
- Track `iframeReady` via `onLoad`; show `busy` if either `loading` or `!iframeReady`.
- Existing `prevIndexRef` guard keeps duplicate-POST suppression.

### 5.3 `ScalarChart.tsx`

- Remove `currentFrame` prop and the cursor line it drives.
- Keep divergence bands (`MODERATE_RATIO` / `SEVERE_RATIO`) and terminal markers.
- Band becomes clickable. ScalarChart accepts a new `busy` prop from the parent (reflecting `RerunViewer` loading state) and disables clicks while busy:
  ```tsx
  const seekThrottled = useMemo(
    () => throttle((f: number) => {
      if (episodeIndex == null || busy) return
      void client.post(`/rerun/seek/${episodeIndex}/${f}`)
    }, 250),
    [episodeIndex, busy],
  )
  // <rect onClick={() => seekThrottled(band.start)} style={{ cursor: busy ? 'wait' : 'pointer' }} />
  ```
- Tooltip styling / colors unchanged.

### 5.4 Terminal chips

In `DatasetPage.tsx`, restore click handlers using the same `POST /rerun/seek` (shared throttle in a util hook). Same `busy`-gated guard as ScalarChart.

### 5.5 Deletions

| File | Action |
|---|---|
| `frontend/src/components/VideoPlayer.tsx` | delete |
| `rerun_service._extract_video_frames` | delete |
| VideoPlayer unit tests | delete |

## 6. Keyboard & focus discipline

Rerun is cross-origin (`localhost:9090` vs app origin). The parent cannot intercept keydown once focus is inside the iframe.

Rules:
- Episode switch → parent programmatically reclaims focus (effect in §5.1). Useful because episode selection from the list naturally leaves focus on the button, and the effect normalizes focus on `document.body`.
- Click on the sidebar / right pane → natural focus return. This is the primary keyboard-less path to regain parent shortcuts.
- Rerun controls (play/pause/step/speed) require clicking into the iframe.
- No `Esc` hotkey from inside iframe: cross-origin iframes do not propagate keydown to parent. Document this in the grade-bar hint ("click sidebar to return shortcuts").

This accepts a platform limitation: while the curator is interacting with Rerun, `j/k/1/2/3` don't fire until they click outside the iframe. In the usual flow (view episode → grade), this cost is low — the curator clicks an episode in the list, which itself defocuses the iframe.

## 7. Tests

### 7.1 Automated

| Layer | Test | Purpose |
|---|---|---|
| Backend unit | `tests/test_rerun_service.py::test_visualize_episode_no_cv2` | monkeypatch `cv2` to `None`; `visualize_episode` completes (OpenCV dep removed) |
| Backend unit | `test_visualize_episode_logs_asset_video` | spy `rr.log`: `AssetVideo` count = N_cams; `VideoFrameReference` count = N_frames × N_cams |
| Backend unit | `test_visualize_runs_in_thread_pool` | during `visualize_episode`, a concurrent `/api/health` call resolves promptly |
| Backend unit | `test_seek_endpoint_404_for_unknown_episode` / `test_seek_endpoint_400_for_negative_frame` | error paths on `/api/rerun/seek/...` |
| Backend unit | `test_send_blueprint_includes_time_cursor` | structural: blueprint object carries cursor value when `time_cursor_frame` given |
| Frontend unit | delete `VideoPlayer` tests |
| Frontend unit | `RerunViewer.test.tsx` | new episode prop triggers POST `/rerun/visualize`; iframe src stays stable |
| Frontend unit | `ScalarChart.test.tsx` | no `currentFrame` prop; band click POSTs `/rerun/seek` with throttle 250 ms |

### 7.2 Manual trust checklist (TA-1 … TA-5)

| ID | Method |
|---|---|
| TA-1 | Open same MP4 in VLC + Rerun; navigate to frame N in each; visually confirm ≤1 frame offset. |
| TA-2 | Play at 2x then 4x, pause, step the Rerun timeline; all cameras share one frame index. |
| TA-3 | Click 10 episodes in sequence; capture `/visualize` response times in DevTools Network; compute p95. |
| TA-4 | While `/visualize` is mid-flight, `curl /api/health` and fetch a video range — both respond promptly. |
| TA-5 | Click a severe band in ScalarChart; Rerun cursor lands on its `start` frame; scalar panel values match. |

### 7.3 Observability

- Log line in `visualize_episode`: `logger.info("visualize ep=%d elapsed=%.2fs n_frames=%d n_cams=%d", ...)`
- Same in `seek_episode_frame`
- Optional `VITE_DEBUG_RERUN=1` mirrors the same events to the browser console (out of v1 scope unless trivial).

## 8. Rollout

Stage by PR:

1. **Backend-first landing.** Rewrite `visualize_episode`, add `/rerun/seek`, flip `enable_rerun` default to `True`. Verify with external Rerun viewer at `localhost:9090` (TA-1..TA-4). `VideoPlayer` stays mounted.
2. **Frontend swap.** Mount `RerunViewer` in `DatasetPage`, remove `VideoPlayer`, convert `ScalarChart` to static mode with clickable bands, restore terminal chip click → seek, add the episode-switch focus-reclaim effect.
3. **Cleanup.** Delete `VideoPlayer.tsx`, `_extract_video_frames`, related types/tests. Update `README.md` to state `rerun-sdk` is required. One-line note in `AGENTS.md`: "Playback is always through the Rerun viewer."

Rollback: revert the Stage-2 PR. Stage-1 alone is a strict improvement (backend path faster, no UX change yet) and can be kept.

## 9. Risks & fallbacks

| Risk | Mitigation |
|---|---|
| Blueprint time-cursor API in `rerun-sdk 0.31.2` doesn't move the runtime cursor | Escalate to re-send blueprint with the final `rr.set_time` write before it. Final fallback: full `visualize_episode` re-call with initial cursor. |
| `rr.AssetVideo(path=...)` embeds full MP4 bytes via gRPC → large initial transfer per episode switch on long files; worse, multiple episodes sharing one file may re-ship the same MP4 | Measure in Stage 1. If re-shipping shows up, add a path-level dedup guard in `rerun_service` (track which `AssetVideo` paths are already logged in the current session). If byte size is the bottleneck regardless, evaluate URL-reference variants of `AssetVideo` or browser caching. Deferred decision. |
| `document.body.focus()` does not blur iframe on some browsers | Use a hidden `<div tabIndex={-1}>` sink instead. |
| User hits `/rerun/seek` before `/rerun/visualize` completes on a new episode (stale session risk) | Parent ScalarChart + terminal chips read `busy` from `RerunViewer` and block POSTs while busy (see §5.3/§5.4). |

## 10. Open items (must resolve during implementation)

| ID | Item |
|---|---|
| O1 | Exact constructor for `rr.components.VideoTimestamp.seconds(...)` in rerun-sdk 0.31.2 (API names shift across minor versions). |
| O2 | Whether `rr.AssetVideo` supports URL references instead of file bytes in this version; if yes, prefer URL. Additionally, whether Rerun deduplicates identical `AssetVideo` paths within a session (if not, add an explicit in-service cache keyed by video path). |
| O3 | Whether `rr.send_columns` can batch `VideoFrameReference` logging for a further win; defer unless trivial. |
| O4 | Multi-dim scalar auto-plotting in `TimeSeriesView` vs per-dim view overrides in blueprint. |
| O5 | Browser-specific behavior of `document.body.focus()` after iframe focus; may need hidden focus-sink `<div>`. |
| O6 | Correct symbol for warn-level text log in rerun-sdk 0.31.2 (`rr.TextLogLevel.WARN` vs `rr.TextLogLevel.WARNING` vs a string literal). Verify during O7 spike. |
| O7 | 1-hour spike on `rrb.Blueprint` / `rrb.TimePanel` / `rrb.TimeCtrl` for runtime cursor control. Outcome picks light-path vs escalation. |
| O8 | Seek click throttle = 250 ms (debounce on the ScalarChart side). |
| O9 | Seek blocked while `RerunViewer` is `busy` — implemented via `busy` prop plumbed from `RerunViewer` to `ScalarChart` and terminal chips. |

## 11. What this spec does not cover

- ScalarChart re-theming beyond "stays static, bands clickable".
- Cross-session persistence of Rerun state (single-user assumption).
- Converter-side camera sync (covered by `2026-04-20-camera-sync-redesign.md`).
