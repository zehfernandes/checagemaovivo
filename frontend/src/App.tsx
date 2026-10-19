import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { ClaimCard } from './components/ClaimCard'
import { Danmu } from './components/Danmu'
import { Player } from './components/Player'
import { TranscriptRail } from './components/TranscriptRail'
import { useSession } from './useSession'
import type { NewsStats, Verdict } from './types'

const DEFAULT_URL = 'https://www.youtube.com/watch?v=Amwukq3hviM'

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
  const [tab, setTab] = useState<'claims' | 'transcript'>('claims')
  const [danmu, setDanmu] = useState(() => localStorage.getItem('danmu') !== 'off')
  useEffect(() => localStorage.setItem('danmu', danmu ? 'on' : 'off'), [danmu])
  const session = useSession()

  const videoId = session.info?.video_id || extractVideoId(url)
  const running = session.state === 'connecting' || session.state === 'resolving' || session.state === 'listening'
  const isLive = session.info?.is_live ?? true

  const [news, setNews] = useState<NewsStats | null>(null)
  const [refreshing, setRefreshing] = useState(false)
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
  async function refreshNews() {
    setRefreshing(true)
    try {
      const r = await fetch('/news/refresh', { method: 'POST' })
      setNews(await r.json())
    } finally {
      setRefreshing(false)
    }
  }

  // Recorded video: if the viewer seeks, restart processing from the new position.
  const lastTime = useRef(0)
  useEffect(() => {
    const jumped = Math.abs(currentTime - lastTime.current) > 5
    lastTime.current = currentTime
    if (jumped && running && session.state === 'listening' && !isLive) {
      session.start(url.trim(), language, currentTime, true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentTime])

  const counts = useMemo(() => {
    const c: Record<Verdict, number> = { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    for (const cl of session.claims) c[cl.verdict]++
    return c
  }, [session.claims])

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (running) session.stop()
    else session.start(url.trim(), language, currentTime)
  }

  return (
    <div className="app">
      <header className="topbar">
        <h1>
          <span className="logo">●</span> Debate Fact-Check
        </h1>
        <form className="controls" onSubmit={onSubmit}>
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="YouTube live / video URL"
            disabled={running}
            required
          />
          <select value={language} onChange={(e) => setLanguage(e.target.value)} disabled={running}>
            <option value="pt">PT</option>
            <option value="en">EN</option>
            <option value="es">ES</option>
            <option value="">Auto</option>
          </select>
          <button type="submit" className={running ? 'btn btn--stop' : 'btn'}>
            {running ? 'Stop' : 'Start'}
          </button>
        </form>
        <div className={`status status--${session.state}`}>
          {session.state === 'listening' && <span className="dot" />}
          {session.statusText || 'Idle'}
          {session.info?.is_live && <span className="live-tag">LIVE</span>}
        </div>
        <button
          type="button"
          className={`toggle ${danmu ? 'toggle--on' : ''}`}
          onClick={() => setDanmu((d) => !d)}
          title="Danmu overlay: claims fly across the video"
          aria-pressed={danmu}
        >
          <span className="toggle__knob" /> Danmu {danmu ? 'On' : 'Off'}
        </button>
        <div className="news-pill" title={news ? Object.entries(news.sources).map(([k, v]) => `${k}: ${v}`).join('\n') : ''}>
          <span className="news-pill__label">News DB</span>
          <strong>{news ? news.docs : '–'}</strong> articles
          {news?.last_refresh && <span className="muted"> · {ago(news.last_refresh)}</span>}
          <button type="button" className="btn btn--ghost" onClick={refreshNews} disabled={refreshing || !news}>
            {refreshing ? 'Updating…' : 'Update'}
          </button>
        </div>
      </header>

      {session.error && <div className="banner banner--error">{session.error}</div>}

      <main className="layout">
        <section className="stage">
          {videoId ? (
            <Player videoId={videoId} onTime={setCurrentTime} />
          ) : (
            <div className="player player--empty">Paste a YouTube URL to begin</div>
          )}
          <Danmu claims={session.claims} isLive={isLive} currentTime={currentTime} enabled={danmu} />
          <div className="scoreboard">
            <Score label="Fake" n={counts.fake} kind="fake" />
            <Score label="Valid" n={counts.valid} kind="valid" />
            <Score label="Inconclusive" n={counts.inconclusive} kind="inconclusive" />
            <Score label="Checking" n={counts.pending} kind="pending" />
          </div>
        </section>

        <aside className="side">
          <nav className="tabs">
            <button className={tab === 'claims' ? 'active' : ''} onClick={() => setTab('claims')}>
              Claims ({session.claims.length})
            </button>
            <button className={tab === 'transcript' ? 'active' : ''} onClick={() => setTab('transcript')}>
              Transcript
            </button>
          </nav>
          {tab === 'claims' ? (
            <div className="claims-list">
              {session.claims.length === 0 && <p className="muted">Checked claims will appear here…</p>}
              {[...session.claims].reverse().map((c) => (
                <ClaimCard key={c.id} claim={c} onRecheck={session.recheck} />
              ))}
            </div>
          ) : (
            <TranscriptRail segments={session.transcript} />
          )}
        </aside>
      </main>
    </div>
  )
}

function ago(ts: number) {
  const m = Math.round((Date.now() / 1000 - ts) / 60)
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  return `${Math.round(m / 60)} h ago`
}

function Score({ label, n, kind }: { label: string; n: number; kind: Verdict }) {
  return (
    <div className={`score score--${kind}`}>
      <strong>{n}</strong> {label}
    </div>
  )
}
