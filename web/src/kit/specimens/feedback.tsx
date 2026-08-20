import { useState } from 'react'
import {
  Button,
  ConfirmDialog,
  Icon,
  NOT_MEASURED,
  Problem,
  Skeleton,
  SkeletonRegion,
  TextLink,
  Toast,
  ToastStack,
  type ConfirmFact,
} from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

const BOOTSTRAP_FACTS: ConfirmFact[] = [
  { label: 'downloads', value: '~224 MB from IMDb (regenerated daily)' },
  { label: 'measured', value: '2 h 40 m on a cold run' },
  { label: 'writes', value: 'title skeletons, ~1.27M rows' },
  { label: 'resumable', value: 'yes — from the stored cursor' },
]

const DELETE_FACTS: ConfirmFact[] = [
  { label: 'removes', value: '4,112 media items and every match they carry' },
  { label: 'keeps', value: 'watch state — it lives on the title, not the source' },
  { label: 'measured', value: NOT_MEASURED },
  { label: 'reversible', value: 'no — a re-add is a full walk' },
]

/**
 * Open is a mount rather than a flag on a permanent tree, which is what makes the
 * typed confirmation reset and the trigger get its focus back. Local state here so
 * the dialog is genuinely operable — Esc, the scrim and Cancel all really close it.
 */
function LiveDialog({
  initialOpen,
  trigger,
  ...dialog
}: {
  initialOpen: boolean
  trigger: string
  title: string
  facts: ConfirmFact[]
  confirmLabel: string
  destructive?: boolean
  loading?: boolean
  requireTyped?: string
  children: string
}) {
  const [open, setOpen] = useState(initialOpen)
  return (
    <>
      <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
        {trigger}
      </Button>
      <ConfirmDialog
        {...dialog}
        open={open}
        onCancel={() => setOpen(false)}
        onConfirm={() => setOpen(false)}
      />
    </>
  )
}

