import { Button, Icon, IconButton, TextLink } from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

/**
 * A fragment rather than `#`. A bare `#` is an invalid destination, and the point
 * of an anchor specimen is that it is a real anchor.
 */
const HERE = '#group-actions'

export function ActionsSpecimens() {
  return (
    <GroupSection
      id="actions"
      title="Actions"
      blurb="Monochrome primary, outlined secondary, borderless ghost, two danger treatments. Loading keeps the button's width and swaps in a spinner — use it for /play and every probe, and never fake it."
    >
      <Specimen
        name="Button/primary"
        note="Monochrome on purpose: teal is reserved for links, focus and the info semantic."
      >
        <Button variant="primary">Play</Button>
      </Specimen>

      <Specimen name="Button/secondary">
        <Button variant="secondary" iconLeft={<Icon name="refresh-cw" />}>
          Probe source
        </Button>
      </Specimen>

      <Specimen name="Button/ghost">
        <Button variant="ghost">Skip</Button>
      </Specimen>

      <Specimen name="Button/danger">
        <Button variant="danger">Release job</Button>
      </Specimen>

      <Specimen name="Button/danger-solid" note="Solid red is for irreversible destruction only.">
        <Button variant="danger-solid">Delete source</Button>
      </Specimen>

      <Specimen name="Button/size-sm">
        <Button size="sm" variant="secondary">
          Small
        </Button>
      </Specimen>

      <Specimen name="Button/size-md">
        <Button variant="secondary">Medium</Button>
      </Specimen>

      <Specimen name="Button/size-lg">
        <Button size="lg" variant="primary">
          Large
        </Button>
      </Specimen>

      <Specimen
        name="Button/icons"
        note="Both slots. The leading slot is what the spinner takes while loading."
      >
        <Button variant="secondary" iconLeft={<Icon name="plus" />} iconRight={<Icon name="chevron-right" />}>
          Add source
        </Button>
      </Specimen>

      <Specimen
        name="Button/loading"
        note="aria-busy, the click blocked, and `loadingLabel` replacing the label so the accessible name is the pending sentence."
      >
        <Button variant="primary" loading loadingLabel="Finding copies…">
          Play
        </Button>
      </Specimen>

      <Specimen
        name="Button/loading-no-label"
        note="Without `loadingLabel` the label survives beside the spinner."
      >
        <Button variant="secondary" loading>
          Probe source
        </Button>
      </Specimen>

      <Specimen
        name="Button/disabled"
        note="Disabled and loading both refuse the click; only one of them says why."
      >
        <Button variant="secondary" disabled>
          Sync (source disabled)
        </Button>
      </Specimen>

      <Specimen name="Button/block" width={320}>
        <Button variant="primary" block>
          Start import
        </Button>
      </Specimen>

      <Specimen
        name="Button/as-anchor"
        note="`as='a'` when the action is navigation — an external one carries target and rel."
      >
        <Button as="a" variant="secondary" href={HERE}>
          Open in Grafana
        </Button>
      </Specimen>

      <Specimen
        name="Button/as-anchor-disabled"
        note="An anchor cannot be disabled, so it carries aria-disabled and the click is suppressed."
      >
        <Button as="a" variant="secondary" href={HERE} disabled>
          Open in Grafana
        </Button>
      </Specimen>

      <Specimen
        name="IconButton/default"
        note="`label` is required and is both the accessible name and the tooltip."
      >
        <IconButton label="Search" icon={<Icon name="search" size={20} />} />
      </Specimen>

      <Specimen
        name="IconButton/outlined"
        note="A --border-control outline, so it reads as a control on artwork."
      >
        <IconButton label="Developer drawer" icon={<Icon name="terminal" size={20} />} outlined />
      </Specimen>

      <Specimen
        name="IconButton/touch"
        note="Forces 44×44. Touch overrides density (§10) — compact does not shrink it."
      >
        <IconButton label="Next" icon={<Icon name="chevron-right" size={20} />} outlined touch />
      </Specimen>

      <Specimen name="IconButton/small">
        <IconButton label="Dismiss" icon={<Icon name="x" size={16} />} size="sm" />
      </Specimen>

      <Specimen name="IconButton/disabled">
        <IconButton label="Delete" icon={<Icon name="trash-2" size={20} />} disabled />
      </Specimen>

      <Specimen name="TextLink/default">
        <TextLink href={HERE}>Stalker</TextLink>
      </Specimen>

      <Specimen
        name="TextLink/quiet"
        note="Neutral until hover — for links inside dense tables where teal would be noise."
      >
        <TextLink href={HERE} quiet>
          emby:4412
        </TextLink>
      </Specimen>

      <Specimen
        name="TextLink/external"
        note="Three things at once: rel so the new document cannot reach window.opener, the glyph so the destination is visible, and a visually-hidden sentence so it is audible."
      >
        <TextLink href="https://grafana.usher.invalid/d/library" external>
          Open in Grafana
        </TextLink>
      </Specimen>
    </GroupSection>
  )
}
