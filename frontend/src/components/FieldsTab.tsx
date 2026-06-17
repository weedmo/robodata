import { useEffect, useState } from 'react'
import { useFields } from '../hooks/useFields'

interface FieldsTabProps {
  datasetPath: string
  readOnly?: boolean
}

type FieldsSection = 'info' | 'columns'

export function FieldsTab({ datasetPath, readOnly = false }: FieldsTabProps) {
  const {
    infoFields, episodeColumns, loading, error,
    fetchInfoFields, fetchEpisodeColumns,
    updateInfoField, deleteInfoField, addEpisodeColumn,
  } = useFields()
  const [section, setSection] = useState<FieldsSection>('info')

  useEffect(() => {
    void fetchInfoFields(datasetPath)
    void fetchEpisodeColumns(datasetPath)
  }, [datasetPath, fetchInfoFields, fetchEpisodeColumns])

  return (
    <div className="fields-layout">
      <div className="fields-nav">
        <button
          className={`fields-nav-item${section === 'info' ? ' active' : ''}`}
          onClick={() => setSection('info')}
        >
          Dataset Info
        </button>
        <button
          className={`fields-nav-item${section === 'columns' ? ' active' : ''}`}
          onClick={() => setSection('columns')}
        >
          Episode Columns
        </button>
      </div>

      <div className="fields-content">
        {loading && <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Loading...</div>}
        {error && <div style={{ fontSize: 11, color: 'var(--c-red)' }}>{error}</div>}

        {section === 'info' && (
          <InfoFieldsPanel
            fields={infoFields}
            datasetPath={datasetPath}
            readOnly={readOnly}
            onUpdate={updateInfoField}
            onDelete={deleteInfoField}
          />
        )}
        {section === 'columns' && (
          <EpisodeColumnsPanel
            columns={episodeColumns}
            datasetPath={datasetPath}
            readOnly={readOnly}
            onAddColumn={addEpisodeColumn}
          />
        )}
      </div>
    </div>
  )
}

function InfoFieldsPanel({
  fields, datasetPath, readOnly, onUpdate, onDelete,
}: {
  fields: { key: string; value: unknown; dtype: string; is_system: boolean }[]
  datasetPath: string
  readOnly: boolean
  onUpdate: (path: string, key: string, value: unknown) => Promise<void>
  onDelete: (path: string, key: string) => Promise<void>
}) {
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')
  const [newType, setNewType] = useState('string')

  const handleAdd = () => {
    if (!newKey.trim()) return
    let val: unknown = newValue
    if (newType === 'number') val = Number(newValue)
    if (newType === 'boolean') val = newValue === 'true'
    void onUpdate(datasetPath, newKey.trim(), val)
    setNewKey('')
    setNewValue('')
  }

  const systemFields = fields.filter(f => f.is_system)
  const customFields = fields.filter(f => !f.is_system)

  return (
    <div>
      <div style={{ fontSize: 10, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>
        System fields
      </div>
      <table className="field-table">
        <thead><tr><th>Key</th><th>Value</th><th>Type</th></tr></thead>
        <tbody>
          {systemFields.map(f => (
            <tr key={f.key}>
              <td className="system">{f.key}</td>
              <td className="system">{JSON.stringify(f.value)}</td>
              <td className="system">{f.dtype}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div style={{ fontSize: 10, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.06em', margin: '16px 0 8px' }}>
        Custom fields
      </div>
      {readOnly && (
        <div className="parquet-warning">
          Raw fields are read-only in v1.
        </div>
      )}
      <table className="field-table">
        <thead><tr><th>Key</th><th>Value</th><th>Type</th><th></th></tr></thead>
        <tbody>
          {customFields.map(f => (
            <tr key={f.key}>
              <td className="custom">{f.key}</td>
              <td className="custom">{JSON.stringify(f.value)}</td>
              <td className="custom">{f.dtype}</td>
              <td>{!readOnly && <button className="field-delete-btn" onClick={() => void onDelete(datasetPath, f.key)}>×</button>}</td>
            </tr>
          ))}
          {customFields.length === 0 && (
            <tr><td colSpan={4} style={{ color: 'var(--text-muted)' }}>No custom fields</td></tr>
          )}
        </tbody>
      </table>

      {!readOnly && (
        <div className="field-add-form">
          <label>Key<input value={newKey} onChange={e => setNewKey(e.target.value)} placeholder="field_name" /></label>
          <label>Type<select value={newType} onChange={e => setNewType(e.target.value)}>
            <option value="string">string</option>
            <option value="number">number</option>
            <option value="boolean">boolean</option>
          </select></label>
          <label>Default<input value={newValue} onChange={e => setNewValue(e.target.value)} placeholder="value" /></label>
          <button className="btn-primary" onClick={handleAdd} disabled={!newKey.trim()}>Add</button>
        </div>
      )}
    </div>
  )
}

function EpisodeColumnsPanel({
  columns, datasetPath, readOnly, onAddColumn,
}: {
  columns: { name: string; dtype: string; is_system: boolean }[]
  datasetPath: string
  readOnly: boolean
  onAddColumn: (path: string, name: string, dtype: string, defaultValue: unknown) => Promise<void>
}) {
  const [newName, setNewName] = useState('')
  const [newDtype, setNewDtype] = useState('string')
  const [newDefault, setNewDefault] = useState('')

  const handleAdd = () => {
    if (!newName.trim()) return
    let val: unknown = newDefault
    if (newDtype === 'int64') val = Number(newDefault) || 0
    if (newDtype === 'float64') val = Number(newDefault) || 0.0
    if (newDtype === 'bool') val = newDefault === 'true'
    void onAddColumn(datasetPath, newName.trim(), newDtype, val)
    setNewName('')
    setNewDefault('')
  }

  return (
    <div>
      <div className="parquet-warning">
        {readOnly
          ? 'Raw episode columns are read-only in v1.'
          : 'Adding a column rewrites all episode parquet files. This may take time for large datasets.'}
      </div>

      <table className="field-table">
        <thead><tr><th>Column</th><th>Type</th><th>Kind</th></tr></thead>
        <tbody>
          {columns.map(c => (
            <tr key={c.name}>
              <td className={c.is_system ? 'system' : 'custom'}>{c.name}</td>
              <td className={c.is_system ? 'system' : 'custom'}>{c.dtype}</td>
              <td className={c.is_system ? 'system' : 'custom'}>{c.is_system ? 'system' : 'custom'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {!readOnly && (
        <div className="field-add-form">
          <label>Column name<input value={newName} onChange={e => setNewName(e.target.value)} placeholder="column_name" /></label>
          <label>Type<select value={newDtype} onChange={e => setNewDtype(e.target.value)}>
            <option value="string">string</option>
            <option value="int64">int64</option>
            <option value="float64">float64</option>
            <option value="bool">bool</option>
          </select></label>
          <label>Default<input value={newDefault} onChange={e => setNewDefault(e.target.value)} placeholder="default" /></label>
          <button className="btn-primary" onClick={handleAdd} disabled={!newName.trim()}>Add Column</button>
        </div>
      )}
    </div>
  )
}
