import { useCallback, useEffect, useState } from 'react'
import { TopNav } from './components/TopNav'
import { LibraryPage } from './components/LibraryPage'
import { SourcePage } from './components/SourcePage'
import { CellPage } from './components/CellPage'
import { DatasetPage } from './components/DatasetPage'
import { ConverterPage } from './components/ConverterPage'
import { shouldShowConverter } from './appChrome'
import { useAppState } from './hooks/useAppState'
import type { CellInfo, ConverterStatus, DatasetSourceInfo, DatasetSummary } from './types'
import './App.css'

interface Theme {
  key: string
  dot: string
  vars: Record<string, string>
}

const DARK: Theme = {
  key: 'dark', dot: '#1c1c1c',
  vars: {
    '--bg': '#1c1c1c', '--bg-deep': '#161616',
    '--panel': '#222222', '--panel2': '#282828', '--panel3': '#1e1e1e',
    '--border': '#2e2e2e', '--border2': '#363636', '--border3': '#404040',
    '--text': '#d9d9d9', '--text-muted': '#999999', '--text-dim': '#666666', '--text-faint': '#333333',
    '--accent': '#ff9830', '--accent-dim': 'rgba(255,152,48,0.08)',
    '--interactive': '#89b4fa', '--interactive-dim': 'rgba(137,180,250,0.08)',
    '--c-green': '#a6e3a1', '--c-yellow': '#f9e2af', '--c-red': '#f38ba8',
    '--c-blue': '#89b4fa', '--c-purple': '#cba6f7',
    '--c-green-dim': 'rgba(166,227,161,0.10)', '--c-yellow-dim': 'rgba(249,226,175,0.10)',
    '--c-red-dim': 'rgba(243,139,168,0.10)', '--c-blue-dim': 'rgba(137,180,250,0.10)',
    '--chart-1': '#89b4fa', '--chart-2': '#a6e3a1', '--chart-3': '#f9e2af',
    '--chart-4': '#f38ba8', '--chart-5': '#cba6f7', '--chart-6': '#ff9830',
  },
}

const WARM: Theme = {
  key: 'warm', dot: '#e8ddd0',
  vars: {
    '--bg': '#e8ddd0', '--bg-deep': '#dfd3c4',
    '--panel': '#f0e6da', '--panel2': '#e4d8cb', '--panel3': '#ebe0d4',
    '--border': '#d0c4b4', '--border2': '#c5b8a6', '--border3': '#b8aa98',
    '--text': '#2c2420', '--text-muted': '#6b5d52', '--text-dim': '#8c7e72', '--text-faint': '#c5b8a6',
    '--accent': '#d47820', '--accent-dim': 'rgba(212,120,32,0.12)',
    '--interactive': '#8b6834', '--interactive-dim': 'rgba(139,104,52,0.10)',
    '--c-green': '#5a8a50', '--c-yellow': '#a08030', '--c-red': '#b84a3a',
    '--c-blue': '#5878a0', '--c-purple': '#7a6090',
    '--c-green-dim': 'rgba(90,138,80,0.12)', '--c-yellow-dim': 'rgba(160,128,48,0.12)',
    '--c-red-dim': 'rgba(184,74,58,0.12)', '--c-blue-dim': 'rgba(88,120,160,0.12)',
    '--chart-1': '#c07828', '--chart-2': '#7a9a48', '--chart-3': '#b08838',
    '--chart-4': '#c06048', '--chart-5': '#8a6898', '--chart-6': '#d47820',
  },
}

const THEMES: Theme[] = [DARK, WARM]

function applyTheme(theme: Theme) {
  const root = document.documentElement
  for (const [k, v] of Object.entries(theme.vars)) root.style.setProperty(k, v)
}

export default function App() {
  const { state, navigateHome, navigateToSource, navigateToCell, navigateToDataset, navigateToConverter, setTab } = useAppState()
  const showConverter = shouldShowConverter(state)

  const [themeKey, setThemeKey] = useState(() => localStorage.getItem('app-theme') || 'dark')

  const theme = THEMES.find(t => t.key === themeKey) || DARK

  useEffect(() => {
    applyTheme(theme)
    localStorage.setItem('app-theme', theme.key)
  }, [theme])

  const [converterStatus, setConverterStatus] = useState<ConverterStatus>({
    container_state: 'unknown',
    docker_available: false,
    exit_code: null,
    oom_killed: false,
    finished_at: null,
    tasks: [],
    summary: '',
  })

  const fetchConverterStatus = useCallback(async () => {
    if (!showConverter) return
    try {
      const res = await fetch('/api/converter/status')
      if (res.ok) setConverterStatus(await res.json())
    } catch {}
  }, [showConverter])

  useEffect(() => {
    if (!showConverter) return
    fetchConverterStatus()
    const id = setInterval(fetchConverterStatus, 5000)
    return () => clearInterval(id)
  }, [fetchConverterStatus, showConverter])

  const handleSelectSource = useCallback((source: DatasetSourceInfo) => {
    navigateToSource(source.name, source.path)
  }, [navigateToSource])

  const handleSelectCell = useCallback((cell: CellInfo) => {
    if (state.view === 'source') {
      navigateToCell(state.sourceName, state.sourcePath, cell.name, cell.path)
    }
  }, [state, navigateToCell])

  const handleSelectDataset = useCallback((ds: DatasetSummary) => {
    if (state.view === 'cell') {
      navigateToDataset(
        state.sourceName,
        state.sourcePath,
        state.cellName,
        state.cellPath,
        ds.path,
        ds.name,
      )
    }
  }, [state, navigateToDataset])

  return (
    <div className="app-root">
      <TopNav
        state={state}
        converterState={converterStatus.container_state}
        onNavigateHome={navigateHome}
        onNavigateSource={navigateToSource}
        onNavigateCell={navigateToCell}
        onTabChange={setTab}
        onNavigateConverter={navigateToConverter}
        themes={THEMES}
        currentTheme={theme.key}
        onThemeChange={setThemeKey}
      />
      <div className="page-content">
        {state.view === 'library' && (
          <LibraryPage onSelectSource={handleSelectSource} />
        )}
        {state.view === 'source' && (
          <SourcePage
            sourceName={state.sourceName}
            sourcePath={state.sourcePath}
            onSelectCell={handleSelectCell}
            onSelectDataset={ds => navigateToDataset(
              state.sourceName,
              state.sourcePath,
              state.sourceName,
              state.sourcePath,
              ds.path,
              ds.name,
            )}
          />
        )}
        {state.view === 'cell' && (
          <CellPage
            cellName={state.cellName}
            cellPath={state.cellPath}
            onSelectDataset={handleSelectDataset}
          />
        )}
        {state.view === 'dataset' && (
          <DatasetPage
            datasetPath={state.datasetPath}
            datasetName={state.datasetName}
            tab={state.tab}
            filter={state.filter}
            onSetTab={setTab}
          />
        )}
        {state.view === 'converter' && (
          <ConverterPage status={converterStatus} onRefresh={fetchConverterStatus} />
        )}
      </div>
    </div>
  )
}
