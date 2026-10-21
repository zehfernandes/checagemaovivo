import { useCallback, useEffect, useMemo, useState } from 'react'
import { adminHeaders } from './admin'
import type { Source, Verdict } from './types'
import { VERDICT_LABEL } from './types'

type Label = 'correct' | 'wrong' | 'not_claim' | 'unsure'
type Final = Exclude<Verdict, 'pending'>

interface Run { name: string; title: string; created: number; windows: number; items: number; labelled: number }
interface Item {
  id: string; run: string; start: number; end: number; segment: string; claim: string
  verdict: Verdict; confidence: number; explanation: string; sources: Source[]; method: string | null
  label: Label | null; correct_verdict: Final | null; note: string | null; source_url: string | null
}
interface Missed { id: string; start: number; segment: string; claim: string; note: string }
interface Segment { idx: number; start: number; end: number; text: string }
interface Metrics {
  items: number; labelled: number; labels: Record<Label, number>; missed: number
  extraction_precision: number | null; extraction_recall: number | null; verdict_accuracy: number | null
  per_method: Record<string, { correct: number; wrong: number; accuracy: number }>
}

const LABEL_TEXT: Record<Label, string> = { correct: 'Correto', wrong: 'Veredito errado', not_claim: 'Não é afirmação', unsure: 'Não sei' }
const FINALS: Final[] = ['fake', 'valid', 'inconclusive']
const METHOD_PT: Record<string, string> = { cache: 'memória', db: 'base', web: 'web', deep: 'profunda' }
const FILTER_TEXT: Record<string, string> = {
  unlabelled: 'sem rótulo', all: 'todas', correct: 'corretas', wrong: 'erradas', not_claim: 'não é afirmação', unsure: 'não sei',
  fake: 'falsas', valid: 'verdadeiras', inconclusive: 'inconclusivas',
}

