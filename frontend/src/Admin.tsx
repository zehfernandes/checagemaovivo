import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import { adminHeaders, clearAdminToken, loadGoogle, setAdminToken, type AdminUser, type AuthMode } from './admin'
import { useSession } from './useSession'
import type { Claim, NewsStats, Room as PublicRoom, Source, UsageSummary, Verdict } from './types'
import { VERDICT_LABEL } from './types'

type Final = Exclude<Verdict, 'pending'>
type Tab = 'rooms' | 'claims' | 'news' | 'glossary' | 'users'

interface Room extends PublicRoom {
  created: number; viewers: number; processed_to: number; state: string; message: string; usage: UsageSummary | null
}
interface Doc { id: number; url: string; source: string; title: string; published: number | null; kind: string; added: number; chunks: number; excerpt: string }
interface Fix { from: string; to: string }
interface ActivityLine { t: number; level: string; src: string; msg: string }
interface Activity { mode: string; in_flight: number; verifying: number; activity: ActivityLine[] }

const TABS: [Tab, string][] = [['rooms', 'Salas'], ['claims', 'Checagens'], ['news', 'Base de notícias'], ['glossary', 'Glossário'], ['users', 'Usuários']]
const CHECKER_TABS: Tab[] = ['claims', 'news', 'glossary'] // checkers: no rooms (create/start/stop) and no users
const KINDS: [string, string][] = [['debate', 'Debate'], ['entrevista', 'Entrevista'], ['outro', 'Outro']]
const KIND_LABEL = Object.fromEntries(KINDS)
const METHOD_PT: Record<string, string> = { cache: 'memória', db: 'base', web: 'web', deep: 'profunda', human: 'humano' }
const FINALS: [Final, string][] = [['fake', 'Falso'], ['valid', 'Verdadeiro'], ['inconclusive', 'Inconclusivo']]

async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = { ...adminHeaders(), ...(init.body ? { 'content-type': 'application/json' } : {}) }
  const r = await fetch(path, { ...init, headers })
  const d = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(d.detail || `Erro ${r.status}`)
  return d as T
}

// ---- primitives --------------------------------------------------------------------------------
function useEscape(fn: (() => void) | undefined) {
  useEffect(() => {
    if (!fn) return
    const on = (e: globalThis.KeyboardEvent) => { if (e.key === 'Escape') fn() }
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  }, [fn])
}

function cmdEnter(e: KeyboardEvent<HTMLFormElement>) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); e.currentTarget.requestSubmit() }
}

function PageHeader({ title, meta, children }: { title: ReactNode; meta?: ReactNode; children?: ReactNode }) {
  return (
    <header className="page__header">
      <h1 className="page__title">{title}</h1>
      {meta && <span className="page__meta">{meta}</span>}
      <span className="spacer" />
      {children}
    </header>
  )
}

function Panel({ title, meta, onClose, actions, footer, children }: {
  title: ReactNode; meta?: ReactNode; onClose: () => void; actions?: ReactNode; footer?: ReactNode; children: ReactNode
}) {
  useEscape(onClose)
  return (
    <aside className="panel">
      <header className="panel__header">
        <div className="panel__heading">
          <h2 className="panel__title">{title}</h2>
          {meta && <span className="muted">{meta}</span>}
        </div>
        <span className="spacer" />
        {actions}
        <button type="button" className="icon-btn" onClick={onClose} title="Fechar (Esc)" aria-label="Fechar">×</button>
      </header>
      <div className="panel__body">{children}</div>
      {footer && <footer className="panel__footer">{footer}</footer>}
    </aside>
  )
}

function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEscape(onClose)
  return (
    <div className="dialog__backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="dialog" role="dialog" aria-label={title}>
        <header className="dialog__header">
          <h2 className="panel__title">{title}</h2>
          <span className="spacer" />
          <button type="button" className="icon-btn" onClick={onClose} title="Fechar (Esc)" aria-label="Fechar">×</button>
        </header>
        {children}
      </div>
    </div>
  )
}

function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      {children}
      {hint && <span className="field__hint">{hint}</span>}
    </label>
  )
}

function Segmented<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <div className="segmented" role="radiogroup">
      {options.map(([v, label]) => (
        <button key={v} type="button" role="radio" aria-checked={value === v} className={`segmented__opt segmented__opt--${v}${value === v ? ' active' : ''}`} onClick={() => onChange(v)}>{label}</button>
      ))}
    </div>
  )
}

function Switch({ checked, onChange, label, hint, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint?: string; disabled?: boolean }) {
  return (
    <label className={`switch${disabled ? ' switch--disabled' : ''}`}>
      <span className="switch__text"><span>{label}</span>{hint && <span className="field__hint">{hint}</span>}</span>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch__track" aria-hidden><span className="switch__thumb" /></span>
    </label>
  )
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>
}

