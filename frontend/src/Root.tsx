import { Suspense, lazy, useEffect, useState } from 'react'
import App from './App.tsx'

const Admin = lazy(() => import('./Admin.tsx').then((m) => ({ default: m.Admin })))
const Review = lazy(() => import('./Review.tsx').then((m) => ({ default: m.Review })))

export function Root() {
  const [hash, setHash] = useState(location.hash)
  useEffect(() => {
    const on = () => setHash(location.hash)
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  if (location.pathname.replace(/\/+$/, '') === '/admin') return <Suspense fallback={null}><Admin /></Suspense>
  if (hash.startsWith('#review')) return <Suspense fallback={null}><Review /></Suspense>
  return <App />
}