export function Review() {
  const [runs, setRuns] = useState<Run[]>([])
  const [run, setRun] = useState<string>(() => new URLSearchParams(location.hash.split('?')[1]).get('run') || '')
  const [items, setItems] = useState<Item[]>([])
  const [missed, setMissed] = useState<Missed[]>([])
  const [metrics, setMetrics] = useState<Metrics | null>(null)
  const [segments, setSegments] = useState<Segment[]>([])
  const [filter, setFilter] = useState<'all' | 'unlabelled' | Label | Final>('unlabelled')
  const [tab, setTab] = useState<'claims' | 'transcript'>('claims')
  const [evalResult, setEvalResult] = useState<string>('')
  const [evaluating, setEvaluating] = useState(false)

  useEffect(() => {
    fetch('/review/runs').then((r) => r.json()).then((rs: Run[]) => {
      setRuns(rs)
      if (!run && rs[0]) setRun(rs[0].name)
    }).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const load = useCallback(() => {
    if (!run) return
    fetch(`/review/items?run=${encodeURIComponent(run)}`).then((r) => r.json()).then((d) => {
      setItems(d.items); setMissed(d.missed); setMetrics(d.metrics)
    }).catch(() => {})
    fetch(`/review/segments?run=${encodeURIComponent(run)}`).then((r) => r.json()).then(setSegments).catch(() => {})
  }, [run])
  useEffect(load, [load])

  const visible = useMemo(() => items.filter((i) => {
    if (filter === 'all') return true
    if (filter === 'unlabelled') return !i.label
    if (FINALS.includes(filter as Final)) return i.verdict === filter
    return i.label === filter
  }), [items, filter])

  async function save(item: Item, label: Label, extra: Partial<Item> = {}) {
    const body = {
      item_id: item.id, label,
      correct_verdict: label === 'wrong' ? (extra.correct_verdict ?? item.correct_verdict ?? null) : null,
      note: extra.note ?? item.note ?? '', source_url: extra.source_url ?? item.source_url ?? '',
    }
    setItems((is) => is.map((i) => (i.id === item.id ? { ...i, ...body } : i)))
    await fetch('/review/label', { method: 'POST', headers: { 'content-type': 'application/json', ...adminHeaders() }, body: JSON.stringify(body) })
    fetch(`/review/items?run=${encodeURIComponent(run)}`).then((r) => r.json()).then((d) => setMetrics(d.metrics)).catch(() => {})
    fetch('/review/runs').then((r) => r.json()).then(setRuns).catch(() => {})
  }

  async function addMissed(seg: Segment) {
    const claim = window.prompt('Afirmação que deveria ter sido checada (uma frase factual curta):', '')
    if (!claim) return
    await fetch('/review/missed', { method: 'POST', headers: { 'content-type': 'application/json', ...adminHeaders() },
      body: JSON.stringify({ run, start: seg.start, segment: seg.text, claim }) })
    load()
  }
  async function removeMissed(id: string) {
    await fetch(`/review/missed/${id}`, { method: 'DELETE', headers: adminHeaders() })
    load()
  }
  async function runEval() {
    setEvaluating(true); setEvalResult('')
    try {
      const r = await fetch(`/review/eval?run=${encodeURIComponent(run)}&rerun=true`, { headers: adminHeaders() })
      const d = await r.json()
      setEvalResult(JSON.stringify(d.rerun ?? d, null, 2))
    } finally { setEvaluating(false) }
  }

  const pct = (v: number | null) => (v == null ? '–' : `${Math.round(v * 100)}%`)

  return (
    <div className="app review">
      <header className="topbar">
        <h1><span className="logo">●</span> Revisão <span className="muted">· rotulagem offline</span></h1>
        <select value={run} onChange={(e) => setRun(e.target.value)}>
          {runs.map((r) => <option key={r.name} value={r.name}>{r.title} ({r.items} afirmações, {r.labelled} rotuladas)</option>)}
        </select>
        <a className="btn btn--ghost" href={`/review/export?run=${encodeURIComponent(run)}`} download>Baixar conjunto de referência (.jsonl)</a>
        <button type="button" className="btn btn--ghost" onClick={runEval} disabled={evaluating || !run}>
          {evaluating ? 'Avaliando…' : 'Avaliar pipeline atual'}
        </button>
        <a className="btn btn--ghost" href="#">← Ao vivo</a>
      </header>

      {metrics && (
        <div className="review__metrics">
          <span><strong>{metrics.labelled}</strong>/{metrics.items} rotuladas</span>
          <span>Precisão da extração <strong>{pct(metrics.extraction_precision)}</strong></span>
          <span>Cobertura da extração <strong>{pct(metrics.extraction_recall)}</strong> ({metrics.missed} perdidas)</span>
          <span>Acerto dos vereditos <strong>{pct(metrics.verdict_accuracy)}</strong></span>
          {Object.entries(metrics.per_method).map(([m, v]) => (
            <span key={m}>{METHOD_PT[m] ?? m}: <strong>{pct(v.accuracy)}</strong> ({v.correct + v.wrong})</span>
          ))}
        </div>
      )}
      {evalResult && <pre className="review__eval">{evalResult}</pre>}

      <main className="review__main">
        <nav className="tabs">
          <button className={tab === 'claims' ? 'active' : ''} onClick={() => setTab('claims')}>Afirmações ({items.length})</button>
          <button className={tab === 'transcript' ? 'active' : ''} onClick={() => setTab('transcript')}>Transcrição · marcar perdidas ({missed.length})</button>
        </nav>
        {tab === 'claims' ? (
          <>
            <div className="review__filters">
              {(['unlabelled', 'all', 'correct', 'wrong', 'not_claim', 'unsure', 'fake', 'valid', 'inconclusive'] as const).map((f) => (
                <button key={f} className={filter === f ? 'active' : ''} onClick={() => setFilter(f)}>{FILTER_TEXT[f]}</button>
              ))}
            </div>
            <div className="review__list">
              {visible.length === 0 && <p className="muted">Nada aqui.</p>}
              {visible.map((it) => <ReviewItem key={it.id} item={it} onSave={save} />)}
            </div>
          </>
        ) : (
          <div className="review__list">
            {missed.length > 0 && (
              <div className="review__missed">
                <strong>Afirmações perdidas</strong>
                {missed.map((m) => (
                  <p key={m.id}><span className="transcript__time">{fmt(m.start)}</span> {m.claim}
                    <button className="btn btn--ghost btn--small" onClick={() => removeMissed(m.id)}>remover</button></p>
                ))}
              </div>
            )}
            {segments.map((s) => (
              <p key={s.idx} className="transcript__line review__segment">
                <span className="transcript__time">{fmt(s.start)}</span>{s.text}
                <button className="btn btn--ghost btn--small" onClick={() => addMissed(s)}>+ afirmação perdida</button>
              </p>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}

function ReviewItem({ item, onSave }: { item: Item; onSave: (i: Item, l: Label, extra?: Partial<Item>) => void }) {
  const [correct, setCorrect] = useState<Final | ''>(item.correct_verdict ?? '')
  const [url, setUrl] = useState(item.source_url ?? '')
  const [note, setNote] = useState(item.note ?? '')
  const [open, setOpen] = useState(item.label === 'wrong')
  return (
    <article className={`claim claim--${item.verdict} review__item ${item.label ? `review__item--${item.label}` : ''}`}>
      <header className="claim__header">
        <span className="claim__badge">{VERDICT_LABEL[item.verdict]}</span>
        <span className="claim__confidence">{Math.round(item.confidence * 100)}%</span>
        {item.method && <span className={`claim__method claim__method--${item.method}`}>{METHOD_PT[item.method] ?? item.method}</span>}
        <span className="claim__time">{fmt(item.start)}</span>
      </header>
      <p className="claim__text">“{item.claim}”</p>
      <p className="review__segment-text">{item.segment}</p>
      {item.explanation && <p className="claim__explanation">{item.explanation}</p>}
      {item.sources.length > 0 && (
        <ul className="claim__sources">{item.sources.map((s) => <li key={s.url}><a href={s.url} target="_blank" rel="noreferrer">{s.title || s.url}</a></li>)}</ul>
      )}
      <div className="review__actions">
        {(['correct', 'wrong', 'not_claim', 'unsure'] as Label[]).map((l) => (
          <button key={l} type="button" className={`review__label review__label--${l} ${item.label === l ? 'active' : ''}`}
            onClick={() => { if (l === 'wrong') { setOpen(true) } else { setOpen(false); onSave(item, l) } }}>
            {LABEL_TEXT[l]}
          </button>
        ))}
      </div>
      {open && (
        <div className="review__fix">
          <select value={correct} onChange={(e) => setCorrect(e.target.value as Final)}>
            <option value="">Veredito correto…</option>
            {FINALS.map((v) => <option key={v} value={v}>{VERDICT_LABEL[v]}</option>)}
          </select>
          <input placeholder="URL da fonte que comprova (entra na base de notícias)" value={url} onChange={(e) => setUrl(e.target.value)} />
          <input placeholder="Observação (opcional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <button type="button" className="btn" disabled={!correct}
            onClick={() => onSave(item, 'wrong', { correct_verdict: correct || null, source_url: url, note })}>Salvar</button>
        </div>
      )}
    </article>
  )
}

function fmt(s: number) {
  const m = Math.floor(s / 60)
  return `${m}:${Math.floor(s % 60).toString().padStart(2, '0')}`
}
