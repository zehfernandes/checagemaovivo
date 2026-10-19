import { useEffect, useState } from 'react'
import App from './App.tsx'
import { Review } from './Review.tsx'

export function Root() {
  const [hash, setHash] = useState(location.hash)
  useEffect(() => {
    const on = () => setHash(location.hash)
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return hash.startsWith('#review') ? <Review /> : <App />
}
