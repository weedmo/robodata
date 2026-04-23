# LeRobot v3.0 Video/Data 번들링 근본 수정 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `rosbag2lerobot-svt`의 `DataCreator._flush_batch()` 가 이전 flush로 이미 디스크에 기록된 "덜 찬" 마지막 video/data 파일을 재사용해서 v3.0 규격(`video_files_size_in_mb=200`, `data_files_size_in_mb=100`) target 크기까지 이어붙이도록 고친다. 현재는 매 flush마다 `file-XXX+1` 부터 새로 써서 auto_converter 연속 동기화 모드에서 recording 당 파일 1개씩 쌓여 1000+ 개의 조각 파일이 생성되는 현상을 제거한다.

**Architecture:** `_flush_batch()` 의 video/data 쓰기 루프 앞에, "마지막 기존 파일이 target 미만이면 확장 대상"으로 판별하는 단계를 추가한다. 확장 시에는 (1) 마지막 파일을 첫 번째 input으로 포함해서 임시 파일로 concat, (2) 완료 후 원본 위에 atomic rename, (3) 새 episode의 `from_timestamp` 를 기존 file 의 누적 frame 수로 offset. Video는 `_concat_videos_pyav` 재인코딩, data parquet 은 `pa.concat_tables` in-memory merge. 기존 episode 의 metadata row(file_index / timestamps) 는 절대 덮어쓰지 않는다 — 오직 이번 flush 의 새 segment 들만 갱신. 기존 counter-regression 테스트(`test_*_not_overwritten`) 는 고친 동작의 **반대** invariant를 검사하므로 제거/재작성한다.

**Tech Stack:** Python 3.12, `pyav` (av.open decode/encode), `pyarrow.parquet` (pq.read_table/write_table), `pyarrow.Table.concat_tables`, `pytest` (tmp_path, monkeypatch), `libsvtav1` codec.

**Scope:**
- **코드 변경**: `rosbag2lerobot-svt` 서브모듈 (별도 브랜치 `fix/v3-bundle-merge`)
- **테스트 변경**: 동일 서브모듈 `test/test_batch_flush.py`
- **상위 repo(curation-tools) 변경**: 서브모듈 포인터 bump 커밋만
- **스코프 외**: 이미 fragmented 상태인 기존 dataset 복구 (별도 `scripts/migrate_video_chunks.py` 역방향 mode — 후속 plan)

**Performance note (후속 개선):** `_concat_videos_pyav` 는 decode + re-encode 를 수행. 같은 파일을 N번 flush 하면 O(N²) encoding + 세대 손실이 발생. 본 plan 은 정확성 우선으로 재인코딩 경로를 사용하고, stream-copy remux 최적화는 이번 plan 이 merge 된 후 follow-up 으로 분리한다.

---

## File Structure

### 수정 (서브모듈)
- `rosbag2lerobot-svt/conversion/data_creator.py`
  - **추가 helper**: `DataCreator._count_existing_video_frames(feat_key, chunk_idx, file_idx) -> int` (기존 episodes parquet 에서 `max(videos/{feat_key}/to_timestamp) * fps` 계산)
  - **추가 helper**: `DataCreator._last_underfilled_file(video_dir, max_bytes) -> tuple[int, int, int] | None` (file_idx, size, frames — video 전용)
  - **리팩터**: `_flush_batch` step 2 (videos) — 마지막 파일이 under-target 이면 concat via 임시 파일 → atomic replace; 그렇지 않으면 기존 `last+1` 경로 유지
  - **리팩터**: `_flush_batch` step 3 (data parquet) — 마지막 parquet 이 under-target 이면 `pa.concat_tables([existing, new_batch])` 후 atomic replace
- `rosbag2lerobot-svt/test/test_batch_flush.py`
  - **삭제**: `TestMultiFlushFileIntegrity.test_video_files_not_overwritten` (신규 동작의 반대 invariant)
  - **삭제**: `TestMultiFlushFileIntegrity.test_data_parquets_not_overwritten` (동일 이유)
  - **추가**: 신규 테스트 6개 (Task 1, 2, 3, 4, 5, 6 에서 작성)

### 수정 (상위 repo)
- `.gitmodules` 계열: 서브모듈 커밋 포인터만 업데이트 (`git submodule update` 결과)

---

## 조사 결과 요약 (Phase 1)

