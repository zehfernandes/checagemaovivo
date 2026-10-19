import { useEffect, useState } from 'react'
import { Admin } from './Admin.tsx'
import App from './App.tsx'
import { Review } from './Review.tsx'

export function Root() {
  const [hash, setHash] = useState(location.hash)
  useEffect(() => {
    const on = () => setHash(location.hash)
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  if (location.pathname.replace(/\/+$/, '') === '/admin') return <Admin />
  return hash.startsWith('#review') ? <Review /> : <App />
}
