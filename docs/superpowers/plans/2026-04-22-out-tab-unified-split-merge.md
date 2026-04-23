# Out 탭 통합 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** TrimPanel의 Split / Merge 두 탭을 단일 Out 탭으로 통합하고, destination을 입력이 아닌 picker 기반 선택 UI로 제공한다.

**Architecture:** 백엔드는 `GET /api/datasets/summary` 1개 엔드포인트만 추가해 merge 대상 메타데이터를 읽게 한다. 나머지 분기 로직(create vs merge)은 기존 `sync_selected_episodes`가 그대로 처리. 프론트엔드는 `TrimPanel.tsx` 한 파일 안에서 `MergeTab` 제거, `SplitTab` → `OutTab` 재설계, `DirectoryPicker` → `DestinationPicker` 확장, `TargetSummary` 신규.

**Tech Stack:** FastAPI + Pydantic (backend), React 18 + TypeScript (frontend), pytest-asyncio + httpx.AsyncClient (backend 테스트). 프론트엔드는 단위 테스트 인프라 부재 → 수동 QA + `tsc --noEmit` 타입 체크.

**Spec:** `docs/superpowers/specs/2026-04-22-out-tab-unified-split-merge-design.md`

---

## File Structure

| 파일 | 역할 | 변경 종류 |
|------|------|----------|
| `backend/datasets/routers/dataset_ops.py` | FastAPI 라우터 — `SummaryResponse` 모델 및 `GET /api/datasets/summary` 엔드포인트 추가 | Modify |
| `tests/test_dataset_ops_router.py` | 새 `TestDatasetSummary` 클래스 (4 케이스) | Modify |
| `frontend/src/components/TrimPanel.tsx` | `TabId` 축소, `MergeTab` 제거, `SplitTab` → `OutTab` 재설계, `DirectoryPicker` → `DestinationPicker` 확장, `TargetSummary` 신규, 필터 grade/tag 다중 선택 | Modify (파일 내부에서 모든 컴포넌트 in-line) |

프론트엔드 컴포넌트 파일 분리는 하지 않는다 — 기존 코드베이스 관행(하나의 패널 파일 내 다중 컴포넌트)을 유지한다.

---

## Task 1: Backend — summary 엔드포인트

**Files:**
- Modify: `backend/datasets/routers/dataset_ops.py` (Pydantic 모델 1개 + 라우트 1개 추가)
- Modify: `tests/test_dataset_ops_router.py` (새 `TestDatasetSummary` 클래스)

- [ ] **Step 1: 실패하는 테스트 추가 (`TestDatasetSummary` 클래스)**

`tests/test_dataset_ops_router.py` 끝부분(`TestBrowseDirs` 클래스 뒤)에 아래 블록을 **추가**한다.

```python
# ---------------------------------------------------------------------------
# GET /api/datasets/summary
# ---------------------------------------------------------------------------


class TestDatasetSummary:
    @pytest.mark.asyncio
    async def test_returns_metadata_for_lerobot_dataset(self, client, tmp_path):
        import json
        ds = tmp_path / "ds01"
        (ds / "meta").mkdir(parents=True)
        (ds / "meta" / "info.json").write_text(
            json.dumps(
                {
                    "total_episodes": 42,
                    "robot_type": "panda",
                    "fps": 30,
                    "features": {"a": {}, "b": {}, "c": {}},
                }
            )
        )

        resp = await client.get(f"/api/datasets/summary?path={ds}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == str(ds)
        assert data["total_episodes"] == 42
        assert data["robot_type"] == "panda"
        assert data["fps"] == 30
        assert data["features_count"] == 3

    @pytest.mark.asyncio
    async def test_handles_missing_optional_fields(self, client, tmp_path):
        import json
        ds = tmp_path / "ds02"
        (ds / "meta").mkdir(parents=True)
        (ds / "meta" / "info.json").write_text(json.dumps({}))

        resp = await client.get(f"/api/datasets/summary?path={ds}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_episodes"] == 0
        assert data["robot_type"] is None
        assert data["fps"] == 0
        assert data["features_count"] == 0

    @pytest.mark.asyncio
    async def test_returns_404_if_not_lerobot_dataset(self, client, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()

        resp = await client.get(f"/api/datasets/summary?path={plain}")
        assert resp.status_code == 404
        assert "Not a LeRobot dataset" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_returns_404_if_path_missing(self, client, tmp_path):
        resp = await client.get(f"/api/datasets/summary?path={tmp_path / 'nope'}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rejects_path_outside_allowed_roots(self, client):
        resp = await client.get("/api/datasets/summary?path=/etc")
        assert resp.status_code == 400
        assert "outside allowed roots" in resp.json()["detail"]
```

