import { useEffect, useMemo, useState, type FormEvent } from 'react'
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
  const [fresh, setFresh] = useState(false)
  // Broadcast delay for live streams: the server works at the live edge, the viewer's player
  // runs N s behind it and each claim is shown when the viewer actually hears that sentence.
  const [delay, setDelay] = useState(() => Number(localStorage.getItem('delay') ?? 20))
  const [nudge, setNudge] = useState(0)
  const [behind, setBehind] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => localStorage.setItem('delay', String(delay)), [delay])
  useEffect(() => localStorage.setItem('danmu', danmu ? 'on' : 'off'), [danmu])
  const session = useSession()

  const typedId = extractVideoId(url)
  const videoId = session.info?.video_id || (typedId && typedId.length === 11 ? typedId : null)
  const running = session.state === 'connecting' || session.state === 'resolving' || session.state === 'listening'
  const isLive = session.info?.is_live ?? true
  const delayed = running && isLive && delay > 0
  useEffect(() => {
    if (!delayed) return
    const t = setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(t)
  }, [delayed])
  // the sentence behind a claim ends at captured_at (server clock); the delayed viewer hears it `delay` s later
  const revealAt = (capturedAt?: number | null) =>
    capturedAt ? (capturedAt + delay + nudge - 3 + session.clockOffset) * 1000 : 0

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

  // Processing is shared per video and cached server-side; for a recording we only reveal
  // what has been said up to the player position (no spoilers, instant seeks).
  const visibleClaims = useMemo(() => {
    if (!isLive) return session.claims.filter((c) => c.start <= currentTime + 1)
    if (!delayed) return session.claims
    return session.claims
      .map((c) => ({ ...c, revealAt: revealAt(c.captured_at) }))
      .filter((c) => c.revealAt <= now)
    // oxlint-disable-next-line react-hooks/exhaustive-deps -- revealAt is derived from delay/nudge/clockOffset listed below
  }, [session.claims, currentTime, isLive, delayed, now, delay, nudge, session.clockOffset])
  const visibleTranscript = useMemo(() => {
    if (!isLive) return session.transcript.filter((s) => s.start <= currentTime + 1)
    if (!delayed) return session.transcript
    return session.transcript.filter((s) => revealAt(s.captured_at) <= now)
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [session.transcript, currentTime, isLive, delayed, now, delay, nudge, session.clockOffset])

  // End-to-end processing latency, measured on the server: audio arrived from YouTube -> claim shown -> verdict.
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

  const counts = useMemo(() => {
    const c: Record<Verdict, number> = { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    for (const cl of visibleClaims) c[cl.verdict]++
    return c
  }, [visibleClaims])

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (running) session.stop()
    else {
      setBehind(null)
      session.start(url.trim(), language, currentTime, false, fresh)
    }
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
          <label className="check" title="Fresh run: process this video again from scratch in a private pipeline — no cached results, not shared with other viewers. Use it to measure real end-to-end latency.">
            <input type="checkbox" checked={fresh} onChange={(e) => setFresh(e.target.checked)} disabled={running} /> Fresh (no cache)
          </label>
          <select
            value={delay}
            onChange={(e) => setDelay(Number(e.target.value))}
            disabled={running}
            title="Live only: play the stream this many seconds behind the live edge (YouTube DVR) while the server checks the real-time feed, so each verdict is on screen when you hear the sentence"
          >
            <option value={0}>No delay</option>
            {[10, 20, 30, 45, 60].map((d) => (
              <option key={d} value={d}>
                Delay {d}s
              </option>
            ))}
          </select>
        </form>
        <div className={`status status--${session.state}`}>
          {session.state === 'listening' && <span className="dot" />}
          {session.statusText || 'Idle'}
          {session.info?.is_live && <span className="live-tag">LIVE</span>}
          {running && session.progress && (
            <span className="muted" title="Processing is shared by all viewers of this video and cached">
              {' '}· {session.progress.fresh ? 'fresh run · not cached' : `${session.progress.viewers} viewer${session.progress.viewers === 1 ? '' : 's'}`}
              {!isLive && ` · ${session.progress.complete ? 'fully processed' : `processed to ${fmtTime(session.progress.processed_to)}`}`}
            </span>
          )}
          {delayed && (
            <span className="muted delay-status" title="Your player runs behind the live edge; claims are shown when you hear the sentence. Use −/+ if they come early/late.">
              {' '}· delayed {delay}s
              {behind != null && Math.abs(behind - delay) > 5 && (
                <span className="warn"> (player is {behind.toFixed(0)}s behind — no DVR on this stream?)</span>
              )}
              {' '}
              <button type="button" className="btn btn--ghost btn--tiny" onClick={() => setNudge((n) => n - 2)}>−2s</button>
              <span className="sync">sync {nudge >= 0 ? '+' : ''}{nudge}s</span>
              <button type="button" className="btn btn--ghost btn--tiny" onClick={() => setNudge((n) => n + 2)}>+2s</button>
            </span>
          )}
          {running && latency && (isLive || session.progress?.fresh) && (
            <span className="latency" title={`Average over the last ${latency.n} claims, measured on the server from the moment the audio arrived from YouTube`}>
              ⏱ claim +{latency.claim!.toFixed(1)}s{latency.verdict != null && ` · verdict +${latency.verdict.toFixed(1)}s`}
            </span>
          )}
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
        <a className="btn btn--ghost" href="#review" title="Offline review: label claims, build the golden set">Review</a>
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
            <Player videoId={videoId} onTime={setCurrentTime} liveDelay={delayed ? delay : 0} onDelay={setBehind} />
          ) : (
            <div className="player player--empty">Paste a YouTube URL to begin</div>
          )}
          <Danmu claims={visibleClaims} isLive={isLive} currentTime={currentTime} enabled={danmu} />
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
              Claims ({visibleClaims.length})
            </button>
            <button className={tab === 'transcript' ? 'active' : ''} onClick={() => setTab('transcript')}>
              Transcript
            </button>
          </nav>
          {tab === 'claims' ? (
            <div className="claims-list">
              {visibleClaims.length === 0 && <p className="muted">Checked claims will appear here…</p>}
              {[...visibleClaims].reverse().map((c) => (
                <ClaimCard key={c.id} claim={c} onRecheck={session.recheck} />
              ))}
            </div>
          ) : (
            <TranscriptRail segments={visibleTranscript} />
          )}
        </aside>
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
