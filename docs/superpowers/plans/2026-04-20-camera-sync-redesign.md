# Camera Sync Redesign for `extract_frames`

Date: 2026-04-20
Scope: `rosbag2lerobot-svt/conversion/mcap_reader.py` + data-flow plumbing
Status: plan (approved directions marked [✓]; open items flagged)

## 1. Problem

Camera-to-camera timestamps are not aligned in converted episodes, yet downstream LeRobot consumers cannot detect the skew because output timestamps are a synthetic `i/fps` grid.

### 1.1 Root causes (verified by `/tmp/verify_camera_sync_bug.py`)

Algorithm: `mcap_reader.extract_frames` (lines 176–245) matches messages by **bag reception order**, not timestamps.

| # | Defect | Evidence (replay on stripped-down loop) |
|---|---|---|
| 1 | No cross-camera timestamp matching | 28 ms phase offset → **all 15 frames** desync by -28 ms |
| 2 | Drop-frame on non-timing camera mixes cross-cycle data | cam_right drops at cycle 3 → that frame: **\|Δ\| = 35.33 ms** (≈ full period) |
| 3 | `ts_ref += timegap` drifts from real time; cam_left 29.7 Hz vs cam_right 30 Hz | \|max\| = 33 ms, mean = -18 ms across 89 frames |
| 4 | `timing_source = camera_names[0]` (alphabetical) | configuration-free; fragile; camera timestamps carry capture+encode+transport latency |
| 5 | `data_creator.py:879` overwrites timestamps with `np.arange(N)/fps` | Skew silently erased; no downstream alert possible |

### 1.2 How LeRobot relates

LeRobot (`src/lerobot/datasets/...`) does NOT synchronize cameras — it assumes each appended frame is already aligned.
- `add_frame(...)` consumes one dict per frame; timestamps are assigned as `i/fps`.
- Validation is **read-side only**: `decode_video_frames_torchvision` enforces `|video PTS − requested ts| < tolerance_s` (default **1e-4 s = 100 µs**).
- This check detects *video encode round-trip drift*, never *input camera misalignment*.

Conclusion: alignment is 100 % upstream of LeRobot, i.e. ours to fix. LeRobot's 100 µs tolerance implicitly expects inputs that are already clean — feeding it inputs with tens of ms of camera skew is well outside the contract.

## 2. Design decisions (recommended defaults — awaiting confirmation)

| Decision | Default |
|---|---|
| `anchor_topic` | `observation` (joint_states), not a camera |
| `max_skew_ns` | `timegap // 2` (e.g. 16.67 ms at 30 Hz) |
| Failure mode | drop offending frame + counter; fail recording if drop-rate > 5 % |
| Capture timestamps in output | add per-topic column `capture.timestamp.<canonical>` to frame parquet |
| Output `timestamp` column | keep `i/fps` grid (LeRobot compat); real ts live alongside |

## 3. New algorithm

Two-pass, timestamp-based nearest-neighbour matching.

```python
def extract_frames(bag_path, config):
    # PASS 1 — bucket messages by canonical name (time-sorted by bag guarantee)
    topic_msgs = {name: [] for name in config.topic_map.values()}
    for topic, msg, t in _read_rosbag_messages(bag_path, topic_filter=set(config.topic_map)):
        canonical = config.topic_map[topic]
        topic_msgs[canonical].append((t, msg))

    anchor = config.anchor_topic  # "observation" by default
    anchor_ts = [t for t, _ in topic_msgs[anchor]]
    if len(anchor_ts) < 2:
        raise ValueError(f"anchor topic {anchor} has too few messages")

    timegap = 1_000_000_000 // config.fps
    max_skew = config.max_skew_ns

    # Frame grid anchored to real first-anchor timestamp (no drift accumulation)
    n_frames = (anchor_ts[-1] - anchor_ts[0]) // timegap + 1
    grid = [anchor_ts[0] + i * timegap for i in range(n_frames)]

    # PASS 2 — nearest-ts selection per topic per grid point
    frames, timestamps, jpeg_sizes, skew_stats = [], {...}, {...}, []
    for ft in grid:
        bundle = {}
        worst_skew = 0
        for canonical, series in topic_msgs.items():
            idx = bisect_left([t for t,_ in series], ft)
            candidates = []
            if idx > 0: candidates.append(series[idx-1])
            if idx < len(series): candidates.append(series[idx])
            t_sel, msg_sel = min(candidates, key=lambda tm: abs(tm[0] - ft))
            bundle[canonical] = (t_sel, msg_sel)
            worst_skew = max(worst_skew, abs(t_sel - ft))
        if worst_skew > max_skew:
            skew_stats.append({"frame_time": ft, "skew_ns": worst_skew, "dropped": True})
            continue
        # dispatch to image/follower/leader dicts and call build_frame(...)
        frames.append(...)
        skew_stats.append({"frame_time": ft, "skew_ns": worst_skew, "dropped": False})

    return frames, timestamps, jpeg_sizes, skew_stats
```

