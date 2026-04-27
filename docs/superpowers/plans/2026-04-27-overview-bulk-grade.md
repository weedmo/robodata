# Overview Bulk Grade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GradeSummary 4 카드(Good/Normal/Bad/Ungraded) 우클릭으로 해당 카테고리 episodes 전체를 good/normal/bad로 일괄 변경할 수 있게 한다.

**Architecture:** 백엔드는 변경하지 않는다 — `POST /api/episodes/bulk-grade`가 이미 모든 grade를 지원하고 reason 검증도 적용 중이다. 프런트는 (1) 메뉴 항목·episode 선택·undo 메시지를 만드는 순수 헬퍼를 별도 `.ts` 파일로 추출하고, (2) `OverviewTab.tsx`의 기존 length/tags 우클릭 흐름(`bulkReasonModal`, `lastBulkOp`, `submitBulkBad`, undo 배너)을 `targetGrade`로 일반화한 다음, (3) `GradeSummary` 카드에 `onContextMenu`만 추가한다.

**Tech Stack:** React 19 + TypeScript 5.7, Vite 6, 기존 `frontend/tests/*.test.ts`(`npx tsx`로 실행) + `*.test.mjs`(소스 문자열 회귀, `node`로 실행) 어셔션 스타일.

**Spec:** `docs/superpowers/specs/2026-04-27-overview-bulk-grade-design.md`

---

## File Structure

- **Create:** `frontend/src/components/overviewBulkGrade.ts` — 순수 헬퍼: `bulkTargetsForCard`, `episodesForGradeKey`, `bulkOpBannerMessage`. DOM/React 의존 없이 Node에서 import 가능.
- **Create:** `frontend/tests/overviewBulkGrade.test.ts` — 위 헬퍼들의 단위 테스트(`npx tsx`).
- **Create:** `frontend/tests/overviewBulkGradeWiring.test.mjs` — `OverviewTab.tsx` / `GradeSummary` 소스 문자열 회귀 (메뉴 항목, 우클릭 핸들러 prop이 GradeSummary에 wired).
- **Modify:** `frontend/src/components/OverviewTab.tsx` — state 일반화 + GradeSummary 카드 우클릭 처리.

각 파일이 한 가지 책임만 갖는다: 헬퍼 파일은 순수 결정 로직, 테스트는 그 로직과 wiring 회귀, OverviewTab은 React state·렌더.

---

## Task 1: Pure helpers (TDD)

**Files:**
- Create: `frontend/src/components/overviewBulkGrade.ts`
- Test: `frontend/tests/overviewBulkGrade.test.ts`

- [ ] **Step 1: 실패하는 단위 테스트 작성**

`frontend/tests/overviewBulkGrade.test.ts` 새로 작성:

```typescript
import {
  bulkTargetsForCard,
  episodesForGradeKey,
  bulkOpBannerMessage,
} from '../src/components/overviewBulkGrade'

function assertEqual<T>(actual: T, expected: T, label: string) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) {
    throw new Error(`[${label}] expected ${e}, got ${a}`)
  }
}

// bulkTargetsForCard — 현재 grade 옵션은 숨김
assertEqual(bulkTargetsForCard('good'),       ['normal', 'bad'],         'card=good')
assertEqual(bulkTargetsForCard('normal'),     ['good', 'bad'],           'card=normal')
assertEqual(bulkTargetsForCard('bad'),        ['good', 'normal'],        'card=bad')
assertEqual(bulkTargetsForCard('(ungraded)'), ['good', 'normal', 'bad'], 'card=ungraded')

// 알 수 없는 key는 모든 옵션 반환 (defensive)
assertEqual(bulkTargetsForCard('???'),        ['good', 'normal', 'bad'], 'card=unknown')

// episodesForGradeKey — grade가 일치하는 episode_index만
const eps = [
  { episode_index: 0, grade: 'good'   },
  { episode_index: 1, grade: 'bad'    },
  { episode_index: 2, grade: null     },
  { episode_index: 3, grade: 'normal' },
  { episode_index: 4, grade: undefined },
]
assertEqual(episodesForGradeKey(eps, 'good'),       [0],    'pick=good')
assertEqual(episodesForGradeKey(eps, 'normal'),     [3],    'pick=normal')
assertEqual(episodesForGradeKey(eps, 'bad'),        [1],    'pick=bad')
assertEqual(episodesForGradeKey(eps, '(ungraded)'), [2, 4], 'pick=ungraded')

// bulkOpBannerMessage — target별 동일 포맷
assertEqual(bulkOpBannerMessage('good',   3), '방금 3개를 good 처리',   'banner=good')
assertEqual(bulkOpBannerMessage('normal', 1), '방금 1개를 normal 처리', 'banner=normal')
assertEqual(bulkOpBannerMessage('bad',   42), '방금 42개를 bad 처리',   'banner=bad')

console.log('overviewBulkGrade helpers: OK')
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd frontend && npx tsx tests/overviewBulkGrade.test.ts`
Expected: 실패 — `Cannot find module '../src/components/overviewBulkGrade'`

