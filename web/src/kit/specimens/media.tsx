import {
  Artwork,
  Badge,
  LandscapeCard,
  PosterCard,
  ProgressBar,
  TitleRow,
  type RowCard,
} from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

/**
 * A literal SVG rather than an image id.
 *
 * `Artwork` builds `/images/{id}?w=` from an id, and there is no image proxy
 * behind the gallery — a bare id would issue a request whose failure timing is
 * not reproducible. `srcOverride` exists for exactly this case (fixtures and UI
 * kits) and is the one prop product code may not use. The two colours are
 * fixture pixels, not tokens.
 */
const ART_SRC =
  'data:image/svg+xml,%3Csvg%20xmlns=%22http://www.w3.org/2000/svg%22%20viewBox=%220%200%20200%20300%22%3E%3Crect%20width=%22200%22%20height=%22300%22%20fill=%22%23223038%22/%3E%3Ccircle%20cx=%22100%22%20cy=%22132%22%20r=%2246%22%20fill=%22%233d5661%22/%3E%3Crect%20y=%22232%22%20width=%22200%22%20height=%2268%22%20fill=%22%231a252b%22/%3E%3C/svg%3E'

const STALKER: RowCard = {
  title_id: 'kit-stalker',
  kind: 'movie',
  name: 'Stalker',
  year: 1979,
  enrichment_state: 'enriched',
  artwork: null,
  position_seconds: 4100,
  runtime_seconds: 9660,
}

const SOLARIS: RowCard = {
  title_id: 'kit-solaris',
  kind: 'movie',
  name: 'Solaris',
  year: 1972,
  enrichment_state: 'skeleton',
  artwork: null,
}

const TWIN_PEAKS: RowCard = {
  title_id: 'kit-twin-peaks',
  kind: 'series',
  name: 'Twin Peaks',
  year: 1990,
  enrichment_state: 'enriched',
  artwork: null,
  episode_id: 'kit-tp-s02e05',
  episode_label: 'S02E05',
  position_seconds: 600,
  runtime_seconds: 2700,
}

const MIRROR_WATCHED: RowCard = {
  title_id: 'kit-mirror',
  kind: 'movie',
  name: 'Mirror',
  year: 1975,
  enrichment_state: 'enriched',
  artwork: null,
  runtime_seconds: 6420,
  played: true,
}

const THE_RETURN: RowCard = {
  title_id: 'kit-the-return',
  kind: 'episode',
  name: 'The Return',
  year: 2019,
  enrichment_state: 'enriched',
  artwork: null,
  episode_label: 'S01E02',
  position_seconds: 1400,
  runtime_seconds: 3000,
}

