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
  method?: 'cache' | 'db' | 'web' | 'deep' | null
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
  video_id: string
  title: string
  is_live: boolean
  duration: number | null
}

export type SessionState = 'idle' | 'connecting' | 'resolving' | 'listening' | 'ended' | 'error'

export type ServerMessage =
  | { type: 'status'; state: SessionState; message: string }
  | { type: 'info'; info: StreamInfo }
  | { type: 'transcript'; segment: TranscriptSegment }
  | { type: 'claim'; claim: Omit<Claim, 'receivedAt' | 'settledAt'> }
  | { type: 'error'; message: string }
  | { type: 'progress'; processed_to: number; complete: boolean; viewers: number; fresh?: boolean; server_time?: number; save?: boolean; usage?: UsageSummary | null }
  | { type: 'pong' }

export const VERDICT_LABEL: Record<Verdict, string> = {
  pending: 'Checking…',
  fake: 'Fake',
  valid: 'Valid',
  inconclusive: 'Inconclusive',
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
