import type { AppState } from './types'

const CONVERTER_SOURCE = 'lerobot'


export function sourceNameForState(state: AppState): string | null {
  switch (state.view) {
    case 'source':
    case 'cell':
    case 'dataset':
      return state.sourceName
    case 'library':
    case 'converter':
      return null
  }
}


export function shouldShowConverter(state: AppState): boolean {
  if (state.view === 'converter') return true
  return sourceNameForState(state) === CONVERTER_SOURCE
}


export function shouldPollConverterStatus(state: AppState): boolean {
  return state.view === 'converter'
}


export function sourceContentMode(cellCount: number): 'cells' | 'datasets' {
  return cellCount > 0 ? 'cells' : 'datasets'
}


export function shouldShowCellBreadcrumb(state: AppState): boolean {
  if (state.view !== 'cell' && state.view !== 'dataset') return false
  return state.cellPath !== state.sourcePath
}