export function FeedbackSpecimens() {
  return (
    <GroupSection
      id="feedback"
      title="Feedback"
      blurb="One error surface at four scales over a closed seven-code vocabulary, receipts that say “queued” and never “done”, skeletons shaped like the thing that is coming, and a confirm that names the consequence instead of asking whether you are sure."
    >
      <Specimen
        name="Problem/panel-source-unavailable"
        wide
        note="code, HTTP status and the server's detail verbatim, plus the trace link. Retry-After is printed here but no retry control is rendered, because the countdown is a live timer and a visual baseline may not tick."
      >
        <div className="k-fill">
          <Problem
            scale="panel"
            problem={{
              code: 'source_unavailable',
              status: 503,
              title: "Couldn't reach your media server.",
              detail: 'Living Room did not answer within 5 s.',
              instance: '/admin/sources/0191f4c2/status',
              retry_after: 5,
            }}
            traceId="4f1c9e7a2b8d"
            traceHref="https://tempo.usher.invalid/trace/4f1c9e7a2b8d"
            icon={<Icon name="server-off" size={20} />}
          />
        </div>
      </Specimen>

      <Specimen
        name="Problem/panel-retry"
        wide
        note="The same code with a retry control and no Retry-After window to wait out."
      >
        <div className="k-fill">
          <Problem
            scale="panel"
            problem={{
              code: 'source_unavailable',
              status: 503,
              detail: 'Attic did not answer within 5 s.',
              instance: '/admin/sources/0191f4d0/status',
            }}
            onRetry={() => undefined}
            traceId="9b3d1c7e5a24"
            onOpenTrace={() => undefined}
            icon={<Icon name="server-off" size={20} />}
          />
        </div>
      </Specimen>

      <Specimen
        name="Problem/panel-warn"
        wide
        note="not_playable gets no retry button — there is no playable file, so the recovery is other copies."
      >
        <div className="k-fill">
          <Problem
            scale="panel"
            tone="warn"
            problem={{
              code: 'not_playable',
              status: 409,
              title: "There's no playable file for this.",
              detail: 'Every copy on Living Room reported no media streams.',
            }}
            actions={
              <Button size="sm" variant="secondary">
                See other copies
              </Button>
            }
            icon={<Icon name="alert-triangle" size={20} />}
          />
        </div>
      </Specimen>

      <Specimen
        name="Problem/panel-validation"
        wide
        note="errors[].loc and errors[].msg, printed and never parsed."
      >
        <div className="k-fill">
          <Problem
            scale="panel"
            problem={{
              code: 'validation_failed',
              status: 422,
              title: 'Some fields need another look.',
              detail: 'The source could not be saved.',
              errors: [
                { loc: ['body', 'base_url'], msg: 'base_url must include a scheme.' },
                { loc: ['body', 'api_key'], msg: 'Field required.' },
              ],
            }}
          />
        </div>
      </Specimen>

      <Specimen
        name="Problem/inline"
        width={420}
        note="Field scale — the 422 message beside the field it belongs to."
      >
        <div className="k-fill">
          <Problem
            scale="inline"
            problem={{ code: 'validation_failed', detail: 'base_url must include a scheme.' }}
          />
        </div>
      </Specimen>

      <Specimen
        name="Problem/inline-ticket"
        width={420}
        note="An expired ticket is a one-tap recovery, not an error page."
      >
        <div className="k-fill">
          <Problem
            scale="inline"
            problem={{ code: 'ticket_invalid', status: 404, detail: 'That playback link expired.' }}
            onRetry={() => undefined}
          />
        </div>
      </Specimen>

      <Specimen
        name="Problem/page"
        wide
        note="Page scale moves focus to its own heading and is not a live region — the screen reader reads the focused heading, and a live region would say it twice."
      >
        <div className="k-fill">
          <Problem
            scale="page"
            problem={{
              code: 'not_found',
              status: 404,
              title: "We couldn't find that.",
              detail: 'No title exists with id 0191f4c2-8a7e-7c31-b0d9-2f6a1e4c8b55.',
            }}
            actions={
              <Button size="sm" variant="secondary">
                Search instead
              </Button>
            }
          />
        </div>
      </Specimen>

      <Specimen name="Problem/toast-scale" width={420}>
        <div className="k-fill">
          <Problem
            scale="toast"
            problem={{
              code: 'method_not_allowed',
              status: 405,
              detail: 'PATCH is not allowed on /admin/sources.',
            }}
          />
        </div>
      </Specimen>

      <Specimen
        name="Problem/invalid-cursor"
        width={420}
        note="Deliberately blank. invalid_cursor renders nothing at all: a filter changed under an outstanding request, the list silently restarts from the top, and a viewer never sees an error."
      >
        <Problem
          problem={{
            code: 'invalid_cursor',
            status: 400,
            detail: 'Cursor does not match the current filter.',
          }}
        />
      </Specimen>

      <Specimen
        name="Toast/receipt"
        width={420}
        note="202 means queued. The key is the only record of the job and nothing can look it up, so the receipt has no timer and the key is selectable prose rather than a button."
      >
        <div className="k-fill">
          <Toast
            tone="info"
            title="Queued a full sync of Living Room"
            jobKey="sync:full:0191f4c2"
            coalesced
            action={<TextLink href="#group-feedback">Watch it on Pipeline</TextLink>}
          >
            A full walk of the library. 41 minutes last time.
          </Toast>
        </div>
      </Specimen>

      <Specimen name="Toast/good" width={420}>
        <div className="k-fill">
          <Toast tone="good" title="Row providers saved" icon={<Icon name="check-circle" size={16} />}>
            Nine providers enabled, one off.
          </Toast>
        </div>
      </Specimen>

      <Specimen name="Toast/warn" width={420}>
        <div className="k-fill">
          <Toast tone="warn" title="Queued a bootstrap phase" icon={<Icon name="alert-triangle" size={16} />}>
            imdb must finish before crosswalk will run.
          </Toast>
        </div>
      </Specimen>

      <Specimen name="Toast/bad" width={420}>
        <div className="k-fill">
          <Toast tone="bad" title="The probe failed" icon={<Icon name="x-circle" size={16} />}>
            Living Room did not answer within 5 s.
          </Toast>
        </div>
      </Specimen>

      <Specimen name="Toast/dismissable" width={420}>
        <div className="k-fill">
          <Toast
            tone="info"
            title="Queued a regeneration of curated rows"
            jobKey="curate:0191f4c2"
            onDismiss={() => undefined}
          >
            Last generation took 6 m 20 s.
          </Toast>
        </div>
      </Specimen>

      <Specimen
        name="Toast/stack"
        wide
        overlay
        note="The stack is fixed to the bottom right of its containing block; here that is this stage rather than the viewport."
      >
        <ToastStack>
          <Toast tone="info" title="Queued a full sync of Living Room" jobKey="sync:full:0191f4c2">
            A full walk of the library.
          </Toast>
          <Toast tone="info" title="Queued an index backfill" jobKey="index:backfill" coalesced>
            304 titles were stale.
          </Toast>
        </ToastStack>
      </Specimen>

      <Specimen name="Skeleton/text" width={340}>
        <div className="k-fill">
          <Skeleton shape="text" lines={3} />
        </div>
      </Specimen>

      <Specimen name="Skeleton/block" width={340}>
        <div className="k-fill">
          <Skeleton shape="block" height={30} width="42%" />
        </div>
      </Specimen>

      <Specimen name="Skeleton/rail" wide>
        <div className="k-fill">
          <Skeleton shape="rail" count={4} />
        </div>
      </Specimen>

      <Specimen name="Skeleton/table" wide>
        <div className="k-fill">
          <Skeleton shape="table" count={4} />
        </div>
      </Specimen>

      <Specimen name="Skeleton/hero" wide>
        <div className="k-fill">
          <Skeleton shape="hero" />
        </div>
      </Specimen>

      <Specimen
        name="Skeleton/region"
        wide
        note="The other half of the contract: the shapes are aria-hidden, so the region owns aria-busy and a visually-hidden sentence. This is the one place on the page that is busy."
      >
        <div className="k-fill">
          <SkeletonRegion busy label="Loading the review queue …">
            <Skeleton shape="table" count={2} />
          </SkeletonRegion>
        </div>
      </Specimen>

      <Specimen
        name="ConfirmDialog/closed"
        wide
        overlay
        note="Closed is nothing rendered at all. Press the trigger to mount it — focus lands on the confirm button and returns to the trigger on cancel."
      >
        <LiveDialog
          initialOpen={false}
          trigger="Run the IMDb bootstrap phase"
          title="Run the IMDb bootstrap phase?"
          facts={BOOTSTRAP_FACTS}
          confirmLabel="Start import"
        >
          This must run before credit-names, aliases, tmdb-ids, crosswalk and movielens. Later phases will
          refuse until it completes.
        </LiveDialog>
      </Specimen>

      <Specimen
        name="ConfirmDialog/open"
        wide
        overlay
        note="Never “Are you sure?”. The facts are the dialog's job: what it downloads, how long it measured, what it writes, and that every phase is resumable."
      >
        <LiveDialog
          initialOpen
          trigger="Run the IMDb bootstrap phase"
          title="Run the IMDb bootstrap phase?"
          facts={BOOTSTRAP_FACTS}
          confirmLabel="Start import"
        >
          This must run before credit-names, aliases, tmdb-ids, crosswalk and movielens. Later phases will
          refuse until it completes.
        </LiveDialog>
      </Specimen>

      <Specimen
        name="ConfirmDialog/destructive"
        wide
        overlay
        note="requireTyped is for source deletion only, where watch state survives but availability does not. An unmeasured duration is prose, not a measurement, so it is not set in mono."
      >
        <LiveDialog
          initialOpen
          trigger="Delete Living Room"
          title="Delete the source Living Room?"
          facts={DELETE_FACTS}
          confirmLabel="Delete source"
          destructive
          requireTyped="Living Room"
        >
          Every file this source contributed becomes unavailable. The titles stay in the catalog and so does
          what you have watched.
        </LiveDialog>
      </Specimen>

      <Specimen
        name="ConfirmDialog/loading"
        wide
        overlay
        note="The confirm is busy and blocked; the dialog stays put until the request answers."
      >
        <LiveDialog
          initialOpen
          trigger="Regenerate curated rows"
          title="Regenerate curated rows?"
          facts={[
            { label: 'calls', value: 'one LLM generation over a 400-title pool' },
            { label: 'measured', value: '6 m 20 s on this deployment' },
            { label: 'replaces', value: "last night's shelves" },
          ]}
          confirmLabel="Regenerate"
          loading
        >
          Curated rows are additive — the rest of the home screen is unaffected while this runs.
        </LiveDialog>
      </Specimen>
    </GroupSection>
  )
}
