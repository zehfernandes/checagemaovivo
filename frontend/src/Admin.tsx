import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { adminHeaders, adminPassword } from './admin'
import { useSession } from './useSession'
import type { Claim, NewsStats, Room as PublicRoom, Source, UsageSummary, Verdict } from './types'
import { VERDICT_LABEL } from './types'

type Final = Exclude<Verdict, 'pending'>
type Tab = 'rooms' | 'claims' | 'news'

interface Room extends PublicRoom {
  created: number; viewers: number; processed_to: number; state: string; message: string; usage: UsageSummary | null
}
interface Doc { id: number; url: string; source: string; title: string; published: number | null; kind: string; added: number; chunks: number; excerpt: string }

const KIND_LABEL: Record<string, string> = { debate: 'Debate', entrevista: 'Entrevista', outro: 'Outro' }
const METHOD_PT: Record<string, string> = { cache: 'memória', db: 'base', web: 'web', deep: 'profunda', human: 'humano' }
const FINALS: Final[] = ['fake', 'valid', 'inconclusive']

async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = { ...adminHeaders(), ...(init.body ? { 'content-type': 'application/json' } : {}) }
  const r = await fetch(path, { ...init, headers })
  const d = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(d.detail || `Erro ${r.status}`)
  return d as T
}