- [ ] **Step 3: 헬퍼 구현**

`frontend/src/components/overviewBulkGrade.ts` 새로 작성:

```typescript
export type BulkTargetGrade = 'good' | 'normal' | 'bad'

const ALL_TARGETS: readonly BulkTargetGrade[] = ['good', 'normal', 'bad']

/**
 * Return menu options for a GradeSummary card. The current-grade option is
 * hidden so the user cannot apply a no-op. The "(ungraded)" key has no
 * matching target, so all three options are shown.
 */
export function bulkTargetsForCard(currentKey: string): BulkTargetGrade[] {
  return ALL_TARGETS.filter(t => t !== currentKey)
}

interface EpisodeLike {
  episode_index: number
  grade?: string | null
}

/**
 * Return episode_indices whose grade matches *currentKey*. The "(ungraded)"
 * key matches null/undefined grades.
 */
export function episodesForGradeKey(
  episodes: readonly EpisodeLike[],
  currentKey: string,
): number[] {
  if (currentKey === '(ungraded)') {
    return episodes.filter(e => e.grade == null).map(e => e.episode_index)
  }
  return episodes.filter(e => e.grade === currentKey).map(e => e.episode_index)
}

/**
 * Format the undo banner message after a bulk grade op.
 * Always Korean, always includes the target grade (good/normal/bad) so the
 * user can confirm what was applied before clicking "되돌리기".
 */
export function bulkOpBannerMessage(target: BulkTargetGrade, count: number): string {
  return `방금 ${count}개를 ${target} 처리`
}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd frontend && npx tsx tests/overviewBulkGrade.test.ts`
Expected: `overviewBulkGrade helpers: OK`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/overviewBulkGrade.ts frontend/tests/overviewBulkGrade.test.ts
git commit -m "feat(overview): add pure helpers for bulk-grade card menu"
```

---

## Task 2: Generalize OverviewTab state (refactor, no behavior change)

기존 length/tags 우클릭 → "Mark as Bad" 흐름이 그대로 동작하도록, state·함수 시그니처만 일반화한다. 이 단계에서는 GradeSummary 카드 우클릭은 **아직 동작하지 않는다**.

**Files:**
- Modify: `frontend/src/components/OverviewTab.tsx` (state types, `submitBulkBad` → `submitBulkGrade`, undo 배너 메시지)

- [ ] **Step 1: import 헬퍼 + 타입**

`frontend/src/components/OverviewTab.tsx` 상단 import 블록에 추가 (기존 import들 유지):

```typescript
import {
  bulkTargetsForCard,
  episodesForGradeKey,
  bulkOpBannerMessage,
  type BulkTargetGrade,
} from './overviewBulkGrade'
```

- [ ] **Step 2: ContextMenuState에 source 필드 추가**

같은 파일의 `interface ContextMenuState` (라인 9-14 근처) 교체:

```typescript
interface ContextMenuState {
  x: number
  y: number
  source: 'bar' | 'grade-card'
  field: string
  label: string
}
```

- [ ] **Step 3: bulkReasonModal·LastBulkOp 일반화**

`bulkReasonModal` state 선언 (라인 51-55 근처) 교체:

```typescript
const [bulkReasonModal, setBulkReasonModal] = useState<{
  episodeIndices: number[]
  targetGrade: 'normal' | 'bad'
  field: string
  label: string
} | null>(null)
```

`LastBulkOp` interface (라인 29-33 근처) 교체:

```typescript
interface LastBulkOp {
  field: string
  targetGrade: BulkTargetGrade
  episodeIndices: number[]
  prevByIdx: Record<number, BulkEpisodeState>
}
```

- [ ] **Step 4: submitBulkBad → submitBulkGrade**

함수 `submitBulkBad`(라인 88-126) 전체를 다음으로 교체. 차이: grade 인자 + lastBulkOp.targetGrade 기록 + 에러 메시지 일반화.

```typescript
const submitBulkGrade = useCallback(
  async (targetGrade: BulkTargetGrade, reason: string | null) => {
    const m = bulkReasonModal
    // good은 모달 안 거치므로 m이 null일 수 있음 → 인자로 받은 indices/field/label은
    // 호출처(applyBulkGrade) 에서 closure로 넘겨야 함. 여기서는 모달 경로 전용.
    if (!m) return
    const prevByIdx: Record<number, BulkEpisodeState> = {}
    for (const episodeIndex of m.episodeIndices) {
      const episode = episodes.find(ep => ep.episode_index === episodeIndex)
      if (!episode) continue
      prevByIdx[episodeIndex] = {
        grade: episode.grade,
        reason: episode.reason,
      }
    }

    setBulkReasonModal(null)
    await client.post('/episodes/bulk-grade', {
      episode_indices: m.episodeIndices,
      grade: targetGrade,
      reason,
    })
    setUndoError(null)
    setLastBulkOp({
      field: m.field,
      targetGrade,
      episodeIndices: m.episodeIndices,
      prevByIdx,
    })

    const refreshFields: Promise<unknown>[] = [
      onBulkGradeApplied(),
      addChart(datasetPath, 'grade', 'auto'),
    ]
    if (m.field === 'length' || m.field === 'tags') {
      refreshFields.push(addChart(datasetPath, m.field, m.field === 'length' ? 'histogram' : 'auto'))
    }
    const refreshResults = await Promise.allSettled(refreshFields)
    const failedRefreshCount = refreshResults.filter(r => r.status === 'rejected').length
    if (failedRefreshCount > 0) {
      setUndoError(`${targetGrade} 처리는 완료됐지만 화면 갱신에 실패했습니다 (${failedRefreshCount}건) · 다시 시도하세요`)
    }
  },
  [bulkReasonModal, episodes, datasetPath, addChart, onBulkGradeApplied],
)
```

- [ ] **Step 5: openBulkBadModal → openBulkBarModal**

함수 `openBulkBadModal` (라인 69-86) 전체 교체. 동작 동일(여전히 'bad' 고정), 새 state 모양 사용:

```typescript
const openBulkBarModal = useCallback((menu: ContextMenuState) => {
  const indices: number[] = []
  if (menu.field === 'length') {
    const parts = menu.label.split('-').map(Number)
    if (parts.length === 2 && parts.every(n => !isNaN(n))) {
      for (const ep of episodes) {
        if (ep.length >= parts[0] && ep.length < parts[1]) indices.push(ep.episode_index)
      }
    }
  } else if (menu.field === 'tags') {
    for (const ep of episodes) {
      if (ep.tags.includes(menu.label)) indices.push(ep.episode_index)
    }
  }
  if (indices.length === 0) return
  setBulkReasonModal({
    episodeIndices: indices,
    targetGrade: 'bad',
    field: menu.field,
    label: menu.label,
  })
  setContextMenu(null)
}, [episodes])
```

- [ ] **Step 6: 컨텍스트 메뉴 렌더에 source 분기**

기존 `{contextMenu && ( ... <button onClick={() => openBulkBadModal(contextMenu)}>Mark as Bad</button> ... )}` 블록 (라인 330-365 근처) 의 `<button>` 부분을 교체. 이 단계에서는 `'bar'` 분기만 채우고 `'grade-card'` 분기는 다음 Task에서 추가:

```tsx
{contextMenu.source === 'bar' && (
  <button
    style={{
      display: 'block',
      width: '100%',
      padding: '6px 12px',
      background: 'none',
      border: 'none',
      color: 'var(--c-red)',
      fontSize: 12,
      textAlign: 'left',
      cursor: 'pointer',
    }}
    onMouseEnter={e => (e.currentTarget.style.background = 'var(--c-red-dim)')}
    onMouseLeave={e => (e.currentTarget.style.background = 'none')}
    onClick={() => openBulkBarModal(contextMenu)}
  >
    Mark as Bad
  </button>
)}
```

- [ ] **Step 7: 막대 차트 우클릭 → setContextMenu에 source='bar' 전달**

`onBarContextMenu={(chart.field === 'length' || chart.field === 'tags') ? (label, x, y) => setContextMenu({ label, field: chart.field, x, y }) : undefined}` (라인 314-316) 부분의 객체 리터럴에 `source: 'bar'` 추가:

```tsx
onBarContextMenu={(chart.field === 'length' || chart.field === 'tags')
  ? (label, x, y) => setContextMenu({ source: 'bar', label, field: chart.field, x, y })
  : undefined}
