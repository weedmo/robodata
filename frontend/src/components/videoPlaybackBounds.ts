export interface PlaybackBounds {
  startTime: number
  endTime?: number | null
  duration?: number | null
  episodeLengthFrames?: number | null
  fps: number
}

function finiteNumber(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function resolveEpisodeBoundaryTime(bounds: PlaybackBounds): number | null {
  const start = finiteNumber(bounds.startTime) ?? 0
  const explicitEnd = finiteNumber(bounds.endTime)
  if (explicitEnd !== null && explicitEnd > start) return explicitEnd

  const frames = finiteNumber(bounds.episodeLengthFrames)
  const fps = finiteNumber(bounds.fps)
  if (frames !== null && frames > 0 && fps !== null && fps > 0) {
    return start + frames / fps
  }

  const duration = finiteNumber(bounds.duration)
  return duration !== null && duration > start ? duration : null
}

export function resolvePlayableEndTime(bounds: PlaybackBounds): number | null {
  const start = finiteNumber(bounds.startTime) ?? 0
  const boundary = resolveEpisodeBoundaryTime(bounds)
  const fps = finiteNumber(bounds.fps)
  if (boundary === null) return null
  if (fps === null || fps <= 0) return boundary
  return Math.max(start, boundary - 1 / fps)
}

export function clampToPlayableRange(time: number, bounds: PlaybackBounds): number {
  const start = finiteNumber(bounds.startTime) ?? 0
  const end = resolvePlayableEndTime(bounds)
  const safeTime = finiteNumber(time) ?? start
  return Math.max(start, end === null ? safeTime : Math.min(end, safeTime))
}

export function hasCrossedEpisodeBoundary(
  time: number,
  bounds: PlaybackBounds,
  epsilonSeconds = 0.002,
): boolean {
  const boundary = resolveEpisodeBoundaryTime(bounds)
  const safeTime = finiteNumber(time)
  return boundary !== null && safeTime !== null && safeTime >= boundary - epsilonSeconds
}

export function isAtPlayableEnd(
  time: number,
  bounds: PlaybackBounds,
  epsilonSeconds = 0.002,
): boolean {
  const playableEnd = resolvePlayableEndTime(bounds)
  const safeTime = finiteNumber(time)
  return playableEnd !== null && safeTime !== null && safeTime >= playableEnd - epsilonSeconds
}