| 항목 | 증거 |
|---|---|
| 증상 | `cell002/HZ_seqpick_deodorant` 의 `videos/observation.images.cam_head/chunk-000/` 에 959개 파일, file-000..003 은 ~180MB (정상 번들), file-004..958 은 각 10~16MB (recording 1개당 1개) |
| 동일 패턴 | `data/chunk-000/` 에 956개 parquet, file-000 은 5.8MB, 나머지는 130~200KB |
| 근본 원인 | `conversion/data_creator.py:618-625` — `file_idx_offset = int(last_name.split("-")[1]) + 1` 로 매 flush 마다 새 파일 생성. 덜 찬 마지막 파일 재사용 로직 없음 |
| 구조적 원인 | `auto_converter.convert_task()` 가 recording 들어올 때마다 `finalize() → _flush_batch()` 호출. 단일 flush 에 segment 1개만 들어오면 번들링 로직 무의미 |
| 스스로 수정 불가능 | `_flush_batch` 내 번들 로직(lines 608-616) 은 "이번 flush 의 segment들끼리만" 그룹핑. 디스크의 기존 file-XXX.mp4 에 접근하지 않음 |

---

## Task Breakdown

각 Task 는 TDD cycle(failing test → minimal impl → passing test → commit) 로 구성. 서브모듈 내부 커맨드는 `git -C rosbag2lerobot-svt ...` 로 실행.

**사전 준비 (Task 0)**

- [ ] **Step 1: 서브모듈에 작업 브랜치 생성**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
git fetch origin
git checkout -b fix/v3-bundle-merge origin/main
```

- [ ] **Step 2: 현재 전체 테스트가 green 인지 baseline 확인**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py -x -q
```

기대: 모든 테스트 PASS. (실패 시 중단하고 원인 먼저 보고)

---

### Task 1: Video — 덜 찬 마지막 파일 확장 (핵심 TDD cycle)

**Files:**
- Modify: `rosbag2lerobot-svt/conversion/data_creator.py` (`_flush_batch` 의 video 섹션, lines 590-657)
- Create: 새 메서드 `DataCreator._count_existing_video_frames`
- Test: `rosbag2lerobot-svt/test/test_batch_flush.py` (신규 클래스 `TestVideoFileMerge`)

- [ ] **Step 1: Write the failing test**

`test/test_batch_flush.py` 파일의 `TestMultiFlushFileIntegrity` 클래스 **바로 위**에 다음 클래스를 추가한다:

```python
class TestVideoFileMerge:
    """Underfilled last video file must be extended on next flush (v3.0 bundling)."""

    def test_video_underfilled_file_is_extended(self, tmp_path):
        """Two single-episode flushes produce ONE video file with both episodes,
        not two separate files, because the first file is under target size."""
        creator = _make_creator(tmp_path)
        try:
            ep0 = _make_episode(n_frames=10, serial="MERGE_V000")
            creator.convert_episode(ep0, custom_metadata={"Serial_number": "MERGE_V000"})
            creator._flush_batch()

            video_dir = (
                Path(creator.root)
                / "videos"
                / "observation.images.cam0"
                / "chunk-000"
            )
            after_first = sorted(video_dir.glob("file-*.mp4"))
            assert [f.name for f in after_first] == ["file-000.mp4"]
            first_size = after_first[0].stat().st_size

            ep1 = _make_episode(n_frames=15, serial="MERGE_V001")
            creator.convert_episode(ep1, custom_metadata={"Serial_number": "MERGE_V001"})
            creator._flush_batch()

            after_second = sorted(video_dir.glob("file-*.mp4"))
            assert [f.name for f in after_second] == ["file-000.mp4"], (
                f"Underfilled file should be extended; got {[f.name for f in after_second]}"
            )
            combined_size = after_second[0].stat().st_size
            assert combined_size > first_size, (
                "Merged file must be larger than the original first-flush output"
            )

            import av as av_mod
            with av_mod.open(str(after_second[0])) as container:
                decoded = [f for f in container.decode(video=0)]
            assert len(decoded) == 25, (
                f"Merged file must contain 10+15=25 frames, got {len(decoded)}"
            )
        finally:
            creator.close()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py::TestVideoFileMerge::test_video_underfilled_file_is_extended -v
```

기대: FAIL — `after_second` 가 `['file-000.mp4', 'file-001.mp4']` 로 나와 assert 실패.

