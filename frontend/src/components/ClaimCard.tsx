import { useState } from 'react'
import type { Claim } from '../types'
import { VERDICT_LABEL } from '../types'

interface Props {
  claim: Claim
  compact?: boolean
  onRecheck?: (id: string) => void
}

const METHOD_LABEL = { cache: 'memória', db: 'base de notícias', web: 'busca na web', deep: 'checagem profunda' } as const
const METHOD_TITLE = {
  cache: 'Reaproveitou o veredito de uma afirmação idêntica já checada (sem chamadas de IA)',
  db: 'Respondida pela base de notícias',
  web: 'Respondida por busca na web',
  deep: 'Respondida pela checagem profunda (modelo mais forte, várias buscas)',
} as const

export function ClaimCard({ claim, compact, onRecheck }: Props) {
  const [open, setOpen] = useState(false)
  const expanded = compact || open
  return (
    <article
      className={`claim claim--${claim.verdict}${compact ? ' claim--compact' : ''}${expanded ? '' : ' claim--collapsed'}`}
      onClick={() => !compact && setOpen((o) => !o)}
      title={compact ? undefined : open ? 'Clique para recolher' : 'Clique para ver explicação e fontes'}
    >
      <header className="claim__header">
        <span className="claim__badge">
          {claim.verdict === 'pending' && <span className="spinner" aria-hidden />}
          {VERDICT_LABEL[claim.verdict]}
        </span>
        {claim.verdict !== 'pending' && <span className="claim__confidence">{Math.round(claim.confidence * 100)}%</span>}
        {claim.method && (
          <span className="claim__method" title={METHOD_TITLE[claim.method]}>
            {METHOD_LABEL[claim.method]}
          </span>
        )}
        {claim.deep_pending && (
          <span className="claim__deep" title="Rodando uma checagem mais profunda: modelo mais forte, várias buscas na web">
            <span className="spinner" aria-hidden /> checagem profunda…
          </span>
        )}
        <span className="claim__time">{fmt(claim.start)}</span>
      </header>
      <p className="claim__text">“{claim.text}”</p>
      {claim.explanation && <p className="claim__explanation">{claim.explanation}</p>}
      {expanded && !compact && claim.sources.length > 0 && (
        <ul className="claim__sources">
          {claim.sources.map((s) => (
            <li key={s.url}>
              <a href={s.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                {s.title || hostname(s.url)}
              </a>
            </li>
          ))}
        </ul>
      )}
      {expanded && !compact && (
        <footer className="claim__footer">
          {claim.captured_at && claim.extracted_at && (
            <span className="claim__latency" title="Segundos depois de o áudio chegar do YouTube (medido no servidor)">
              ⏱ +{(claim.extracted_at - claim.captured_at).toFixed(1)}s
              {claim.verified_at && ` · veredito +${(claim.verified_at - claim.captured_at).toFixed(1)}s`}
            </span>
          )}
          {onRecheck && claim.verdict !== 'pending' && !claim.deep_pending && (
            <button type="button" className="link" onClick={(e) => { e.stopPropagation(); onRecheck(claim.id) }}>
              rechecar
            </button>
          )}
        </footer>
      )}
    </article>
  )
}

function fmt(s: number) {
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${sec.toString().padStart(2, '0')}`
}

function hostname(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}
