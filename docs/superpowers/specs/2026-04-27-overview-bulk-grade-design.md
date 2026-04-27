# Overview 탭에서 grade 카드 우클릭으로 일괄 grade 처리

**Status:** Implemented
**Date:** 2026-04-27
**Scope:** `frontend/src/components/OverviewTab.tsx` (UI/state) + `frontend/tests/` (단위 테스트)

## 배경

현재 `OverviewTab`은 length 차트와 tags 차트의 막대 우클릭 → "Mark as Bad" 컨텍스트 메뉴로만 일괄 처리가 가능하다. 상단의 `GradeSummary`(Good / Normal / Bad / Ungraded 4 카드)는 좌클릭 시 Curate 탭의 해당 grade 필터로 이동만 한다.

요구사항:
- GradeSummary 4 카드 각각에서 우클릭으로 해당 카테고리 episodes 전체를 good/normal/bad로 일괄 변경할 수 있어야 한다.
- "Ungraded" 카드 포함 — 즉 grade=null 인 모든 episode 도 일괄 처리 대상.

비-요구사항 / 제외 (확인됨):
- **자동 normal 처리 로직 제거**는 이미 commit `1a162cf` (2026-04-24, "drop auto-grade-on-registration pass")에서 완료되어 추가 작업 없음.
- 백엔드 변경 없음 — `POST /api/episodes/bulk-grade` 가 이미 good/normal/bad 모두 지원하고, bad/normal에 대한 reason validation도 적용되어 있음 (`backend/datasets/schemas.py:73-78`).
- length / tags 차트 우클릭 메뉴는 현행 "Mark as Bad" 그대로 유지 (이번 spec 범위 밖).

## 결정 사항

### UX

- GradeSummary 4 카드의 **좌클릭** 동작은 기존대로 `onNavigateCurate({ grade })` 유지.
- **우클릭**은 일괄 처리 컨텍스트 메뉴를 새로 띄움.
- 카드의 카운트가 0이면 우클릭 메뉴를 띄우지 않음.
- 메뉴 항목은 카드의 현재 grade와 동일한 옵션을 숨김:

| 카드 (현재 grade) | 메뉴 항목 |
|---|---|
| Good (key: `good`) | Mark all as Normal · Mark all as Bad |
| Normal (key: `normal`) | Mark all as Good · Mark all as Bad |
| Bad (key: `bad`) | Mark all as Good · Mark all as Normal |
| Ungraded (key: `(ungraded)`) | Mark all as Good · Mark all as Normal · Mark all as Bad |

- target = `good` 선택 시 reason 입력 없이 바로 `POST /api/episodes/bulk-grade { grade: 'good', reason: null }` 호출.
- target = `normal` 또는 `bad` 선택 시 기존 `GradeReasonModal`(이미 `'normal' | 'bad'` 두 grade를 지원) 띄우고 reason 입력 → bulk-grade 호출.
- 성공 시 기존 undo 배너를 재사용. 메시지는 `방금 {N}개를 {targetGrade} 처리`로 일반화.

### Frontend 상태/구조

`OverviewTab.tsx` 안에서만 변경한다. `GradeSummary`는 props 추가만 받고 자체 상태는 두지 않는다.

#### `ContextMenuState` 확장

```ts
interface ContextMenuState {
  x: number
  y: number
  source: 'bar' | 'grade-card'
  field: string   // 'length' | 'tags' | 'grade'
  label: string   // bar label, 또는 카드의 현재 grade key
}
```

기존 length/tags 막대 메뉴는 `source: 'bar'`로 마이그레이션. GradeSummary 카드는 `source: 'grade-card'`, `field: 'grade'`, `label: <currentKey>`로 호출.

메뉴 렌더 분기:
- `source === 'bar'` → 기존 "Mark as Bad" 항목 1개.
- `source === 'grade-card'` → 위 표 규칙대로 2~3개 항목.

#### `bulkReasonModal` 일반화

```ts
const [bulkReasonModal, setBulkReasonModal] = useState<{
  episodeIndices: number[]
  targetGrade: 'normal' | 'bad'    // good은 모달 안 거침
  field: string
  label: string
} | null>(null)
```

#### `LastBulkOp` 일반화

```ts
interface LastBulkOp {
  field: string
  targetGrade: 'good' | 'normal' | 'bad'
  episodeIndices: number[]
  prevByIdx: Record<number, BulkEpisodeState>
}
```

(기존 undo 로직 — `prevByIdx`로 이전 grade를 grade-별로 그룹핑해 복원하는 부분 — 변경 없음. target만 메시지에 사용.)

#### 핵심 함수

- `submitBulkBad` → `submitBulkGrade(targetGrade, reason | null)`로 일반화. body에 `grade: targetGrade` 전달.
- `applyBulkGrade(indices, targetGrade, field, label)` 신규:
  - `targetGrade === 'good'` → 바로 `submitBulkGrade('good', null)`.
  - 그 외 → `setBulkReasonModal({ episodeIndices: indices, targetGrade, field, label })`.
