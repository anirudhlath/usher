import { useState } from 'react'
import {
  Checkbox,
  FilterChip,
  Icon,
  Input,
  SearchCombobox,
  Select,
  Switch,
  type SuggestGroup,
} from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

const SORT_OPTIONS = [
  { value: 'popularity', label: 'Popularity' },
  { value: 'name', label: 'Name' },
  { value: 'year', label: 'Year' },
  { value: 'vote_count', label: 'Vote count' },
]

/**
 * The two suggest tiers, frozen. They are two different queries against two
 * different indexes and never a fallback chain, which is why each keeps its own
 * group header.
 */
const SUGGEST_GROUPS: SuggestGroup[] = [
  {
    tier: 'prefix',
    label: 'Starts with',
    items: [
      { title_id: 'kit-1', name: 'Solaris', year: 1972, tier: 'enriched' },
      { title_id: 'kit-2', name: 'Solaris', year: 2002, tier: 'skeleton' },
    ],
  },
  {
    tier: 'fuzzy',
    label: 'Close matches',
    items: [{ title_id: 'kit-3', name: 'Solar Opposites', year: 2020, tier: 'stub' }],
  },
]

/** Local state, fixed initial value: operable without being non-deterministic. */
function LiveSwitch({
  id,
  label,
  description,
  initial,
  disabled = false,
}: {
  id: string
  label: string
  description?: string
  initial: boolean
  disabled?: boolean
}) {
  const [on, setOn] = useState(initial)
  return (
    <Switch
      id={id}
      checked={on}
      onChange={setOn}
      label={label}
      disabled={disabled}
      {...(description === undefined ? {} : { description })}
    />
  )
}

function TriChip({ initial }: { initial: boolean | undefined }) {
  const [value, setValue] = useState<boolean | undefined>(initial)
  return <FilterChip label="Owned" tri value={value} onToggle={setValue} />
}

/**
 * Every combobox specimen is stateful, for two reasons. The open one has to be
 * genuinely operable — the axe sweep needs a real open listbox with a real
 * `aria-activedescendant`, not a closed shell — and a `value` with no `onChange`
 * is a React warning on every one of them.
 */
function LiveCombobox({
  id,
  initialValue,
  initialOpen,
  groups,
  loading = false,
  activeFirst = false,
}: {
  id: string
  initialValue: string
  initialOpen: boolean
  groups: SuggestGroup[]
  loading?: boolean
  activeFirst?: boolean
}) {
  const [value, setValue] = useState(initialValue)
  const [open, setOpen] = useState(initialOpen)
  const [activeIndex, setActiveIndex] = useState(activeFirst ? 0 : -1)
  return (
    <SearchCombobox
      id={id}
      value={value}
      onChange={setValue}
      open={open}
      onOpenChange={setOpen}
      activeIndex={activeIndex}
      onActiveIndexChange={setActiveIndex}
      groups={groups}
      loading={loading}
    />
  )
}

