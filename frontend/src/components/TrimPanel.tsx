import { useState } from 'react'
import type { CSSProperties } from 'react'
import type { Episode } from '../types/index'
import { CyclesWorkflow } from './trim/CyclesWorkflow'
import { DeleteWorkflow } from './trim/DeleteWorkflow'
import { OutWorkflow } from './trim/OutWorkflow'
import type { TrimTabId } from './trim/shared'
import { trimStyles as s } from './trim/styles'

interface TrimPanelProps {
  datasetPath: string | null
  episodes: Episode[]
}

const TRIM_TABS: TrimTabId[] = ['out', 'delete', 'cycles']

function tabButtonStyle(tab: TrimTabId, activeTab: TrimTabId): CSSProperties {
  if (tab !== activeTab) {
    return s.tabBtn
  }
  if (tab === 'delete') {
    return { ...s.tabBtn, ...s.tabBtnActive, color: 'var(--c-red)' }
  }
  return { ...s.tabBtn, ...s.tabBtnActive }
}

function tabLabel(tab: TrimTabId): string {
  return tab.charAt(0).toUpperCase() + tab.slice(1)
}

export function TrimPanel({ datasetPath, episodes }: TrimPanelProps) {
  const [tab, setTab] = useState<TrimTabId>('out')

  return (
    <div style={s.container}>
      <div style={s.body}>
        <div style={s.tabs}>
          {TRIM_TABS.map(t => (
            <button
              key={t}
              style={tabButtonStyle(t, tab)}
              onClick={() => setTab(t)}
            >
              {tabLabel(t)}
            </button>
          ))}
        </div>

        {tab === 'out' && <OutWorkflow datasetPath={datasetPath} episodes={episodes} />}
        {tab === 'delete' && <DeleteWorkflow datasetPath={datasetPath} episodes={episodes} />}
        {tab === 'cycles' && <CyclesWorkflow datasetPath={datasetPath} />}
      </div>
    </div>
  )
}
