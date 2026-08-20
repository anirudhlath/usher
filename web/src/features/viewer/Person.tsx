import type { ReactElement } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Badge, Icon, PosterCard, Skeleton, SkeletonRegion, StateBlock, type RowCard } from '@/design-system'
import { usePerson, type PersonResponse, type Schemas } from '@/api'
import { titlePath } from '@/app/routes'
import { NotFound, ScreenProblem } from '@/features/shared/NotFound'

type FilmographyGroup = Schemas['FilmographyGroupResponse']
type FilmographyTitle = Schemas['FilmographyTitleResponse']

/**
 * `GET /people/{id}` caps a person's credits at **50, with no cursor and no
 * total**, and answers a 200 either way. So a page that looks complete and a
 * page that has been cut in half are the same document, and the only honest
 * thing a client can do is say so — which is what the notice at the foot of
 * this screen is for. It is not a warning about an error; it is a statement
 * about what the route can and cannot tell us.
 */
const CREDIT_CAP = 50

/**
 * Person — the filmography, grouped by the labels the API supplies.
 *
 * Two design rules that look like omissions and are not:
 *
 * · **The group labels are printed raw.** `role` is "a label to print, never a
 *   key to branch on": `cast` arrives lowercase and `Director`, `Screenplay`,
 *   `Story` and `Writer` arrive capitalised, because that is what the credit
 *   records say. Title-casing them would make the screen disagree with the
 *   data it is showing, and grouping "Screenplay" under a relabelled "Writing"
 *   would merge two distinct credits the API keeps apart.
 * · **There is no photograph, and no placeholder avatar stands in for one.**
 *   The API carries no image, no biography and no birth year for a person, so
 *   a grey silhouette would be inventing a missing asset where there is no
 *   asset to miss. The slot is drawn as an explicitly empty one and the
 *   sentence beside it says why.
 */
export default function Person(): ReactElement {
  const { personId } = useParams()
  const navigate = useNavigate()
  const query = usePerson(personId)

  if (personId === undefined) return <NotFound />
  if (query.isPending) return <PersonSkeleton />
  if (query.isError) {
    return (
      <div className="mx-auto flex w-full max-w-content flex-col gap-6 px-4 py-8 tablet:px-6">
        <ScreenProblem
          error={query.error}
          instance={`/people/${personId}`}
          onRetry={() => query.refetch().then(() => undefined)}
        />
      </div>
    )
  }

  const person = query.data
  const groups = groupsOf(person)
  const credits = groups.reduce((total, group) => total + group.titles.length, 0)

  return (
    <div className="mx-auto flex w-full max-w-content flex-col gap-8 px-4 py-8 tablet:px-6">
      <PersonHeader name={person.name} />

      {groups.length === 0 ? (
        <StateBlock kind="empty" title="No credits on record" meta="groups: absent from payload">
          We hold this person because a credit pointed at them, and then the credit was superseded. There is
          nothing to list.
        </StateBlock>
      ) : (
        <>
          {groups.map((group) => (
            // One group per role by contract: a title appears once in a group
            // however many credits put it there, and a role appears once per
            // person.
            <CreditGroup
              key={group.role}
              group={group}
              onOpen={(title) => navigate(titlePath(title.title_id))}
            />
          ))}
          <TruncationNotice credits={credits} />
        </>
      )}
    </div>
  )
}

/**
 * `groups` is **absent** rather than empty when a person has no derived
 * credits — the route runs with `response_model_exclude_unset=True` for exactly
 * that reason — while the generated type declares it required with a default of
 * `[]`. So the key has to be read defensively: `person.groups.length` compiles
 * and then throws on the one payload the field was designed to express.
 */
function groupsOf(person: PersonResponse): FilmographyGroup[] {
  return Array.isArray(person.groups) ? person.groups : []
}

