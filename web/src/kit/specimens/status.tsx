import { Badge, Button, CursorProgress, Icon, LiveIndicator, StateBlock } from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

export function StatusSpecimens() {
  return (
    <GroupSection
      id="status"
      title="Status"
      blurb="Four absent states with four treatments, because the API distinguishes them and choosing the wrong one is a correctness bug. Progress without a denominator reports throughput and position and says outright that no estimate exists."
    >
      <Specimen name="Badge/tier-skeleton">
        <Badge tier="skeleton">skeleton</Badge>
      </Specimen>

      <Specimen name="Badge/tier-stub">
        <Badge tier="stub">stub</Badge>
      </Specimen>

      <Specimen name="Badge/tier-enriched">
        <Badge tier="enriched">enriched</Badge>
      </Specimen>

      <Specimen name="Badge/tier-failed">
        <Badge tier="failed">failed</Badge>
      </Specimen>

      <Specimen name="Badge/tone-neutral" note="Neutral encodes no state, so it gets no glyph.">
        <Badge>catalog only</Badge>
      </Specimen>

      <Specimen
        name="Badge/tone-good"
        note="The fixed state glyph is supplied rather than merely allowed — a caller who forgot it would be encoding in hue alone."
      >
        <Badge tone="good">owned</Badge>
      </Specimen>

      <Specimen name="Badge/tone-warn">
        <Badge tone="warn">missing</Badge>
      </Specimen>

      <Specimen name="Badge/tone-bad">
        <Badge tone="bad">parked</Badge>
      </Specimen>

      <Specimen name="Badge/tone-info">
        <Badge tone="info">fused</Badge>
      </Specimen>

      <Specimen name="Badge/custom-icon">
        <Badge tone="info" icon={<Icon name="zap" />}>
          semantic
        </Badge>
      </Specimen>

      <Specimen
        name="Badge/mono-outline"
        note="There is no quality string in the API — this one is composed from the target's fields."
      >
        <Badge mono outline>
          2160p · HDR10 · HEVC · MKV
        </Badge>
      </Specimen>

      <Specimen
        name="StateBlock/never"
        width={340}
        note="Dashed hairline, italic sentence. `meta` names the field that proves the claim and is not droppable to reduce clutter."
      >
        <div className="k-fill">
          <StateBlock kind="never" meta="computed_at: null">
            We have never computed similar titles for this one.
          </StateBlock>
        </div>
      </Specimen>

      <Specimen
        name="StateBlock/empty"
        width={340}
        note="Solid hairline on a sunken fill: computed, and genuinely nothing there."
      >
        <div className="k-fill">
          <StateBlock kind="empty" meta="neighbors: [] · computed 3 days ago">
            Nothing scored close enough to show.
          </StateBlock>
        </div>
      </Specimen>

      <Specimen
        name="StateBlock/stale"
        width={340}
        note="Amber hairline: computed, but the inputs changed since."
      >
        <div className="k-fill">
          <StateBlock kind="stale" meta="stale: true">
            Computed before the scoring blend changed. Shown as they were.
          </StateBlock>
        </div>
      </Specimen>

      <Specimen
        name="StateBlock/na"
        width={340}
        note="Not applicable to this kind of title. An em dash and one clause, inline — no heading."
      >
        <div className="k-fill">
          <StateBlock kind="na">Collections are films only.</StateBlock>
        </div>
      </Specimen>

      <Specimen name="StateBlock/action" width={340}>
        <div className="k-fill">
          <StateBlock
            kind="never"
            meta="expanded_query: null"
            action={
              <Button size="sm" variant="secondary">
                Queue a rebuild
              </Button>
            }
          >
            Query expansion has never run on this deployment.
          </StateBlock>
        </div>
      </Specimen>

      <Specimen
        name="LiveIndicator/connected"
        note="A frame count is drawn and never announced — §7 announces only reconnecting and resync_required."
      >
        <LiveIndicator state="connected" detail="3 frames in the last minute" />
      </Specimen>

      <Specimen
        name="LiveIndicator/idle"
        note="Quiet is healthy. A heartbeat comment arrives every 20 s, so an idle stream is drawn in the same neutral tone as a busy one."
      >
        <LiveIndicator state="idle" lastEventAt="14:22" />
      </Specimen>

      <Specimen name="LiveIndicator/reconnecting">
        <LiveIndicator state="reconnecting" />
      </Specimen>

      <Specimen name="LiveIndicator/resync" note="The one sentence a resync is allowed to say out loud.">
        <LiveIndicator state="reconnecting" detail="resync_required — refetching" />
      </Specimen>

      <Specimen name="LiveIndicator/off" note="The UI must be fully correct if zero frames ever arrive.">
        <LiveIndicator state="off" />
      </Specimen>

      <Specimen
        name="CursorProgress/running"
        wide
        note="Counts, throughput and a durable position — and no percentage, because the server reports a cursor."
      >
        <div className="k-fill">
          <CursorProgress
            dataset="imdb"
            phase="title.basics"
            status="running"
            rowsSeen={4120338}
            rowsWritten={3988104}
            rowsPerSecond={1240}
            position="tt0104988"
            elapsed="1 h 12 m"
            heartbeatAgoSeconds={4}
            revision="2026-08-19"
          />
        </div>
      </Specimen>

      <Specimen
        name="CursorProgress/stalled"
        wide
        note="No heartbeat for over 120 s. “Stalled?” keeps the question mark: the inference is the design's, not the API's. The age is injected, never read from a clock."
      >
        <div className="k-fill">
          <CursorProgress
            dataset="movielens"
            phase="genome"
            status="running"
            rowsSeen={881204}
            rowsWritten={874001}
            rowsPerSecond={0}
            position="tag:3182"
            elapsed="4 h 02 m"
            heartbeatAgoSeconds={412}
          />
        </div>
      </Specimen>

      <Specimen
        name="CursorProgress/failed"
        wide
        note="A failed run is a normal state, and `error` is printed verbatim."
      >
        <div className="k-fill">
          <CursorProgress
            dataset="crosswalk"
            phase="wikidata"
            status="failed"
            rowsSeen={220144}
            rowsWritten={219008}
            position="Q19241"
            elapsed="18 m"
            heartbeatAgoSeconds={null}
            error="HTTPStatusError: 429 Too Many Requests from query.wikidata.org"
          />
        </div>
      </Specimen>

      <Specimen name="CursorProgress/completed" wide>
        <div className="k-fill">
          <CursorProgress
            dataset="imdb"
            phase="credit-names"
            status="completed"
            rowsSeen={203969}
            rowsWritten={203969}
            rowsPerSecond={null}
            position="nm9871123"
            elapsed="41 m"
            heartbeatAgoSeconds={null}
            revision="2026-08-18"
          />
        </div>
      </Specimen>
    </GroupSection>
  )
}
