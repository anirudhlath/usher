import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// Self-hosted, not a CDN. This is a LAN-only, air-gappable product; a console
// that phones fonts.googleapis.com on first paint is a worse failure than an
// unstyled heading. Both families are SIL OFL and Vite fingerprints the woff2
// into /console/assets/ alongside everything else.
import '@fontsource-variable/instrument-sans'
import '@fontsource-variable/jetbrains-mono'

import './styles/app.css'
import { App } from './app/App'

const container: HTMLElement | null = document.getElementById('root')
if (!container) throw new Error('#root is missing from index.html')
const root = createRoot(container)

function mount() {
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

// `import.meta.env.MODE` is replaced with a string literal at build time, so in
// every build except `--mode fixtures` this whole branch is `if (false)` and
// rollup drops MSW and the fixtures with it. See `src/test/browser-fixtures.ts`.
if (import.meta.env.MODE === 'fixtures') {
  void import('./test/browser-fixtures').then(({ startBrowserFixtures }) =>
    startBrowserFixtures().then(mount),
  )
} else {
  mount()
}
