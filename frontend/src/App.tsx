import { useRef, useEffect, useMemo, useState, type FormEvent } from 'react'
import { ClaimCard } from './components/ClaimCard'
import { Danmu } from './components/Danmu'
import { Player } from './components/Player'
import { useSession } from './useSession'
import type { NewsStats, Room, Verdict } from './types'

const DEFAULT_URL = 'https://www.youtube.com/watch?v=Amwukq3hviM'
// A chunk's audio spans [captured_at - 8, captured_at] on the server clock. Measured on YouTube live:
// the embedded player's own live edge trails the server's HLS reader by ~12 s, so a viewer `delay` s
// behind the player edge starts hearing that chunk at about captured_at + delay + 8.
const PLAYER_EDGE_LAG_S = 8

function extractVideoId(url: string): string | null {
  try {
    const u = new URL(url)
    if (u.hostname === 'youtu.be') return u.pathname.slice(1) || null
    if (u.pathname.startsWith('/live/')) return u.pathname.split('/')[2] || null
    return u.searchParams.get('v')
  } catch {
    return null
  }
}

export default function App() {
  const [url, setUrl] = useState(DEFAULT_URL)
  const [language, setLanguage] = useState('pt')
  const [currentTime, setCurrentTime] = useState(0)
  const [tab, setTab] = useState<'recent' | 'all'>('recent')
  const [danmu, setDanmu] = useState(() => localStorage.getItem('danmu') !== 'off')
  const [sideOpen, setSideOpen] = useState(() => localStorage.getItem('side') !== 'closed')
  const [details, setDetails] = useState(false)
  // recordings: show only what is said from the position where Start was pressed (no backlog dump)
  const [baseline, setBaseline] = useState(0)
  useEffect(() => localStorage.setItem('side', sideOpen ? 'open' : 'closed'), [sideOpen])
  const [delay, setDelay] = useState(() => Number(localStorage.getItem('delay') ?? 20))
  const [nudge, setNudge] = useState(0)
  const [behind, setBehind] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => localStorage.setItem('danmu', danmu ? 'on' : 'off'), [danmu])
  useEffect(() => localStorage.setItem('delay', String(delay)), [delay])
  const session = useSession()

  // Rooms: broadcasts opened by the newsroom. When there are rooms, viewers pick one instead of pasting a URL.
  const [rooms, setRooms] = useState<Room[] | null>(null)
  const [roomId, setRoomId] = useState(() => new URLSearchParams(location.search).get('room') ?? '')
  useEffect(() => {
    let alive = true
    const load = () => fetch('/rooms').then((r) => r.json()).then((d: Room[]) => alive && setRooms(d)).catch(() => alive && setRooms([]))
    load()
    const t = setInterval(load, 30_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])
  const room = rooms?.find((r) => r.id === roomId) ?? rooms?.[0]
  const roomUrl = room?.url
  useEffect(() => {
    if (!roomUrl) return
    // oxlint-disable-next-line react-hooks/set-state-in-effect -- the room decides the URL
    setUrl(roomUrl)
    setBehind(null)
    setBaseline(0)
  }, [roomUrl])
  function pickRoom(id: string) {
    setRoomId(id)
    const p = new URLSearchParams(location.search)
    p.set('room', id)
    history.replaceState(null, '', `?${p}`)
  }

  const typedId = extractVideoId(url)
  const videoId = session.info?.video_id || (typedId && typedId.length === 11 ? typedId : null)
  const running = session.state === 'connecting' || session.state === 'resolving' || session.state === 'listening'
  const isLive = session.info?.is_live ?? true
  // broadcast delay: only once the server confirmed a live stream; kept after Stop so buffered
  // claims don't flush at once (the player stays behind the edge too)
  const delayed = session.info?.is_live === true && delay > 0
  useEffect(() => {
    if (!delayed) return
    const t = setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(t)
  }, [delayed])
  useEffect(() => {
    if (!isLive && currentTime < baseline - 2) setBaseline(Math.max(0, currentTime))
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [currentTime, isLive])
  const revealAt = (capturedAt?: number | null) =>
    capturedAt ? (capturedAt + delay + nudge + PLAYER_EDGE_LAG_S + session.clockOffset) * 1000 : 0

  const [news, setNews] = useState<NewsStats | null>(null)
  useEffect(() => {
    let alive = true
    const load = () => fetch('/news').then((r) => r.json()).then((d) => alive && setNews(d)).catch(() => {})
    load()
    const t = setInterval(load, 60_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  // Processing is shared per video and cached server-side; for a recording we only reveal
  // what has been said up to the player position; for a delayed live, what the viewer heard.
  const visibleClaims = useMemo(() => {
    if (!isLive) return session.claims.filter((c) => c.start >= baseline - 1 && c.start <= currentTime + 1)
    if (!delayed) return session.claims
    return session.claims
      .map((c) => ({ ...c, revealAt: revealAt(c.captured_at) }))
      .filter((c) => c.revealAt <= now)
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- revealAt is derived from delay/nudge/clockOffset listed below
  }, [session.claims, currentTime, isLive, delayed, now, delay, nudge, session.clockOffset, baseline])

  const latency = useMemo(() => {
    const recent = session.claims.filter((c) => c.captured_at && c.extracted_at).slice(-20)
    if (recent.length === 0) return null
    const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null)
    return {
      n: recent.length,
      claim: avg(recent.map((c) => c.extracted_at! - c.captured_at!)),
      verdict: avg(recent.filter((c) => c.verified_at).map((c) => c.verified_at! - c.captured_at!)),
    }
  }, [session.claims])

  // last 5 minutes of the video (stream time), newest first
  const recentClaims = useMemo(() => {
    // recordings: relative to the player position; live: relative to the newest claim
    const ref = isLive ? visibleClaims.reduce((m, c) => Math.max(m, c.start), 0) : currentTime
    return visibleClaims.filter((c) => c.start >= ref - 300)
  }, [visibleClaims, isLive, currentTime])

  const counts = useMemo(() => {
    const c: Record<Verdict, number> = { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    for (const cl of visibleClaims) c[cl.verdict]++
    return c
  }, [visibleClaims])

  const startedFor = useRef('')
  function begin() {
    const u = url.trim()
    const from = u === startedFor.current ? currentTime : 0 // a new video starts from the top; retries resume
    startedFor.current = u
    setBehind(null)
    setBaseline(Math.max(0, from))
    session.start(u, language, from, false, false, false, true)
  }
  function onSubmit(e: FormEvent) {
    e.preventDefault()
  }
  // no button: checking starts by itself when a video is chosen, follows the player and reconnects
  // if the connection drops (e.g. server restart). A room whose checking hasn't started is retried quietly.
  useEffect(() => {
    const u = url.trim()
    if (!u || (!roomUrl && !(typedId && typedId.length === 11))) return
    const isNew = u !== startedFor.current
    const lost = session.state === 'error' && session.lost
    const waiting = session.state === 'error' && !lost && !!roomUrl
    if (!isNew && !lost && !waiting) return
    const t = setTimeout(begin, isNew ? 0 : lost ? 2000 : 15000)
    return () => clearTimeout(t)
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [session.state, session.lost, url, roomUrl, typedId])

  const progress = session.progress
  const clip = useMemo<[number, number] | undefined>(() => {
    const i = session.info
    return i?.clip_start != null && i?.clip_end != null ? [i.clip_start, i.clip_end] : undefined
  }, [session.info])
  const usage = progress?.usage
  const usageTitle = usage
    ? `Esta execução: ${usage.calls} chamadas de modelo · ${(usage.tokens_in / 1000).toFixed(1)}k tokens de entrada / ${(usage.tokens_out / 1000).toFixed(1)}k de saída · ${usage.searches} buscas na web · ${Math.round(usage.audio_seconds / 60)} min de áudio — estimativa pelos preços de tabela`
    : ''

  return (
    <div className="app">
      <header className="topbar">
        <form className="controls" onSubmit={onSubmit}>
          <span className="logo" aria-hidden>●</span>
          {rooms && rooms.length > 0 ? (
            <select className="room-picker" value={room?.id ?? ''} onChange={(e) => pickRoom(e.target.value)} title="Transmissão">
              {rooms.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          ) : (
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="Cole a URL de um vídeo ou live do YouTube"
              required
            />
          )}
          <span className={`indicator status--${session.state}`}>
            {session.state === 'listening' && <span className="dot" />}
            {running && <span>{session.statusText}</span>}
            {running && session.info?.is_live && <span className="live-tag">AO VIVO</span>}
          </span>
        </form>
        <button
          type="button"
          className={`menu__button${details ? ' menu__button--on' : ''}`}
          onClick={() => setDetails((d) => !d)}
          title="Detalhes da execução: espectadores, custo, latência, atraso"
          aria-expanded={details}
        >
          Detalhes
        </button>
        <details className="menu">
          <summary className="menu__button">Opções</summary>
          <div className="menu__panel">
            <label className="menu__row">
              <span>Idioma</span>
              <select value={language} onChange={(e) => setLanguage(e.target.value)} disabled={running}>
                <option value="pt">Português</option>
                <option value="en">Inglês</option>
                <option value="es">Espanhol</option>
                <option value="">Automático</option>
              </select>
            </label>
            <label className="menu__row" title="Atraso da transmissão: o player fica alguns segundos atrás do ao vivo enquanto o servidor checa o sinal em tempo real, para o veredito já estar na tela quando você ouve a frase (só para lives com DVR).">
              <span>Atraso (lives)</span>
              <select value={delay} onChange={(e) => setDelay(Number(e.target.value))} disabled={running}>
                <option value={0}>sem atraso</option>
                {[10, 20, 30, 45, 60].map((d) => (
                  <option key={d} value={d}>{d} s</option>
                ))}
              </select>
            </label>
            <label className="menu__row menu__row--check" title="Mostra as afirmações voando sobre o vídeo">
              <span>Danmu sobre o vídeo</span>
              <input type="checkbox" checked={danmu} onChange={(e) => setDanmu(e.target.checked)} />
            </label>
            <div className="menu__row menu__row--info" title={news ? Object.entries(news.sources).map(([k, v]) => `${k}: ${v}`).join('\n') : ''}>
              <span>Base de notícias</span>
              <span>
                {news ? `${news.docs} artigos` : '–'}
                {news?.last_refresh && <span className="muted"> · {ago(news.last_refresh)}</span>}
              </span>
            </div>
          </div>
        </details>
      </header>

      {details && (
      <div className={`statusline status--${session.state}`}>
        <span>{session.statusText || 'Pronto'}</span>
        {running && progress && (
          <span className="muted">
            {progress.fresh ? 'sem cache' : `${progress.viewers} ${progress.viewers === 1 ? 'espectador' : 'espectadores'}`}
            {!isLive && ` · ${progress.complete ? 'processado por completo' : `processado até ${fmtTime(progress.processed_to)}`}`}
            {progress.save && ' · modo econômico'}
            {usage && <span title={usageTitle}> · ≈ US${usage.usd.toFixed(2)}</span>}
          </span>
        )}
        {running && delayed && (
          <span className="muted delay-status" title="O player está atrás do ao vivo; as afirmações aparecem quando você ouve a frase. Use −/+ se vierem cedo/tarde.">
            · atraso {delay}s
            {behind != null && Math.abs(behind - delay) > 5 && (
              <span className="warn"> (player só {behind.toFixed(0)}s atrás — esta live não tem DVR?)</span>
            )}
            <button type="button" className="link" onClick={() => setNudge((n) => n - 2)}>−2s</button>
            <span className="sync">{nudge >= 0 ? '+' : ''}{nudge}s</span>
            <button type="button" className="link" onClick={() => setNudge((n) => n + 2)}>+2s</button>
          </span>
        )}
        {running && latency && (isLive || progress?.fresh) && (
          <span className="muted" title={`Média das últimas ${latency.n} afirmações, medida no servidor a partir da chegada do áudio do YouTube`}>
            · ⏱ afirmação +{latency.claim!.toFixed(1)}s{latency.verdict != null && ` · veredito +${latency.verdict.toFixed(1)}s`}
          </span>
        )}
        {!running && <span className="muted">· os detalhes aparecem com uma execução em andamento</span>}
      </div>
      )}

      {session.error && <div className="banner banner--error">{session.error}</div>}

      <main className={`layout${sideOpen ? '' : ' layout--collapsed'}`}>
        <section className="stage">
          <button
            type="button"
            className="side-toggle"
            onClick={() => setSideOpen((o) => !o)}
            title={sideOpen ? 'Recolher a barra lateral' : 'Mostrar afirmações e transcrição'}
            aria-expanded={sideOpen}
          >
            {sideOpen ? '›' : '‹'}
            {!sideOpen && visibleClaims.length > 0 && <span className="side-toggle__count">{visibleClaims.length}</span>}
          </button>
          {videoId ? (
            <Player videoId={videoId} onTime={setCurrentTime} liveDelay={delayed ? delay : 0} onDelay={setBehind} clip={clip} />
          ) : (
            <div className="player player--empty">Cole uma URL do YouTube para começar</div>
          )}
          <Danmu claims={visibleClaims} isLive={isLive} currentTime={currentTime} enabled={danmu} />
          <div className="scoreboard">
            <Score label="falsas" n={counts.fake} kind="fake" />
            <Score label="verdadeiras" n={counts.valid} kind="valid" />
            <Score label="inconclusivas" n={counts.inconclusive} kind="inconclusive" />
            {counts.pending > 0 && <Score label="verificando" n={counts.pending} kind="pending" />}
          </div>
        </section>

        {sideOpen && (
        <aside className="side">
          <nav className="tabs">
            <button className={tab === 'recent' ? 'active' : ''} onClick={() => setTab('recent')} title="Afirmações dos últimos 5 minutos">
              Recentes {recentClaims.length > 0 && <span className="muted">{recentClaims.length}</span>}
            </button>
            <button className={tab === 'all' ? 'active' : ''} onClick={() => setTab('all')}>
              Todas {visibleClaims.length > 0 && <span className="muted">{visibleClaims.length}</span>}
            </button>
          </nav>
          <div className="claims-list">
              {(tab === 'recent' ? recentClaims : visibleClaims).length === 0 && (
                <p className="muted empty">{tab === 'recent' ? 'Nada nos últimos 5 minutos.' : 'As afirmações verificadas aparecem aqui.'}</p>
              )}
              {[...(tab === 'recent' ? recentClaims : visibleClaims)].reverse().map((c) => (
                <ClaimCard key={c.id} claim={c} />
              ))}
          </div>
        </aside>
        )}
      </main>
    </div>
  )
}

function fmtTime(s: number) {
  const m = Math.floor(s / 60)
  return `${m}:${Math.floor(s % 60).toString().padStart(2, '0')}`
}

function ago(ts: number) {
  const m = Math.round((Date.now() / 1000 - ts) / 60)
  if (m < 1) return 'agora'
  if (m < 60) return `há ${m} min`
  return `há ${Math.round(m / 60)} h`
}

function Score({ label, n, kind }: { label: string; n: number; kind: Verdict }) {
  return (
    <span className={`score score--${kind}`}>
      <strong>{n}</strong> {label}
    </span>
  )
}