- [ ] **Step 2: 실패 확인**

```
python -m pytest tests/test_dataset_ops_router.py::TestDatasetSummary -v
```

기대: 5개 모두 404 또는 `AssertionError: response.status_code` 형태로 실패 (엔드포인트가 아직 없음).

- [ ] **Step 3: `SummaryResponse` Pydantic 모델 추가**

`backend/datasets/routers/dataset_ops.py`에서 `BrowseDirsResponse` 바로 아래에 추가한다.

```python
class SummaryResponse(BaseModel):
    path: str
    total_episodes: int
    robot_type: str | None
    fps: int
    features_count: int
```

- [ ] **Step 4: `GET /summary` 엔드포인트 구현**

같은 파일, `@router.get("/browse-dirs", ...)` 함수 바로 아래에 추가한다.

```python
@router.get("/summary", response_model=SummaryResponse)
async def dataset_summary(path: str = Query(..., description="Absolute dataset path to summarize")):
    """Return a small metadata summary used by the Out tab's TargetSummary."""
    import json

    resolved = Path(path).resolve()
    allowed_roots = [Path(r).resolve() for r in settings.allowed_dataset_roots]
    if not any(resolved == r or r in resolved.parents for r in allowed_roots):
        raise HTTPException(status_code=400, detail=f"Path outside allowed roots: {path}")

    info_path = resolved / "meta" / "info.json"
    if not info_path.exists():
        raise HTTPException(status_code=404, detail="Not a LeRobot dataset")

    try:
        info = json.loads(info_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read info.json: {exc}") from exc

    features = info.get("features") or {}
    return SummaryResponse(
        path=str(resolved),
        total_episodes=int(info.get("total_episodes") or 0),
        robot_type=info.get("robot_type"),
        fps=int(info.get("fps") or 0),
        features_count=len(features) if isinstance(features, dict) else 0,
    )
```

- [ ] **Step 5: 통과 확인**

```
python -m pytest tests/test_dataset_ops_router.py::TestDatasetSummary -v
```

기대: 5 passed.

- [ ] **Step 6: 인접 테스트 회귀 없음 확인**

```
python -m pytest tests/test_dataset_ops_router.py tests/test_config.py tests/test_cells_api.py -q
```

기대: 모두 pass.

- [ ] **Step 7: 커밋**

```
git add backend/datasets/routers/dataset_ops.py tests/test_dataset_ops_router.py
git commit -m "feat(backend): add GET /api/datasets/summary for Out tab merge preview

Reads meta/info.json and returns total_episodes, robot_type, fps, features_count.
Used by the Out tab's TargetSummary to show what merge target looks like.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Frontend — TrimPanel refactor (Out 탭 통합)

**Files:**
- Modify: `frontend/src/components/TrimPanel.tsx`

전체 변경을 한 커밋으로 한다. 이유: `TabId` 축소, `MergeTab` 제거, `DirectoryPicker` API 변경(`string` → `Target`), `SplitTab` → `OutTab` 재배선은 서로 얽혀 있어 중간 단계에서 TS 컴파일이 깨진다.

Diff 양이 크므로 Step 단위로 쪼개되 하나의 commit으로 묶는다.

### 컴포넌트 및 상태 타입

이 타입은 이번 Task 내 여러 Step에서 공통으로 쓰인다.

```tsx
type TargetMode = 'create' | 'merge'
type Target = { mode: TargetMode; path: string }

