export type BulkTargetGrade = 'good' | 'normal' | 'bad'

export const UNGRADED_GRADE_KEY = '(ungraded)'

const ALL_TARGETS: readonly BulkTargetGrade[] = ['good', 'normal', 'bad']

function normalizeGrade(grade: string | null | undefined): string | null {
  if (grade == null) return null
  const normalized = grade.trim().toLowerCase()
  return normalized === '' ? null : normalized
}

function isUngradedKey(key: string): boolean {
  return key === UNGRADED_GRADE_KEY || key === 'ungraded'
}

export function gradeKeyForEpisode(grade: string | null | undefined): BulkTargetGrade | typeof UNGRADED_GRADE_KEY {
  const normalized = normalizeGrade(grade)
  if (normalized === 'good' || normalized === 'normal' || normalized === 'bad') {
    return normalized
  }
  return UNGRADED_GRADE_KEY
}

/**
 * Return menu options for a GradeSummary card. The current-grade option is
 * hidden so the user cannot apply a no-op. The "(ungraded)" key has no
 * matching target, so all three options are shown.
 */
export function bulkTargetsForCard(currentKey: string): BulkTargetGrade[] {
  if (isUngradedKey(currentKey)) return [...ALL_TARGETS]
  const normalized = normalizeGrade(currentKey)
  return ALL_TARGETS.filter(t => t !== normalized)
}

interface EpisodeLike {
  episode_index: number
  grade?: string | null
}

/**
 * Return episode_indices whose grade matches *currentKey*. The "(ungraded)"
 * key matches null/undefined/blank grades and unknown grade values.
 */
export function episodesForGradeKey(
  episodes: readonly EpisodeLike[],
  currentKey: string,
): number[] {
  if (isUngradedKey(currentKey)) {
    return episodes
      .filter(e => gradeKeyForEpisode(e.grade) === UNGRADED_GRADE_KEY)
      .map(e => e.episode_index)
  }

  const normalized = normalizeGrade(currentKey)
  return episodes
    .filter(e => gradeKeyForEpisode(e.grade) === normalized)
    .map(e => e.episode_index)
}

/**
 * Format the undo banner message after a bulk grade op.
 * Always Korean, always includes the target grade (good/normal/bad) so the
 * user can confirm what was applied before clicking "되돌리기".
 */
export function bulkOpBannerMessage(target: BulkTargetGrade, count: number): string {
  return `방금 ${count}개를 ${target} 처리`
}
