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
