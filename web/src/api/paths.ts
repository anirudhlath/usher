/**
 * The root path segments Usher's HTTP API owns.
 *
 * Usher includes all seventeen of its routers with **no prefix** — there is no
 * `/api` namespace and adding one would be a breaking change to a documented,
 * public contract. So the console cannot live at `/`: its own `/titles/:id`
 * route would be shadowed by the API's `GET /titles/{title_id}`, and
 * `/search`, `/home` and `/browse` collide the same way.
 *
 * The console therefore serves from `/console/`, which is the same shape Plex
 * (`/web/`) and Emby (`/web/`) use for exactly this reason. Two consumers read
 * this list and they must not drift:
 *
 * · `vite.config.ts` — proxies these to the real backend in dev.
 * · `src/usher/api/console.py` — refuses to mount the console over any of them.
 *
 * `openapi.json`, `docs` and `redoc` are FastAPI's own and are included because
 * nothing in Usher disables them.
 */
export const USHER_API_ROOTS = [
  'admin',
  'browse',
  'collections',
  'docs',
  'episodes',
  'events',
  'health',
  'home',
  'images',
  'meta',
  'openapi.json',
  'people',
  'redoc',
  'search',
  'seasons',
  'series',
  'stream',
  'titles',
  'watch',
] as const

/** Where the console is mounted. Baked into the bundle by Vite's `base`. */
export const CONSOLE_BASE = '/console'
