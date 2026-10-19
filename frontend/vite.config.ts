import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.BACKEND_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/ws': { target: backend, ws: true },
      '/health': backend,
      '/news': backend,
      '/review': backend,
      '/rooms': backend,
      '/channels': backend,
      // API calls go to the backend; a browser navigation to /admin is the SPA page
      '/admin': {
        target: backend,
        bypass: (req) => (req.method === 'GET' && req.headers.accept?.includes('text/html') ? '/index.html' : undefined),
      },
    },
  },
})
