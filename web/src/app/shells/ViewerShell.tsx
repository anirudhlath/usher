import { useEffect, useRef, useState, type ReactNode } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Icon } from '@/design-system/components/icon'
import { IconButton } from '@/design-system/components/actions'
import { LiveIndicator } from '@/design-system/components/status'
import { useEventStream } from '@/api/events'
import { AppearanceProvider, RouteAnnouncer, SkipLink, useFocusOnRouteChange } from '@/patterns'
import { ROUTES, VIEWER_NAV, VIEWER_TABS } from '../routes'
import { Wordmark } from '../Wordmark'
import { useDevDrawer } from '../dev-drawer-context'
import { useViewport } from '../useViewport'

/**
 * Viewer chrome: a sticky 56 px header at desktop and tablet, a bottom tab bar
 * at phone. **Dark only** — a light theme here is not a missing feature, it is
 * a product rule: this half lives behind film artwork, which is overwhelmingly
 * warm, and the neutral ramp was chosen for that.
 */
export function ViewerShell() {
  return (
    <AppearanceProvider pinnedTheme="dark" density="comfortable">
      <ViewerChrome />
    </AppearanceProvider>
  )
}

function ViewerChrome() {
  const { phone } = useViewport()
  const heading = useRef<HTMLElement>(null)
  const location = useLocation()
  useFocusOnRouteChange(heading)

  // The stream is opened once, by the shell, and nothing below depends on it.
  // patterns.md §7: the UI must be fully correct if zero frames ever arrive.
  const live = useEventStream()

  return (
    <div className="u-shell">
      <SkipLink />
      {!phone && <ViewerHeader liveState={live.state} lastEventAt={live.lastEventAt} />}
      {phone && <ViewerPhoneHeader />}
      <main id="main" ref={heading} tabIndex={-1} className="flex-1">
        <Outlet />
      </main>
      {phone && <ViewerTabBar />}
      <RouteAnnouncer label={routeLabel(location.pathname)} />
    </div>
  )
}

function routeLabel(pathname: string): string {
  const match = VIEWER_TABS.find((item) => item.to === pathname)
  return match ? `${match.label}, Usher` : 'Usher'
}

/** Only shown in idle state, and only as a wall-clock time. */
function clockOf(timestamp: number | null): string | undefined {
  if (timestamp === null) return undefined
  return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function ViewerHeader({
  liveState,
  lastEventAt,
}: {
  liveState: 'connected' | 'idle' | 'reconnecting' | 'off'
  lastEventAt: number | null
}) {
  const navigate = useNavigate()
  const drawer = useDevDrawer()
  const scrolled = useScrolled()

  return (
    <header className={scrolled ? 'u-vheader' : 'u-vheader u-vheader--transparent'}>
      <NavLink to={ROUTES.home} className="u-navlink" aria-label="Usher, home">
        <Wordmark />
      </NavLink>
      <nav className="u-vheader__nav" aria-label="Main">
        {VIEWER_NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === ROUTES.home}
            className="u-navlink"
            aria-current={undefined}
          >
            {({ isActive }) => (
              <>
                <Icon name={item.icon} size={16} />
                {item.label}
                {isActive && <span className="u-visually-hidden">(current page)</span>}
              </>
            )}
          </NavLink>
        ))}
      </nav>
      <span className="u-vheader__spacer">
        <LiveIndicator state={liveState} {...withClock(lastEventAt)} />
        <span className="u-vheader__actions">
          <IconButton
            label="Operator console"
            icon={<Icon name="sliders-horizontal" size={20} />}
            onClick={() => navigate(ROUTES.ops)}
          />
          <IconButton
            label="About and attribution"
            icon={<Icon name="info" size={20} />}
            onClick={() => navigate(ROUTES.about)}
          />
          <IconButton
            label="Developer drawer (⌘\)"
            icon={<Icon name="terminal" size={20} />}
            outlined
            onClick={drawer.toggle}
          />
        </span>
      </span>
    </header>
  )
}

/**
 * `exactOptionalPropertyTypes` means `lastEventAt={undefined}` is not the same
 * as omitting it, so the prop is spread in or not at all.
 */
function withClock(timestamp: number | null): { lastEventAt?: string } {
  const clock = clockOf(timestamp)
  return clock ? { lastEventAt: clock } : {}
}

/**
 * The phone header holds the wordmark and two icon buttons and nothing else.
 * patterns.md §11 names this specifically: the previous client's phone header
 * overflowed, and everything else belongs in the tab bar or a sheet.
 */
function ViewerPhoneHeader() {
  const navigate = useNavigate()
  const scrolled = useScrolled()
  return (
    <header className={scrolled ? 'u-vheader' : 'u-vheader u-vheader--transparent'}>
      <NavLink to={ROUTES.home} className="u-navlink" aria-label="Usher, home">
        <Wordmark size="sm" />
      </NavLink>
      <span className="u-vheader__spacer">
        <span className="u-vheader__actions">
          <IconButton
            label="Search"
            icon={<Icon name="search" size={20} />}
            touch
            onClick={() => navigate(ROUTES.search)}
          />
          <IconButton
            label="About and attribution"
            icon={<Icon name="info" size={20} />}
            touch
            onClick={() => navigate(ROUTES.about)}
          />
        </span>
      </span>
    </header>
  )
}

function ViewerTabBar() {
  return (
    <nav className="u-tabbar" aria-label="Main">
      {VIEWER_TABS.map((item) => (
        <NavLink key={item.to} to={item.to} end={item.to === ROUTES.home} className="u-tabbar__item">
          <Icon name={item.icon} size={20} />
          <span className="u-tabbar__label">{item.label}</span>
        </NavLink>
      ))}
    </nav>
  )
}

/**
 * The header is transparent over a title screen's full-bleed backdrop until
 * something scrolls under it, at which point it takes `--glass-header` and a
 * hairline. `passive: true` because this listener never calls `preventDefault`
 * and a non-passive scroll listener blocks the compositor.
 */
function useScrolled(threshold = 8): boolean {
  const [scrolled, setScrolled] = useState(false)
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > threshold)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [threshold])
  return scrolled
}

/** For screens that want the shell's section heading style. */
export function SectionHead({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="u-sectionhead">
      <h2 className="u-sectionhead__title">{children}</h2>
      {action && <span className="u-sectionhead__action">{action}</span>}
    </div>
  )
}