- [ ] **Step 3: Write minimal implementation**

`conversion/data_creator.py` 에서 `_flush_batch` 바로 위(line 573 `_should_flush` 다음)에 helper 추가:

```python
    def _count_existing_video_frames(
        self, feat_key: str, chunk_idx: int, file_idx: int,
    ) -> int:
        """Return frame count already present in the target video file.

        Reads episodes parquet(s) under meta/episodes/ and computes
        max(videos/{feat_key}/to_timestamp) * fps for rows whose
        (chunk_index, file_index) match the given file. Returns 0 when
        no such rows exist yet (i.e. file is fresh or missing metadata).
        """
        episodes_dir = Path(self.root) / "meta" / "episodes"
        if not episodes_dir.is_dir():
            return 0
        ci_col = f"videos/{feat_key}/chunk_index"
        fi_col = f"videos/{feat_key}/file_index"
        to_col = f"videos/{feat_key}/to_timestamp"
        max_to = 0.0
        for pq_path in episodes_dir.rglob("*.parquet"):
            try:
                table = pq.read_table(pq_path)
            except Exception:
                continue
            cols = set(table.column_names)
            if not {ci_col, fi_col, to_col}.issubset(cols):
                continue
            chunks = table.column(ci_col).to_pylist()
            files = table.column(fi_col).to_pylist()
            tos = table.column(to_col).to_pylist()
            for c, f, t in zip(chunks, files, tos):
                if c == chunk_idx and f == file_idx and t is not None:
                    if float(t) > max_to:
                        max_to = float(t)
        return int(round(max_to * self.fps))
```

`_flush_batch` 의 video 섹션 (현재 lines 602-657) 을 아래로 완전히 교체:

```python
        # --- 2. Write video segments to final files ---
        for chunk_idx in sorted(self._video_segments.keys()):
            for feat_key, segments in self._video_segments[chunk_idx].items():
                if not segments:
                    continue

                video_dir = root / f"videos/{feat_key}/chunk-{chunk_idx:03d}"

                # Decide whether to extend the last existing file.
                extend_last = False
                base_file_idx = 0
                existing_base_size = 0
                existing_base_frames = 0
                existing_video_path: Path | None = None
                if video_dir.is_dir():
                    existing_vids = sorted(video_dir.glob("file-*.mp4"))
                    if existing_vids:
                        last_vid = existing_vids[-1]
                        last_size = last_vid.stat().st_size
                        last_idx = int(last_vid.stem.split("-")[1])
                        if last_size < video_max_bytes:
                            extend_last = True
                            base_file_idx = last_idx
                            existing_base_size = last_size
                            existing_base_frames = self._count_existing_video_frames(
                                feat_key, chunk_idx, last_idx,
                            )
                            existing_video_path = last_vid
                        else:
                            base_file_idx = last_idx + 1

                # Group new segments into files by size, seeded with existing size
                # when extending the last file.
                file_groups: List[List[dict]] = [[]]
                current_size = existing_base_size if extend_last else 0
                for seg in segments:
                    seg_size = seg["temp_path"].stat().st_size if seg["temp_path"].exists() else 0
                    if current_size + seg_size > video_max_bytes and file_groups[-1]:
                        file_groups.append([])
                        current_size = 0
                    file_groups[-1].append(seg)
                    current_size += seg_size

                # Write each group.
                for rel_idx, group in enumerate(file_groups):
                    file_idx = base_file_idx + rel_idx
                    accumulated_frames = existing_base_frames if (rel_idx == 0 and extend_last) else 0
                    for seg in group:
                        row = ep_meta_lookup.get(seg["episode_index"])
                        if row:
                            row[f"videos/{feat_key}/chunk_index"] = chunk_idx
                            row[f"videos/{feat_key}/file_index"] = file_idx
                            row[f"videos/{feat_key}/from_timestamp"] = float(accumulated_frames) / self.fps
                            accumulated_frames += seg["frame_count"]
                            row[f"videos/{feat_key}/to_timestamp"] = float(accumulated_frames) / self.fps

                    final_path = root / _VIDEO_PATH.format(
                        video_key=feat_key, chunk_index=chunk_idx, file_index=file_idx,
                    )
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    temp_paths = [seg["temp_path"] for seg in group]

                    if rel_idx == 0 and extend_last and existing_video_path is not None:
                        # Concat existing file + new segments into a temp output,
                        # then atomically replace the original.
                        tmp_out = final_path.with_suffix(".mp4.partial")
                        if tmp_out.exists():
                            tmp_out.unlink()
                        _concat_videos_pyav(
                            [existing_video_path, *temp_paths],
                            tmp_out,
                            vcodec="libsvtav1",
                            fps=self.fps,
                        )
                        os.replace(str(tmp_out), str(final_path))
                    elif len(temp_paths) == 1:
                        shutil.move(str(temp_paths[0]), str(final_path))
                    else:
                        _concat_videos_pyav(
                            temp_paths, final_path, vcodec="libsvtav1", fps=self.fps,
                        )
                    ensure_file_is_readable(final_path)

                    for tmp in temp_paths:
                        try:
                            tmp.unlink(missing_ok=True)
                        except OSError:
                            pass

        self._video_segments.clear()
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py::TestVideoFileMerge::test_video_underfilled_file_is_extended -v
```