export function Admin() {
  const [auth, setAuth] = useState<'checking' | 'in' | 'out'>('checking')
  useEffect(() => {
    fetch('/admin/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password: adminPassword() }) })
      .then((r) => r.json()).then((d) => setAuth(d.ok ? 'in' : 'out')).catch(() => setAuth('out'))
  }, [])
  if (auth === 'checking') return <div className="admin admin--center muted">Carregando…</div>
  if (auth === 'out') return <Login onOk={() => setAuth('in')} />
  return <Dashboard onLogout={() => { localStorage.removeItem('admin'); setAuth('out') }} />
}

function Login({ onOk }: { onOk: () => void }) {
  const [pwd, setPwd] = useState('')
  const [error, setError] = useState(false)
  async function submit(e: FormEvent) {
    e.preventDefault()
    const r = await fetch('/admin/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password: pwd }) })
    const d = await r.json()
    if (d.ok) { localStorage.setItem('admin', pwd); onOk() } else setError(true)
  }
  return (
    <div className="admin admin--center">
      <form className="admin__login" onSubmit={submit}>
        <h1><span className="logo">●</span> Administração</h1>
        <input type="password" autoFocus value={pwd} onChange={(e) => { setPwd(e.target.value); setError(false) }} placeholder="Senha" className={error ? 'error' : ''} />
        {error && <p className="admin__error">Senha inválida</p>}
        <button type="submit" className="btn" disabled={!pwd}>Entrar</button>
      </form>
    </div>
  )
}

function Dashboard({ onLogout }: { onLogout: () => void }) {
  const params = new URLSearchParams(location.search)
  const [tab, setTab] = useState<Tab>((params.get('tab') as Tab) || 'rooms')
  const [rooms, setRooms] = useState<Room[]>([])
  const [roomId, setRoomId] = useState<string>(params.get('room') || '')
  const [toast, setToast] = useState<string | null>(null)
  const notify = useCallback((msg: string) => { setToast(msg); setTimeout(() => setToast(null), 4000) }, [])

  async function loadRooms() {
    try { setRooms(await api<Room[]>('/admin/rooms')) } catch (e) { notify((e as Error).message) }
  }
  useEffect(() => {
    // oxlint-disable-next-line react-hooks/set-state-in-effect -- initial load + 5s poll of room state
    loadRooms()
    const t = setInterval(loadRooms, 5000)
    return () => clearInterval(t)
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- poll once per mount
  }, [])
  useEffect(() => {
    const p = new URLSearchParams()
    p.set('tab', tab)
    if (roomId) p.set('room', roomId)
    history.replaceState(null, '', `/admin?${p}`)
  }, [tab, roomId])
  const room = rooms.find((r) => r.id === roomId) ?? rooms.find((r) => r.processing || r.claims > 0) ?? rooms[0]

  return (
    <div className="app admin">
      <header className="topbar admin__topbar">
        <h1><span className="logo">●</span> Administração</h1>
        <nav className="tabs admin__tabs">
          <button className={tab === 'rooms' ? 'active' : ''} onClick={() => setTab('rooms')}>Salas {rooms.length > 0 && <span className="muted">{rooms.length}</span>}</button>
          <button className={tab === 'claims' ? 'active' : ''} onClick={() => setTab('claims')}>Checagens</button>
          <button className={tab === 'news' ? 'active' : ''} onClick={() => setTab('news')}>Base de notícias</button>
        </nav>
        <span className="admin__spacer" />
        <a className="link" href="/#review">Revisão offline</a>
        <a className="link" href="/" target="_blank" rel="noreferrer">Ver como espectador ↗</a>
        <button type="button" className="link" onClick={onLogout}>sair</button>
      </header>
      {toast && <div className="banner banner--error admin__toast" onClick={() => setToast(null)}>{toast}</div>}
      <main className="admin__main">
        {tab === 'rooms' && <Rooms rooms={rooms} reload={loadRooms} notify={notify} onOpen={(id) => { setRoomId(id); setTab('claims') }} />}
        {tab === 'claims' && <Claims rooms={rooms} room={room} onPick={setRoomId} reload={loadRooms} notify={notify} />}
        {tab === 'news' && <News notify={notify} />}
      </main>
    </div>
  )
}

// ---- Salas ----------------------------------------------------------------------------------
function Rooms({ rooms, reload, notify, onOpen }: { rooms: Room[]; reload: () => Promise<void>; notify: (m: string) => void; onOpen: (id: string) => void }) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [kind, setKind] = useState('debate')
  const [busy, setBusy] = useState<string | null>(null)
  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key)
    try { await fn(); await reload() } catch (e) { notify((e as Error).message) } finally { setBusy(null) }
  }
  async function create(e: FormEvent) {
    e.preventDefault()
    await run('create', async () => {
      await api('/admin/rooms', { method: 'POST', body: JSON.stringify({ name, url, kind }) })
      setName(''); setUrl('')
    })
  }
  return (
    <div className="admin__section">
      <form className="admin__form admin__form--inline" onSubmit={create}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Nome da sala (ex.: Entrevista com o candidato X)" required />
        <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="Link do YouTube (vídeo ou live)" required />
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {Object.entries(KIND_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <button type="submit" className="btn" disabled={busy === 'create'}>Criar sala</button>
      </form>
      {rooms.length === 0 && <p className="muted empty">Nenhuma sala ainda. Crie a primeira acima.</p>}
      <div className="admin__cards">
        {rooms.map((r) => <RoomCard key={r.id} room={r} busy={busy} run={run} onOpen={() => onOpen(r.id)} />)}
      </div>
    </div>
  )
}

function RoomCard({ room: r, busy, run, onOpen }: { room: Room; busy: string | null; run: (k: string, fn: () => Promise<unknown>) => Promise<void>; onOpen: () => void }) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(r.name)
  const [url, setUrl] = useState(r.url)
  const [kind, setKind] = useState(r.kind)
  const [save, setSave] = useState(false)
  return (
    <article className={`room${r.active ? '' : ' room--inactive'}`}>
      <header className="room__header">
        <span className={`room__kind`}>{KIND_LABEL[r.kind] ?? r.kind}</span>
        {r.is_live && r.processing && <span className="live-tag">AO VIVO</span>}
        {!r.active && <span className="muted">oculta para o público</span>}
        <span className="room__status">
          {r.processing && <span className="dot" />}
          <RoomState room={r} />
        </span>
      </header>
      {editing ? (
        <form className="admin__form" onSubmit={async (e) => { e.preventDefault(); await run(r.id, () => api(`/admin/rooms/${r.id}`, { method: 'PATCH', body: JSON.stringify({ name, url, kind }) })); setEditing(false) }}>
          <input value={name} onChange={(e) => setName(e.target.value)} required />
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} required />
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {Object.entries(KIND_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <div className="admin__actions">
            <button type="submit" className="btn" disabled={busy === r.id}>Salvar</button>
            <button type="button" className="link" onClick={() => setEditing(false)}>cancelar</button>
          </div>
        </form>
      ) : (
        <>
          <h2 className="room__name">{r.name}</h2>
          <p className="muted room__meta">
            <a href={r.url} target="_blank" rel="noreferrer">{r.title || r.url}</a>
            {' · '}{r.claims} afirmações
            {!r.is_live && r.processed_to > 0 && <> · {r.complete ? 'processado por completo' : `processado até ${fmt(r.processed_to)}`}</>}
            {r.viewers > 0 && <> · {r.viewers} {r.viewers === 1 ? 'espectador' : 'espectadores'}</>}
            {r.usage && <> · ≈ US${r.usage.usd.toFixed(2)}</>}
          </p>
        </>
      )}
      <footer className="admin__actions">
        {r.processing ? (
          <button type="button" className="btn btn--stop" disabled={busy === r.id} onClick={() => run(r.id, () => api(`/admin/rooms/${r.id}/stop`, { method: 'POST', body: '{}' }))}>Parar checagem</button>
        ) : (
          <button type="button" className="btn" disabled={busy === r.id || r.complete} title={r.complete ? 'Gravação já processada por completo' : 'Começa a transcrever e checar este vídeo para todos os espectadores'}
            onClick={() => run(r.id, () => api(`/admin/rooms/${r.id}/start`, { method: 'POST', body: JSON.stringify({ save }) }))}>
            {busy === r.id ? 'Abrindo…' : r.processed_to > 0 && !r.is_live ? 'Continuar checagem' : 'Iniciar checagem'}
          </button>
        )}
        {!r.processing && !r.complete && (
          <label className="admin__check" title="Menos chamadas de IA: sem checagem profunda automática, filtro mais rígido, no máximo 6 afirmações/min">
            <input type="checkbox" checked={save} onChange={(e) => setSave(e.target.checked)} /> econômico
          </label>
        )}
        <button type="button" className="link" onClick={onOpen}>checagens →</button>
        <a className="link" href={`/?room=${r.id}`} target="_blank" rel="noreferrer">ver sala ↗</a>
        <span className="admin__spacer" />
        <button type="button" className="link" onClick={() => run(r.id, () => api(`/admin/rooms/${r.id}`, { method: 'PATCH', body: JSON.stringify({ active: !r.active }) }))}>
          {r.active ? 'ocultar' : 'publicar'}
        </button>
        <button type="button" className="link" onClick={() => setEditing((v) => !v)}>editar</button>
        <button type="button" className="link link--danger" onClick={() => { if (confirm(`Excluir a sala "${r.name}"? As checagens ficam no cache.`)) run(r.id, () => api(`/admin/rooms/${r.id}`, { method: 'DELETE' })) }}>excluir</button>
      </footer>
    </article>
  )
}

function RoomState({ room: r }: { room: Room }) {
  if (r.processing) return <span className="status--listening">{r.message || 'Checando'}</span>
  if (r.state === 'error') return <span className="status--error">{r.message}</span>
  if (r.complete) return <span>Processado</span>
  if (r.processed_to > 0) return <span>Pausado</span>
  return <span className="muted">Não iniciado</span>
}

// ---- Checagens --------------------------------------------------------------------------------
function Claims({ rooms, room, onPick, reload, notify }: { rooms: Room[]; room: Room | undefined; onPick: (id: string) => void; reload: () => Promise<void>; notify: (m: string) => void }) {
  const session = useSession()
  const [filter, setFilter] = useState<'all' | Verdict>('all')
  const [q, setQ] = useState('')
  const url = room?.url
  useEffect(() => {
    if (url) session.start(url, '', 0, false, false, false, true)
    else session.stop()
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- (re)subscribe only when the room changes
  }, [url])

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return [...session.claims]
      .filter((c) => filter === 'all' || c.verdict === filter)
      .filter((c) => !needle || c.text.toLowerCase().includes(needle) || c.explanation.toLowerCase().includes(needle))
      .sort((a, b) => b.start - a.start)
  }, [session.claims, filter, q])
  const counts = useMemo(() => {
    const c: Record<Verdict, number> = { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    for (const cl of session.claims) c[cl.verdict]++
    return c
  }, [session.claims])

  async function save(c: Claim, verdict: Final, explanation: string, sources: Source[]) {
    if (!url) return
    await api('/admin/claim', { method: 'POST', body: JSON.stringify({ url, claim_id: c.id, verdict, explanation, sources }) })
  }
  async function remove(c: Claim) {
    if (!room) return
    await api(`/admin/claims/${room.video_id}/${c.id}`, { method: 'DELETE' })
  }
  async function toggle() {
    if (!room) return
    try {
      await api(`/admin/rooms/${room.id}/${room.processing ? 'stop' : 'start'}`, { method: 'POST', body: '{}' })
      await reload()
    } catch (e) { notify((e as Error).message) }
  }

  return (
    <div className="admin__section">
      <div className="admin__bar">
        <select value={room?.id ?? ''} onChange={(e) => onPick(e.target.value)}>
          {rooms.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
        {room && (
          <>
            <span className="room__status">{room.processing && <span className="dot" />}<RoomState room={room} /></span>
            <span className="muted">{session.statusText}</span>
            <span className="admin__spacer" />
            {!room.complete && (
              <button type="button" className={room.processing ? 'btn btn--stop' : 'btn'} onClick={toggle}>
                {room.processing ? 'Parar checagem' : 'Iniciar checagem'}
              </button>
            )}
          </>
        )}
      </div>
      <div className="review__filters">
        {(['all', 'pending', 'fake', 'valid', 'inconclusive'] as const).map((f) => (
          <button key={f} className={filter === f ? 'active' : ''} onClick={() => setFilter(f)}>
            {f === 'all' ? `todas (${session.claims.length})` : `${VERDICT_LABEL[f].toLowerCase()} (${counts[f]})`}
          </button>
        ))}
        <input className="admin__search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar na afirmação ou explicação…" />
      </div>
      <div className="review__list">
        {!room && <p className="muted">Crie uma sala primeiro.</p>}
        {room && visible.length === 0 && <p className="muted">{session.claims.length === 0 ? 'Nenhuma afirmação ainda — as checagens aparecem aqui em tempo real.' : 'Nada com esse filtro.'}</p>}
        {visible.map((c) => <AdminClaim key={c.id} claim={c} onSave={save} onDelete={remove} onRecheck={session.recheck} notify={notify} />)}
      </div>
    </div>
  )
}

function AdminClaim({ claim: c, onSave, onDelete, onRecheck, notify }: {
  claim: Claim; onSave: (c: Claim, v: Final, e: string, s: Source[]) => Promise<void>; onDelete: (c: Claim) => Promise<void>
  onRecheck: (id: string) => void; notify: (m: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [verdict, setVerdict] = useState<Final>(c.verdict === 'pending' ? 'inconclusive' : c.verdict)
  const [explanation, setExplanation] = useState(c.explanation)
  const [sources, setSources] = useState<Source[]>(c.sources)
  const [saving, setSaving] = useState(false)
  function startEdit() {
    setVerdict(c.verdict === 'pending' ? 'inconclusive' : c.verdict)
    setExplanation(c.explanation)
    setSources(c.sources.length ? c.sources : [{ title: '', url: '' }])
    setEditing(true)
  }
  async function submit(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    try {
      await onSave(c, verdict, explanation, sources.filter((s) => s.url.trim()).map((s) => ({ title: s.title.trim(), url: s.url.trim() })))
      setEditing(false)
    } catch (err) { notify((err as Error).message) } finally { setSaving(false) }
  }
  return (
    <article className={`claim claim--compact claim--${c.verdict} admin__claim`}>
      <header className="claim__header">
        <span className="claim__badge">{c.verdict === 'pending' && <span className="spinner" aria-hidden />}{VERDICT_LABEL[c.verdict]}</span>
        {c.verdict !== 'pending' && <span className="claim__confidence">{Math.round(c.confidence * 100)}%</span>}
        {c.method && <span className="claim__method">{METHOD_PT[c.method] ?? c.method}</span>}
        {c.deep_pending && <span className="claim__deep"><span className="spinner" aria-hidden /> checagem profunda…</span>}
        <span className="claim__time">{fmt(c.start)}</span>
      </header>
      <p className="claim__text">“{c.text}”</p>
      {!editing && c.explanation && <p className="claim__explanation">{c.explanation}</p>}
      {!editing && c.sources.length > 0 && (
        <ul className="claim__sources">{c.sources.map((s, i) => <li key={`${s.url}-${i}`}><a href={s.url} target="_blank" rel="noreferrer">{s.title || s.url}</a></li>)}</ul>
      )}
      {editing ? (
        <form className="claim__edit admin__edit" onSubmit={submit}>
          <label>Status
            <select value={verdict} onChange={(e) => setVerdict(e.target.value as Final)}>
              {FINALS.map((v) => <option key={v} value={v}>{VERDICT_LABEL[v]}</option>)}
            </select>
          </label>
          <label>Descrição
            <textarea value={explanation} onChange={(e) => setExplanation(e.target.value)} rows={3} placeholder="Explicação que o público vai ler" />
          </label>
          <div className="admin__sources">
            <span>Links</span>
            {sources.map((s, i) => (
              <div key={i} className="admin__source">
                <input value={s.title} onChange={(e) => setSources(sources.map((x, j) => (j === i ? { ...x, title: e.target.value } : x)))} placeholder="Título" />
                <input type="url" value={s.url} onChange={(e) => setSources(sources.map((x, j) => (j === i ? { ...x, url: e.target.value } : x)))} placeholder="https://…" />
                <button type="button" className="link link--danger" onClick={() => setSources(sources.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
            <button type="button" className="link" onClick={() => setSources([...sources, { title: '', url: '' }])}>+ link</button>
          </div>
          <div className="admin__actions">
            <button type="submit" disabled={saving}>{saving ? 'salvando…' : 'Publicar para todos'}</button>
            <button type="button" className="link" onClick={() => setEditing(false)}>cancelar</button>
          </div>
        </form>
      ) : (
        <footer className="admin__actions">
          <button type="button" className="link" onClick={startEdit}>editar</button>
          {c.verdict !== 'pending' && !c.deep_pending && <button type="button" className="link" onClick={() => onRecheck(c.id)}>rechecar com IA</button>}
          <span className="admin__spacer" />
          <button type="button" className="link link--danger" onClick={() => { if (confirm('Excluir esta afirmação para todos os espectadores?')) onDelete(c).catch((e) => notify(e.message)) }}>excluir</button>
        </footer>
      )}
    </article>
  )
}

// ---- Base de notícias -------------------------------------------------------------------------
function News({ notify }: { notify: (m: string) => void }) {
  const [stats, setStats] = useState<NewsStats | null>(null)
  const [docs, setDocs] = useState<Doc[]>([])
  const [total, setTotal] = useState(0)
  const [disabled, setDisabled] = useState(false)
  const [q, setQ] = useState('')
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const load = useCallback(async (query: string) => {
    try {
      const d = await api<{ total: number; docs: Doc[]; disabled?: boolean }>(`/admin/news/docs?q=${encodeURIComponent(query)}&limit=200`)
      setDocs(d.docs); setTotal(d.total); setDisabled(!!d.disabled)
      setStats(await (await fetch('/news')).json())
    } catch (e) { notify((e as Error).message) }
  }, [notify])
  useEffect(() => {
    const t = setTimeout(() => load(q), q ? 300 : 0)
    return () => clearTimeout(t)
  }, [q, load])

  async function add(e: FormEvent) {
    e.preventDefault()
    setBusy('add')
    try {
      await api('/admin/news/docs', { method: 'POST', body: JSON.stringify({ url, title, text }) })
      setUrl(''); setTitle(''); setText('')
      await load(q)
    } catch (err) { notify((err as Error).message) } finally { setBusy(null) }
  }
  async function del(d: Doc) {
    if (!confirm(`Remover "${d.title || d.url}" da base?`)) return
    try { await api(`/admin/news/docs/${d.id}`, { method: 'DELETE' }); await load(q) } catch (err) { notify((err as Error).message) }
  }
  async function refresh() {
    setBusy('refresh')
    try { await api('/news/refresh', { method: 'POST', body: '{}' }); await load(q) } catch (err) { notify((err as Error).message) } finally { setBusy(null) }
  }

  return (
    <div className="admin__section">
      {disabled && <div className="banner banner--error">Base de notícias desativada neste servidor (PROVIDER=mock ou sem chave da OpenAI).</div>}
      <div className="admin__bar">
        <span className="muted" title={stats ? Object.entries(stats.sources).map(([k, v]) => `${k}: ${v}`).join('\n') : ''}>
          {stats ? `${stats.docs} artigos · ${stats.chunks} trechos` : '–'}
          {stats?.last_refresh && <> · feeds atualizados {ago(stats.last_refresh)}</>}
        </span>
        <span className="admin__spacer" />
        <button type="button" className="btn btn--ghost" onClick={refresh} disabled={busy === 'refresh' || disabled}>{busy === 'refresh' ? 'Atualizando feeds…' : 'Atualizar feeds'}</button>
      </div>
      <form className="admin__form admin__form--doc" onSubmit={add}>
        <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="Link do artigo / checagem (o texto é extraído da página)" />
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Título (opcional)" />
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} placeholder="Ou cole aqui o texto (obrigatório se não houver link, ou se a página bloquear a leitura)" />
        <div className="admin__actions">
          <button type="submit" className="btn" disabled={busy === 'add' || disabled || (!url && text.trim().length < 40)}>{busy === 'add' ? 'Indexando…' : 'Adicionar à base'}</button>
          <span className="muted">Vira evidência imediata para as próximas checagens.</span>
        </div>
      </form>
      <div className="admin__bar">
        <input className="admin__search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar por título, fonte ou URL…" />
        <span className="muted">{total} resultados{total > docs.length && ` · mostrando ${docs.length}`}</span>
      </div>
      <div className="review__list">
        {docs.map((d) => (
          <article key={d.id} className="doc">
            <header className="claim__header">
              <span className="doc__source">{d.source}</span>
              <span className="muted">{d.kind}</span>
              <span className="muted">{d.chunks} {d.chunks === 1 ? 'trecho' : 'trechos'}</span>
              <span className="claim__time">{d.published ? new Date(d.published * 1000).toLocaleDateString('pt-BR') : ''}</span>
            </header>
            <p className="claim__text">{d.url.startsWith('http') ? <a href={d.url} target="_blank" rel="noreferrer">{d.title || d.url}</a> : (d.title || 'Entrada manual')}</p>
            {d.excerpt && <p className="claim__explanation">{d.excerpt}{d.excerpt.length >= 240 && '…'}</p>}
            <footer className="admin__actions">
              <span className="admin__spacer" />
              <button type="button" className="link link--danger" onClick={() => del(d)}>remover</button>
            </footer>
          </article>
        ))}
      </div>
    </div>
  )
}

function fmt(s: number) {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60).toString().padStart(2, '0')
  return h > 0 ? `${h}:${m.toString().padStart(2, '0')}:${sec}` : `${m}:${sec}`
}

function ago(ts: number) {
  const m = Math.round((Date.now() / 1000 - ts) / 60)
  if (m < 1) return 'agora'
  if (m < 60) return `há ${m} min`
  return `há ${Math.round(m / 60)} h`
}
