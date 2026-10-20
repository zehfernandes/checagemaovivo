// The admin credential kept in this browser: a session token from Google sign-in
// (or, before Google is configured, the shared password itself).
export const adminToken = () => localStorage.getItem('admin') ?? ''
export const setAdminToken = (token: string) => localStorage.setItem('admin', token)
export const clearAdminToken = () => localStorage.removeItem('admin')
export const adminPassword = adminToken // the WebSocket sends it as "admin"
export const adminHeaders = (): Record<string, string> => ({ 'x-admin-token': adminToken() })

export interface AdminUser { email: string; role: 'owner' | 'checker'; name: string; picture: string }
export type AuthMode = 'google' | 'password' | 'open'

// ---- Google Identity Services (the "Sign in with Google" button) --------------------------------------
interface GoogleId {
  initialize(o: { client_id: string; callback: (r: { credential: string }) => void; ux_mode?: string }): void
  renderButton(el: HTMLElement, o: Record<string, unknown>): void
  disableAutoSelect(): void
}
declare global { interface Window { google?: { accounts: { id: GoogleId } } } }

let gsi: Promise<GoogleId> | null = null
export function loadGoogle(): Promise<GoogleId> {
  gsi ??= new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = 'https://accounts.google.com/gsi/client'
    s.async = true
    s.onload = () => (window.google ? resolve(window.google.accounts.id) : reject(new Error('Google indisponível')))
    s.onerror = () => { gsi = null; reject(new Error('Não foi possível carregar o login do Google')) }
    document.head.appendChild(s)
  })
  return gsi
}
