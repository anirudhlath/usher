import { useSearchParams } from 'react-router-dom'
import { Checkbox } from '@/design-system'
import { AppearanceProvider, type Density, type Theme } from '@/patterns'
import { ActionsSpecimens } from './specimens/actions'
import { ChartsSpecimens } from './specimens/charts'
import { DataSpecimens } from './specimens/data'
import { FeedbackSpecimens } from './specimens/feedback'
import { FormsSpecimens } from './specimens/forms'
import { IconSpecimens } from './specimens/icon'
import { MediaSpecimens } from './specimens/media'
import { NavigationSpecimens } from './specimens/navigation'
import { PlaybackSpecimens } from './specimens/playback'
import { StatusSpecimens } from './specimens/status'
import './gallery.css'

/**
 * The component gallery — every component in every state, in both themes and both
 * densities, built from the real components rather than from a static sheet.
 *
 * It replaces the handoff's ten `<group>.card.html` specimen sheets, which cannot
 * regress with the code, and it has two jobs beyond being a developer surface:
 *
 * · **the visual-regression target.** `e2e/kit.spec.ts` screenshots each
 *   `#group-*` section at 1440 / 834 / 390 and diffs it against a committed
 *   baseline, so a token that stops resolving is caught by a pixel diff rather
 *   than by somebody noticing months later.
 * · **the accessibility sweep target.** One axe run over a page holding every
 *   component, in a real browser with the real stylesheet — which is the only
 *   place **colour contrast** is genuinely checked, because jsdom resolves no
 *   custom properties and the component tests disable that rule.
 *
 * Both jobs impose the same rule on every specimen: **nothing here may move on
 * its own.** No clock, no random, no relative time, no live data. Every value is
 * a frozen literal, because a baseline that drifts trains everyone to ignore it.
 * The one live timer in the library — `Problem`'s Retry-After countdown — is
 * therefore never rendered with a retry control beside it; see that specimen.
 *
 * The route is gated out of a production build in `app/App.tsx`.
 */

const GROUPS: ReadonlyArray<{ id: string; label: string }> = [
  { id: 'icon', label: 'Icon' },
  { id: 'actions', label: 'Actions' },
  { id: 'forms', label: 'Forms' },
  { id: 'navigation', label: 'Navigation' },
  { id: 'media', label: 'Media' },
  { id: 'data', label: 'Data' },
  { id: 'status', label: 'Status' },
  { id: 'feedback', label: 'Feedback' },
  { id: 'playback', label: 'Playback' },
  { id: 'charts', label: 'Charts' },
]

export default function Gallery() {
  /**
   * Appearance lives in the query string so a specimen sheet is addressable:
   * `/console/kit?theme=light&density=compact` is one of the four combinations
   * the Playwright spec walks, and the control strip below writes the same two
   * parameters. Anything not recognised falls back to the viewer's own defaults.
   */
  const [params, setParams] = useSearchParams()
  const theme: Theme = params.get('theme') === 'light' ? 'light' : 'dark'
  const density: Density = params.get('density') === 'compact' ? 'compact' : 'comfortable'

  const setAppearance = (next: { theme: Theme; density: Density }): void => {
    setParams({ theme: next.theme, density: next.density }, { replace: true })
  }

  return (
    /* The two attributes belong to `AppearanceProvider`, which owns `<html>` and
       puts back whatever was there on unmount. Setting `data-theme` by hand on a
       wrapper here would be a second mechanism for the one thing patterns.md §10
       says has exactly one. */
    <AppearanceProvider pinnedTheme={theme} density={density}>
      <div className="k-gallery">
        <header className="k-head">
          <h1 className="k-head__title">Usher Console — component gallery</h1>
          <p className="k-head__blurb">
            Twenty-eight components in ten groups, each in every state its specimen sheet shows. Every value
            on this page is a frozen literal: no clock, no random, no live data. Not part of a production
            build.
          </p>
        </header>

        <AppearanceStrip theme={theme} density={density} onChange={setAppearance} />

        <nav aria-label="Component groups">
          <ul className="k-index">
            {GROUPS.map((group) => (
              <li key={group.id}>
                <a className="u-link" href={`#group-${group.id}`}>
                  {group.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <main id="main">
          <IconSpecimens />
          <ActionsSpecimens />
          <FormsSpecimens />
          <NavigationSpecimens />
          <MediaSpecimens />
          <DataSpecimens />
          <StatusSpecimens />
          <FeedbackSpecimens />
          <PlaybackSpecimens />
          <ChartsSpecimens />
        </main>
      </div>
    </AppearanceProvider>
  )
}

/**
 * Two radio groups rather than two toggles: theme and density are each a choice
 * between named alternatives, and a fieldset with a legend is what says so to a
 * screen reader. Both are ordinary design-system controls — the gallery does not
 * get its own widgets.
 */
function AppearanceStrip({
  theme,
  density,
  onChange,
}: {
  theme: Theme
  density: Density
  onChange: (next: { theme: Theme; density: Density }) => void
}) {
  return (
    <div className="k-bar">
      <fieldset className="k-bar__set">
        <legend className="k-bar__legend">Theme</legend>
        <div className="k-bar__options">
          <Checkbox
            id="kit-theme-dark"
            name="kit-theme"
            radio
            label="Dark"
            checked={theme === 'dark'}
            onChange={() => onChange({ theme: 'dark', density })}
          />
          <Checkbox
            id="kit-theme-light"
            name="kit-theme"
            radio
            label="Light"
            checked={theme === 'light'}
            onChange={() => onChange({ theme: 'light', density })}
          />
        </div>
      </fieldset>

      <fieldset className="k-bar__set">
        <legend className="k-bar__legend">Density</legend>
        <div className="k-bar__options">
          <Checkbox
            id="kit-density-comfortable"
            name="kit-density"
            radio
            label="Comfortable"
            checked={density === 'comfortable'}
            onChange={() => onChange({ theme, density: 'comfortable' })}
          />
          <Checkbox
            id="kit-density-compact"
            name="kit-density"
            radio
            label="Compact"
            checked={density === 'compact'}
            onChange={() => onChange({ theme, density: 'compact' })}
          />
        </div>
      </fieldset>
    </div>
  )
}