기대: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
git add conversion/data_creator.py test/test_batch_flush.py
git commit -m "fix: extend underfilled last video file instead of creating new one

_flush_batch now reuses the last file-XXX.mp4 when it is smaller than
video_files_size_in_mb, concatenating new segments into it atomically.
Without this, auto_converter incremental cycles produced 1 tiny mp4 per
episode instead of honoring the v3.0 200MB bundling target."
```

---

### Task 2: Video — target 이상인 파일은 새 파일로 분리됨을 보장

**Files:**
- Test: `rosbag2lerobot-svt/test/test_batch_flush.py` (`TestVideoFileMerge` 에 케이스 추가)

- [ ] **Step 1: Write the test**

`TestVideoFileMerge` 클래스 안에 다음 테스트를 추가:

```python
    def test_video_full_file_triggers_new_file(self, tmp_path):
        """When last file size >= video_files_size_in_mb, next flush starts
        a fresh file-(last+1)."""
        creator = _make_creator(tmp_path)
        try:
            ep0 = _make_episode(n_frames=10, serial="FULL_V000")
            creator.convert_episode(ep0, custom_metadata={"Serial_number": "FULL_V000"})
            creator._flush_batch()

            # Mark the target size as zero so file-000.mp4 counts as "full".
            creator._info["video_files_size_in_mb"] = 0

            ep1 = _make_episode(n_frames=10, serial="FULL_V001")
            creator.convert_episode(ep1, custom_metadata={"Serial_number": "FULL_V001"})
            creator._flush_batch()

            video_dir = (
                Path(creator.root)
                / "videos"
                / "observation.images.cam0"
                / "chunk-000"
            )
            files = sorted(video_dir.glob("file-*.mp4"))
            assert [f.name for f in files] == ["file-000.mp4", "file-001.mp4"]
        finally:
            creator.close()
```

- [ ] **Step 2: Run the test to verify it passes without further code changes**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py::TestVideoFileMerge::test_video_full_file_triggers_new_file -v
```

기대: PASS — Task 1 구현의 `if last_size < video_max_bytes` 분기가 false 가 되어 기존 `last_idx + 1` 경로를 그대로 타기 때문에 코드 변경 없이 통과.

- [ ] **Step 3: Commit**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
git add test/test_batch_flush.py
git commit -m "test: cover the 'full file → new file' branch of video bundling"
```

---

### Task 3: Video — 확장된 파일의 timestamp 연속성 검증 + 레거시 테스트 제거

**Files:**
- Test: `rosbag2lerobot-svt/test/test_batch_flush.py`

- [ ] **Step 1: Add timestamp continuity test**

`TestVideoFileMerge` 클래스에 추가:

```python
    def test_video_from_timestamp_continues_after_merge(self, tmp_path):
        """After merging a new episode into an underfilled file, the new
        episode's from_timestamp equals (existing frames) / fps."""
        creator = _make_creator(tmp_path)
        try:
            ep0 = _make_episode(n_frames=10, serial="TS_V000")
            creator.convert_episode(ep0, custom_metadata={"Serial_number": "TS_V000"})
            creator._flush_batch()

            ep1 = _make_episode(n_frames=15, serial="TS_V001")
            creator.convert_episode(ep1, custom_metadata={"Serial_number": "TS_V001"})
            creator._flush_batch()

            ep_pq = (
                Path(creator.root)
                / "meta"
                / "episodes"
                / "chunk-000"
                / "file-000.parquet"
            )
            tbl = pq.read_table(ep_pq)
            rows = tbl.to_pylist()
            by_index = {r["episode_index"]: r for r in rows}

            # fps = 10 per _make_creator; ep0 10 frames → [0.0, 1.0]
            # ep1 merged after ep0 → [1.0, 2.5]
            assert by_index[0]["videos/observation.images.cam0/file_index"] == 0
            assert by_index[0]["videos/observation.images.cam0/from_timestamp"] == pytest.approx(0.0)
            assert by_index[0]["videos/observation.images.cam0/to_timestamp"] == pytest.approx(1.0)

            assert by_index[1]["videos/observation.images.cam0/file_index"] == 0
            assert by_index[1]["videos/observation.images.cam0/from_timestamp"] == pytest.approx(1.0)
            assert by_index[1]["videos/observation.images.cam0/to_timestamp"] == pytest.approx(2.5)
        finally:
            creator.close()
