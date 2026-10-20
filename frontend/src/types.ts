export type Verdict = 'pending' | 'fake' | 'valid' | 'inconclusive'

export interface Source {
  title: string
  url: string
}

export interface Claim {
  id: string
  text: string
  verdict: Verdict
  confidence: number
  explanation: string
  sources: Source[]
  start: number
  end: number
  method?: 'cache' | 'db' | 'web' | 'deep' | 'human' | null
  deep_pending?: boolean
  captured_at?: number | null
  extracted_at?: number | null
  verified_at?: number | null
  receivedAt: number
  settledAt?: number
  revealAt?: number // client ms: when a delayed live viewer should see it
}

export interface TranscriptSegment {
  id: string
  text: string
  start: number
  end: number
  captured_at?: number | null
  transcribed_at?: number | null
}

export interface StreamInfo {
  clip_start?: number | null
  clip_end?: number | null
  video_id: string
  title: string
  is_live: boolean
  duration: number | null
  /** 'youtube' (embedded player) or the site hosting the page: g1, globoplay, … (no player) */
  source?: string
  page_url?: string | null
}

export type SessionState = 'idle' | 'connecting' | 'resolving' | 'listening' | 'ended' | 'error'

export type ServerMessage =
  | { type: 'status'; state: SessionState; message: string; code?: 'not_started' | 'resolve_failed' }
  | { type: 'info'; info: StreamInfo }
  | { type: 'transcript'; segment: TranscriptSegment }
  | { type: 'claim'; claim: Omit<Claim, 'receivedAt' | 'settledAt'> }
  | { type: 'error'; message: string }
  | { type: 'claim_removed'; id: string }
  | { type: 'progress'; processed_to: number; complete: boolean; viewers: number; fresh?: boolean; server_time?: number; save?: boolean; usage?: UsageSummary | null }
  | { type: 'pong' }

export const VERDICT_LABEL: Record<Verdict, string> = {
  pending: 'Verificando…',
  fake: 'Falso',
  valid: 'Verdadeiro',
  inconclusive: 'Inconclusivo',
}

export interface UsageSummary {
  tokens_in: number
  tokens_out: number
  searches: number
  audio_seconds: number
  calls: number
  usd: number
}

export interface Progress {
  processed_to: number
  complete: boolean
  viewers: number
  fresh: boolean
  save: boolean
  usage: UsageSummary | null
}

export interface NewsStats {
  docs: number
  chunks: number
  sources: Record<string, number>
  last_refresh: number | null
}

export interface Room {
  id: string
  name: string
  url: string
  video_id: string
  kind: string
  active: boolean
  title: string | null
  is_live: boolean
  processing: boolean
  complete: boolean
  claims: number
}
