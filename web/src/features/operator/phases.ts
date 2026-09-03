/**
 * The bootstrap phase vocabulary, shared by every operator surface that renders
 * an `ImportRun`.
 *
 * **One table, because a phase is a vocabulary and not a caption.** It lived
 * inside `Bootstrap.tsx` while `Overview.tsx` rendered the same runs under a
 * hardcoded `phase="bootstrap"` — a second, weaker spelling of the same fact.
 * `BootstrapPhase`'s members are the API's own, shared by
 * `POST /admin/bootstrap/{phase}` and the CLI's `--phase` so that a phase
 * cannot exist on one boundary and not the other; this module is the
 * client-side end of that, and it is deliberately the only place in `web/` that
 * knows what a phase is called.
 */

import type { BootstrapPhase, ImportRun } from '@/api'

export interface PhaseSpec {
  phase: Exclude<BootstrapPhase, 'all'>
  label: string
  /** What it downloads. A dataset fact, not a measurement of this deployment. */
  size: string
  writes: string
  /** Why this one cannot move up the list. */
  because: string
}

/**
 * `BootstrapPhase`'s members in execution order — one vocabulary, shared by
 * `POST /admin/bootstrap/{phase}` and the CLI's `--phase`, so a phase cannot
 * exist on one boundary and not the other.
 */
export const PHASES: readonly PhaseSpec[] = [
  {
    phase: 'imdb',
    label: 'IMDb basics',
    size: '~224 MB from IMDb (regenerated daily)',
    writes: 'title skeletons — names, years, runtimes',
    because: 'first: everything below joins to the titles this writes',
  },
  {
    phase: 'credit-names',
    label: 'Credit names',
    size: '~730 MB from IMDb',
    writes: 'person records for every credited name',
    because:
      'joins to titles on imdb_id, and it writes only skeletons — a title already enriched is deferred to TMDb for good, so it runs before anything that enriches',
  },
  {
    phase: 'aliases',
    label: 'Aliases',
    size: '~380 MB from IMDb',
    writes: 'alternate titles used by search',
    because: 'joins to titles on imdb_id',
  },
  {
    phase: 'tmdb-ids',
    label: 'TMDb id export',
    size: '~18 MB from TMDb',
    writes: 'the TMDb id crosswalk',
    because: 'needs the titles the IMDb phase wrote to attach ids to',
  },
  {
    phase: 'crosswalk',
    label: 'Wikidata crosswalk',
    size: 'SPARQL against query.wikidata.org, no dump',
    writes: 'IMDb ↔ TMDb ↔ Wikidata links',
    because: 'links the two id spaces the phases above populate',
  },
  {
    phase: 'movielens',
    label: 'MovieLens genome',
    size: '~265 MB from GroupLens',
    writes: 'tag genome vectors used by similarity',
    because: 'joins to titles on imdb_id',
  },
]

/**
 * A run's human label, taken from the **phase that owns its dataset** and
 * falling back to the wire name.
 *
 * ⚠️ **This took `run.dataset` and compared it to a phase, which matches
 * nothing.** The eight dataset names are `imdb.title.basics`,
 * `imdb.title.ratings`, `imdb.credit_names`, `imdb.title.akas`,
 * `tmdb.ids.movie`, `tmdb.ids.series`, `wikidata.crosswalk` and
 * `movielens.genome`; the six phases are `imdb`, `credit-names`, `aliases`,
 * `tmdb-ids`, `crosswalk` and `movielens`. The two sets are disjoint, so every
 * lookup fell through to the fallback and every phase row read "never run" on a
 * fully imported catalog. `ImportRunResponse.phase` is the field that closes
 * it, and the fixtures were spelling `dataset` as a phase name, which is why
 * nothing in the suite could see any of this.
 */
export function labelFor(run: ImportRun): string {
  return PHASES.find((spec) => spec.phase === run.phase)?.label ?? run.dataset
}
