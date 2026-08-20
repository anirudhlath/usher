/**
 * `GET /people/{id}` and `GET /collections/{id}`.
 *
 * Two absent-shapes live here and they are different from each other:
 *
 * · A person with no credits has **`groups` absent**, not `groups: []` — the
 *   §2 "not applicable" case, rendered as an em dash and one clause.
 * · A collection is **films only**, because `belongs_to_collection` is a field
 *   of TMDb's `/movie/{id}` with no `/tv/{id}` counterpart. That is the clause
 *   patterns.md quotes verbatim: "— Collections are films only."
 */

import type { CollectionResponse, PersonResponse } from '@/api'
import {
  COLLECTION_TRILOGY,
  PERSON_DIRECTOR,
  TITLE_ENRICHED,
  TITLE_SIMILAR_EMPTY,
  TITLE_SIMILAR_STALE,
  TITLE_SKELETON,
} from './ids'

/**
 * `role` is a label to print, never a key to branch on. Titles are newest first
 * with `title_id` breaking a tie, and a title appears **once** per group
 * however many credits put it there — two characters in one film is one entry.
 */
export const person: PersonResponse = {
  id: PERSON_DIRECTOR,
  name: 'Andrei Tarkovsky',
  known_for_department: 'Directing',
  groups: [
    {
      role: 'Director',
      titles: [
        {
          title_id: TITLE_SIMILAR_STALE,
          kind: 'movie',
          name: 'The Mirror',
          year: 1975,
          enrichment_state: 'enriched',
        },
        {
          title_id: TITLE_SKELETON,
          kind: 'movie',
          name: 'Solaris',
          year: 1972,
          enrichment_state: 'skeleton',
        },
        {
          title_id: TITLE_SIMILAR_EMPTY,
          kind: 'movie',
          name: 'Andrei Rublev',
          year: 1966,
          enrichment_state: 'stub',
        },
      ],
    },
    {
      role: 'Writer',
      titles: [
        {
          title_id: TITLE_ENRICHED,
          kind: 'movie',
          name: 'Stalker',
          year: 1979,
          enrichment_state: 'enriched',
        },
      ],
    },
  ],
}

/**
 * A person with no derived credits. `groups` is **absent**, and the type says
 * so — a consumer reading `person.groups.length` on this fails to compile,
 * which is the whole reason the fixture is typed with `Omit` rather than
 * carrying an empty array.
 */
export type PersonWithoutGroups = Omit<PersonResponse, 'groups'>

export const personWithoutGroups: PersonWithoutGroups = {
  id: '0191f4cd-d9f2-7b20-9fca-960d1e2f3a45',
  name: 'Larisa Tarkovskaya',
  // Nobody has told us what they are known for, which is a `null` rather than
  // an absence: the field is on the wire and empty.
  known_for_department: null,
}

/**
 * Members are in **release order** — the order the repository returned, which a
 * franchise page renders in — and deliberately not owned-first. Sorting the
 * owned to the top is a plausible "show me what I can play" instinct that turns
 * a timeline into two piles.
 *
 * `owned_count` and `total_count` are both given, which makes collection
 * completion **the one legitimate percentage in this product** (patterns.md §14).
 */
export const collection: CollectionResponse = {
  id: COLLECTION_TRILOGY,
  name: 'The Zone Trilogy',
  owned_count: 2,
  total_count: 3,
  titles: [
    {
      title_id: TITLE_SKELETON,
      kind: 'movie',
      name: 'Solaris',
      year: 1972,
      enrichment_state: 'skeleton',
      owned: false,
    },
    {
      title_id: TITLE_SIMILAR_STALE,
      kind: 'movie',
      name: 'The Mirror',
      year: 1975,
      enrichment_state: 'enriched',
      owned: true,
    },
    {
      title_id: TITLE_ENRICHED,
      kind: 'movie',
      name: 'Stalker',
      year: 1979,
      enrichment_state: 'enriched',
      owned: true,
    },
  ],
}

/** A franchise the household owns none of. 0 of 3 is a real, showable number. */
export const collectionUnowned: CollectionResponse = {
  id: '0191f4cd-ea03-7c31-80d9-a71e2f3a4b56',
  name: 'The Apu Trilogy',
  owned_count: 0,
  total_count: 3,
  titles: [
    {
      title_id: '0191f4cd-fb14-7d42-91c8-b82f3a4b5c67',
      kind: 'movie',
      name: 'Pather Panchali',
      year: 1955,
      enrichment_state: 'skeleton',
      owned: false,
    },
    {
      title_id: '0191f4ce-0c25-7e53-a2b7-c9304b5c6d78',
      kind: 'movie',
      name: 'Aparajito',
      year: 1956,
      enrichment_state: 'skeleton',
      owned: false,
    },
    {
      title_id: '0191f4ce-1d36-7f64-b3a6-da415c6d7e89',
      kind: 'movie',
      name: 'The World of Apu',
      year: 1959,
      enrichment_state: 'skeleton',
      owned: false,
    },
  ],
}