```

- [ ] **Step 2: Remove the obsolete anti-invariant test**

`test/test_batch_flush.py` 의 `TestMultiFlushFileIntegrity` 클래스에서 `test_video_files_not_overwritten` 메서드(현재 파일의 289-309 라인 부근) 를 **통째로 삭제**한다. 이 테스트는 "2회 flush 후 2개의 영상 파일이 생성되어야 한다" 는 invariant 를 검증했으며, 수정 동작과 정반대이다.

삭제 후 클래스 내 첫 메서드는 `test_video_files_are_world_readable_after_flush` 가 되어야 한다.

- [ ] **Step 3: Run the affected test file end-to-end**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py -v
```

기대: 모든 테스트 PASS. `test_video_files_not_overwritten` 은 더 이상 존재하지 않고, 새 `TestVideoFileMerge` 3개가 green.

- [ ] **Step 4: Commit**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
git add test/test_batch_flush.py
git commit -m "test: replace video-overwrite invariant with bundle-merge cases

Drop test_video_files_not_overwritten — it enforced the old broken
behavior of creating one file per flush. Add from_timestamp continuity
check that exercises ep_meta_lookup offset across a merge."
```

---

### Task 4: Data parquet — 덜 찬 마지막 parquet 확장 (핵심 TDD cycle)

**Files:**
- Modify: `rosbag2lerobot-svt/conversion/data_creator.py` (`_flush_batch` 의 data parquet 섹션, 현재 lines 659-696)
- Test: `rosbag2lerobot-svt/test/test_batch_flush.py` (신규 클래스 `TestDataParquetMerge`)

- [ ] **Step 1: Write the failing test**

`TestVideoFileMerge` 클래스 아래에 추가:

```python
class TestDataParquetMerge:
    """Underfilled last data parquet must be extended on next flush (v3.0 bundling)."""

    def test_data_parquet_underfilled_file_is_extended(self, tmp_path):
        """Two single-episode flushes produce ONE data parquet with both
        episodes' rows, not two separate files."""
        creator = _make_creator(tmp_path)
        try:
            ep0 = _make_episode(n_frames=10, serial="MERGE_D000")
            creator.convert_episode(ep0, custom_metadata={"Serial_number": "MERGE_D000"})
            creator._flush_batch()

            data_dir = Path(creator.root) / "data" / "chunk-000"
            after_first = sorted(data_dir.glob("file-*.parquet"))
            assert [f.name for f in after_first] == ["file-000.parquet"]

            ep1 = _make_episode(n_frames=15, serial="MERGE_D001")
            creator.convert_episode(ep1, custom_metadata={"Serial_number": "MERGE_D001"})
            creator._flush_batch()

            after_second = sorted(data_dir.glob("file-*.parquet"))
            assert [f.name for f in after_second] == ["file-000.parquet"], (
                f"Underfilled parquet should be extended; got {[f.name for f in after_second]}"
            )

            merged = pq.read_table(after_second[0])
            assert merged.num_rows == 25, (
                f"Merged parquet must contain 10+15=25 rows, got {merged.num_rows}"
            )
            # Episode indices 0 and 1 must both be present.
            assert set(merged.column("episode_index").to_pylist()) == {0, 1}
        finally:
            creator.close()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py::TestDataParquetMerge::test_data_parquet_underfilled_file_is_extended -v