export function FormsSpecimens() {
  return (
    <GroupSection
      id="forms"
      title="Forms"
      blurb="Fields carry their own label, hint, error and aria wiring. An error is the server's errors[].msg printed verbatim — never reworded, never parsed, never invented."
    >
      <Specimen name="Input/labelled-mono" width={340}>
        <Input
          id="kit-input-base-url"
          label="Base URL"
          mono
          defaultValue="http://emby.lan:8096"
          hint="Stored on the server, never returned."
          className="k-fill"
        />
      </Specimen>

      <Specimen
        name="Input/error"
        width={340}
        note="aria-invalid, role=alert, and the x-circle glyph so the hue is not the only carrier. The hint is replaced rather than stacked, so its id is never dangling."
      >
        <Input
          id="kit-input-username"
          label="Username"
          defaultValue=""
          error="Field required."
          className="k-fill"
        />
      </Specimen>

      <Specimen name="Input/hint" width={340}>
        <Input
          id="kit-input-device"
          label="Device id"
          mono
          defaultValue="usher-console"
          hint="Sent to Emby as the client identifier."
          className="k-fill"
        />
      </Specimen>

      <Specimen name="Input/lead-trail" width={340}>
        <Input
          id="kit-input-search"
          label="Filter"
          defaultValue="tarkovsky"
          lead={<Icon name="search" size={16} />}
          trail={<Icon name="x" size={16} />}
          className="k-fill"
        />
      </Specimen>

      <Specimen name="Input/textarea" width={340}>
        <Input
          id="kit-input-notes"
          label="Notes"
          textarea
          defaultValue="Living Room holds the 4K copies."
          className="k-fill"
        />
      </Specimen>

      <Specimen
        name="Input/password"
        width={340}
        note="A credential is write-only (§13): nothing reads the value back into markup, and the hint says where it goes."
      >
        <Input
          id="kit-input-api-key"
          label="API key"
          type="password"
          hint="Stored encrypted on the server, never returned by the API."
          className="k-fill"
        />
      </Specimen>

      <Specimen
        name="Input/required"
        width={340}
        note="aria-required only. The native attribute would hand validation to the browser's bubbles, and the field-scale error belongs to the server's 422."
      >
        <Input id="kit-input-name" label="Display name" required defaultValue="" className="k-fill" />
      </Specimen>

      <Specimen name="Input/disabled" width={340}>
        <Input
          id="kit-input-disabled"
          label="Source id"
          mono
          defaultValue="0191f4c2"
          disabled
          className="k-fill"
        />
      </Specimen>

      <Specimen name="Select/default" width={280}>
        <Select
          id="kit-select-sort"
          label="Sort"
          options={SORT_OPTIONS}
          defaultValue="popularity"
          className="k-fill"
        />
      </Specimen>

      <Specimen name="Select/hint" width={280}>
        <Select
          id="kit-select-mode"
          label="Search mode"
          options={[
            { value: 'full_text', label: 'Full text' },
            { value: 'semantic', label: 'Semantic' },
            { value: 'fused', label: 'Fused' },
          ]}
          defaultValue="fused"
          hint="Changing this restarts the list from the top."
          className="k-fill"
        />
      </Specimen>

      <Specimen name="Select/error" width={280}>
        <Select
          id="kit-select-error"
          label="Sort"
          options={SORT_OPTIONS}
          error="popularity is not a sortable field for this collection."
          className="k-fill"
        />
      </Specimen>

      <Specimen name="Select/disabled" width={280}>
        <Select id="kit-select-disabled" label="Sort" options={SORT_OPTIONS} disabled className="k-fill" />
      </Specimen>

      <Specimen name="Checkbox/checked">
        <Checkbox id="kit-check-owned" label="Owned only" defaultChecked />
      </Specimen>

      <Specimen name="Checkbox/unchecked">
        <Checkbox id="kit-check-unwatched" label="Unwatched only" />
      </Specimen>

      <Specimen
        name="Checkbox/indeterminate"
        note="The review queue's select-all: some of the page, not all of it. A DOM property, written through a ref — there is no indeterminate attribute in HTML."
      >
        <Checkbox id="kit-check-all" label="Select all" indeterminate />
      </Specimen>

      <Specimen name="Checkbox/hint">
        <Checkbox
          id="kit-check-retract"
          label="Allow full retraction"
          hint="Lets a delta sync remove every item a source stopped reporting."
        />
      </Specimen>

      <Specimen name="Checkbox/disabled">
        <Checkbox id="kit-check-disabled" label="Disabled" disabled />
      </Specimen>

      <Specimen name="Checkbox/radio" note="`radio` swaps the input type; the label binding is unchanged.">
        <div className="k-col">
          <Checkbox id="kit-radio-delta" name="kit-sync-kind" radio label="Delta sync" defaultChecked />
          <Checkbox id="kit-radio-full" name="kit-sync-kind" radio label="Full walk" />
        </div>
      </Specimen>

      <Specimen
        name="Switch/on"
        width={340}
        note="The description is where an opaque provider slug becomes a sentence — that is the reason it exists."
      >
        <LiveSwitch
          id="kit-switch-curated"
          initial
          label="curated"
          description="Last night's LLM shelves. Off by default."
        />
      </Specimen>

      <Specimen name="Switch/off" width={340}>
        <LiveSwitch
          id="kit-switch-franchise"
          initial={false}
          label="franchise-completion"
          description="Films from a series you have started but not finished."
        />
      </Specimen>

      <Specimen name="Switch/disabled" width={340}>
        <LiveSwitch
          id="kit-switch-disabled"
          initial
          disabled
          label="genre-affinity"
          description="Locked while a regeneration is running."
        />
      </Specimen>

      <Specimen name="FilterChip/active">
        <FilterChip label="Science Fiction" active removable />
      </Specimen>

      <Specimen name="FilterChip/inactive">
        <FilterChip label="Drama" />
      </Specimen>

      <Specimen
        name="FilterChip/tri-either"
        note="The one genuinely tri-state filter in the product, so it prints its state as a word — three states cannot be read off a border."
      >
        <FilterChip label="Owned" tri value={undefined} />
      </Specimen>

      <Specimen name="FilterChip/tri-owned">
        <FilterChip label="Owned" tri value={true} />
      </Specimen>

      <Specimen name="FilterChip/tri-not-owned">
        <FilterChip label="Owned" tri value={false} />
      </Specimen>

      <Specimen name="FilterChip/tri-interactive" note="Cycles either → owned → not owned.">
        <TriChip initial={undefined} />
      </Specimen>

      <Specimen
        name="SearchCombobox/open"
        width={400}
        minHeight={280}
        note="Operable: ↓/↑ move the active descendant, Esc closes then clears, Enter submits the active suggestion or the free text."
      >
        <LiveCombobox
          id="kit-combo-open"
          initialValue="sol"
          initialOpen
          groups={SUGGEST_GROUPS}
          activeFirst
        />
      </Specimen>

      <Specimen name="SearchCombobox/closed" width={400}>
        <LiveCombobox id="kit-combo-closed" initialValue="" initialOpen={false} groups={[]} />
      </Specimen>

      <Specimen name="SearchCombobox/loading" width={400} minHeight={160}>
        <LiveCombobox id="kit-combo-loading" initialValue="sola" initialOpen groups={[]} loading />
      </Specimen>

      <Specimen
        name="SearchCombobox/empty"
        width={400}
        minHeight={160}
        note="A status message rather than a fake option in a list of real ones. Free text is a first-class outcome — the catalog is far larger than the suggest tiers."
      >
        <LiveCombobox id="kit-combo-empty" initialValue="zzzzz" initialOpen groups={[]} />
      </Specimen>
    </GroupSection>
  )
}
