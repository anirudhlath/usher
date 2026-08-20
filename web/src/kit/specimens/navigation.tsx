import { useState, type ReactNode } from 'react'
import { Badge, Icon, Tabs, type TabItem } from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

const SEASONS: TabItem[] = [
  { value: 'specials', label: 'Specials', count: 6 },
  { value: 's1', label: 'Season 1', count: 10 },
  { value: 's2', label: 'Season 2', count: 10 },
  { value: 's3', label: 'Season 3' },
]

/** Nine, fixed — enough to overflow the 390 px tablist. */
const MANY_SEASONS: TabItem[] = [
  { value: 'specials', label: 'Specials', count: 6 },
  { value: 's1', label: 'Season 1', count: 22 },
  { value: 's2', label: 'Season 2', count: 24 },
  { value: 's3', label: 'Season 3', count: 24 },
  { value: 's4', label: 'Season 4', count: 21 },
  { value: 's5', label: 'Season 5', count: 22 },
  { value: 's6', label: 'Season 6', count: 22 },
  { value: 's7', label: 'Season 7', count: 25 },
  { value: 's8', label: 'Season 8', count: 24 },
]

const SURFACES: TabItem[] = [
  { value: 'overview', label: 'Overview', icon: <Icon name="gauge" size={16} /> },
  { value: 'unmatched', label: 'Unmatched', icon: <Icon name="scan-search" size={16} /> },
  { value: 'jobs', label: 'Jobs', icon: <Icon name="git-branch" size={16} /> },
]

function LiveTabs({
  id,
  tabs,
  initial,
  children,
}: {
  id: string
  tabs: TabItem[]
  initial: string
  children: (value: string) => ReactNode
}) {
  const [value, setValue] = useState(initial)
  return (
    <Tabs id={id} tabs={tabs} value={value} onChange={setValue}>
      {children(value)}
    </Tabs>
  )
}

export function NavigationSpecimens() {
  return (
    <GroupSection
      id="navigation"
      title="Navigation"
      blurb="Real tabs — roving tabindex, arrow keys, Home and End, aria-controls wiring. A count belongs on a tab only when the API actually counts the thing; there are no totals over a keyset list."
    >
      <Specimen
        name="Tabs/seasons"
        wide
        note="Operable: ← and → move focus and selection together, Home and End jump to the ends. Season 3 has no count because none was returned."
      >
        <div className="k-fill">
          <LiveTabs id="kit-tabs-seasons" tabs={SEASONS} initial="s1">
            {(value) => (
              <div className="k-row">
                <span>Episodes for {value} load keyset-paged, 50 at a time.</span>
                <Badge tone="warn">episode_count 10 · list returned 9</Badge>
              </div>
            )}
          </LiveTabs>
        </div>
      </Specimen>

      <Specimen
        name="Tabs/with-icons"
        wide
        note="Icons are decoration on a labelled tab; the label is still the accessible name."
      >
        <div className="k-fill">
          <LiveTabs id="kit-tabs-surfaces" tabs={SURFACES} initial="overview">
            {(value) => <span>The {value} panel for this source.</span>}
          </LiveTabs>
        </div>
      </Specimen>

      <Specimen
        name="Tabs/overflow"
        wide
        note="Nine seasons at 390 px. The tablist scrolls rather than wrapping or shrinking its type, and the roving tab stop still enters it once."
      >
        <div className="k-fill">
          <LiveTabs id="kit-tabs-overflow" tabs={MANY_SEASONS} initial="s4">
            {(value) => <span>Episodes for {value}.</span>}
          </LiveTabs>
        </div>
      </Specimen>
    </GroupSection>
  )
}
