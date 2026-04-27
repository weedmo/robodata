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
