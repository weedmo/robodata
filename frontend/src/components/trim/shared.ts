export type TrimTabId = 'out' | 'delete' | 'cycles'
export type SplitMode = 'grade' | 'tag'

export const GRADE_OPTIONS = ['good', 'normal', 'bad', 'Ungraded'] as const

export function gradeColor(grade: string): string {
  switch (grade) {
    case 'good':
      return 'var(--c-green)'
    case 'bad':
      return 'var(--c-red)'
    case 'normal':
      return 'var(--c-yellow)'
    default:
      return 'var(--text-muted)'
  }
}

export function formatEpisodeRanges(indices: number[]): string {
  if (indices.length === 0) return 'none'
  const sorted = [...indices].sort((a, b) => a - b)
  const ranges: string[] = []
  let start = sorted[0]
  let end = sorted[0]
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i] === end + 1) {
      end = sorted[i]
    } else {
      ranges.push(start === end ? `${start}` : `${start}-${end}`)
      start = sorted[i]
      end = sorted[i]
    }
  }
  ranges.push(start === end ? `${start}` : `${start}-${end}`)
  return `Episodes: ${ranges.join(', ')}`
}
