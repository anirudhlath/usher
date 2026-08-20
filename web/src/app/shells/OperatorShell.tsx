import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { Icon } from '@/design-system/components/icon'
import { IconButton } from '@/design-system/components/actions'
import { Badge, LiveIndicator } from '@/design-system/components/status'
import { useEventStream } from '@/api/events'
import { AppearanceProvider, RouteAnnouncer, SkipLink, useFocusOnRouteChange, useLayer } from '@/patterns'
import { OPS_NAV, ROUTES } from '../routes'
import { Wordmark } from '../Wordmark'
import { useRuntimeConfig } from '../runtime-config-context'
import { useDevDrawer } from '../dev-drawer-context'
import { useViewport } from '../useViewport'

/**
 * The operator nav has three forms (patterns.md §11): a 240 px sidebar at 1440,
 * a 56 px icon rail at 834, and **a menu button opening a sheet at 390**. The
 * third one is not optional — "nothing is hidden at a smaller width without an
 * equivalent path to it", and without it the eight operator surfaces are
 * unreachable on a phone once you are on one of them.
 *
 * The handler is provided here rather than by each screen so a screen cannot
 * forget it; `OpsHeader` reads it from context.
 */
const NavSheetContext = createContext<(() => void) | null>(null)

/**
 * Operator chrome: a 240 px sidebar at desktop, a 56 px icon rail at tablet, a
 * menu button and sheet at phone. **Light theme by default and compact density
 * throughout** — these surfaces get used in daylight, and a control room is
 * dense. The theme is a default rather than a rule here; the viewer's is a rule.
 */
export function OperatorShell() {
  return (
    <AppearanceProvider density="compact">
      <OperatorChrome />
    </AppearanceProvider>
  )
}

function OperatorChrome() {
  const { phone, tablet } = useViewport()
  const heading = useRef<HTMLElement>(null)
  const location = useLocation()
  useFocusOnRouteChange(heading)
  const live = useEventStream()

  const [navOpen, setNavOpen] = useState(false)
  const openNav = useCallback(() => setNavOpen(true), [])
  const closeNav = useCallback(() => setNavOpen(false), [])

  // A sheet that survives navigation covers the page it just took you to.
  useEffect(() => setNavOpen(false), [location.pathname])

  const current = OPS_NAV.find((item) => item.to === location.pathname)

  return (
    <NavSheetContext.Provider value={phone ? openNav : null}>
      <div className="u-shell">
        <SkipLink />
        <div className="u-ops">
          {!phone && <OpsSidebar collapsed={tablet} liveState={live.state} lastEventAt={live.lastEventAt} />}
          <div className="u-ops__main">
            <main id="main" ref={heading} tabIndex={-1}>
              <Outlet />
            </main>
          </div>
        </div>
        {phone && <OpsNavSheet open={navOpen} onClose={closeNav} />}
        <RouteAnnouncer label={current ? `${current.label}, Usher Console` : 'Usher Console'} />
      </div>
    </NavSheetContext.Provider>
  )
}

/**
 * The phone nav. One of exactly two places blur appears in this product (the
 * other is the viewer header over a scrolled backdrop) — never on a card, a
 * table, or behind body text.
 *
 * Registered with the layer stack so `Esc` closes it and exactly one other
 * thing does not close with it. Focus moves in on open and back to the trigger
 * on close, and while it is open the rest of the page is `inert` — which is one
 * attribute instead of a hand-rolled focus trap, and unlike a trap it also
 * takes the background out of the accessibility tree.
 */
function OpsNavSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const sheet = useRef<HTMLDivElement>(null)
  const returnTo = useRef<HTMLElement | null>(null)

  useLayer('sheet', open, onClose)

  useEffect(() => {
    if (!open) {
      returnTo.current?.focus()
      returnTo.current = null
      return
    }
    returnTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    sheet.current?.focus()
  }, [open])

  if (!open) return null

  return (
    <>
      <div className="u-sheet__scrim" onClick={onClose} aria-hidden="true" />
      <div
        className="u-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="Operator navigation"
        tabIndex={-1}
        ref={sheet}
      >
        <div className="u-sheet__head">
          <Wordmark size="sidebar" />
          <IconButton label="Close navigation" icon={<Icon name="x" size={20} />} touch onClick={onClose} />
        </div>
        <nav className="u-sidebar__nav" aria-label="Operator">
          {OPS_NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === ROUTES.ops} className="u-sidelink">
              <Icon name={item.icon} size={20} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
      </div>
    </>
  )
}