Notes:
- Time-sorted invariant: MCAP reader emits messages in log order; rosbag2 sorts by log timestamp. If not guaranteed for a given bag we sort in PASS 1.
- `bisect` on a parallel list of just timestamps keeps PASS 2 at `O(N log M)` per topic.
- No per-frame mutable cursor state → trivially correct for drops, drift, and phase offset.

## 4. File-level changes

| File | Change |
|---|---|
| `conversion/data_spec.py` | `Rosbag` gains `anchor_topic: str = "observation"`, `max_skew_ns: int` (computed in config builder), default `timegap // 2`. |
| `conversion/mcap_reader.py` | Replace `extract_frames` with 2-pass algorithm. `build_extraction_config` populates `anchor_topic` and `max_skew_ns`. Keep return tuple length stable; add `skew_stats` as 4th element. |
| `conversion/pipeline.py` | Accept 4-tuple; pass `skew_stats` to quality check; fail recording when `dropped_ratio > 0.05` (configurable). |
| `conversion/quality_checker.py` | Add `validate_sync(skew_stats, total_frames, target_hz)` → returns `SyncReport` with counts + distribution. Fold into `QualityReport`. |
| `conversion/data_creator.py` | Accept optional `capture_timestamps: dict[str, np.ndarray]` on `convert_episode`; write `capture.timestamp.<canonical>` columns next to `timestamp` (float64 seconds, relative to episode start). No change to existing `timestamp` column. |
| `conversion/data_converter.py` | `frames_to_episode` forwards capture ts into the episode dict. |

## 5. Tests (TDD order)

All tests live in `rosbag2lerobot-svt/test/` and monkeypatch `_read_rosbag_messages` like the existing `test_mcap_reader.py` so ROS runtime is not required.

### 5.1 Failing tests (must fail against current code; passing post-redesign)

- `test_camera_sync::test_phase_offset_rejected` — cameras with 28 ms offset → frames whose worst skew > timegap/2 are dropped; each kept frame has worst skew ≤ timegap/2.
- `test_camera_sync::test_drop_frame_does_not_smear` — one dropped cam_right → no kept frame carries a cross-cycle mismatch > max_skew; dropped_ratio ≈ 1/N.
- `test_camera_sync::test_drift_bounded` — anchor 30 Hz, cam_left 29.7 Hz → dropped_ratio ≤ expected, kept skews bounded.
- `test_camera_sync::test_anchor_observation_default` — with 3 cameras + observation, anchor is `observation` and timing_source is not camera.

### 5.2 Invariants to preserve

- Existing `test_mcap_reader::test_extract_frames_discards_pre_anchor_messages_when_timing_window_starts` — must be rewritten to express the *correct* invariant (nearest-ts, not FIFO). The old assertion `{head-anchor, left-new, obs-new, action-new}` encodes the FIFO bug and will be replaced.
- `TestE2EConversion` (`test_pipeline.py`) must still pass inside docker.

### 5.3 Quality report surface

- `test_quality_checker::test_sync_report_counts_drops` — feed synthetic `skew_stats` → counts match.

## 6. Backward compat & migration

- Older datasets already written with synthetic `timestamp` and no `capture.timestamp.*` columns: readers that look only at `timestamp` keep working.
- New column is additive; LeRobot/HF-datasets ignore unknown columns.
- A one-off rebuild of affected recordings is needed to gain real capture ts; existing files are not silently edited.

## 7. Rollout

1. Land data_spec + test scaffolding (failing tests).
2. Land 2-pass `extract_frames`; make 5.1 pass.
3. Update pipeline + quality_checker (sync gate).
4. Extend `DataCreator` with capture-ts column; wire through `data_converter`.
5. Re-run `TestE2EConversion` inside `convert-server-convert-server:latest`.
6. Convert one reference recording with known cross-camera skew, confirm `capture.timestamp.*` deltas are realistic.
7. Doc note in `rosbag2lerobot-svt/AGENTS.md` about `anchor_topic` contract.

## 8. Open items

- [ ] Confirm `anchor_topic = "observation"` as the default (vs per-profile in `configs/robots/*.yaml`).
- [ ] Confirm 5 % drop threshold (alternative: absolute count, e.g. > 3 consecutive drops).
- [ ] Decide whether `capture.timestamp.<canonical>` is absolute ns or relative-to-episode-start seconds (float64). Proposal: relative seconds, consistent with existing `timestamp`.
- [ ] Profile guidance: cells that already publish a hardware-synchronized multi-cam trigger topic could use it as `anchor_topic` for better accuracy.
