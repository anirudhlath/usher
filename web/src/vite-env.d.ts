/// <reference types="vite/client" />

/**
 * `import.meta.env.MODE` is compared against `'fixtures'` in two places —
 * `main.tsx` (start MSW) and `app/App.tsx` (mount the component gallery) — and
 * Vite replaces it with a string literal at build time, which is what lets
 * rollup drop both branches from a production build. Declaring the union here
 * makes a typo in either comparison a type error rather than a branch that is
 * silently always false.
 */
interface ImportMetaEnv {
  readonly MODE: 'development' | 'production' | 'fixtures'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