function OpsSidebar({
  collapsed,
  liveState,
  lastEventAt,
}: {
  collapsed: boolean
  liveState: 'connected' | 'idle' | 'reconnecting' | 'off'
  lastEventAt: number | null
}) {
  const { version } = useRuntimeConfig()
  const clock =
    lastEventAt === null
      ? undefined
      : new Date(lastEventAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

  return (
    <aside className={collapsed ? 'u-sidebar u-sidebar--collapsed' : 'u-sidebar'}>
      <div className="u-sidebar__brand">
        <NavLink to={ROUTES.home} className="u-navlink" aria-label="Usher, viewer home">
          <Wordmark size="sidebar" abbreviated={collapsed} />
        </NavLink>
        {!collapsed && <Badge tone="neutral">console</Badge>}
      </div>
      <nav className="u-sidebar__nav" aria-label="Operator">
        {OPS_NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === ROUTES.ops}
            className={collapsed ? 'u-sidelink u-sidelink--collapsed' : 'u-sidelink'}
            title={collapsed ? item.label : undefined}
          >
            <Icon name={item.icon} size={16} />
            {/* Collapsed, the label is still the accessible name — a rail of
                eight unlabelled glyphs is unusable with a screen reader, and
                `title` alone is not a reliable accessible name. */}
            <span className={collapsed ? 'u-visually-hidden' : undefined}>{item.label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="u-sidebar__foot">
        {!collapsed && <LiveIndicator state={liveState} {...(clock ? { lastEventAt: clock } : {})} />}
        {/* The version comes from the server, not from the bundle, so a
            console cached from a previous build reports the version actually
            answering it. "unauthenticated LAN" is a statement of fact the
            design insists on: the API has no user or auth concept at all. */}
        {!collapsed && (
          <span className="u-sidebar__version">
            {version ? `v${version}` : 'version unknown'} · unauthenticated LAN
          </span>
        )}
      </div>
    </aside>
  )
}

export function OpsHeader({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: string
  actions?: ReactNode
}) {
  // Read from context rather than taken as a prop: a screen that forgot to pass
  // it would leave the phone nav unreachable, and "every screen remembers" is
  // not a mechanism. Null at ≥834, where the sidebar or the rail is present.
  const openNav = useContext(NavSheetContext)
  const drawer = useDevDrawer()
  return (
    <header className="u-opsheader">
      {openNav && (
        <IconButton label="Open navigation" icon={<Icon name="menu" size={20} />} touch onClick={openNav} />
      )}
      <div className="min-w-0">
        <h1 className="u-opsheader__title">{title}</h1>
        {subtitle && <p className="u-opsheader__subtitle">{subtitle}</p>}
      </div>
      {/*
        The drawer toggle is the shell's, not a screen's. Two operator screens
        had shipped their own copy of this button before it moved here, which
        is eight copies at eight screens and eight chances for one to drift —
        and the drawer is global (it outlives the route, and `⌘\` opens it from
        anywhere), so it was never a screen's to own.
      */}
      <div className="u-opsheader__actions">
        {actions}
        <IconButton
          label="Developer drawer (⌘\)"
          icon={<Icon name="terminal" size={20} />}
          outlined
          onClick={drawer.toggle}
        />
      </div>
    </header>
  )
}

export function OpsSection({
  title,
  note,
  action,
  children,
}: {
  title: string
  note?: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="u-opssection">
      <div className="u-opssection__head">
        <h2 className="u-opssection__title">{title}</h2>
        {note && <span className="u-opssection__note">{note}</span>}
        {action && <span className="u-opssection__action">{action}</span>}
      </div>
      {children}
    </section>
  )
}

/**
 * `REQUIRES BACKEND WORK` is a first-class on-screen label, not a comment in a
 * spec. Seven surfaces in this console are designed and not implementable
 * today (patterns.md §15), and each says so with the missing routes printed in
 * mono — so nobody builds a client against an endpoint that does not exist, and
 * nobody has to redesign the screen when it does.
 *
 * **Do not quietly build a fake around a missing route.**
 */
export function BackendWork({ children, routes }: { children: ReactNode; routes?: string }) {
  return (
    <div className="u-backendwork">
      <span className="u-backendwork__icon">
        <Icon name="hammer" size={16} />
      </span>
      <span className="u-backendwork__body">
        <span className="u-backendwork__eyebrow">Requires backend work</span>
        <span className="u-backendwork__text">{children}</span>
        {routes && <span className="u-backendwork__routes">{routes}</span>}
      </span>
    </div>
  )
}

/**
 * Tri-state, never a bare boolean. A source probe answers yes / no / **unknown**
 * — "we have not asked" is a different fact from "we asked and it said no", and
 * collapsing them is the correctness bug this whole product is organised
 * against. Colour is never the only carrier: hue + glyph + word.
 */
export function Tri({
  value,
  labels = ['yes', 'no', 'unknown'],
}: {
  value: boolean | null | undefined
  labels?: readonly [string, string, string]
}) {
  if (value === true) return <Badge tone="good">{labels[0]}</Badge>
  if (value === false) return <Badge tone="bad">{labels[1]}</Badge>
  return (
    <Badge tone="neutral" icon={<Icon name="circle-dashed" />}>
      {labels[2]}
    </Badge>
  )
}
