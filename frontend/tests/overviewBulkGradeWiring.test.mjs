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
assertIncludes(overviewSrc, 'undoLastBulkGrade', 'undo function renamed')
assertNotIncludes(overviewSrc, 'undoLastBulkBad', 'old bad-only undo name removed')

// 6. GradeReasonModal grade prop이 dynamic
assertIncludes(overviewSrc, 'grade={bulkReasonModal?.targetGrade', 'modal grade is dynamic')
assertNotIncludes(overviewSrc, 'grade="bad"', 'modal no longer hardcoded to bad')

// 7. good 분기는 reason 모달을 거치지 않고 직접 client.post 호출
assertIncludes(overviewSrc, "targetGrade === 'good'", 'good fast path branch present')

console.log('overviewBulkGradeWiring: OK')