- `openBulkBadModal` → `openBulkBarModal(menu)` 로 이름 정리, 내부에서 `applyBulkGrade(..., 'bad', menu.field, menu.label)` 호출.
- `openBulkGradeCardMenu(currentKey, x, y)`:
  - currentKey === `'(ungraded)'` → indices = `episodes.filter(e => e.grade == null).map(e => e.episode_index)`.
  - 그 외 → indices = `episodes.filter(e => e.grade === currentKey).map(e => e.episode_index)`.
  - `indices.length === 0` 이면 메뉴 안 띄움 (early return).
  - 그 외 `setContextMenu({ source: 'grade-card', field: 'grade', label: currentKey, x, y })`.

#### `GradeSummary` 컴포넌트

- 새 prop: `onCardContextMenu: (currentKey: string, x: number, y: number) => void`
- 카드 div에 `onContextMenu={e => { e.preventDefault(); onCardContextMenu(item.key, e.clientX, e.clientY) }}` 추가.
- `count === 0`인 카드는 핸들러 호출 안 함 (부모에서도 가드하지만 명확히 두 곳에서 막음).

#### 차트/캐시 갱신

GradeSummary 일괄 처리는 `addChart(datasetPath, 'grade', 'auto')`만 다시 호출 (length·tags 차트는 grade 변경의 영향이 없음). 막대 흐름은 기존대로 `addChart('grade')` + `addChart(field)`.

### Undo 동작

- 배너 메시지: `방금 {N}개를 {targetGrade} 처리`. 기존 "bad 처리" 하드코딩 제거.
- undo 동작 자체는 변경 없음 — `prevByIdx`로 grade-별 그룹핑하고 grade=null 그룹은 `PATCH /api/episodes/{idx}`로 grade·reason 둘 다 null로 복원하는 기존 로직 그대로.

## 에러 처리

- bulk-grade 호출 실패 → 기존처럼 throw, undo 배너 안 뜸. (기존 동작 유지)
- 화면 갱신 실패 (`onBulkGradeApplied`, `addChart`) → 기존 `setUndoError(...)` 분기 메시지 그대로.
- undo 시 일부 실패 → 기존 분기 메시지 그대로.

## 백엔드

변경 없음.

- `POST /api/episodes/bulk-grade` (`backend/datasets/routers/episodes.py:48`)는 이미 모든 grade 지원.
- `BulkGradeRequest` schema (`backend/datasets/schemas.py:61-78`)가 grade 검증 + bad/normal에 대한 reason 필수 검증을 이미 수행.

## 테스트 (CLAUDE.md "Test 순서" 준수)

### 1. 단위 테스트 (`frontend/tests/overviewBulkGrade.test.ts` 신규)

- GradeSummary의 카드 우클릭 → 컨텍스트 메뉴 나타남.
- 현재 grade 옵션이 메뉴에서 숨겨짐 (Good 카드에 "Mark all as good" 없음 등).
- count=0 카드 우클릭 시 메뉴 안 뜸.
- "Mark all as good" 클릭 → reason modal 안 거침, `POST /api/episodes/bulk-grade` 호출 시 `grade: 'good'`, `reason: null`.
- "Mark all as normal/bad" 클릭 → reason modal 뜸, reason 저장 → `grade: target`, 입력한 reason 으로 호출.
- undo 배너 메시지가 일반화된 포맷("{N}개를 {target} 처리")으로 표시.
- prev grade가 섞여 있을 때 (예: Ungraded 카드를 normal로 바꾼 뒤 undo) grouped restore 호출 + grade=null인 episode는 PATCH 사용.

### 2. 백엔드 단위 테스트

변경 없음. 기존 `tests/test_grade_reason.py`가 normal/bad/good을 cover.

### 3. Docker 내 mockup data 테스트

`docs/ui/frontend-rebuild-checklist.md` (또는 동급 절차) 따라 frontend rebuild + UI 컨테이너 재기동 → 4 카드 전부에서 우클릭 동작 + 좌클릭 네비게이션이 그대로 동작하는지 확인.

### 4. 실제 data 테스트

사용자 검수.

## graphify 갱신

CLAUDE.md 규칙대로 `OverviewTab.tsx` 수정 후:

```bash
python3 -c "from graphify.watch import _rebuild_code; from pathlib import Path; _rebuild_code(Path('.'))"
```

## 위험 / 미해결 이슈

- 사용자가 "Good" 카드에서 "Mark all as bad" 같은 잠재적으로 파괴적인 일괄 변경을 실수로 누를 수 있다. 완화: reason 모달이 사실상 confirmation 단계 역할을 하고(취소 버튼), undo 배너가 즉시 제공된다. good→good 같은 noop은 메뉴에서 숨김으로 차단.
- 컨텍스트 메뉴 외부 클릭 닫기 — 기존 `useEffect(() => { window.addEventListener('click', close) }, [contextMenu])` 그대로 적용.