export function MediaSpecimens() {
  return (
    <GroupSection
      id="media"
      title="Media"
      blurb="Artwork comes from the image proxy by id and never by URL, and every failure is a sentence rather than a broken-image glyph. A card is one focusable button carrying a composed name — there is no nested play button anywhere."
    >
      <Specimen
        name="Artwork/no-artwork"
        width={168}
        note="The API omits images when it has none. This is the first of the three absent treatments and it is not a failure."
      >
        <Artwork id={null} kind="poster" name="Solaris" />
      </Specimen>

      <Specimen
        name="Artwork/declined"
        width={168}
        note="404 — this proxy declines that artwork. No request is issued for a known failure."
      >
        <Artwork id="kit-art-1" kind="poster" name="Solaris" status="declined" />
      </Specimen>

      <Specimen
        name="Artwork/retry"
        width={168}
        note="503 with Retry-After. Warn tone, so §12's hue plus icon plus word applies."
      >
        <Artwork id="kit-art-2" kind="poster" name="Mirror" status="retry" />
      </Specimen>

      <Specimen name="Artwork/poster" width={168} note="2:3 — poster and profile.">
        <Artwork srcOverride={ART_SRC} kind="poster" alt="" name="Stalker" />
      </Specimen>

      <Specimen name="Artwork/backdrop" width={280} note="16:9 — backdrop and still.">
        <Artwork srcOverride={ART_SRC} kind="backdrop" alt="" name="Stalker" />
      </Specimen>

      <Specimen name="Artwork/logo" width={168} note="A logo gets the square box.">
        <Artwork srcOverride={ART_SRC} kind="logo" alt="" name="Stalker" />
      </Specimen>

      <Specimen
        name="PosterCard/partly-watched"
        note="The watch state is composed into the button's accessible name — aria-label replaces the subtree, so the progress bar cannot carry it."
      >
        <PosterCard card={STALKER} />
      </Specimen>

      <Specimen
        name="PosterCard/skeleton-tier"
        note="A skeleton is the majority of the catalog, so the tier is printed rather than flagged."
      >
        <PosterCard card={SOLARIS} />
      </Specimen>

      <Specimen name="PosterCard/episode">
        <PosterCard card={TWIN_PEAKS} />
      </Specimen>

      <Specimen name="PosterCard/watched">
        <PosterCard card={MIRROR_WATCHED} />
      </Specimen>

      <Specimen name="PosterCard/hide-tier" note="showTier=false, for a surface where every row is enriched.">
        <PosterCard card={SOLARIS} showTier={false} />
      </Specimen>

      <Specimen
        name="PosterCard/unowned"
        note="Dimmed to --unowned-opacity for a collection member the library does not hold."
      >
        <PosterCard card={SOLARIS} unowned />
      </Specimen>

      <Specimen name="PosterCard/badge">
        <PosterCard card={STALKER} badge={<Badge tone="info">4K</Badge>} />
      </Specimen>

      <Specimen
        name="PosterCard/patched"
        note="A title.updated frame landed within the last second. Colour only — nothing moves, resizes or reorders."
      >
        <PosterCard card={STALKER} patched />
      </Specimen>

      <Specimen name="LandscapeCard/continue-watching">
        <LandscapeCard card={THE_RETURN} subtitle="Aired 12 Mar 2019 · 50 min" />
      </Specimen>

      <Specimen name="LandscapeCard/square">
        <LandscapeCard card={SOLARIS} aspect="square" />
      </Specimen>

      <Specimen name="LandscapeCard/badge">
        <LandscapeCard card={THE_RETURN} badge={<Badge tone="good">owned</Badge>} />
      </Specimen>

      <Specimen name="LandscapeCard/patched">
        <LandscapeCard card={THE_RETURN} patched />
      </Specimen>

      <Specimen name="TitleRow/enriched" wide>
        <div className="k-fill">
          <TitleRow
            title={{
              title_id: 'kit-stalker',
              name: 'Stalker',
              year: 1979,
              kind: 'movie',
              enrichment_state: 'enriched',
              genres: ['Science Fiction', 'Drama'],
            }}
            trailing={<Badge tone="good">owned</Badge>}
          />
        </div>
      </Specimen>

      <Specimen
        name="TitleRow/skeleton"
        wide
        note="A sparse title is not a broken one: the year falls back to an em dash and the tier is stated plainly. The name alone is a legitimate row."
      >
        <div className="k-fill">
          <TitleRow
            title={{
              title_id: 'kit-solaris',
              name: 'Solaris',
              year: 1972,
              kind: 'movie',
              enrichment_state: 'skeleton',
            }}
            trailing={<Badge tone="neutral">catalog only</Badge>}
          />
        </div>
      </Specimen>

      <Specimen
        name="TitleRow/thumb"
        wide
        note="Only when the payload carries artwork. /browse does not, and nothing here fetches one per row."
      >
        <div className="k-fill">
          <TitleRow
            title={{
              title_id: 'kit-mirror',
              name: 'Mirror',
              year: 1975,
              kind: 'movie',
              enrichment_state: 'enriched',
            }}
            thumb
          />
        </div>
      </Specimen>

      <Specimen name="TitleRow/meta" wide>
        <div className="k-fill">
          <TitleRow
            title={{
              title_id: 'kit-tp',
              name: 'Twin Peaks',
              year: 1990,
              kind: 'series',
              enrichment_state: 'enriched',
            }}
            meta={<span>3 seasons on record</span>}
            trailing={<Badge tier="enriched">enriched</Badge>}
          />
        </div>
      </Specimen>

      <Specimen name="ProgressBar/partly-watched" width={240}>
        <div className="k-fill">
          <ProgressBar positionSeconds={4100} runtimeSeconds={9660} />
        </div>
      </Specimen>

      <Specimen name="ProgressBar/watched" width={240}>
        <div className="k-fill">
          <ProgressBar positionSeconds={9660} runtimeSeconds={9660} played />
        </div>
      </Specimen>

      <Specimen
        name="ProgressBar/no-runtime"
        width={240}
        note="No denominator, so aria-valuenow is omitted and aria-valuetext says so. A valuenow of 0 would be announced as 0 per cent, which is a different claim from “we do not know”."
      >
        <div className="k-fill">
          <ProgressBar positionSeconds={600} runtimeSeconds={null} />
        </div>
      </Specimen>

      <Specimen name="ProgressBar/labelled" width={240}>
        <div className="k-fill">
          <ProgressBar positionSeconds={600} runtimeSeconds={2700} label="10 of 45 min watched" />
        </div>
      </Specimen>
    </GroupSection>
  )
}