// ---- shell ------------------------------------------------------------------------------------
export function Admin() {
  const [me, setMe] = useState<AdminUser | null>(null)
  const [auth, setAuth] = useState<{ mode: AuthMode; google_client_id: string | null } | null>(null)
  const [checking, setChecking] = useState(true)
  useEffect(() => {
    fetch('/admin/me', { headers: adminHeaders() })
      .then(async (r) => {
        if (r.ok) { setMe(await r.json()); return }
        setAuth(await (await fetch('/admin/auth')).json())
      })
      .catch(() => setAuth({ mode: 'password', google_client_id: null }))
      .finally(() => setChecking(false))
  }, [])
  function logout() {
    clearAdminToken()
    window.google?.accounts.id.disableAutoSelect() // don't sign straight back in with the same Google account
    location.reload()
  }
  if (checking) return <div className="admin admin--center muted">Carregando…</div>
  if (!me) return <Login auth={auth} onOk={(token, user) => { setAdminToken(token); setMe(user) }} />
  return <Shell me={me} onLogout={logout} />
}

function Login({ auth, onOk }: { auth: { mode: AuthMode; google_client_id: string | null } | null; onOk: (token: string, user: AdminUser) => void }) {
  const [pwd, setPwd] = useState('')
  const [error, setError] = useState<string | null>(null)
  const button = useRef<HTMLDivElement>(null)
  const google = auth?.mode === 'google' && auth.google_client_id
  useEffect(() => {
    if (!google || !button.current) return
    const el = button.current
    loadGoogle().then((gid) => {
      gid.initialize({
        client_id: auth!.google_client_id!,
        callback: async ({ credential }) => {
          const r = await fetch('/admin/login/google', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ credential }) })
          const d = await r.json().catch(() => ({}))
          if (r.ok) onOk(d.token, d.user)
          else setError(d.detail || 'Não foi possível entrar')
        },
      })
      gid.renderButton(el, { theme: 'filled_black', size: 'large', shape: 'pill', text: 'signin_with', locale: 'pt-BR', width: 280 })
    }).catch((e) => setError((e as Error).message))
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- render the Google button once
  }, [google])
  async function submit(e: FormEvent) {
    e.preventDefault()
    const r = await fetch('/admin/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ password: pwd }) })
    const d = await r.json()
    if (d.ok) onOk(d.token, d.user); else setError('Senha inválida')
  }
  return (
    <div className="admin admin--center">
      <div className="admin__login">
        <h1><span className="logo">●</span> Administração</h1>
        {google ? (
          <>
            <p className="muted admin__login-hint">Entre com a conta Google cadastrada pela coordenação.</p>
            <div ref={button} className="admin__google" />
          </>
        ) : (
          <form className="admin__login-form" onSubmit={submit}>
            <input type="password" autoFocus value={pwd} onChange={(e) => { setPwd(e.target.value); setError(null) }} placeholder="Senha" className={error ? 'error' : ''} />
            <button type="submit" className="btn" disabled={!pwd}>Entrar</button>
          </form>
        )}
        {error && <p className="admin__error">{error}</p>}
      </div>
    </div>
  )
}

function Shell({ me, onLogout }: { me: AdminUser; onLogout: () => void }) {
  const params = new URLSearchParams(location.search)
  const owner = me.role === 'owner'
  const tabs = TABS.filter(([t]) => owner || CHECKER_TABS.includes(t))
  const asked = params.get('tab') as Tab
  const [tab, setTab] = useState<Tab>(tabs.some(([t]) => t === asked) ? asked : tabs[0][0])
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
  const live = rooms.filter((r) => r.processing).length

  return (
    <div className="admin shell">
      <nav className="sidebar">
        <div className="sidebar__brand"><span className="logo">●</span> Checagem ao vivo</div>
        <div className="sidebar__nav">
          {tabs.map(([t, label]) => (
            <button key={t} className={`sidebar__item${tab === t ? ' active' : ''}`} onClick={() => setTab(t)}>
              {label}
              {t === 'rooms' && live > 0 && <span className="sidebar__count sidebar__count--live">{live} ao vivo</span>}
              {t === 'rooms' && live === 0 && rooms.length > 0 && <span className="sidebar__count">{rooms.length}</span>}
            </button>
          ))}
        </div>
        <div className="sidebar__foot">
          <a className="sidebar__item" href="/" target="_blank" rel="noreferrer">Ver como espectador ↗</a>
          <a className="sidebar__item" href="/#review">Revisão offline</a>
          <div className="sidebar__user" title={me.email}>
            {me.picture ? <img src={me.picture} alt="" referrerPolicy="no-referrer" /> : <span className="sidebar__avatar">{(me.name || me.email)[0]?.toUpperCase()}</span>}
            <span className="sidebar__who"><span>{me.name || me.email}</span><span className="muted">{owner ? 'Owner' : 'Checador'}</span></span>
          </div>
          <button type="button" className="sidebar__item" onClick={onLogout}>Sair</button>
        </div>
      </nav>
      <main className="shell__main">
        {tab === 'rooms' && <RoomsPage rooms={rooms} reload={loadRooms} notify={notify} openClaims={(id) => { setRoomId(id); setTab('claims') }} />}
        {tab === 'claims' && <ClaimsPage rooms={rooms} room={room} onPick={setRoomId} reload={loadRooms} notify={notify} owner={owner} />}
        {tab === 'news' && <NewsPage notify={notify} />}
        {tab === 'glossary' && <GlossaryPage notify={notify} />}
        {tab === 'users' && owner && <UsersPage me={me} notify={notify} />}
      </main>
      {toast && <div className="toast" onClick={() => setToast(null)}>{toast}</div>}
    </div>
  )
}

