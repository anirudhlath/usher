import type { IconName } from '@/design-system/components/icon'

/**
 * Every route in the console, in one place.
 *
 * Paths here are written **without** the `/console` basename — React Router's
 * `basename` prepends it, and a literal `/console/...` in a `to=` would be
 * doubled. The basename itself lives in `@/api/paths` beside the list of root
 * segments the API owns, because those two facts are the same fact.
 *
 * The operator half is under `/ops` rather than at the top level so that a
 * viewer route and an operator route can never collide as the product grows,
 * and so the two shells have an unambiguous boundary to switch on.
 */
export const ROUTES = {
  home: '/',
  browse: '/browse',
  search: '/search',
  title: '/titles/:titleId',
  series: '/series/:titleId',
  episode: '/episodes/:episodeId',
  person: '/people/:personId',
  collection: '/collections/:collectionId',
  /**
   * Playback is a route rather than a modal so a hand-off to an external player
   * survives a reload — but it is deliberately **not** a shareable link: the
   * route carries a title or episode id, and the ticket is minted fresh on
   * arrival. A ticket URL never appears in the address bar, in a copy button,
   * or anywhere else (patterns.md §13).
   */
  player: '/play/:kind/:id',
  about: '/about',

  ops: '/ops',
  sources: '/ops/sources',
  bootstrap: '/ops/bootstrap',
  review: '/ops/review',
  rows: '/ops/rows',
  pipeline: '/ops/pipeline',
  insights: '/ops/insights',
  config: '/ops/config',
} as const

export type RouteKey = keyof typeof ROUTES

export const titlePath = (titleId: string) => `/titles/${titleId}`
export const seriesPath = (titleId: string) => `/series/${titleId}`
export const episodePath = (episodeId: string) => `/episodes/${episodeId}`
export const personPath = (personId: string) => `/people/${personId}`
export const collectionPath = (collectionId: string) => `/collections/${collectionId}`
export const playerPath = (kind: 'title' | 'episode', id: string) => `/play/${kind}/${id}`

export interface NavItem {
  to: string
  label: string
  icon: IconName
}

/** The viewer's three, in the header at ≥834 and in the tab bar at 390. */
export const VIEWER_NAV: readonly NavItem[] = [
  { to: ROUTES.home, label: 'Home', icon: 'house' },
  { to: ROUTES.browse, label: 'Browse', icon: 'library-big' },
  { to: ROUTES.search, label: 'Search', icon: 'search' },
]

/** The phone tab bar adds About, because the header has room for two icons. */
export const VIEWER_TABS: readonly NavItem[] = [
  ...VIEWER_NAV,
  { to: ROUTES.about, label: 'About', icon: 'info' },
]

export interface OpsNavItem extends NavItem {
  /** A count in mono, e.g. items waiting in the review queue. */
  countKey?: 'unmatched'
  /** An alarm as a `bad` badge, e.g. parked jobs. */
  alarmKey?: 'parked'
}

export const OPS_NAV: readonly OpsNavItem[] = [
  { to: ROUTES.ops, label: 'Overview', icon: 'gauge' },
  { to: ROUTES.sources, label: 'Sources', icon: 'server' },
  { to: ROUTES.bootstrap, label: 'Bootstrap', icon: 'database' },
  { to: ROUTES.review, label: 'Review queue', icon: 'scan-search', countKey: 'unmatched' },
  { to: ROUTES.rows, label: 'Recommendations', icon: 'list-video' },
  { to: ROUTES.pipeline, label: 'Pipeline', icon: 'git-branch', alarmKey: 'parked' },
  { to: ROUTES.insights, label: 'Insights', icon: 'activity' },
  { to: ROUTES.config, label: 'Configuration', icon: 'sliders-horizontal' },
]
