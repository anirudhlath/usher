import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { CONSOLE_BASE } from '@/api/paths'
import { ROUTES } from './routes'
import { Providers, createQueryClient } from './providers'
import { DevDrawerProvider } from './dev-drawer-context'
import { ViewerShell } from './shells/ViewerShell'
import { OperatorShell } from './shells/OperatorShell'
import { DevDrawer } from '@/features/operator/DevDrawer'
import { ToastStack } from '@/features/shared/ToastStack'
import { NotFound } from '@/features/shared/NotFound'
import './shell.css'

/**
 * Every screen is code-split. The two halves are used in different sittings —
 * nobody drains a review queue and browses films in the same minute — so a
 * viewer first paint should not carry the operator's eight surfaces, and the
 * chunking in `vite.config.ts` keeps the vendor split stable underneath.
 *
 * **No route-level spinner** (patterns.md §1). The `Suspense` fallback is
 * `null`: a chunk arrives in single-digit milliseconds off a same-origin
 * server, and a spinner that flashes for 8 ms reads as a page restarting. Each
 * screen renders its own skeleton, shaped like the thing that is coming.
 */
const Home = lazy(() => import('@/features/viewer/Home'))
const Browse = lazy(() => import('@/features/viewer/Browse'))
const Search = lazy(() => import('@/features/viewer/Search'))
const TitleDetail = lazy(() => import('@/features/viewer/TitleDetail'))
const Series = lazy(() => import('@/features/viewer/Series'))
const Episode = lazy(() => import('@/features/viewer/Episode'))
const Person = lazy(() => import('@/features/viewer/Person'))
const Collection = lazy(() => import('@/features/viewer/Collection'))
const Player = lazy(() => import('@/features/viewer/Player'))
const About = lazy(() => import('@/features/viewer/About'))

const Overview = lazy(() => import('@/features/operator/Overview'))
const Sources = lazy(() => import('@/features/operator/Sources'))
const Bootstrap = lazy(() => import('@/features/operator/Bootstrap'))
const Review = lazy(() => import('@/features/operator/Review'))
const Rows = lazy(() => import('@/features/operator/Rows'))
const Pipeline = lazy(() => import('@/features/operator/Pipeline'))
const Insights = lazy(() => import('@/features/operator/Insights'))
const Config = lazy(() => import('@/features/operator/Config'))

/**
 * The component gallery, and the mechanism that keeps it out of a release.
 *
 * Vite replaces both operands with literals, so in `npm run build` this is
 * `false` and rollup drops the branch — the `lazy()` call included, which is why
 * it sits inside the conditional rather than beside it. `src/main.tsx` gates MSW
 * the same way.
 */
const showKit = import.meta.env.DEV || import.meta.env.MODE === 'fixtures'
const Kit = showKit ? lazy(() => import('@/kit')) : null

const queryClient = createQueryClient()

export function App() {
  return (
    <BrowserRouter basename={CONSOLE_BASE}>
      <Providers client={queryClient}>
        <DevDrawerProvider>
          <Suspense fallback={null}>
            <Routes>
              <Route element={<ViewerShell />}>
                <Route index element={<Home />} />
                <Route path={strip(ROUTES.browse)} element={<Browse />} />
                <Route path={strip(ROUTES.search)} element={<Search />} />
                <Route path={strip(ROUTES.title)} element={<TitleDetail />} />
                <Route path={strip(ROUTES.series)} element={<Series />} />
                <Route path={strip(ROUTES.episode)} element={<Episode />} />
                <Route path={strip(ROUTES.person)} element={<Person />} />
                <Route path={strip(ROUTES.collection)} element={<Collection />} />
                <Route path={strip(ROUTES.player)} element={<Player />} />
                <Route path={strip(ROUTES.about)} element={<About />} />
              </Route>
              <Route path="ops" element={<OperatorShell />}>
                <Route index element={<Overview />} />
                <Route path="sources" element={<Sources />} />
                <Route path="bootstrap" element={<Bootstrap />} />
                <Route path="review" element={<Review />} />
                <Route path="rows" element={<Rows />} />
                <Route path="pipeline" element={<Pipeline />} />
                <Route path="insights" element={<Insights />} />
                <Route path="config" element={<Config />} />
              </Route>
              {Kit && <Route path="kit" element={<Kit />} />}
              <Route path="*" element={<NotFound />} />
            </Routes>
          </Suspense>
          {/* Above every screen and outside the shells, because a receipt for a
              queued job and the request journal both outlive the screen that
              produced them. */}
          <ToastStack />
          <DevDrawer />
        </DevDrawerProvider>
      </Providers>
    </BrowserRouter>
  )
}

/** `ROUTES` holds absolute paths; nested `<Route>` wants them relative. */
function strip(path: string): string {
  return path.replace(/^\//, '')
}
