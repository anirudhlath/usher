import type { ReactElement } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Badge, PosterCard, Skeleton, SkeletonRegion, StateBlock, type RowCard } from '@/design-system'
import { useCollection, type Schemas } from '@/api'
import { titlePath } from '@/app/routes'
import { NotFound, ScreenProblem } from '@/features/shared/NotFound'

type CollectionMember = Schemas['CollectionMemberResponse']

/**
 * Collection — franchise completion, and **the one screen in this product that
 * is allowed a percentage**.
 *
 * patterns.md §14 bans every other one: bootstrap gets counts and throughput,
 * lists get "72 loaded so far", `semantic_coverage` is quoted against the
 * denominator it was actually computed over. The rule is not "percentages are
 * ugly" — it is that a percentage is a claim about a whole, and almost nothing
 * in this API knows its whole. `GET /browse` has no total by construction
 * (keyset, ADR-0034), `GET /admin/bootstrap/status` returns a cursor and
 * deliberately no total, and a rail is a sample rather than a population.
 *
 * `CollectionResponse` is the exception because it hands over **both**
 * `owned_count` and `total_count` as real, server-computed numbers over the
 * same set. The denominator is not inferred, not summed client-side from a page
 * of results, and not a proxy for something else. **Do not copy this pattern
 * onto another screen**: if you find yourself dividing on any other surface in
 * this product, the denominator you have is borrowed and the number is a lie.
 */
export default function Collection(): ReactElement {
  const { collectionId } = useParams()
  const navigate = useNavigate()
  const query = useCollection(collectionId)

  if (collectionId === undefined) return <NotFound />
  if (query.isPending) return <CollectionSkeleton />
  if (query.isError) {
    return (
      <div className="mx-auto flex w-full max-w-content flex-col gap-6 px-4 py-8 tablet:px-6">
        <ScreenProblem
          error={query.error}
          instance={`/collections/${collectionId}`}
          onRetry={() => query.refetch().then(() => undefined)}
        />
      </div>
    )
  }

  const collection = query.data
  const members = collection.titles
  /**
   * A collection holds films and only films: `belongs_to_collection` is a field
   * of TMDb's `/movie/{id}` and has no `/tv/{id}` counterpart, so a television
   * library correctly never produces one. A series arriving here is therefore
   * not an empty list and not a failure — it is the fact being **not
   * applicable**, which is its own treatment (patterns.md §2).
   */
  const films = members.filter((member) => member.kind === 'movie')
  const notFilms = members.length - films.length

  return (
    <div className="mx-auto flex w-full max-w-content flex-col gap-6 px-4 py-8 tablet:px-6">
      <header className="flex flex-col gap-3">
        <span className="u-eyebrow">Collection</span>
        <h1
          style={{
            font: 'var(--text-display-sm)',
            letterSpacing: 'var(--track-display)',
            color: 'var(--text-primary)',
          }}
        >
          {collection.name}
        </h1>
        <Completion owned={collection.owned_count} total={collection.total_count} />
        <span className="max-w-[70ch]" style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
          Both numbers come from the collection record, which is why a percentage is honest here and nowhere
          else in this product. Titles are in release order.
        </span>
      </header>

      {members.length === 0 ? (
        <StateBlock kind="na">
          This household has no collections. Collections are films only — a television library correctly never
          gets one.
        </StateBlock>
      ) : (
        <>
          {/* Release order, exactly as the repository returned it, and NOT
              owned-first: sorting the owned to the top is a plausible "show me
              what I can play" instinct that turns a timeline into two piles. */}
          <div className="u-poster-grid">
            {films.map((member) => (
              <PosterCard
                key={member.title_id}
                card={cardOf(member)}
                // Dimmed to --unowned-opacity **in place**. Hiding an unowned
                // member would make a three-film franchise you own one of look
                // like a one-film franchise you own all of.
                unowned={!member.owned}
                onOpen={() => navigate(titlePath(member.title_id))}
                {...(member.owned ? {} : { badge: <Badge tone="neutral">not owned</Badge> })}
              />
            ))}
          </div>
          {notFilms > 0 && (
            <StateBlock kind="na">
              Collections are films only — a television library correctly never gets one, and {notFilms}{' '}
              {notFilms === 1 ? 'member' : 'members'} of this record{' '}
              {notFilms === 1 ? 'is not a film' : 'are not films'} and cannot be shown here.
            </StateBlock>
          )}
        </>
      )}
    </div>
  )
}

/**
 * The legitimate percentage, with both of its numbers shown beside it.
 *
 * The share is rendered **next to** "2 of 3" rather than instead of it, so the
 * reader can see what it was computed from. And when `total_count` is zero
 * there is no percentage at all: a divide by zero is exactly the fabricated
 * denominator §14 forbids, and 0 of 0 is a fact that needs no share.
 */
function Completion({ owned, total }: { owned: number; total: number }): ReactElement {
  const percent = total > 0 ? Math.round((owned / total) * 100) : null

  return (
    <div className="flex flex-wrap items-center gap-3">
      <span data-numeric style={{ font: 'var(--text-metric-sm)', color: 'var(--text-primary)' }}>
        {owned} of {total}
      </span>
      <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-muted)' }}>
        {percent === null ? 'owned' : `owned · ${percent}% complete`}
      </span>
      {percent !== null && (
        <span
          role="progressbar"
          aria-label="Collection completion"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
          aria-valuetext={`${owned} of ${total} owned — ${percent}% complete`}
          className="block h-1 w-40 overflow-hidden"
          style={{ background: 'var(--bg-inset)', borderRadius: 'var(--radius-pill)' }}
        >
          <span className="block h-full" style={{ width: `${percent}%`, background: 'var(--good-text)' }} />
        </span>
      )}
    </div>
  )
}

/**
 * A collection member carries no progress and no watch state — those are
 * per-user and this route takes no user — so the card gets ownership and
 * nothing else. `played` is not passed rather than passed as `false`, because
 * "not watched" and "we were not told" are different claims.
 */
function cardOf(member: CollectionMember): RowCard {
  return {
    title_id: member.title_id,
    kind: member.kind,
    name: member.name,
    year: member.year,
    enrichment_state: member.enrichment_state,
    owned: member.owned,
  }
}

/** Shaped like the grid that is coming, at the grid's own column count. */
function CollectionSkeleton(): ReactElement {
  return (
    <SkeletonRegion
      busy
      label="Loading this collection …"
      className="mx-auto flex w-full max-w-content flex-col gap-6 px-4 py-8 tablet:px-6"
    >
      <Skeleton shape="block" width={90} height={11} />
      <Skeleton shape="block" width={320} height={34} />
      <Skeleton shape="block" width={280} height={20} />
      <Skeleton shape="rail" count={6} />
    </SkeletonRegion>
  )
}