// ---- Salas ------------------------------------------------------------------------------------
function roomState(r: Room): { word: string; cls: string } {
  if (r.processing) return { word: r.is_live ? 'Ao vivo' : 'Checando', cls: 'live' }
  if (r.state === 'error') return { word: 'Erro', cls: 'error' }
  if (r.complete) return { word: 'Processado', cls: 'done' }
  if (r.processed_to > 0) return { word: 'Pausado', cls: 'paused' }
  return { word: 'Não iniciado', cls: 'idle' }
}

function RoomsPage({ rooms, reload, notify, openClaims }: { rooms: Room[]; reload: () => Promise<void>; notify: (m: string) => void; openClaims: (id: string) => void }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const room = rooms.find((r) => r.id === selected)
  async function run(key: string, fn: () => Promise<unknown>) {
    setBusy(key)
    try { await fn(); await reload() } catch (e) { notify((e as Error).message) } finally { setBusy(null) }
  }
  return (
    <div className="page">
      <div className="page__content">
        <PageHeader title="Salas" meta={rooms.length > 0 && `${rooms.length} ${rooms.length === 1 ? 'sala' : 'salas'}`}>
          <button type="button" className="btn" onClick={() => setCreating(true)}>Nova sala</button>
        </PageHeader>
        {rooms.length === 0 ? (
          <Empty>
            <p>Nenhuma sala ainda.</p>
            <p className="muted">Uma sala liga um vídeo ou live (YouTube, g1/Globo) à checagem que todos os espectadores veem.</p>
            <button type="button" className="btn" onClick={() => setCreating(true)}>Criar a primeira sala</button>
          </Empty>
        ) : (
          <div className="rows">
            {rooms.map((r) => {
              const st = roomState(r)
              return (
                <button key={r.id} type="button" className={`row${selected === r.id ? ' active' : ''}${r.active ? '' : ' row--dim'}`} onClick={() => setSelected(r.id)}>
                  <span className={`state state--${st.cls}`}><span className="state__dot" />{st.word}</span>
                  <span className="row__main">
                    <span className="row__title">{r.name}</span>
                    <span className="row__sub">{r.title || r.url}</span>
                  </span>
                  <span className="chip">{KIND_LABEL[r.kind] ?? r.kind}</span>
                  {!r.active && <span className="chip chip--muted">oculta</span>}
                  <span className="row__meta">{r.claims} afirmações{r.viewers > 0 && ` · ${r.viewers} assistindo`}</span>
                  <span className="row__chevron">›</span>
                </button>
              )
            })}
          </div>
        )}
      </div>
      {room && <RoomPanel key={room.id} room={room} busy={busy} run={run} onClose={() => setSelected(null)} openClaims={() => openClaims(room.id)} />}
      {creating && <NewRoomDialog onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); setSelected(id) }} run={run} />}
    </div>
  )
}

function NewRoomDialog({ onClose, onCreated, run }: { onClose: () => void; onCreated: (id: string) => void; run: (k: string, fn: () => Promise<unknown>) => Promise<void> }) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [kind, setKind] = useState('debate')
  const [busy, setBusy] = useState(false)
  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    let id = ''
    await run('create', async () => { id = (await api<Room>('/admin/rooms', { method: 'POST', body: JSON.stringify({ name, url, kind }) })).id })
    setBusy(false)
    if (id) onCreated(id)
  }
  return (
    <Dialog title="Nova sala" onClose={onClose}>
      <form className="form" onSubmit={submit} onKeyDown={cmdEnter}>
        <Field label="Link da transmissão" hint="YouTube ou g1/Globo, gravação ou ao vivo. Fora do YouTube o espectador vê só as checagens, sem player.">
          <input type="url" autoFocus value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.youtube.com/watch?v=… ou https://g1.globo.com/…" required />
        </Field>
        <Field label="Nome">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Debate Band — 2º turno" required />
        </Field>
        <Field label="Tipo">
          <Segmented value={kind} options={KINDS} onChange={setKind} />
        </Field>
        <footer className="form__actions">
          <span className="kbd-hint">⌘↵ para criar</span>
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancelar</button>
          <button type="submit" className="btn" disabled={busy || !name || !url}>{busy ? 'Criando…' : 'Criar sala'}</button>
        </footer>
      </form>
    </Dialog>
  )
}

