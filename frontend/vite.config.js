import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vercel gives the BUILD these; the browser never sees them unless we pass them
// through. Without them a bundle cannot say which commit it is, which is how a
// stale production alias hides (see src/lib/build.js).
const commit = process.env.VERCEL_GIT_COMMIT_SHA || process.env.COMMIT_SHA || ''
const ref = process.env.VERCEL_GIT_COMMIT_REF || process.env.COMMIT_REF || ''

export default defineConfig({
  plugins: [react()],
  define: {
    'import.meta.env.VITE_COMMIT_SHA': JSON.stringify(commit),
    'import.meta.env.VITE_COMMIT_REF': JSON.stringify(ref),
    'import.meta.env.VITE_BUILT_AT': JSON.stringify(new Date().toISOString()),
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
})