```

기대: FAIL — `after_second` 가 `['file-000.parquet', 'file-001.parquet']`.

- [ ] **Step 3: Write minimal implementation**

`conversion/data_creator.py` 의 data parquet 섹션 (현재 lines 659-696) 을 아래로 교체:

```python
        # --- 3. Write data parquet files ---
        for chunk_idx in sorted(self._data_by_chunk.keys()):
            entries = self._data_by_chunk[chunk_idx]
            file_idx = 0
            batch: List[pa.Table] = []
            batch_bytes = 0

            chunk_dir = root / f"data/chunk-{chunk_idx:03d}"
            extend_last = False
            if chunk_dir.is_dir():
                existing = sorted(chunk_dir.glob("file-*.parquet"))
                if existing:
                    last_name = existing[-1].stem  # e.g. "file-002"
                    last_idx = int(last_name.split("-")[1])
                    last_path = existing[-1]
                    last_size = last_path.stat().st_size
                    if last_size < data_max_bytes:
                        # Seed the current batch with the existing parquet so
                        # new entries extend it instead of creating file-(n+1).
                        existing_table = pq.read_table(last_path)
                        batch.append(existing_table)
                        batch_bytes = existing_table.nbytes
                        file_idx = last_idx
                        extend_last = True
                    else:
                        file_idx = last_idx + 1

            for ep_idx, tbl in entries:
                if batch_bytes + tbl.nbytes > data_max_bytes and batch:
                    merged = pa.concat_tables(batch, promote_options="default")
                    path = root / _DATA_PATH.format(chunk_index=chunk_idx, file_index=file_idx)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = path.with_suffix(".parquet.partial")
                    pq.write_table(merged, tmp_path, compression="snappy")
                    os.replace(str(tmp_path), str(path))
                    file_idx += 1
                    batch = []
                    batch_bytes = 0
                    extend_last = False  # any further files are brand new

                row = ep_meta_lookup.get(ep_idx)
                if row:
                    row["data/file_index"] = file_idx
                batch.append(tbl)
                batch_bytes += tbl.nbytes

            if batch:
                merged = pa.concat_tables(batch, promote_options="default")
                path = root / _DATA_PATH.format(chunk_index=chunk_idx, file_index=file_idx)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_suffix(".parquet.partial")
                pq.write_table(merged, tmp_path, compression="snappy")
                os.replace(str(tmp_path), str(path))

        self._data_by_chunk.clear()
```

주의:
- `pa.concat_tables(..., promote_options="default")` 를 사용해 기존/신규 테이블의 column type 불일치를 안전하게 흡수.
- 덮어쓰기는 `.parquet.partial` → `os.replace` 로 atomic 하게 처리.
- `extend_last = False` 를 batch split 지점에서 해제해야 2번째 그룹부터는 새 파일임.

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py::TestDataParquetMerge::test_data_parquet_underfilled_file_is_extended -v
```

기대: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
git add conversion/data_creator.py test/test_batch_flush.py
git commit -m "fix: extend underfilled last data parquet instead of creating new one

Same pattern as the video fix: when the last data/chunk-XXX/file-YYY.parquet
is under data_files_size_in_mb, the next _flush_batch reads it back,
concats new per-episode tables in memory, and atomically rewrites the
same file_idx — honoring the v3.0 100MB data bundling target."
```

---

### Task 5: Data parquet — full → new file 분기 검증 + 레거시 테스트 제거

**Files:**
- Test: `rosbag2lerobot-svt/test/test_batch_flush.py`

- [ ] **Step 1: Add the 'full triggers new file' test**

`TestDataParquetMerge` 클래스에 추가:

```python
    def test_data_parquet_full_file_triggers_new_file(self, tmp_path):
        """When last parquet size >= data_files_size_in_mb, next flush
        starts a fresh file-(last+1)."""
        creator = _make_creator(tmp_path)
        try:
            ep0 = _make_episode(n_frames=10, serial="FULL_D000")
            creator.convert_episode(ep0, custom_metadata={"Serial_number": "FULL_D000"})
            creator._flush_batch()

            creator._info["data_files_size_in_mb"] = 0

            ep1 = _make_episode(n_frames=10, serial="FULL_D001")
            creator.convert_episode(ep1, custom_metadata={"Serial_number": "FULL_D001"})
            creator._flush_batch()

            data_dir = Path(creator.root) / "data" / "chunk-000"
            files = sorted(data_dir.glob("file-*.parquet"))
            assert [f.name for f in files] == ["file-000.parquet", "file-001.parquet"]
        finally:
            creator.close()