interface SummaryResponse {
  path: string
  total_episodes: number
  robot_type: string | null
  fps: number
  features_count: number
}
```

- [ ] **Step 1: `TabId` 타입 축소 및 탭 배열 변경**

`TrimPanel.tsx`에서 `TabId` 정의 부분을 찾아 교체한다.

찾기:
```tsx
type TabId = 'split' | 'merge' | 'delete' | 'cycles'
```

교체:
```tsx
type TabId = 'out' | 'delete' | 'cycles'
```

같은 파일 하단 `TrimPanel` 컴포넌트의 `tabs` 배열(또는 switch 블록)에서 `split`·`merge` 탭 정의를 `out` 한 개로 바꾼다. 다음은 예시 패턴 — 현재 파일 구조에 맞춰 라벨만 조정한다.

찾기:
```tsx
{tab === 'split' && <SplitTab datasetPath={datasetPath} episodes={episodes} />}
{tab === 'merge' && <MergeTab />}
```

교체:
```tsx
{tab === 'out' && <OutTab datasetPath={datasetPath} episodes={episodes} />}
```

그리고 탭 버튼 목록(보통 `const TABS: { id: TabId; label: string }[]`)에서 `split`, `merge` 항목을 `{ id: 'out', label: 'Out' }` 하나로 교체한다. `delete`, `cycles` 항목은 그대로 둔다. 초기값이 `useState<TabId>('split')`이면 `useState<TabId>('out')`으로 바꾼다.

- [ ] **Step 2: `MergeTab` 함수 전체 제거**

`function MergeTab() { ... }` 블록 전체를 파일에서 삭제한다. 내부에서만 쓰이는 state (`availableDatasets`, `selectedPaths`, `targetName` 등)도 함께 사라진다.

같은 파일 상단에 `useCallback`·`useEffect`·`useRef`가 import되어 있는데, 이 중 사용처가 MergeTab에만 있었다면 TS에러가 나므로 Step 8(타입 체크)에서 일괄 정리한다. 지금은 건드리지 않는다.

- [ ] **Step 3: 기존 `DirectoryPicker`를 `DestinationPicker`로 재설계**

파일 안의 `function DirectoryPicker(...)` 함수와 그 위의 인터페이스(`BrowseDirEntry`, `BrowseDirsResponse`)를 찾아 전체를 아래 블록으로 **교체**한다.

```tsx
interface BrowseDirEntry {
  name: string
  path: string
  is_lerobot_dataset: boolean
}

interface BrowseDirsResponse {
  path: string
  parent: string | null
  roots: string[]
  entries: BrowseDirEntry[]
}

type TargetMode = 'create' | 'merge'
type Target = { mode: TargetMode; path: string }

function formatSuggestedName(sourceDatasetName: string): string {
  const now = new Date()
  const yyyy = now.getFullYear()
  const mm = String(now.getMonth() + 1).padStart(2, '0')
  const dd = String(now.getDate()).padStart(2, '0')
  return `${sourceDatasetName}__out_${yyyy}${mm}${dd}`
}

function uniqueName(base: string, existing: Set<string>): string {
  if (!existing.has(base)) return base
  let i = 2
  while (existing.has(`${base}_${i}`)) i += 1
  return `${base}_${i}`
}