function RoomPanel({ room: r, busy, run, onClose, openClaims }: {
  room: Room; busy: string | null; run: (k: string, fn: () => Promise<unknown>) => Promise<void>; onClose: () => void; openClaims: () => void
}) {
  const [name, setName] = useState(r.name)
  const [url, setUrl] = useState(r.url)
  const [kind, setKind] = useState(r.kind)
  const [save, setSave] = useState(false)
  const dirty = name !== r.name || url !== r.url || kind !== r.kind
  const st = roomState(r)
  const working = busy === r.id
  const patch = (body: object) => run(r.id, () => api(`/admin/rooms/${r.id}`, { method: 'PATCH', body: JSON.stringify(body) }))

  const primary = r.processing
    ? <button type="button" className="btn btn--stop" disabled={working} onClick={() => run(r.id, () => api(`/admin/rooms/${r.id}/stop`, { method: 'POST', body: '{}' }))}>Parar</button>
    : !r.complete && (
      <button type="button" className="btn" disabled={working} onClick={() => run(r.id, () => api(`/admin/rooms/${r.id}/start`, { method: 'POST', body: JSON.stringify({ save }) }))}>
        {working ? 'Abrindo…' : r.processed_to > 0 && !r.is_live ? 'Continuar checagem' : 'Iniciar checagem'}
      </button>
    )

  return (
    <Panel title={r.name} meta={KIND_LABEL[r.kind] ?? r.kind} onClose={onClose} actions={primary}
      footer={
        <>
          <Switch label="Visível para o público" checked={r.active} onChange={(v) => patch({ active: v })} />
          <button type="button" className="link link--danger" onClick={() => { if (confirm(`Excluir a sala "${r.name}"? As checagens ficam no cache.`)) run(r.id, () => api(`/admin/rooms/${r.id}`, { method: 'DELETE' })).then(onClose) }}>Excluir sala</button>
        </>
      }>
      <section className="panel__section">
        <div className={`state state--${st.cls} state--big`}><span className="state__dot" />{st.word}</div>
        <p className="muted panel__status">
          {r.processing && r.message ? r.message : r.state === 'error' && r.message ? r.message : r.complete ? 'Gravação processada por completo; os espectadores recebem tudo do cache.' : r.processed_to > 0 ? `Processado até ${fmt(r.processed_to)}.` : 'A checagem ainda não começou para esta sala.'}
        </p>
        <dl className="facts">
          <div><dt>Afirmações</dt><dd>{r.claims}</dd></div>
          <div><dt>Assistindo</dt><dd>{r.viewers}</dd></div>
          {r.usage && <div><dt>Custo</dt><dd>≈ US${r.usage.usd.toFixed(2)}</dd></div>}
        </dl>
        {!r.processing && !r.complete && (
          <Switch label="Modo econômico" hint="Menos chamadas de IA: sem checagem profunda automática, no máximo 6 afirmações/min." checked={save} onChange={setSave} />
        )}
        {r.usage && (
          <dl className="facts">
            <div><dt>Chamadas IA</dt><dd>{r.usage.calls}</dd></div>
            <div><dt>Buscas web</dt><dd>{r.usage.searches}</dd></div>
            <div><dt>Áudio</dt><dd>{fmt(r.usage.audio_seconds)}</dd></div>
          </dl>
        )}
        <div className="panel__links">
          <button type="button" className="link" onClick={openClaims}>Abrir checagens →</button>
          <a className="link" href={`/?room=${r.id}`} target="_blank" rel="noreferrer">Ver como espectador ↗</a>
        </div>
      </section>
      <ActivitySection roomId={r.id} live={r.processing} />
      <form className="panel__section form" onSubmit={(e) => { e.preventDefault(); patch({ name, url, kind }) }} onKeyDown={cmdEnter}>
        <Field label="Nome"><input value={name} onChange={(e) => setName(e.target.value)} required /></Field>
        <Field label="Link da transmissão"><input type="url" value={url} onChange={(e) => setUrl(e.target.value)} required /></Field>
        <Field label="Tipo"><Segmented value={kind} options={KINDS} onChange={setKind} /></Field>
        {dirty && (
          <footer className="form__actions">
            <button type="button" className="btn btn--ghost" onClick={() => { setName(r.name); setUrl(r.url); setKind(r.kind) }}>Descartar</button>
            <button type="submit" className="btn" disabled={working}>Salvar</button>
          </footer>
        )}
      </form>
    </Panel>
  )
}

const SRC_PT: Record<string, string> = { pipeline: 'pipeline', transcribe: 'áudio', factcheck: 'checagem', decider: 'decisor', news: 'base' }

