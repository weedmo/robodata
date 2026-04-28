import { useState, useCallback } from 'react'
import client from '../api/client'
import type { Episode, EpisodeUpdate } from '../types'

interface UseEpisodesReturn {
  episodes: Episode[]
  selectedEpisode: Episode | null
  loading: boolean
  error: string | null
  fetchEpisodes: () => Promise<void>
  selectEpisode: (index: number) => void
  updateEpisode: (
    index: number,
    grade: string | null,
    tags: string[],
    reason?: string | null,
  ) => Promise<void>
}

export function useEpisodes(datasetPath: string): UseEpisodesReturn {
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [selectedEpisode, setSelectedEpisode] = useState<Episode | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchEpisodes = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await client.get<Episode[]>('/episodes', {
        params: { dataset_path: datasetPath },
      })
      setEpisodes(response.data)
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to fetch episodes'
      setError(message)
      throw err instanceof Error ? err : new Error(message)
    } finally {
      setLoading(false)
    }
  }, [datasetPath])

  const selectEpisode = useCallback((index: number) => {
    setEpisodes(prev => {
      const ep = prev.find(e => e.episode_index === index) ?? null
      setSelectedEpisode(ep)
      return prev
    })
  }, [])

  const updateEpisode = useCallback(
    async (index: number, grade: string | null, tags: string[], reason?: string | null) => {
      const update: EpisodeUpdate = { dataset_path: datasetPath, grade, tags }
      if (reason !== undefined) update.reason = reason
      const response = await client.patch<Episode>(`/episodes/${index}`, update)
      const updated = response.data
      setEpisodes(prev => prev.map(e => e.episode_index === index ? updated : e))
      setSelectedEpisode(prev => prev?.episode_index === index ? updated : prev)
    },
    [datasetPath],
  )

  return { episodes, selectedEpisode, loading, error, fetchEpisodes, selectEpisode, updateEpisode }
}
