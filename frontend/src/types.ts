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
  method?: 'db' | 'web' | 'deep' | null
  deep_pending?: boolean
  receivedAt: number
  settledAt?: number
}

export interface TranscriptSegment {
  id: string
  text: string
  start: number
  end: number
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
  | { type: 'progress'; processed_to: number; complete: boolean; viewers: number }
  | { type: 'pong' }

export const VERDICT_LABEL: Record<Verdict, string> = {
  pending: 'Checking…',
  fake: 'Fake',
  valid: 'Valid',
  inconclusive: 'Inconclusive',
}

export interface Progress {
  processed_to: number
  complete: boolean
  viewers: number
}

export interface NewsStats {
  docs: number
  chunks: number
  sources: Record<string, number>
  last_refresh: number | null
}