```

- [ ] **Step 2: Remove the obsolete data-parquet anti-invariant test**

`test/test_batch_flush.py` 의 `TestMultiFlushFileIntegrity.test_data_parquets_not_overwritten` (현재 파일의 311-331 라인 부근) 메서드를 **통째로 삭제**한다. 비디오 쪽과 마찬가지로 이 테스트는 수정 이후 정반대 invariant 를 검사한다.

- [ ] **Step 3: Run all batch-flush tests**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py -v
```

기대: 모든 테스트 PASS. 새 `TestDataParquetMerge` 2개 green, `test_data_parquets_not_overwritten` 부재 확인.

- [ ] **Step 4: Commit**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
git add test/test_batch_flush.py
git commit -m "test: replace parquet-overwrite invariant with bundle-merge cases"
```

---

### Task 6: 통합 트리클 플러시 테스트 + 전체 회귀 실행

auto_converter 연속 동기화 모드 시나리오를 재현: 1 episode × N flush. 결과 파일 수가 (전체 frame 수 × 대략 segment byte size) / target 에 가까운지 확인.

**Files:**
- Test: `rosbag2lerobot-svt/test/test_batch_flush.py` (신규 클래스)

- [ ] **Step 1: Write the trickle-flush integration test**

`TestDataParquetMerge` 아래에 추가:

```python
class TestTrickleFlushBundling:
    """Simulate auto_converter's continuous mode: 1 episode per flush, many flushes.
    The result should be a small number of bundled files, not N separate files."""

    def test_trickle_flushes_produce_few_bundled_files(self, tmp_path):
        creator = _make_creator(tmp_path)
        try:
            n_trickle = 8
            for i in range(n_trickle):
                ep = _make_episode(n_frames=10, serial=f"TRICKLE_{i:03d}")
                creator.convert_episode(ep, custom_metadata={"Serial_number": f"TRICKLE_{i:03d}"})
                creator._flush_batch()

            video_dir = (
                Path(creator.root)
                / "videos"
                / "observation.images.cam0"
                / "chunk-000"
            )
            video_files = sorted(video_dir.glob("file-*.mp4"))
            data_dir = Path(creator.root) / "data" / "chunk-000"
            data_files = sorted(data_dir.glob("file-*.parquet"))

            # With default target sizes (200MB / 100MB) and ~120x160 random frames
            # per episode, 8 episodes easily fit under both targets → exactly 1 file
            # of each type.
            assert len(video_files) == 1, (
                f"Trickle flushes must bundle into 1 video file, got "
                f"{[f.name for f in video_files]}"
            )
            assert len(data_files) == 1, (
                f"Trickle flushes must bundle into 1 data parquet, got "
                f"{[f.name for f in data_files]}"
            )

            # All episode rows must be present in the single parquet.
            merged = pq.read_table(data_files[0])
            assert merged.num_rows == n_trickle * 10

            # All episode frames must be present in the merged video.
            import av as av_mod
            with av_mod.open(str(video_files[0])) as container:
                decoded = [f for f in container.decode(video=0)]
            assert len(decoded) == n_trickle * 10
        finally:
            creator.close()
```

- [ ] **Step 2: Run the full test suite in the submodule**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py -v
```

기대: 새로 추가된 `TestTrickleFlushBundling` 포함 모든 테스트 PASS.

- [ ] **Step 3: Run wider regression (auto_converter + batch_flush)**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
python -m pytest test/test_batch_flush.py test/test_auto_converter.py test/test_stats_json.py -v
```

기대: 모든 테스트 PASS. `test_auto_converter.py` 와 `test_stats_json.py` 가 기존 파일 수 가정에 의존했는지 확인 — 실패 시 이 step 에서 원인 파악 후 추가 수정 필요.

- [ ] **Step 4: Commit**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
git add test/test_batch_flush.py
git commit -m "test: integration test for trickle-flush bundling

Simulates the auto_converter continuous mode (1 episode × N flushes)
and asserts that the resulting dataset has a small, bundled number of
files rather than one per flush."
```

---

### Task 7: 서브모듈 원격 push + 상위 repo 포인터 bump

**Files:**
- Modify: `/home/tommoro/jm_ws/local_data_pipline/curation-tools` (서브모듈 SHA 업데이트 commit)

