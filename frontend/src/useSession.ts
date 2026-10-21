import { useCallback, useEffect, useRef, useState } from 'react'
import { adminPassword } from './admin'
import type { Claim, Progress, ServerMessage, SessionState, StreamInfo, TranscriptSegment } from './types'

const MAX_TRANSCRIPT = 5000

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/ws`
}

export function useSession() {
  const ws = useRef<WebSocket | null>(null)
  const [state, setState] = useState<SessionState>('idle')
  const [lost, setLost] = useState(false)
  const [statusText, setStatusText] = useState('')
  const [statusCode, setStatusCode] = useState<string | null>(null)
  const [info, setInfo] = useState<StreamInfo | null>(null)
  const [transcript, setTranscript] = useState<TranscriptSegment[]>([])
  const [claims, setClaims] = useState<Claim[]>([])
  const [error, setError] = useState<string | null>(null)
  const [progress, setProgress] = useState<Progress | null>(null)
  // browser clock minus server clock (s); lets the UI schedule claims by the server's captured_at
  const [clockOffset, setClockOffset] = useState(0)

  const handle = useCallback((msg: ServerMessage) => {
    switch (msg.type) {
      case 'status':
        setState(msg.state)
        setStatusText(msg.message)
        setStatusCode(msg.code ?? null)
        break
      case 'info':
        setInfo(msg.info)
        break
      case 'transcript':
        setTranscript((t) => (t.some((s) => s.id === msg.segment.id) ? t : [...t, msg.segment].slice(-MAX_TRANSCRIPT)))
        break
      case 'claim':
        setClaims((cs) => {
          const now = Date.now()
          const i = cs.findIndex((c) => c.id === msg.claim.id)
          if (i === -1) return [...cs, { ...msg.claim, receivedAt: now }]
          const next = cs.slice()
          next[i] = {
            ...next[i],
            ...msg.claim,
            settledAt: msg.claim.verdict === 'pending' ? undefined : now,
          }
          return next
        })
        break
      case 'error':
        setError(msg.message)
        break
      case 'claim_removed':
        setClaims((cs) => cs.filter((c) => c.id !== msg.id))
        break
      case 'progress':
        setProgress({ processed_to: msg.processed_to, complete: msg.complete, viewers: msg.viewers, fresh: !!msg.fresh,
          save: !!msg.save, usage: msg.usage ?? null })
        if (msg.server_time) setClockOffset(Date.now() / 1000 - msg.server_time)
        break
    }
  }, [])

  // A new viewer gets the whole history (1000+ messages) in one burst; applying them one render
  // at a time froze the page, so messages are queued and flushed together once per frame.
  const inbox = useRef<ServerMessage[]>([])
  const flushTimer = useRef<number | null>(null)
  const flush = useCallback(() => {
    flushTimer.current = null
    const batch = inbox.current
    inbox.current = []
    for (const msg of batch) handle(msg)
  }, [handle])
  const enqueue = useCallback(
    (msg: ServerMessage) => {
      inbox.current.push(msg)
      if (flushTimer.current !== null) return
      flushTimer.current = document.hidden ? window.setTimeout(flush, 200) : window.requestAnimationFrame(flush)
    },
    [flush],
  )

  const connect = useCallback(() => {
    if (ws.current && ws.current.readyState <= WebSocket.OPEN) return ws.current
    const socket = new WebSocket(wsUrl())
    socket.onmessage = (e) => enqueue(JSON.parse(e.data))
    socket.onclose = () => {
      ws.current = null
      setState((s) => {
        if (s === 'connecting' || s === 'resolving' || s === 'listening') {
          setStatusText('Conexão com o servidor perdida')
          setLost(true)
          return 'error'
        }
        return s
      })
    }
    ws.current = socket
    return socket
  }, [enqueue])

  const start = useCallback(
    (url: string, language: string, startSeconds = 0, keep = false, fresh = false, save = false, watch = false, review = false) => {
      setError(null)
      setLost(false)
      if (!keep) {
        inbox.current = []
        setInfo(null)
        setTranscript([])
        setClaims([])
        setProgress(null)
      }
      setState('connecting')
      setStatusText(keep ? 'Ressincronizando…' : 'Conectando…')
      setStatusCode(null)
      const socket = connect()
      const payload = JSON.stringify({ type: 'start', url, language: language || null, start_seconds: startSeconds, fresh, save, watch, review, admin: adminPassword() })
      if (socket.readyState === WebSocket.OPEN) socket.send(payload)
      else socket.addEventListener('open', () => socket.send(payload), { once: true })
    },
    [connect],
  )

  const recheck = useCallback((claimId: string) => {
    ws.current?.send(JSON.stringify({ type: 'recheck', claim_id: claimId, admin: adminPassword() }))
  }, [])

  const stop = useCallback(() => {
    ws.current?.send(JSON.stringify({ type: 'stop' }))
    setState('idle')
  }, [])

  useEffect(() => () => ws.current?.close(), [])

  return { state, lost, statusText, statusCode, info, transcript, claims, error, progress, clockOffset, start, stop, recheck }
}
