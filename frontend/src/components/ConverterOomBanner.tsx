interface Props {
  finishedAt: string | null
}

function formatFinishedAt(finishedAt: string) {
  const parsed = new Date(finishedAt)
  if (Number.isNaN(parsed.getTime())) return finishedAt
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed)
}

export function ConverterOomBanner({ finishedAt }: Props) {
  return (
    <div className="converter-oom-banner" role="alert">
      <div className="converter-oom-banner-title">
        Converter stopped because it hit the memory limit and was OOM-killed.
      </div>
      {finishedAt && (
        <div className="converter-oom-banner-meta">
          <span className="converter-oom-banner-label">Last finished</span>
          <span className="converter-oom-banner-value">
            {formatFinishedAt(finishedAt)}
          </span>
        </div>
      )}
    </div>
  )
}
