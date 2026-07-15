import { useCallback, useState } from 'react'
import axios from 'axios'
import client from '../api/client'
import type {
  InstructionEditMode,
  InstructionPreview,
  InstructionUpdateResult,
} from '../types'

export class InstructionRequestError extends Error {
  readonly code: string | null

  constructor(message: string, code: string | null = null) {
    super(message)
    this.name = 'InstructionRequestError'
    this.code = code
  }
}

function requestError(error: unknown): InstructionRequestError {
  if (!axios.isAxiosError(error)) {
    return new InstructionRequestError(error instanceof Error ? error.message : 'Instruction update failed')
  }

  const detail = error.response?.data?.detail
  if (detail && typeof detail === 'object') {
    return new InstructionRequestError(
      typeof detail.message === 'string' ? detail.message : 'Instruction update failed',
      typeof detail.code === 'string' ? detail.code : null,
    )
  }
  return new InstructionRequestError(typeof detail === 'string' ? detail : error.message)
}

interface UseEpisodeInstructionReturn {
  preview: (episodeIndex: number, instruction: string, mode: InstructionEditMode) => Promise<InstructionPreview>
  commit: (
    episodeIndex: number,
    instruction: string,
    mode: InstructionEditMode,
    fingerprint: string,
  ) => Promise<InstructionUpdateResult>
  previewing: boolean
  saving: boolean
}

export function useEpisodeInstruction(datasetPath: string): UseEpisodeInstructionReturn {
  const [previewing, setPreviewing] = useState(false)
  const [saving, setSaving] = useState(false)

  const preview = useCallback(async (
    episodeIndex: number,
    instruction: string,
    mode: InstructionEditMode,
  ) => {
    setPreviewing(true)
    try {
      const response = await client.post<InstructionPreview>(
        `/episodes/${episodeIndex}/instruction-preview`,
        { dataset_path: datasetPath, instruction, mode },
      )
      return response.data
    } catch (error) {
      throw requestError(error)
    } finally {
      setPreviewing(false)
    }
  }, [datasetPath])

  const commit = useCallback(async (
    episodeIndex: number,
    instruction: string,
    mode: InstructionEditMode,
    fingerprint: string,
  ) => {
    setSaving(true)
    try {
      const response = await client.post<InstructionUpdateResult>(
        `/episodes/${episodeIndex}/instruction`,
        {
          dataset_path: datasetPath,
          instruction,
          mode,
          fingerprint,
          ...(mode === 'shared' ? { confirm_shared: true } : {}),
        },
      )
      return response.data
    } catch (error) {
      throw requestError(error)
    } finally {
      setSaving(false)
    }
  }, [datasetPath])

  return { preview, commit, previewing, saving }
}