function DestinationPicker({
  sourceDatasetName,
  value,
  onChange,
  disabled,
}: {
  sourceDatasetName: string
  value: Target | null
  onChange: (t: Target | null) => void
  disabled?: boolean
}) {
  const [currentDir, setCurrentDir] = useState<string | null>(null)
  const [parent, setParent] = useState<string | null>(null)
  const [entries, setEntries] = useState<BrowseDirEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [newName, setNewName] = useState('')

  const fetchDir = useCallback(async (path: string | null) => {
    setLoading(true)
    setError(null)
    try {
      const url = path
        ? `/datasets/browse-dirs?path=${encodeURIComponent(path)}`
        : '/datasets/browse-dirs'
      const resp = await client.get<BrowseDirsResponse>(url)
      setCurrentDir(resp.data.path)
      setParent(resp.data.parent)
      setEntries(resp.data.entries)
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Failed to load directory'
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void fetchDir(null) }, [fetchDir])

  // When the user opens "Create new", seed a unique suggested name from source + today.
  useEffect(() => {
    if (!createOpen || !currentDir) return
    const existing = new Set(entries.map(e => e.name))
    const base = formatSuggestedName(sourceDatasetName)
    setNewName(uniqueName(base, existing))
  }, [createOpen, currentDir, entries, sourceDatasetName])

  const selectMergeTarget = (entry: BrowseDirEntry) => {
    onChange({ mode: 'merge', path: entry.path })
    setCreateOpen(false)
  }

  const confirmCreate = () => {
    if (!currentDir || !newName.trim()) return
    const finalName = newName.trim()
    const fullPath = `${currentDir}/${finalName}`
    onChange({ mode: 'create', path: fullPath })
  }

  const breadcrumb = currentDir ? currentDir.split('/').filter(Boolean) : []
  const nameCollides = createOpen && entries.some(e => e.name === newName.trim())

  return (
    <div style={s.pickerBox}>
      <div style={s.pickerBreadcrumb}>
        <span style={s.pickerPathRoot}>/</span>
        {breadcrumb.map((seg, i) => (
          <span key={i} style={s.pickerPathSeg}>
            {seg}
            {i < breadcrumb.length - 1 && <span style={s.pickerPathSep}>/</span>}
          </span>
        ))}
      </div>

      <div style={s.pickerList}>
        {loading && <div style={s.pickerHint}>Loading…</div>}
        {!loading && parent && (
          <button
            style={s.pickerEntry}
            onClick={() => void fetchDir(parent)}
            disabled={disabled}
            type="button"
          >
            <span style={s.pickerIcon}>↑</span>
            <span>..</span>
          </button>
        )}
        {!loading && entries.map(entry => {
          const isSelected =
            value?.mode === 'merge' && value.path === entry.path
          if (entry.is_lerobot_dataset) {
            return (
              <button
                key={entry.path}
                style={{
                  ...s.pickerEntry,
                  background: isSelected ? 'var(--interactive-dim)' : 'transparent',
                  color: isSelected ? 'var(--interactive)' : 'var(--text)',
                }}
                onClick={() => selectMergeTarget(entry)}
                disabled={disabled}
                type="button"
                title="LeRobot dataset — click to select as merge target"
              >
                <span style={s.pickerIcon}>◆</span>
                <span>{entry.name}</span>
              </button>
            )
          }
          return (
            <button
              key={entry.path}
              style={s.pickerEntry}
              onClick={() => void fetchDir(entry.path)}
              disabled={disabled}
              type="button"
            >
              <span style={s.pickerIcon}>▸</span>
              <span>{entry.name}</span>
            </button>
          )
        })}
        {!loading && entries.length === 0 && !parent && (
          <div style={s.pickerHint}>No subdirectories</div>
        )}
        {error && <div style={s.errorText}>{error}</div>}
      </div>

      {!createOpen && (
        <button
          style={s.pickerCreateBtn}
          onClick={() => setCreateOpen(true)}
          disabled={disabled || !currentDir}
          type="button"
        >
          ➕ Create new dataset here
        </button>
      )}

      {createOpen && (
        <div style={s.pickerCreateRow}>
          <span style={s.pickerNewLabel}>New dataset name</span>
          <input
            style={s.textInput}
            type="text"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            disabled={disabled}
          />
          {nameCollides && (
            <div style={s.errorText}>Name already exists in this folder</div>
          )}
          <div style={s.pickerCreateActions}>
            <button
              style={s.pickerCreateConfirm}
              onClick={confirmCreate}
              disabled={disabled || !newName.trim() || nameCollides}
              type="button"
            >
              Select
            </button>
            <button
              style={s.pickerCreateCancel}
              onClick={() => { setCreateOpen(false); setNewName('') }}
              disabled={disabled}
              type="button"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 4: `TargetSummary` 컴포넌트 추가**

`DestinationPicker` 함수 바로 아래에 추가한다.

```tsx
interface SummaryResponse {
  path: string
  total_episodes: number
  robot_type: string | null
  fps: number
  features_count: number
}

function TargetSummary({ target }: { target: Target }) {
  const [summary, setSummary] = useState<SummaryResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (target.mode !== 'merge') {
      setSummary(null)
      setError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    client
      .get<SummaryResponse>(`/datasets/summary?path=${encodeURIComponent(target.path)}`)
      .then(resp => { if (!cancelled) setSummary(resp.data) })
      .catch((err: unknown) => {
        if (cancelled) return
        const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Failed to load target summary'
        setError(msg)
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [target])

  if (target.mode === 'create') {
    return (
      <div style={s.targetSummaryBox}>
        <span style={s.targetSummaryHead}>📄 Create new dataset</span>
        <span style={s.targetSummaryPath}>{target.path}</span>
      </div>
    )
  }

  if (loading) {
    return <div style={s.targetSummaryBox}><span style={s.pickerHint}>Loading target…</span></div>
  }
  if (error) {
    return <div style={s.targetSummaryBox}><span style={s.errorText}>{error}</span></div>
  }
  if (!summary) return null

  const name = target.path.split('/').filter(Boolean).pop() ?? target.path

  return (
    <div style={s.targetSummaryBox}>
      <span style={s.targetSummaryHead}>🔗 Merge into {name}</span>
      <span style={s.targetSummaryMeta}>
        {summary.total_episodes} episodes · robot_type: {summary.robot_type ?? 'unknown'} · fps: {summary.fps}
      </span>
      <span style={s.targetSummaryHint}>중복은 Serial_number로 자동 skip됩니다</span>
    </div>
  )
}
```

- [ ] **Step 5: `SplitTab` 함수를 `OutTab`으로 교체**

기존 `function SplitTab({ datasetPath, episodes }: ...) { ... }` 블록 전체를 아래로 **교체**한다.

```tsx
function OutTab({
  datasetPath,
  episodes,
}: {
  datasetPath: string | null
  episodes: Episode[]
}) {
  const [selectedGrades, setSelectedGrades] = useState<Set<string>>(new Set())
  const [selectedTags, setSelectedTags] = useState<Set<string>>(new Set())
  const [target, setTarget] = useState<Target | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const { jobStatus, polling, startPolling, reset } = useJobPoller()

  const allTags = Array.from(new Set(episodes.flatMap(e => e.tags ?? []))).sort()
  const matchingEpisodes = episodes
    .filter(e =>
      selectedGrades.size === 0 || selectedGrades.has(e.grade ?? 'Ungraded'),
    )
    .filter(e =>
      selectedTags.size === 0 || (e.tags ?? []).some(t => selectedTags.has(t)),
    )

  const toggleGrade = (grade: string) => {
    setSelectedGrades(prev => {
      const next = new Set(prev)
      if (next.has(grade)) next.delete(grade)
      else next.add(grade)
      return next
    })
  }
  const toggleTag = (tag: string) => {
    setSelectedTags(prev => {
      const next = new Set(prev)
      if (next.has(tag)) next.delete(tag)
      else next.add(tag)
      return next
    })
  }

  const sourceDatasetName = datasetPath
    ? (datasetPath.split('/').filter(Boolean).pop() ?? 'dataset')
    : 'dataset'

  const handleSubmit = async () => {
    if (!datasetPath || !target) return
    if (matchingEpisodes.length === 0) {
      setSubmitError('No episodes match the selected filter')
      return
    }

    setSubmitting(true)
    setSubmitError(null)
    reset()

    try {
      const resp = await client.post<{ job_id: string; operation: string; status: string }>(
        '/datasets/split-into',
        {
          source_path: datasetPath,
          episode_ids: matchingEpisodes.map(e => e.episode_index).sort((a, b) => a - b),
          destination_path: target.path,
        },
      )
      startPolling(resp.data.job_id)
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Out failed'
      setSubmitError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  if (!datasetPath) {
    return <div style={s.emptyState}>Load a dataset first to run Out.</div>
  }

  const submitDisabled =
    submitting || polling || !target || matchingEpisodes.length === 0

  return (
    <div style={s.tabContent}>
      <div style={s.fieldLabel}>Grade filter</div>
      <div style={s.chipRow}>
        {GRADE_OPTIONS.map(grade => {
          const active = selectedGrades.has(grade)
          return (
            <button
              key={grade}
              style={{
                ...s.chip,
                borderColor: active ? 'var(--interactive)' : 'var(--border3)',
                color: active ? 'var(--interactive)' : 'var(--text-dim)',
                background: active ? 'var(--interactive-dim)' : 'transparent',
              }}
              onClick={() => toggleGrade(grade)}
              type="button"
            >
              {grade}
            </button>
          )
        })}
      </div>

      <div style={s.fieldLabel}>Tag filter</div>
      {allTags.length === 0 ? (
        <div style={s.empty}>No tags on episodes. Tag filter is skipped.</div>
      ) : (
        <div style={s.chipRow}>
          {allTags.map(tag => {
            const active = selectedTags.has(tag)
            return (
              <button
                key={tag}
                style={{
                  ...s.chip,
                  borderColor: active ? 'var(--interactive)' : 'var(--border3)',
                  color: active ? 'var(--interactive)' : 'var(--text-dim)',
                  background: active ? 'var(--interactive-dim)' : 'transparent',
                }}
                onClick={() => toggleTag(tag)}
                type="button"
              >
                {tag}
              </button>
            )
          })}
        </div>
      )}

      <div style={s.matchPreview}>
        <span style={{ color: matchingEpisodes.length > 0 ? 'var(--interactive)' : 'var(--text-dim)' }}>
          {matchingEpisodes.length} episode{matchingEpisodes.length !== 1 ? 's' : ''} selected
        </span>
        {matchingEpisodes.length > 0 && (
          <div style={s.matchRanges}>
            {formatEpisodeRanges(matchingEpisodes.map(e => e.episode_index))}
          </div>
        )}
      </div>

      <div style={s.fieldLabel}>Destination</div>
      <DestinationPicker
        sourceDatasetName={sourceDatasetName}
        value={target}
        onChange={setTarget}
        disabled={submitting || polling}
      />

      {target && <TargetSummary target={target} />}

      {submitError && <div style={s.errorText}>{submitError}</div>}

      {(jobStatus?.status === 'complete' || jobStatus?.status === 'completed') && jobStatus?.summary && (
        <div style={s.matchPreview}>
          <span style={{ color: 'var(--c-green)' }}>
            {jobStatus.summary.created} created, {jobStatus.summary.skipped_duplicates} skipped as duplicates
          </span>
          <span style={{ color: 'var(--text-muted)' }}>Mode: {jobStatus.summary.mode}</span>
        </div>
      )}

      <button
        style={{ ...s.actionBtn, opacity: submitDisabled ? 0.6 : 1 }}
        onClick={handleSubmit}
        disabled={submitDisabled}
        type="button"
      >
        {submitting ? 'Submitting...' : 'Run Out'}
      </button>

      <JobProgress jobStatus={jobStatus} polling={polling} />
    </div>
  )
}
```

**참고** — `GRADE_OPTIONS` 상수는 현재 `DeleteTab` 위에 이미 정의되어 있다 (`const GRADE_OPTIONS = ['good', 'normal', 'bad', 'Ungraded'] as const`). 필요하면 그 위치에서 가져다 쓴다 (모듈 스코프라 자동 접근 가능).

- [ ] **Step 6: 새 styles 추가**

`s` 객체(파일 하단 `const s: Record<string, React.CSSProperties> = { ... }`) 안에 아래 키들을 추가한다. 기존 `pickerBox`·`pickerBreadcrumb`·`pickerList`·`pickerEntry`·`pickerIcon`·`pickerHint`·`textInput`·`errorText`는 이미 있으므로 그대로 둔다. 추가할 것들:

```ts
  pickerCreateBtn: {
    background: 'transparent',
    border: '1px dashed var(--border3)',
    borderRadius: 3,
    color: 'var(--text-dim)',
    fontSize: 12,
    padding: '6px 8px',
    cursor: 'pointer',
    textAlign: 'left' as const,
  },
  pickerCreateRow: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: 4,
    padding: '6px 0 0',
  },
  pickerCreateActions: {
    display: 'flex',
    gap: 6,
  },
  pickerCreateConfirm: {
    background: 'var(--interactive)',
    border: 'none',
    borderRadius: 3,
    color: '#fff',
    fontSize: 11,
    padding: '4px 10px',
    cursor: 'pointer',
  },
  pickerCreateCancel: {
    background: 'transparent',
    border: '1px solid var(--border3)',
    borderRadius: 3,
    color: 'var(--text-dim)',
    fontSize: 11,
    padding: '4px 10px',
    cursor: 'pointer',
  },
  targetSummaryBox: {
    background: 'var(--panel2)',
    border: '1px solid var(--border2)',
    borderRadius: 4,
    padding: '8px 10px',
    display: 'flex',
    flexDirection: 'column' as const,
    gap: 3,
    fontSize: 12,
  },
  targetSummaryHead: {
    color: 'var(--interactive)',
    fontWeight: 600,
  },
  targetSummaryPath: {
    color: 'var(--text)',
    fontFamily: 'var(--font-mono)',
    fontSize: 11,
    wordBreak: 'break-all' as const,
  },
  targetSummaryMeta: {
    color: 'var(--text-muted)',
    fontSize: 11,
  },
  targetSummaryHint: {
    color: 'var(--text-dim)',
    fontSize: 10,
  },
```

기존에 이미 있던 (이전 커밋에서 추가된) `pickerResolved`, `pickerResolvedLabel`, `pickerResolvedPath`, `pickerNewRow` 스타일은 더 이상 쓰이지 않는다. 파일을 열어 세 키 모두 찾아서 **삭제**한다 (사용처 없으므로 TS 경고는 없지만 dead code 정리).

- [ ] **Step 7: `matchPreview`에서 기존 `good-only` 문구 의존성 제거 확인**

`SplitTab` 교체가 완료되었으므로 파일 내에 `goodEpisodes`, `Good Episode Filter`, `Sync Good Episodes` 같은 문자열이 남아 있지 않은지 확인한다. 남아 있으면 삭제한다 (grep).

```
grep -n "goodEpisodes\|Good Episode\|Sync Good" frontend/src/components/TrimPanel.tsx
```

기대: 출력 없음.

- [ ] **Step 8: 타입 체크**

```
cd frontend && npx tsc --noEmit -p tsconfig.app.json
```

기대: 오류 없음. 흔한 오류:
- `MergeTab` 참조가 아직 남아 있음 → Step 1 재확인.
- `TargetMode` / `Target` 타입이 중복 선언 → DestinationPicker 블록과 OutTab 사이에 타입이 한 번만 정의되도록 확인.
- `client` import 누락 → 파일 상단 import 확인 (`import client from '../api/client'`).
- 사용되지 않는 import 경고 (`useRef` 등) → 사용처 없으면 import에서 제거.

- [ ] **Step 9: 수동 QA 체크리스트 실행**

백엔드 띄우고 (`python -m backend.main` 또는 프로젝트 관행대로), 프론트 dev 서버 기동 후 브라우저에서 다음을 하나씩 확인한다.

- [ ] 탭 목록이 `Out / Delete / Cycles` 3개.
- [ ] Grade chip 다중 선택 → matching episode count 반영.
- [ ] Tag chip 다중 선택 → AND 조건 동작.
- [ ] 두 필터 모두 비우면 전체 에피소드 대상.
- [ ] Picker에서 일반 폴더(▸) 클릭 → 하위 이동.
- [ ] LeRobot 데이터셋(◆) 클릭 → 드릴-인 없이 선택되고 배경 강조.
- [ ] TargetSummary가 total_episodes / robot_type / fps 로드.
- [ ] "Create new dataset here" 클릭 → 자동 제안 이름 표시 (`<source>__out_<YYYYMMDD>`).
- [ ] 제안 이름이 이미 있으면 `_2` 등으로 suffix 부여.
- [ ] Create 모드 확정(Select) 후 TargetSummary가 "Create new dataset: <path>" 표시 (summary API 호출 없음).
- [ ] Run Out → 잡 폴링 → 완료 시 `created / skipped_duplicates` 표시.
- [ ] 의도적으로 기존 데이터셋(호환 불가한 다른 robot_type)을 target으로 선택 → 잡 failed + 에러 메시지 노출.
- [ ] 기존 Merge 탭이 UI에 존재하지 않음.

결과를 PR 설명에 스크린샷/텍스트로 첨부한다.

- [ ] **Step 10: 커밋**

```
git add frontend/src/components/TrimPanel.tsx
git commit -m "feat(ui): consolidate Split/Merge tabs into Out tab

- Single Out tab replaces Split and Merge.
- DestinationPicker: drill into plain folders, select LeRobot datasets
  as merge target, or 'Create new dataset here' with auto-suggested name
  (source__out_YYYYMMDD, auto-incremented on name collision).
- TargetSummary fetches GET /api/datasets/summary for merge targets and
  shows total_episodes / robot_type / fps.
- Filters widened: grade multi-select (good / normal / bad / Ungraded)
  AND tag multi-select.
- MergeTab (multi-dataset merge into new dataset) removed from the UI.
  Its backend endpoint /api/datasets/merge is left as dead code for a
  follow-up cleanup PR.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec 커버리지 확인:**

- 백엔드 summary 엔드포인트 + 4개(실제로는 5개) 테스트 → Task 1 ✓
- `POST /datasets/split-into` 재사용 → Task 2 Step 5 (`handleSubmit`) ✓
- `GET /datasets/browse-dirs` 재사용 + `is_lerobot_dataset`로 drill/select 구분 → Task 2 Step 3 ✓
- MergeTab 제거 / SplitTab → OutTab → Task 2 Steps 1·2·5 ✓
- DestinationPicker: drill vs select + Create new → Task 2 Step 3 ✓
- TargetSummary (merge 모드 API 호출 / create 모드 라벨만) → Task 2 Step 4 ✓
- 필터 grade AND tag, 둘 다 비우면 전체 → Task 2 Step 5 `matchingEpisodes` ✓
- 자동 제안 이름 `{source}__out_{YYYYMMDD}` + suffix → Task 2 Step 3 (`formatSuggestedName`, `uniqueName`) ✓
- 에러 처리: 400(range) / 400(self) → `submitError` + JobProgress; 404(summary) → TargetSummary error state; merge 호환 불가 → 잡 failed → `JobProgress` ✓
- 수동 QA 체크리스트 → Task 2 Step 9 ✓
- 백엔드 엔드포인트/테스트 회귀 없음 확인 → Task 1 Step 6 ✓
- dead code(`/datasets/merge`) 존치 → Task 2 Step 10 커밋 메시지에 명시 ✓

**Placeholder 스캔:** 각 Step에 실제 코드/명령/기대 출력이 있음. "TODO", "적절히", "비슷하게" 같은 표현 없음.

**Type 일관성:** `Target`, `TargetMode`, `SummaryResponse`는 Task 2 Step 3·4에서 한 번씩 정의(같은 블록 내). OutTab(Step 5)도 동일 이름 참조. `Target.path`는 항상 string 단일 필드. `onChange`는 `(t: Target | null) => void`로 통일. `sourceDatasetName`은 string 단일. 백엔드 `SummaryResponse` 필드 이름과 프론트 인터페이스 필드 이름 일치(`path`, `total_episodes`, `robot_type`, `fps`, `features_count`).

리뷰 결과 이상 없음.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-22-out-tab-unified-split-merge.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