// The backend's own log for this room's run (not what viewers received): transcripts with their lag,
// extraction/gate/dedup drops, each verification with method and time, errors and reconnects.
function ActivitySection({ roomId, live }: { roomId: string; live: boolean }) {
  const [a, setA] = useState<Activity | null>(null)
  const [errorsOnly, setErrorsOnly] = useState(false)
  useEffect(() => {
    let alive = true
    const load = () => api<Activity>(`/admin/rooms/${roomId}/activity`).then((v) => { if (alive) setA(v) }).catch(() => {})
    load()
    const t = live ? setInterval(load, 2000) : undefined
    return () => { alive = false; clearInterval(t) }
  }, [roomId, live])
  if (!a || (!a.mode && a.activity.length === 0)) return null
  const lines = a.activity.filter((l) => !errorsOnly || l.level !== 'INFO').reverse()
  return (
    <section className="panel__section">
      <dl className="facts">
        <div><dt>Transcrição</dt><dd>{a.mode === 'realtime' ? 'tempo real' : 'blocos'}</dd></div>
        <div><dt>Em extração</dt><dd>{a.in_flight}</dd></div>
        <div><dt>Verificando</dt><dd>{a.verifying}</dd></div>
      </dl>
      <Switch label="Só avisos e erros" checked={errorsOnly} onChange={setErrorsOnly} />
      <ol className="activity">
        {lines.length === 0 && <li className="muted">Nada ainda.</li>}
        {lines.map((l, i) => (
          <li key={`${l.t}-${i}`} className={`activity__line activity__line--${l.level.toLowerCase()}`}>
            <time>{new Date(l.t * 1000).toLocaleTimeString('pt-BR')}</time>
            <span className="activity__src">{SRC_PT[l.src] ?? l.src}</span>
            <span className="activity__msg">{l.msg}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}

// ---- Checagens --------------------------------------------------------------------------------
function ClaimsPage({ rooms, room, onPick, reload, notify, owner }: { rooms: Room[]; room: Room | undefined; onPick: (id: string) => void; reload: () => Promise<void>; notify: (m: string) => void; owner: boolean }) {
  const session = useSession()
  const [filter, setFilter] = useState<'all' | Verdict>('all')
  const [q, setQ] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
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
  const claim = session.claims.find((c) => c.id === selected)

  async function save(c: Claim, verdict: Final, explanation: string, sources: Source[], text: string, speaker: string) {
    if (!url) return
    await api('/admin/claim', { method: 'POST', body: JSON.stringify({ url, claim_id: c.id, verdict, explanation, sources, text, speaker }) })
  }
  async function remove(c: Claim) {
    if (!room) return
    await api(`/admin/claims/${room.video_id}/${c.id}`, { method: 'DELETE' })
    setSelected(null)
  }
  async function toggle() {
    if (!room) return
    try {
      await api(`/admin/rooms/${room.id}/${room.processing ? 'stop' : 'start'}`, { method: 'POST', body: '{}' })
      await reload()
    } catch (e) { notify((e as Error).message) }
  }
  const st = room && roomState(room)

  return (
    <div className="page">
      <div className="page__content">
        <PageHeader
          title={rooms.length > 1 ? (
            <select className="title-select" value={room?.id ?? ''} onChange={(e) => { onPick(e.target.value); setSelected(null) }}>
              {rooms.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          ) : room?.name ?? 'Checagens'}
          meta={st && <span className={`state state--${st.cls}`}><span className="state__dot" />{st.word}</span>}>
          {owner && room && !room.complete && (
            <button type="button" className={room.processing ? 'btn btn--stop' : 'btn'} onClick={toggle}>{room.processing ? 'Parar' : 'Iniciar checagem'}</button>
          )}
        </PageHeader>
        <div className="toolbar">
          <div className="pills">
            {(['all', 'fake', 'valid', 'inconclusive', 'pending'] as const).map((f) => (
              <button key={f} type="button" className={`pill${filter === f ? ' active' : ''}`} onClick={() => setFilter(f)}>
                {f === 'all' ? 'Todas' : f === 'pending' ? 'Verificando' : VERDICT_LABEL[f]} <span className="pill__n">{f === 'all' ? session.claims.length : counts[f]}</span>
              </button>
            ))}
          </div>
          <input className="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar…" />
        </div>
        {!room && <Empty><p>Crie uma sala primeiro.</p></Empty>}
        {room && visible.length === 0 && (
          <Empty><p className="muted">{session.claims.length === 0 ? 'Nenhuma afirmação ainda — as checagens aparecem aqui em tempo real.' : 'Nada com esse filtro.'}</p></Empty>
        )}
        <div className="rows">
          {visible.map((c) => (
            <button key={c.id} type="button" className={`row row--claim claim--${c.verdict}${selected === c.id ? ' active' : ''}`} onClick={() => setSelected(c.id)}>
              <span className="claim__badge">{c.verdict === 'pending' && <span className="spinner" aria-hidden />}{VERDICT_LABEL[c.verdict]}</span>
              <span className="row__main"><span className="row__title row__title--normal">{c.speaker && <strong>{c.speaker}: </strong>}{c.text}</span></span>
              {c.method === 'human' && <span className="chip">editada</span>}
              <span className="row__meta">{c.verdict !== 'pending' && `${Math.round(c.confidence * 100)}% · `}{fmt(c.start)}</span>
              <span className="row__chevron">›</span>
            </button>
          ))}
        </div>
      </div>
      {claim && <ClaimPanel key={claim.id} claim={claim} onClose={() => setSelected(null)} onSave={save} onDelete={owner ? remove : undefined} onRecheck={session.recheck} notify={notify} />}
    </div>
  )
}

function ClaimPanel({ claim: c, onClose, onSave, onDelete, onRecheck, notify }: {
  claim: Claim; onClose: () => void; onSave: (c: Claim, v: Final, e: string, s: Source[], t: string, sp: string) => Promise<void>; onDelete?: (c: Claim) => Promise<void>
  onRecheck: (id: string) => void; notify: (m: string) => void
}) {
  const initialVerdict: Final = c.verdict === 'pending' ? 'inconclusive' : c.verdict
  const [verdict, setVerdict] = useState<Final>(initialVerdict)
  const [text, setText] = useState(c.text)
  const [speaker, setSpeaker] = useState(c.speaker ?? '')
  const [explanation, setExplanation] = useState(c.explanation)
  const [sources, setSources] = useState<Source[]>(c.sources)
  const [saving, setSaving] = useState(false)
  const cleanSources = sources.filter((s) => s.url.trim()).map((s) => ({ title: s.title.trim(), url: s.url.trim() }))
  const dirty = verdict !== initialVerdict || explanation !== c.explanation || text.trim() !== c.text || speaker.trim() !== (c.speaker ?? '')
    || JSON.stringify(cleanSources) !== JSON.stringify(c.sources)
  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!dirty) return
    setSaving(true)
    try { await onSave(c, verdict, explanation, cleanSources, text.trim() || c.text, speaker.trim()) } catch (err) { notify((err as Error).message) } finally { setSaving(false) }
  }
  const formId = `claim-${c.id}`
  return (
    <Panel title="Checagem" meta={<>{fmt(c.start)}{c.method && ` · ${METHOD_PT[c.method] ?? c.method}`}{c.verdict !== 'pending' && ` · ${Math.round(c.confidence * 100)}%`}</>} onClose={onClose}
      actions={<button type="submit" form={formId} className="btn" disabled={!dirty || saving}>{saving ? 'Salvando…' : 'Publicar'}</button>}
      footer={
        <>
          {c.verdict !== 'pending' && !c.deep_pending
            ? <button type="button" className="link" onClick={() => onRecheck(c.id)}>Rechecar com IA</button>
            : <span className="muted">{c.deep_pending ? 'Checagem profunda em andamento…' : 'Verificando…'}</span>}
          {onDelete && <button type="button" className="link link--danger" onClick={() => { if (confirm('Excluir esta afirmação para todos os espectadores?')) onDelete(c).catch((e) => notify(e.message)) }}>Excluir</button>}
        </>
      }>
      <form id={formId} className="form" onSubmit={submit} onKeyDown={cmdEnter}>
        <Field label="Quem falou" hint="Aparece antes da fala para o público. Corrigir só o nome não altera o veredito.">
          <input value={speaker} onChange={(e) => setSpeaker(e.target.value)} placeholder="Ex.: Lula, Flávio Bolsonaro" />
        </Field>
        <Field label="Afirmação" hint="O que foi dito, com quem fez/recebeu o quê. Corrija se a extração trocou o sujeito.">
          <textarea className={`quote claim--${c.verdict}`} value={text} onChange={(e) => setText(e.target.value)} rows={Math.min(6, Math.max(2, Math.ceil(text.length / 44)))} />
        </Field>
        <Field label="Veredito"><Segmented value={verdict} options={FINALS} onChange={setVerdict} /></Field>
        <Field label="Explicação" hint="É o texto que o público lê embaixo da afirmação.">
          <textarea value={explanation} onChange={(e) => setExplanation(e.target.value)} rows={Math.min(16, Math.max(5, Math.ceil(explanation.length / 44)))} />
        </Field>
        <div className="field">
          <span className="field__label">Fontes</span>
          <div className="list-edit">
            {sources.map((s, i) => (
              <div key={i} className="list-edit__row list-edit__row--source">
                <input value={s.title} onChange={(e) => setSources(sources.map((x, j) => (j === i ? { ...x, title: e.target.value } : x)))} placeholder="Título" />
                <input type="url" value={s.url} onChange={(e) => setSources(sources.map((x, j) => (j === i ? { ...x, url: e.target.value } : x)))} placeholder="https://…" />
                <button type="button" className="icon-btn" aria-label="Remover fonte" onClick={() => setSources(sources.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
            <button type="button" className="link" onClick={() => setSources([...sources, { title: '', url: '' }])}>+ Adicionar fonte</button>
          </div>
        </div>
        {dirty && <p className="kbd-hint">⌘↵ publica para todos os espectadores</p>}
      </form>
    </Panel>
  )
}

// ---- Usuários (owners only) ----------------------------------------------------------------------
interface ListedUser { email: string; role: 'owner' | 'checker'; name?: string | null; picture?: string | null; added_by?: string | null; added_at?: number | null; last_login?: number | null; fixed: boolean }

function UsersPage({ me, notify }: { me: AdminUser; notify: (m: string) => void }) {
  const [list, setList] = useState<ListedUser[] | null>(null)
  const [email, setEmail] = useState('')
  const [saving, setSaving] = useState(false)
  const load = useCallback(() => api<ListedUser[]>('/admin/users').then(setList).catch((e) => notify((e as Error).message)), [notify])
  // oxlint-disable-next-line react-hooks/set-state-in-effect -- initial load
  useEffect(() => { load() }, [load])
  async function add(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    try {
      await api('/admin/users', { method: 'POST', body: JSON.stringify({ email: email.trim() }) })
      setEmail('')
      await load()
    } catch (err) { notify((err as Error).message) } finally { setSaving(false) }
  }
  async function remove(u: ListedUser) {
    if (!confirm(`Remover o acesso de ${u.email}? A pessoa sai do admin na hora.`)) return
    try { await api(`/admin/users/${encodeURIComponent(u.email)}`, { method: 'DELETE' }); await load() } catch (err) { notify((err as Error).message) }
  }
  const checkers = list?.filter((u) => !u.fixed).length ?? 0
  return (
    <div className="page">
      <div className="page__content">
        <PageHeader title="Usuários" meta={list && `${list.length - checkers} owner${list.length - checkers === 1 ? '' : 's'} · ${checkers} checador${checkers === 1 ? '' : 'es'}`} />
        <form className="toolbar users__add" onSubmit={add}>
          <input className="search" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="e-mail do Google do checador" required />
          <button type="submit" className="btn" disabled={saving || !email.trim()}>{saving ? 'Adicionando…' : 'Adicionar checador'}</button>
          <span className="muted users__hint">Checadores revisam checagens e cuidam da base de notícias e do glossário. Não criam nem iniciam salas, nem gerenciam usuários.</span>
        </form>
        {list && list.length === 0 && <Empty><p className="muted">Ninguém cadastrado.</p></Empty>}
        <div className="rows">
          {list?.map((u) => (
            <div key={u.email} className="row row--static">
              {u.picture ? <img className="users__avatar" src={u.picture} alt="" referrerPolicy="no-referrer" /> : <span className="users__avatar users__avatar--empty">{(u.name || u.email)[0]?.toUpperCase()}</span>}
              <span className="row__main">
                <span className="row__title">{u.name || u.email}{u.email === me.email && <span className="muted"> · você</span>}</span>
                <span className="row__sub">{u.name ? u.email : u.last_login ? '' : 'ainda não entrou'}</span>
              </span>
              <span className={`chip${u.role === 'owner' ? ' chip--owner' : ''}`}>{u.role === 'owner' ? 'Owner' : 'Checador'}</span>
              <span className="row__meta">{u.fixed ? 'definido no servidor' : u.last_login ? `entrou ${ago(u.last_login)}` : u.added_by ? `por ${u.added_by}` : ''}</span>
              {u.fixed ? <span className="users__remove" /> : <button type="button" className="link link--danger users__remove" onClick={() => remove(u)}>Remover</button>}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ---- Base de notícias -------------------------------------------------------------------------
function NewsPage({ notify }: { notify: (m: string) => void }) {
  const [stats, setStats] = useState<NewsStats | null>(null)
  const [docs, setDocs] = useState<Doc[]>([])
  const [total, setTotal] = useState(0)
  const [disabled, setDisabled] = useState(false)
  const [q, setQ] = useState('')
  const [adding, setAdding] = useState(false)
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

  async function del(d: Doc) {
    if (!confirm(`Remover "${d.title || d.url}" da base?`)) return
    try { await api(`/admin/news/docs/${d.id}`, { method: 'DELETE' }); await load(q) } catch (err) { notify((err as Error).message) }
  }
  async function refresh() {
    setBusy('refresh')
    try { await api('/news/refresh', { method: 'POST', body: '{}' }); await load(q) } catch (err) { notify((err as Error).message) } finally { setBusy(null) }
  }

  return (
    <div className="page">
      <div className="page__content">
        <PageHeader title="Base de notícias" meta={stats && <span title={Object.entries(stats.sources).map(([k, v]) => `${k}: ${v}`).join('\n')}>{stats.docs} artigos{stats.last_refresh ? ` · feeds ${ago(stats.last_refresh)}` : ''}</span>}>
          <button type="button" className="btn btn--ghost" onClick={refresh} disabled={busy === 'refresh' || disabled}>{busy === 'refresh' ? 'Atualizando…' : 'Atualizar feeds'}</button>
          <button type="button" className="btn" onClick={() => setAdding(true)} disabled={disabled}>Adicionar artigo</button>
        </PageHeader>
        {disabled && <div className="banner banner--error">Base de notícias desativada neste servidor (PROVIDER=mock ou sem chave da OpenAI).</div>}
        <div className="toolbar">
          <input className="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar por título, fonte ou URL…" autoFocus />
          <span className="muted">{total} {total === 1 ? 'resultado' : 'resultados'}{total > docs.length && ` · mostrando ${docs.length}`}</span>
        </div>
        <div className="rows">
          {docs.map((d) => (
            <div key={d.id} className="row row--static">
              <span className="row__source">{d.source}</span>
              <span className="row__main">
                {d.url.startsWith('http')
                  ? <a className="row__title row__title--normal" href={d.url} target="_blank" rel="noreferrer">{d.title || d.url}</a>
                  : <span className="row__title row__title--normal">{d.title || 'Entrada manual'}</span>}
                {d.excerpt && <span className="row__sub">{d.excerpt}</span>}
              </span>
              <span className="row__meta">{d.published ? new Date(d.published * 1000).toLocaleDateString('pt-BR') : d.kind}</span>
              <button type="button" className="icon-btn row__action" title="Remover da base" aria-label="Remover da base" onClick={() => del(d)}>×</button>
            </div>
          ))}
        </div>
      </div>
      {adding && <AddDocDialog onClose={() => setAdding(false)} onAdded={() => { setAdding(false); load(q) }} notify={notify} />}
    </div>
  )
}

function AddDocDialog({ onClose, onAdded, notify }: { onClose: () => void; onAdded: () => void; notify: (m: string) => void }) {
  const [mode, setMode] = useState<'url' | 'text'>('url')
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const ok = mode === 'url' ? !!url : text.trim().length >= 40
  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      await api('/admin/news/docs', { method: 'POST', body: JSON.stringify(mode === 'url' ? { url, title } : { url: '', title, text }) })
      onAdded()
    } catch (err) { notify((err as Error).message) } finally { setBusy(false) }
  }
  return (
    <Dialog title="Adicionar artigo" onClose={onClose}>
      <form className="form" onSubmit={submit} onKeyDown={cmdEnter}>
        <Segmented value={mode} options={[['url', 'Por link'], ['text', 'Colar texto']]} onChange={setMode} />
        {mode === 'url' ? (
          <Field label="Link do artigo ou checagem" hint="O texto é extraído da página. Se o site bloquear a leitura, use “Colar texto”.">
            <input type="url" autoFocus value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" required />
          </Field>
        ) : (
          <>
            <Field label="Título"><input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Opcional" /></Field>
            <Field label="Texto" hint="Mínimo de 40 caracteres.">
              <textarea autoFocus value={text} onChange={(e) => setText(e.target.value)} rows={8} />
            </Field>
          </>
        )}
        <footer className="form__actions">
          <span className="kbd-hint">Vira evidência para as próximas checagens.</span>
          <button type="button" className="btn btn--ghost" onClick={onClose}>Cancelar</button>
          <button type="submit" className="btn" disabled={busy || !ok}>{busy ? 'Indexando…' : 'Adicionar'}</button>
        </footer>
      </form>
    </Dialog>
  )
}

// ---- Glossário --------------------------------------------------------------------------------
function GlossaryPage({ notify }: { notify: (m: string) => void }) {
  const [saved, setSaved] = useState<{ terms: string; fixes: Fix[] } | null>(null)
  const [terms, setTerms] = useState('')
  const [fixes, setFixes] = useState<Fix[]>([])
  const [updated, setUpdated] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    api<{ terms: string; replacements: Fix[]; updated_at: number | null }>('/admin/glossary')
      .then((d) => { setTerms(d.terms); setFixes(d.replacements); setSaved({ terms: d.terms, fixes: d.replacements }); setUpdated(d.updated_at) })
      .catch((e) => notify((e as Error).message))
  }, [notify])
  const clean = fixes.filter((f) => f.from.trim() && f.to.trim()).map((f) => ({ from: f.from.trim(), to: f.to.trim() }))
  const dirty = !!saved && (terms !== saved.terms || JSON.stringify(clean) !== JSON.stringify(saved.fixes))
  async function save(e: FormEvent) {
    e.preventDefault()
    if (!dirty) return
    setBusy(true)
    try {
      const d = await api<{ terms: string; updated_at: number | null; replacements: Fix[] }>('/admin/glossary', { method: 'PUT', body: JSON.stringify({ terms, replacements: clean }) })
      setUpdated(d.updated_at); setFixes(d.replacements); setSaved({ terms: d.terms, fixes: d.replacements })
    } catch (err) { notify((err as Error).message) } finally { setBusy(false) }
  }
  const setFix = (i: number, patch: Partial<Fix>) => setFixes(fixes.map((f, j) => (j === i ? { ...f, ...patch } : f)))
  return (
    <div className="page">
      <div className="page__content">
        <form onSubmit={save} onKeyDown={cmdEnter}>
          <PageHeader title="Glossário" meta={updated ? `salvo ${ago(updated)}` : saved ? 'padrões do servidor' : ''}>
            {dirty && <button type="button" className="btn btn--ghost" onClick={() => { if (saved) { setTerms(saved.terms); setFixes(saved.fixes) } }}>Descartar</button>}
            <button type="submit" className="btn" disabled={!dirty || busy}>{busy ? 'Salvando…' : 'Salvar'}</button>
          </PageHeader>
          <div className="form form--narrow">
            <p className="muted">Vale para as próximas transcrições de qualquer sala, gravação ou ao vivo, sem reiniciar. O que já está no cache não muda.</p>
            <Field label="Nomes e termos" hint="Separados por vírgula. O modelo de voz usa a lista para acertar a grafia.">
              <textarea value={terms} onChange={(e) => setTerms(e.target.value)} rows={4} placeholder="Lula, Flávio Bolsonaro, Daniel Vorcaro, Banco Master, Galípolo…" />
            </Field>
            <div className="field">
              <span className="field__label">Correções</span>
              <span className="field__hint">Troca feita no texto transcrito, palavra inteira, sem diferenciar maiúsculas.</span>
              <div className="list-edit">
                {fixes.map((f, i) => (
                  <div key={i} className="list-edit__row list-edit__row--fix">
                    <input value={f.from} onChange={(e) => setFix(i, { from: e.target.value })} placeholder="Bocaro" />
                    <span className="muted">→</span>
                    <input value={f.to} onChange={(e) => setFix(i, { to: e.target.value })} placeholder="Vorcaro" />
                    <button type="button" className="icon-btn" aria-label="Remover correção" onClick={() => setFixes(fixes.filter((_, j) => j !== i))}>×</button>
                  </div>
                ))}
                <button type="button" className="link" onClick={() => setFixes([...fixes, { from: '', to: '' }])}>+ Adicionar correção</button>
              </div>
            </div>
          </div>
        </form>
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
