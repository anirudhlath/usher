import { fileURLToPath } from 'node:url'
// `defineConfig` from `vitest/config`, not from `vite`: the `test` key is
// vitest's and `vite`'s own type does not declare it, so `tsc -b` rejects it.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { USHER_API_ROOTS, CONSOLE_BASE } from './src/api/paths.ts'

/**
 * The backend the dev server proxies to. Defaults to compose's published port.
 */
const UPSTREAM = process.env.USHER_ORIGIN ?? 'http://localhost:8100'

/**
 * In production the console is served *by Usher itself* at `/console/`, so every
 * API call is same-origin at the root and no proxy exists at all. The dev server
 * has to fake that shape: Vite owns `/console/`, and each of Usher's nineteen
 * root segments is forwarded upstream untouched — no path rewriting anywhere,
 * because the paths the client sends in dev are the paths it sends in prod.
 *
 * That symmetry is the point. `usher-web` rewrote `/api/*` → `/*` in two places
 * (nginx and this file) and the rewrite is what made `POST /play`'s ticket URL —
 * which Usher mints from the incoming `Host` header — land on the wrong port.
 * With no prefix there is no rewrite and no header to get wrong.
 */
const proxy = Object.fromEntries(
  USHER_API_ROOTS.map((root) => [
    `/${root}`,
    {
      target: UPSTREAM,
      changeOrigin: false,
      // SSE: without this Vite buffers the response until upstream closes it,
      // which for a stream that never closes means the browser sees nothing.
      ...(root === 'events' ? { ws: false, selfHandleResponse: false } : {}),
    },
  ]),
)

export default defineConfig({
  base: `${CONSOLE_BASE}/`,
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy,
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    rollupOptions: {
      output: {
        // The operator half and the viewer half are rarely used in the same
        // sitting; splitting them keeps first paint on either side small.
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('react-dom') || id.includes('/react/')) return 'react'
            if (id.includes('@tanstack')) return 'query'
            if (id.includes('lucide-react')) return 'icons'
            return 'vendor'
          }
          return undefined
        },
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.test.{ts,tsx}', 'src/api/schema.d.ts', 'src/test/**', 'src/kit/**'],
    },
  },
})