```

- [ ] **Step 8: undo 배너 메시지 일반화**

기존 `<span>방금 {lastBulkOp.episodeIndices.length}개를 bad 처리</span>` (라인 275 근처) 교체:

```tsx
<span>{bulkOpBannerMessage(lastBulkOp.targetGrade, lastBulkOp.episodeIndices.length)}</span>
```

- [ ] **Step 9: GradeReasonModal grade prop을 모달의 targetGrade로**

기존 `<GradeReasonModal open={bulkReasonModal !== null} grade="bad" ...>` (라인 367-374) 교체:

```tsx
<GradeReasonModal
  open={bulkReasonModal !== null}
  grade={bulkReasonModal?.targetGrade ?? 'bad'}
  initialReason=""
  episodeCount={bulkReasonModal?.episodeIndices.length}
  onSave={(reason) => void submitBulkGrade(bulkReasonModal!.targetGrade, reason)}
  onCancel={() => setBulkReasonModal(null)}
/>
```

- [ ] **Step 10: TypeScript 빌드 통과 확인**

Run: `cd frontend && npx tsc -b`
Expected: 0 errors. (이 단계까지는 GradeSummary 카드 우클릭은 아직 wired 되지 않았음.)

- [ ] **Step 11: 기존 .test.ts들 회귀 통과**

Run: `cd frontend && for f in tests/*.test.ts; do echo "=== $f ==="; npx tsx "$f" || exit 1; done`
Expected: 모든 기존 테스트 + Task 1 신규 테스트 PASS.

- [ ] **Step 12: 기존 .test.mjs들 회귀 통과**

Run: `cd frontend && for f in tests/*.test.mjs; do echo "=== $f ==="; node "$f" || exit 1; done`
Expected: 전부 PASS.

- [ ] **Step 13: Commit**

```bash
git add frontend/src/components/OverviewTab.tsx
git commit -m "refactor(overview): generalize bulk-grade state to support any target grade"
```

---

## Task 3: Wire GradeSummary card right-click

이제 카드 우클릭이 컨텍스트 메뉴를 띄우고, 메뉴 항목 클릭이 `applyBulkGrade`(good=직접 호출, normal/bad=모달)로 분기되게 한다.

**Files:**
- Modify: `frontend/src/components/OverviewTab.tsx` (GradeSummary props, 카드 onContextMenu, openBulkGradeCardMenu, applyBulkGrade, 컨텍스트 메뉴의 grade-card 분기)

- [ ] **Step 1: GradeSummary 컴포넌트에 onCardContextMenu prop 추가**

`function GradeSummary({...})` 시그니처(라인 399-404 근처) 교체:

```tsx
function GradeSummary({ chart, fps, episodes, onNavigateCurate, onCardContextMenu }: {
  chart: DistributionResult
  fps: number
  episodes: Episode[]
  onNavigateCurate: (filter: CurateFilter) => void
  onCardContextMenu: (currentKey: string, count: number, x: number, y: number) => void
}) {
```

- [ ] **Step 2: 카드 div에 onContextMenu 핸들러**

GradeSummary 내부 카드 `<div key={item.key} ... onClick={() => onNavigateCurate(...)} ...>` (라인 441-458 근처) 의 props에 `onContextMenu` 추가. 카운트가 0이면 기본 컨텍스트 메뉴(브라우저)를 막지도 않고 부모 핸들러도 호출하지 않음:

```tsx
<div key={item.key} style={{
  background: 'var(--panel2)',
  border: `1px solid ${count > 0 ? item.color : 'var(--border)'}`,
  borderRadius: 8,
  padding: '12px 10px',
  textAlign: 'center',
  cursor: 'pointer',
  transition: 'transform 0.15s, border-color 0.15s',
}}
  onClick={() => onNavigateCurate({ grade: item.filterKey })}
  onContextMenu={(e) => {
    if (count <= 0) return
    e.preventDefault()
    onCardContextMenu(item.key, count, e.clientX, e.clientY)
  }}
  onMouseEnter={...}
  onMouseLeave={...}
>
```

(`onMouseEnter`, `onMouseLeave`는 기존대로 유지.)

- [ ] **Step 3: OverviewTab에서 GradeSummary 렌더 시 prop 전달**

기존 `{gradeChart && <GradeSummary chart={gradeChart} fps={fps} episodes={episodes} onNavigateCurate={onNavigateCurate} />}` (라인 288 근처) 교체:

```tsx
{gradeChart && (
  <GradeSummary
    chart={gradeChart}
    fps={fps}
    episodes={episodes}
    onNavigateCurate={onNavigateCurate}
    onCardContextMenu={openBulkGradeCardMenu}
  />
)}
```

- [ ] **Step 4: openBulkGradeCardMenu·applyBulkGrade 신규 함수**

`openBulkBarModal` 정의 다음 위치에 추가:

```tsx
const openBulkGradeCardMenu = useCallback(
  (currentKey: string, count: number, x: number, y: number) => {
    if (count <= 0) return
    const indices = episodesForGradeKey(episodes, currentKey)
    if (indices.length === 0) return
    setContextMenu({
      source: 'grade-card',
      field: 'grade',
      label: currentKey,
      x,
      y,
    })
  },
  [episodes],
)

const applyBulkGrade = useCallback(
  (targetGrade: BulkTargetGrade) => {
    const menu = contextMenu
    if (!menu || menu.source !== 'grade-card') return
    const indices = episodesForGradeKey(episodes, menu.label)
    if (indices.length === 0) {
      setContextMenu(null)
      return
    }
    setContextMenu(null)
    if (targetGrade === 'good') {
      // good은 reason 모달 없이 즉시 호출. submitBulkGrade는 모달 경로 전용이므로
      // good 분기는 인라인으로 처리.
      void (async () => {
        const prevByIdx: Record<number, BulkEpisodeState> = {}
        for (const episodeIndex of indices) {
          const episode = episodes.find(ep => ep.episode_index === episodeIndex)
          if (!episode) continue
          prevByIdx[episodeIndex] = { grade: episode.grade, reason: episode.reason }
        }
        await client.post('/episodes/bulk-grade', {
          episode_indices: indices,
          grade: 'good',
          reason: null,
        })
        setUndoError(null)
        setLastBulkOp({
          field: 'grade',
          targetGrade: 'good',
          episodeIndices: indices,
          prevByIdx,
        })
        const refreshResults = await Promise.allSettled([
          onBulkGradeApplied(),
          addChart(datasetPath, 'grade', 'auto'),
        ])
        const failedRefreshCount = refreshResults.filter(r => r.status === 'rejected').length
        if (failedRefreshCount > 0) {
          setUndoError(`good 처리는 완료됐지만 화면 갱신에 실패했습니다 (${failedRefreshCount}건) · 다시 시도하세요`)
        }
      })()
      return
    }
    // normal/bad는 reason 모달 경유
    setBulkReasonModal({
      episodeIndices: indices,
      targetGrade,
      field: 'grade',
      label: menu.label,
    })
  },
  [contextMenu, episodes, datasetPath, addChart, onBulkGradeApplied],
)
```

- [ ] **Step 5: 컨텍스트 메뉴에 grade-card 분기 추가**

Task 2 Step 6에서 추가한 `{contextMenu.source === 'bar' && (...)}` 블록 다음에 추가:

```tsx
{contextMenu.source === 'grade-card' && (
  <>
    {bulkTargetsForCard(contextMenu.label).map(target => {
      const color =
        target === 'good'   ? 'var(--c-green)'  :
        target === 'normal' ? 'var(--c-yellow)' :
                              'var(--c-red)'
      const hover =
        target === 'good'   ? 'var(--c-green-dim)'  :
        target === 'normal' ? 'var(--c-yellow-dim)' :
                              'var(--c-red-dim)'
      const labelText =
        target === 'good'   ? 'Mark all as Good'   :
        target === 'normal' ? 'Mark all as Normal' :
                              'Mark all as Bad'
      return (
        <button
          key={target}
          style={{
            display: 'block',
            width: '100%',
            padding: '6px 12px',
            background: 'none',
            border: 'none',
            color,
            fontSize: 12,
            textAlign: 'left',
            cursor: 'pointer',
          }}
          onMouseEnter={e => (e.currentTarget.style.background = hover)}
          onMouseLeave={e => (e.currentTarget.style.background = 'none')}
          onClick={() => applyBulkGrade(target)}
        >
          {labelText}
        </button>
      )
    })}
  </>
)}
```

- [ ] **Step 6: TypeScript 빌드 통과 확인**

Run: `cd frontend && npx tsc -b`
Expected: 0 errors.

- [ ] **Step 7: 기존 + 신규 .test.ts 회귀**

Run: `cd frontend && for f in tests/*.test.ts; do echo "=== $f ==="; npx tsx "$f" || exit 1; done`
Expected: 전부 PASS.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/OverviewTab.tsx
git commit -m "feat(overview): bulk-grade GradeSummary cards via right-click"
```

---

## Task 4: Source-string regression for wiring

순수 헬퍼는 Task 1에서 unit-test했고, OverviewTab 자체의 wiring(메뉴 항목 라벨, GradeSummary prop이 연결됨, 'good' 분기가 reason 모달을 거치지 않음)은 React 렌더 없이 소스 문자열로 회귀한다. 기존 `converterHostHintSingleSurface.test.mjs` 패턴.

**Files:**
- Create: `frontend/tests/overviewBulkGradeWiring.test.mjs`

- [ ] **Step 1: 회귀 테스트 작성**

`frontend/tests/overviewBulkGradeWiring.test.mjs`:

```javascript
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const testDir = dirname(fileURLToPath(import.meta.url))
const overviewSrc = readFileSync(
  join(testDir, '../src/components/OverviewTab.tsx'),
  'utf8',
)

function assertIncludes(actual, expected, label) {
  if (!actual.includes(expected)) {
    throw new Error(`[${label}] expected source to include: ${expected}`)
  }
}
function assertNotIncludes(actual, unexpected, label) {
  if (actual.includes(unexpected)) {
    throw new Error(`[${label}] source must NOT include: ${unexpected}`)
  }
}

// 1. 헬퍼가 import 됨
assertIncludes(overviewSrc, "from './overviewBulkGrade'", 'imports overviewBulkGrade module')
assertIncludes(overviewSrc, 'bulkTargetsForCard', 'imports bulkTargetsForCard')
assertIncludes(overviewSrc, 'episodesForGradeKey', 'imports episodesForGradeKey')
assertIncludes(overviewSrc, 'bulkOpBannerMessage', 'imports bulkOpBannerMessage')

// 2. 컨텍스트 메뉴 source 분기 양쪽 모두 존재
assertIncludes(overviewSrc, "contextMenu.source === 'bar'",       'menu branch: bar')
assertIncludes(overviewSrc, "contextMenu.source === 'grade-card'", 'menu branch: grade-card')

// 3. grade-card 분기에 3개 라벨 모두 노출
assertIncludes(overviewSrc, 'Mark all as Good',   'menu label: good')
assertIncludes(overviewSrc, 'Mark all as Normal', 'menu label: normal')
assertIncludes(overviewSrc, 'Mark all as Bad',    'menu label: bad')

// 4. GradeSummary가 onCardContextMenu prop을 받고 호출됨
assertIncludes(overviewSrc, 'onCardContextMenu={openBulkGradeCardMenu}', 'GradeSummary wired')
assertIncludes(overviewSrc, 'onContextMenu={(e)', 'card onContextMenu present')

// 5. 'bad' 하드코딩된 옛 배너 메시지가 더 이상 없음
assertNotIncludes(overviewSrc, '방금 {lastBulkOp.episodeIndices.length}개를 bad 처리', 'old bad-only banner removed')

// 6. GradeReasonModal grade prop이 dynamic
assertIncludes(overviewSrc, 'grade={bulkReasonModal?.targetGrade', 'modal grade is dynamic')
assertNotIncludes(overviewSrc, 'grade="bad"', 'modal no longer hardcoded to bad')

// 7. good 분기는 reason 모달을 거치지 않고 직접 client.post 호출
assertIncludes(overviewSrc, "targetGrade === 'good'", 'good fast path branch present')

console.log('overviewBulkGradeWiring: OK')
```

- [ ] **Step 2: 회귀 테스트 통과 확인**

Run: `node frontend/tests/overviewBulkGradeWiring.test.mjs`
Expected: `overviewBulkGradeWiring: OK`, exit 0.

만약 어떤 assertion이 실패하면 OverviewTab.tsx의 해당 부분을 점검(Task 2/3에서 빠진 wiring일 수 있음).

- [ ] **Step 3: Commit**

```bash
git add frontend/tests/overviewBulkGradeWiring.test.mjs
git commit -m "test(overview): regression for grade-card right-click wiring"
```

---

## Task 5: Frontend bundle rebuild + Docker mockup verify

CLAUDE.md "Test 순서": pytest → Docker 내 mockup data → 실제 data. 백엔드 변경이 없으므로 pytest는 이미 PASS 상태이고(기존 `test_grade_reason.py` 등이 cover), 여기서는 프런트 번들을 굽고 Docker 컨테이너에서 직접 확인한다.

**Files:**
- Modify: 없음. 빌드/실행/수동 검증.

- [ ] **Step 1: 프런트엔드 prod 빌드**

Run: `cd frontend && npm run build`
Expected: `vite build` 성공, `dist/` 갱신.

- [ ] **Step 2: nginx 컨테이너 이미지 재빌드 + 재기동**

Run: `docker compose -f docker/compose.ui.yml build ui-nginx && docker compose -f docker/compose.ui.yml up -d ui-nginx`
(레포의 실제 nginx compose 파일명에 맞춰 조정. 정확한 파일은 `docker/` 디렉터리에서 확인.)
Expected: 컨테이너 새 이미지로 기동.

- [ ] **Step 3: 번들이 신규 코드를 포함하는지 검증**

Run: `bash scripts/verify_ui_bundle.sh`
Expected: marker assertion 통과(스크립트는 기본 marker만 검사하지만, 우리는 추가로 새 코드 표식을 직접 grep):

```bash
docker exec curation-ui-nginx-1 sh -c 'cat /usr/share/nginx/html/assets/*.js' | grep -q 'Mark all as Good' \
  && echo "bundle contains new menu labels" \
  || (echo "bundle is stale!" && exit 1)
```

- [ ] **Step 4: Mockup data 로 수동 검증 (체크리스트)**

main.sh / start.sh로 백엔드 + UI 기동 후, 브라우저에서 mockup dataset 로드 → Overview 탭 진입. 다음을 모두 확인:

1. Good 카드 우클릭 → 메뉴에 `Mark all as Normal`, `Mark all as Bad` 두 항목, `Mark all as Good`은 **없음**.
2. Normal 카드 우클릭 → `Mark all as Good`, `Mark all as Bad` 두 항목.
3. Bad 카드 우클릭 → `Mark all as Good`, `Mark all as Normal` 두 항목.
4. Ungraded 카드 우클릭 → 세 항목 모두.
5. 카운트 0인 카드 우클릭 → 메뉴 안 뜸 (브라우저 기본 컨텍스트 메뉴는 떠도 무방).
6. `Mark all as Good` 클릭 → reason 모달 없이 즉시 적용, undo 배너 `방금 N개를 good 처리` 표시.
7. `Mark all as Normal/Bad` 클릭 → reason 모달 표시, 빈 reason은 Save 비활성, reason 입력 후 Save → 적용 + undo 배너 `방금 N개를 {target} 처리`.
8. 좌클릭은 여전히 Curate 탭의 해당 grade 필터로 이동.
9. length·tags 차트 막대 우클릭 → 기존 `Mark as Bad` 1 항목, 동작 그대로.
10. undo 배너 클릭 → 이전 grade(섞여있을 수 있음)로 복원, Ungraded(=null) 그룹은 `PATCH /episodes/{idx}` 로 처리됨(네트워크 탭에서 확인).
11. modal 외부 클릭/ESC로 닫힘.

위 항목 중 하나라도 실패하면: 어느 step이 깨졌는지 확인 후 Task 2 또는 Task 3로 돌아간다.

- [ ] **Step 5: graphify 코드 그래프 갱신**

CLAUDE.md 규칙대로:

Run: `python3 -c "from graphify.watch import _rebuild_code; from pathlib import Path; _rebuild_code(Path('.'))"`
Expected: `OverviewTab.tsx`, `overviewBulkGrade.ts` 노드/엣지 반영.

- [ ] **Step 6: Final commit (테스트·검증 결과 기록 및 docs spec status 업데이트)**

`docs/superpowers/specs/2026-04-27-overview-bulk-grade-design.md` 의 `**Status:** Draft` 라인을 `**Status:** Implemented` 로 변경.

```bash
git add docs/superpowers/specs/2026-04-27-overview-bulk-grade-design.md
git commit -m "docs(spec): mark overview bulk-grade design as implemented"
```

---

## Self-review

- **Spec coverage:**
  - 우클릭 카드 메뉴 4종 시나리오 → Task 3 Step 5 (메뉴 분기 분기) + Task 4 회귀 + Task 5 Step 4 항목 1~4. ✓
  - 동일 grade 옵션 숨김 → Task 1 `bulkTargetsForCard` + Task 4. ✓
  - count=0 카드 메뉴 차단 → Task 3 Step 2 + Task 5 Step 4 항목 5. ✓
  - good은 reason 없이 / normal·bad는 모달 → Task 3 Step 4 + Task 4 (good 분기 회귀) + Task 5 Step 4 항목 6,7. ✓
  - 좌클릭 navigate 유지 → Task 3 Step 2에서 onClick 보존 + Task 5 Step 4 항목 8. ✓
  - undo 배너 일반화 메시지 → Task 1 + Task 2 Step 8 + Task 4 (구 메시지 부재 회귀). ✓
  - undo 시 grouped restore + null 그룹 PATCH → 기존 `undoLastBulkBad` 변경 없음, Task 5 Step 4 항목 10에서 검증. ✓
  - length/tags 막대 흐름 회귀 → Task 2 (기능 동등 refactor) + Task 5 Step 4 항목 9. ✓
  - 백엔드 변경 없음 → 명시. ✓
  - graphify 갱신 → Task 5 Step 5. ✓

- **Placeholder scan:** "TBD/TODO/이후에/적절히" 없음. 모든 코드 step에 실제 코드 또는 실제 명령. ✓

- **Type consistency:**
  - `BulkTargetGrade` 정의(Task 1)와 `LastBulkOp.targetGrade`/`bulkReasonModal.targetGrade` 사용(Task 2,3) 일치. `bulkReasonModal`은 `'normal' | 'bad'` 서브셋(good은 모달 없음).
  - `bulkTargetsForCard` 반환 `BulkTargetGrade[]`와 `applyBulkGrade(target: BulkTargetGrade)` 시그니처 일치.
  - `episodesForGradeKey(episodes, currentKey)` — `EpisodeLike` 인터페이스가 frontend `Episode` 타입과 호환(`episode_index: number`, `grade?: string | null`).
  - `bulkOpBannerMessage(target, count)` — 두 호출처(undo 배너) 시그니처 일치.

다 정합. 진행 가능.