- [ ] **Step 1: Push the submodule branch**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools/rosbag2lerobot-svt
git push -u origin fix/v3-bundle-merge
```

기대: push 성공, 브랜치 remote 에 등록.

- [ ] **Step 2: Bump the submodule pointer in the parent repo**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools
git status --short rosbag2lerobot-svt
git add rosbag2lerobot-svt
git diff --cached rosbag2lerobot-svt
```

기대: `M rosbag2lerobot-svt` 가 staging 되어 있고, diff 는 "Subproject commit <old> -> <new>" 형식.

- [ ] **Step 3: Commit the pointer bump**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools
git commit -m "chore: bump rosbag2lerobot-svt to fix/v3-bundle-merge

Pulls in the v3.0 bundling fix that extends underfilled last video/data
files instead of creating one per flush. Resolves the 959-tiny-files
symptom in live-synced datasets like cell002/HZ_seqpick_deodorant."
```

- [ ] **Step 4: Confirm final state**

```bash
cd /home/tommoro/jm_ws/local_data_pipline/curation-tools
git log --oneline -1
git submodule status rosbag2lerobot-svt
```

기대: 최신 parent commit 에 포인터 bump 가 있고, 서브모듈 status 가 새 SHA 에 클린하게 고정됨.

---

## Self-Review Checklist (author-run)

**1. Spec coverage (Phase 1 증거 vs plan):**
- "덜 찬 마지막 파일 재사용 불가" → Task 1 (video), Task 4 (data parquet) ✓
- "file_idx_offset always last+1" → Task 1/4 의 `extend_last` 분기 ✓
- Timestamp continuity → Task 3 ✓
- Threshold 초과 시 새 파일 생성 유지 → Task 2, Task 5 ✓
- 실제 운영 시나리오(1ep × N flush) 재현 → Task 6 ✓
- 구 동작을 강제하던 회귀 테스트 제거 → Task 3 (`test_video_files_not_overwritten`), Task 5 (`test_data_parquets_not_overwritten`) ✓
- 서브모듈 + 부모 repo 동기화 → Task 7 ✓

**2. Placeholder scan:** "TBD", "similar to Task N", 빈 코드 블록 없음. 각 step 실행 명령/기대 결과 명시. ✓

**3. Type consistency:**
- helper `_count_existing_video_frames(feat_key, chunk_idx, file_idx) -> int` — Task 1 에서 정의, Task 1 본문 `_flush_batch` 에서만 호출, 동일 시그니처 ✓
- `extend_last`, `base_file_idx`, `existing_base_frames` — video / data parquet 경로에서 이름은 유사하지만 각 섹션 내 로컬 변수라 충돌 없음 ✓
- `pa.concat_tables(..., promote_options="default")` — Task 4 에서만 사용, 다른 concat 경로(episodes parquet step 4) 는 변경하지 않음 ✓

**4. Edge cases 반영 여부:**
- 첫 flush 이고 기존 파일 없음: `video_dir.is_dir()` false 분기 → `extend_last=False`, 기존 로직 유지 ✓
- `existing_video_path` 와 `final_path` 가 동일 경로 이므로 `.partial` temp 파일 via `os.replace` ✓
- `_concat_videos_pyav` 재인코딩 cost / generational loss 는 performance note 로 follow-up 명시 ✓
- `file_groups[-1]` 가 empty 인 첫 segment 는 분할 조건을 만족해도 분할하지 않는 기존 동작 유지 — 이는 "최소 1 segment 는 반드시 어딘가에 들어간다" 를 보장 ✓
- `promote_options` 는 기존/신규 테이블 column 타입 미세차 흡수용. 만약 이 옵션이 현재 pyarrow 버전과 호환되지 않으면 Task 4 Step 4 에서 실패하고 원인 보고 필요 — fallback 으로 `pa.concat_tables(batch)` 만 사용하고 schema 불일치 발생 시 기존 step 4 (episodes 병합) 의 schema unify 로직을 이식 ✓ (원칙: 실패 관찰 후 대응)

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-22-lerobot-v3-video-bundle-fix.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 각 Task 당 fresh subagent 를 dispatch, Task 간 review 로 iteration 빠름

**2. Inline Execution** — 이 세션에서 `superpowers:executing-plans` 로 batch 실행, checkpoint 에서 review

**Which approach?**
