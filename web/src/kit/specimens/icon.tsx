import { Icon, ICONS, STATE_ICON, type IconName, type StateTone } from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

/** The twelve nouns the handoff's specimen sheet names, with the concept each one stands for. */
const NOUNS: ReadonlyArray<{ name: IconName; label: string }> = [
  { name: 'server', label: 'source' },
  { name: 'database', label: 'catalog' },
  { name: 'scan-search', label: 'review queue' },
  { name: 'git-branch', label: 'pipeline' },
  { name: 'activity', label: 'insights' },
  { name: 'heart-pulse', label: 'readiness' },
  { name: 'play', label: 'play' },
  { name: 'list-video', label: 'rows' },
  { name: 'radio', label: 'live / SSE' },
  { name: 'terminal', label: 'dev drawer' },
  { name: 'sliders-horizontal', label: 'configuration' },
  { name: 'image', label: 'artwork' },
]

/** patterns.md §12: six states, six fixed glyphs, and the word alongside the hue. */
const STATES: ReadonlyArray<{ tone: StateTone; label: string; hue: string }> = [
  { tone: 'good', label: 'good', hue: 'k-tone--good' },
  { tone: 'warn', label: 'warn', hue: 'k-tone--warn' },
  { tone: 'bad', label: 'bad', hue: 'k-tone--bad' },
  { tone: 'info', label: 'info', hue: 'k-tone--info' },
  { tone: 'never', label: 'never computed', hue: 'k-tone--muted' },
  { tone: 'stale', label: 'stale', hue: 'k-tone--muted' },
]

function isIconName(key: string): key is IconName {
  return key in ICONS
}

/** Registry order is declaration order, which is alphabetical and does not drift. */
const REGISTRY: readonly IconName[] = Object.keys(ICONS).filter(isIconName)

export function IconSpecimens() {
  return (
    <GroupSection
      id="icon"
      title="Icon"
      blurb="Lucide, stroke-based, always currentColor. Three sizes and no others: 16 inline with text and in compact rows, 20 in controls and nav, 24 in empty states and headers."
    >
      <Specimen name="Icon/nouns" wide note="20 px — the sizes controls and navigation use.">
        <div className="k-row">
          {NOUNS.map((noun) => (
            <span className="k-cell" key={noun.name}>
              <Icon name={noun.name} size={20} />
              <span className="k-cell__label">{noun.label}</span>
            </span>
          ))}
        </div>
      </Specimen>

      <Specimen
        name="Icon/states"
        wide
        note="The six fixed pairings. A state is hue plus icon plus word, never hue alone."
      >
        <div className="k-row">
          {STATES.map((state) => (
            <span className={`k-pair ${state.hue}`} key={state.tone}>
              <Icon name={STATE_ICON[state.tone]} size={16} />
              <span>{state.label}</span>
            </span>
          ))}
        </div>
      </Specimen>

      <Specimen name="Icon/sizes" note="16 · 20 · 24. Stroke is 1.75 at 16 and 20, 2 at 24.">
        <Icon name="film" size={16} />
        <Icon name="film" size={20} />
        <Icon name="film" size={24} />
      </Specimen>

      <Specimen
        name="Icon/labelled"
        note="`label` is only for an icon that is the sole carrier of meaning: it becomes role=img with an accessible name. Everything else stays aria-hidden."
      >
        <Icon name="radio" size={20} label="Live" />
      </Specimen>

      <Specimen
        name="Icon/custom-svg"
        note="`svg` takes a glyph from outside the registry and wins over `name`."
      >
        <Icon
          size={24}
          label="Usher mark"
          svg={
            <svg viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="9" />
              <path d="M8 12h8" />
            </svg>
          }
        />
      </Specimen>

      <Specimen
        name="Icon/fallback"
        note="No name and no svg — a dashed box rather than a silent gap, so a missing glyph is visible in review."
      >
        <Icon size={24} />
      </Specimen>

      <Specimen
        name="Icon/registry"
        wide
        note="Every glyph the product may use. Adding one is a deliberate act in registry.ts."
      >
        <div className="k-row">
          {REGISTRY.map((name) => (
            <span className="k-cell" key={name}>
              <Icon name={name} size={20} />
              <span className="k-cell__label">{name}</span>
            </span>
          ))}
        </div>
      </Specimen>
    </GroupSection>
  )
}
