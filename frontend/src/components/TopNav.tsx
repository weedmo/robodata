import type { AppState, DatasetTab, ConverterState } from '../types'
import { shouldShowCellBreadcrumb, shouldShowConverter } from '../appChrome'

interface ThemeOption { key: string; dot: string }

interface TopNavProps {
  state: AppState
  converterState: ConverterState
  onNavigateHome: () => void
  onNavigateSource: (sourceName: string, sourcePath: string) => void
  onNavigateCell: (sourceName: string, sourcePath: string, cellName: string, cellPath: string) => void
  onTabChange?: (tab: DatasetTab) => void
  onNavigateConverter?: () => void
  themes: readonly ThemeOption[]
  currentTheme: string
  onThemeChange: (key: string) => void
}

const TABS: { id: DatasetTab; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'curate',   label: 'Curate' },
  { id: 'fields',   label: 'Fields' },
]

const DOT_CLASS: Record<ConverterState, string> = {
  running: 'converter-dot-running',
  stopped: 'converter-dot-stopped',
  building: 'converter-dot-building',
  error: 'converter-dot-error',
  unknown: 'converter-dot-stopped',
}

export function TopNav({ state, converterState, onNavigateHome, onNavigateSource, onNavigateCell, onTabChange, onNavigateConverter, themes, currentTheme, onThemeChange }: TopNavProps) {
  const showConverter = shouldShowConverter(state)

  return (
    <nav className="top-nav">
      <button className="top-nav-logo" onClick={onNavigateHome}>
        robo<span>data</span>
      </button>

      <div className="top-nav-breadcrumb">
        {(state.view === 'source' || state.view === 'cell' || state.view === 'dataset') && (
          <>
            <span className="sep">/</span>
            <button onClick={() => onNavigateSource(state.sourceName, state.sourcePath)}>
              <em>{state.sourceName}</em>
            </button>
          </>
        )}
        {(state.view === 'cell' || state.view === 'dataset') && shouldShowCellBreadcrumb(state) && (
          <>
            <span className="sep">/</span>
            <button onClick={() => onNavigateCell(state.sourceName, state.sourcePath, state.cellName, state.cellPath)}>
              <em>{state.cellName}</em>
            </button>
          </>
        )}
        {state.view === 'dataset' && (
          <>
            <span className="sep">/</span>
            <em>{state.datasetName}</em>
          </>
        )}
        {state.view === 'converter' && (
          <>
            <span className="sep">/</span>
            <em>Converter</em>
          </>
        )}
      </div>

      {state.view === 'dataset' && (
        <div className="top-nav-tabs">
          {TABS.map(tab => (
            <button
              key={tab.id}
              className={`top-nav-tab${state.tab === tab.id ? ' active' : ''}`}
              onClick={() => onTabChange?.(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
      )}

      <button
        className={`top-nav-convert${state.view === 'converter' ? ' active' : ''} ${showConverter ? DOT_CLASS[converterState] : ''}`}
        onClick={onNavigateConverter}
        title={showConverter ? `Converter: ${converterState}` : 'Open converter'}
      >
        {showConverter && <span className="converter-nav-dot" />}
        <span>convert</span>
      </button>

      <div className="bg-picker">
        {themes.map(t => (
          <button
            key={t.key}
            className={`bg-dot${currentTheme === t.key ? ' active' : ''}`}
            style={{ background: t.dot }}
            onClick={() => onThemeChange(t.key)}
            title={t.key}
          />
        ))}
      </div>
    </nav>
  )
}
