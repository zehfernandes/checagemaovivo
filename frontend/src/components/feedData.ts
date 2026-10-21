import { createContext, useEffect, useRef } from 'react'
import type { Claim, Speaker, Verdict } from '../types'

// Data side of the viewer's feed (labels, outlets, filters, score rows); the components are in Feed.tsx.

export type Final = Exclude<Verdict, 'pending'>
export const FINAL: Final[] = ['valid', 'fake', 'inconclusive'] // the order of the score bars
export const PUBLIC_VERDICT: Record<Verdict, string> = { pending: 'Verificando', fake: 'Mentira', valid: 'Verdade', inconclusive: 'Inconclusivo' }
export const PLURAL: Record<Final, string> = { fake: 'mentiras', valid: 'verdades', inconclusive: 'inconclusivas' }
export const UNNAMED = '' // claims with no identified speaker
export const totalOf = (row: Record<Verdict, number>) => FINAL.reduce((n, v) => n + row[v], 0)

// a person published or corrected it, or the machine's verdict is on the site as it came out
export const humanReviewed = (c: Claim) => c.method === 'human' || !!c.human_reviewed

const PUBLISHERS: [RegExp, string][] = [
  [/(^|\.)cnnbrasil\.com\.br$/, 'CNN Brasil'], [/^g1\.globo\.com$/, 'g1'], [/^oglobo\.globo\.com$/, 'O Globo'],
  [/^valor\.globo\.com$/, 'Valor'], [/(^|\.)globo\.com$/, 'Globo.com'], [/(^|\.)folha\.uol\.com\.br$/, 'Folha'],
  [/(^|\.)uol\.com\.br$/, 'UOL'], [/(^|\.)estadao\.com\.br$/, 'Estadão'], [/(^|\.)band\.uol\.com\.br$/, 'Band'],
  [/(^|\.)poder360\.com\.br$/, 'Poder360'], [/(^|\.)metropoles\.com$/, 'Metrópoles'], [/(^|\.)bbc\.com$/, 'BBC'],
  [/(^|\.)aosfatos\.org$/, 'Aos Fatos'], [/(^|\.)lupa\.uol\.com\.br$/, 'Lupa'], [/(^|\.)ibge\.gov\.br$/, 'IBGE'],
  [/(^|\.)tse\.jus\.br$/, 'TSE'], [/(^|\.)stf\.jus\.br$/, 'STF'], [/(^|\.)gov\.br$/, 'Governo Federal'],
]
export function hostOf(url: string) {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url }
}
export function publisherOf(url: string) {
  const host = hostOf(url)
  return PUBLISHERS.find(([re]) => re.test(host))?.[1] ?? host
}

export const claimLink = (id: string) => {
  const u = new URL(location.href)
  u.searchParams.set('checagem', id)
  return u.toString()
}

export function scoreRows(claims: Claim[]) {
  const m = new Map<string, Record<Verdict, number>>()
  for (const c of claims) {
    const who = c.speaker || UNNAMED
    const row = m.get(who) ?? { pending: 0, fake: 0, valid: 0, inconclusive: 0 }
    row[c.verdict]++
    m.set(who, row)
  }
  // most checked first; the unidentified row always last
  return [...m].sort((a, b) => (a[0] === UNNAMED ? 1 : b[0] === UNNAMED ? -1 : totalOf(b[1]) - totalOf(a[1])))
}

export const roomTitle = (name: string) => name.replace(/\s+[—–-]\s+/g, ' · ')

export type VerdictFilter = 'all' | Final
export interface Filters { verdict: VerdictFilter; speaker: string | null } // speaker null = everyone
export const NO_FILTER: Filters = { verdict: 'all', speaker: null }
export const applyFilters = (claims: Claim[], f: Filters) => claims.filter((c) =>
  (f.verdict === 'all' || c.verdict === f.verdict) && (f.speaker === null || (c.speaker || UNNAMED) === f.speaker))

// a shared link (?checagem=<id>) opens that claim once it arrives
export function useSharedClaim(claims: Claim[], open: (id: string) => void) {
  const wanted = useRef(new URLSearchParams(location.search).get('checagem'))
  useEffect(() => {
    if (wanted.current && claims.some((c) => c.id === wanted.current)) { open(wanted.current); wanted.current = null }
  }, [claims, open])
}

// ---- the room's speakers (set in /admin) -------------------------------------------------------------------
const words = (s: string) => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().split(' ').filter(Boolean)
const within = (a: string[], b: string[]) => a.every((w) => b.includes(w))

/** Which registered speaker a name the machine wrote refers to: the same name, or one inside the other word by word
 * ("Lula" ⊂ "Luiz Inácio Lula da Silva"); nothing when it is nobody's or could be more than one person's. */
export function rosterMatcher(roster: Speaker[]) {
  const regs = roster.map((r) => ({ r, w: words(r.name) }))
  return (name?: string | null): Speaker | null => {
    const w = name ? words(name) : []
    if (w.length === 0) return null
    const same = regs.find((x) => x.w.join(' ') === w.join(' '))
    if (same) return same.r
    const hits = regs.filter((x) => within(x.w, w) || within(w, x.w))
    return hits.length === 1 ? hits[0].r : null
  }
}

/** With speakers registered, the site shows only what they said, under the registered name; without, everything. */
export function applyRoster<T extends Claim>(claims: T[], roster: Speaker[] | undefined): T[] {
  if (!roster?.length) return claims
  const match = rosterMatcher(roster)
  const out: T[] = []
  for (const c of claims) {
    const s = match(c.speaker)
    if (s) out.push(s.name === c.speaker ? c : { ...c, speaker: s.name })
  }
  return out
}

// registered name -> photo (data: URL), read by <Avatar>
export const PhotosContext = createContext<Map<string, string>>(new Map())
export const photoMap = (roster: Speaker[] | undefined) => new Map((roster ?? []).filter((s) => s.photo).map((s) => [s.name, s.photo as string]))