function PersonHeader({ name }: { name: string }): ReactElement {
  return (
    <header className="flex items-start gap-4">
      {/* An explicitly empty slot, not an avatar. `aria-hidden` because the
          initials are a drawing of an absence and the sentence beside it is
          the announcement. */}
      <span
        aria-hidden="true"
        className="grid h-18 w-18 flex-none place-items-center"
        style={{
          borderRadius: 'var(--radius-pill)',
          border: '1px dashed var(--border-default)',
          font: 'var(--text-title)',
          color: 'var(--text-disabled)',
        }}
      >
        {initials(name)}
      </span>
      <div className="flex flex-col gap-1">
        <h1
          style={{
            font: 'var(--text-display-sm)',
            letterSpacing: 'var(--track-display)',
            color: 'var(--text-primary)',
          }}
        >
          {name}
        </h1>
        <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-muted)' }}>
          No photograph, biography or birth year exists for people in this API.
        </span>
      </div>
    </header>
  )
}

function CreditGroup({
  group,
  onOpen,
}: {
  group: FilmographyGroup
  onOpen: (title: FilmographyTitle) => void
}): ReactElement {
  const count = group.titles.length
  return (
    <section>
      <div className="mb-3 flex items-baseline gap-3">
        {/* Printed exactly as the record spells it — see the file header. */}
        <h2 style={{ font: 'var(--text-title-sm)', color: 'var(--text-primary)' }}>{group.role}</h2>
        <span className="u-mono" style={{ font: 'var(--text-mono-xs)', color: 'var(--text-muted)' }}>
          {count} {count === 1 ? 'credit' : 'credits'}
        </span>
      </div>
      <div className="flex gap-4 overflow-x-auto pb-1">
        {group.titles.map((title) => (
          <PosterCard key={title.title_id} card={cardOf(title)} onOpen={() => onOpen(title)} />
        ))}
      </div>
    </section>
  )
}

/**
 * The silent truncation, said out loud.
 *
 * patterns.md §4 forbids denominators for the same reason this notice exists:
 * there is no total on this route, so the sentence counts what is **on the
 * page** and then names the cap rather than implying a fraction of anything.
 * There is no percentage on this screen and there is nothing here one could
 * honestly be computed from.
 */
function TruncationNotice({ credits }: { credits: number }): ReactElement {
  return (
    <div className="flex max-w-[72ch] items-start gap-3">
      <Badge tone="warn" icon={<Icon name="alert-triangle" />}>
        possibly truncated
      </Badge>
      <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
        This page shows {credits} credits and the API caps a person at {CREDIT_CAP} with no cursor and no
        total. A full-looking page may be missing credits, and there is no way from here to tell. Group labels
        are printed exactly as the data supplies them — “cast” is lowercase because the record says so.
      </span>
    </div>
  )
}

/**
 * A filmography entry is deliberately narrower than a row card: no `owned`, no
 * progress, no `episode_id`. Those are facts about a household and this route
 * takes no user, so the card is given what exists and nothing is invented to
 * fill the rest.
 */
function cardOf(title: FilmographyTitle): RowCard {
  return {
    title_id: title.title_id,
    kind: title.kind,
    name: title.name,
    year: title.year,
    enrichment_state: title.enrichment_state,
  }
}

/** The two-letter mark inside the empty slot. Never rendered to assistive tech. */
function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter((part) => part.length > 0)
    .slice(0, 2)
    .map((part) => part.slice(0, 1).toUpperCase())
    .join('')
}

/**
 * Shaped like the thing that is coming (patterns.md §1): a header block and two
 * rails, at the heights the real content lands at. No route-level spinner.
 */
function PersonSkeleton(): ReactElement {
  return (
    <SkeletonRegion
      busy
      label="Loading this person's filmography …"
      className="mx-auto flex w-full max-w-content flex-col gap-8 px-4 py-8 tablet:px-6"
    >
      <div className="flex items-start gap-4">
        <Skeleton shape="block" width={72} height={72} style={{ borderRadius: 'var(--radius-pill)' }} />
        <div className="flex flex-col gap-2">
          <Skeleton shape="block" width={280} height={34} />
          <Skeleton shape="block" width={360} height={14} />
        </div>
      </div>
      <Skeleton shape="block" width={140} height={20} />
      <Skeleton shape="rail" count={6} />
      <Skeleton shape="block" width={140} height={20} />
      <Skeleton shape="rail" count={6} />
    </SkeletonRegion>
  )
}
