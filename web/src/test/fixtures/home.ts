/**
 * `GET /home` — the whole screen in one response, with no cursor.
 *
 * Three rows, because three is what the skeleton is shaped for (patterns.md §1)
 * and because the three exercise the parts of `RowResponse` that differ:
 *
 * · a row with a `reason` sentence and a `null` one — `reason` is nullable and
 *   a row that has none must not get an invented explanation;
 * · three of the four `display_hint` members, which say what shape a card *is*
 *   and never where to put it (ADR-0006);
 * · a card mid-episode (`episode_id` + `episode_label` + `position_seconds`),
 *   a card played through, and cards the household does not own.
 *
 * `RowCardResponse` is the **only** DTO in this API that carries artwork, and it
 * carries an image *id string* rather than an object. Search, browse,
 * similarity, collection members and filmography entries have no artwork member
 * at all, so those grids are text-forward by contract rather than by omission.
 */

import type { HomeResponse, RowCard, RowResponse } from '@/api'
import {
  IMAGE_BACKDROP,
  IMAGE_POSTER,
  TITLE_ENRICHED,
  TITLE_SERIES,
  TITLE_SIMILAR_STALE,
  TITLE_SKELETON,
  EPISODE_PILOT,
} from './ids'

export const cardStalker: RowCard = {
  title_id: TITLE_ENRICHED,
  kind: 'movie',
  name: 'Stalker',
  year: 1979,
  enrichment_state: 'enriched',
  owned: true,
  position_seconds: 3_142,
  runtime_seconds: 9_720,
  played: false,
  episode_id: null,
  episode_label: null,
  artwork: IMAGE_POSTER,
}

export const cardSolaris: RowCard = {
  title_id: TITLE_SKELETON,
  kind: 'movie',
  name: 'Solaris',
  year: 1972,
  enrichment_state: 'skeleton',
  owned: false,
  position_seconds: 0,
  runtime_seconds: null,
  played: false,
  episode_id: null,
  episode_label: null,
  // A skeleton title has no images derived yet, and `artwork: null` is how the
  // row says so. It is not the same as a card whose row provider declined to
  // read artwork — that provider would not be composing a card at all.
  artwork: null,
}

export const cardTwinPeaks: RowCard = {
  title_id: TITLE_SERIES,
  kind: 'series',
  name: 'Twin Peaks',
  year: 1990,
  enrichment_state: 'enriched',
  owned: true,
  position_seconds: 812,
  runtime_seconds: 5_820,
  played: false,
  episode_id: EPISODE_PILOT,
  episode_label: 'S1E1 · Pilot',
  artwork: IMAGE_BACKDROP,
}

export const cardMirror: RowCard = {
  title_id: TITLE_SIMILAR_STALE,
  kind: 'movie',
  name: 'The Mirror',
  year: 1975,
  enrichment_state: 'enriched',
  owned: true,
  position_seconds: 6_240,
  runtime_seconds: 6_240,
  played: true,
  episode_id: null,
  episode_label: null,
  artwork: IMAGE_POSTER,
}

/** Continue watching. A `reason` a person can check against their own memory. */
export const rowContinue: RowResponse = {
  slug: 'continue-watching',
  title: 'Continue watching',
  reason: 'You stopped 52 minutes into Stalker on 14 August.',
  display_hint: 'landscape',
  cards: [cardStalker, cardTwinPeaks],
}

/** A taste row. Its reason names the evidence rather than asserting a taste. */
export const rowBecause: RowResponse = {
  slug: 'because-you-watched-tarkovsky',
  title: 'Because you watched Andrei Tarkovsky',
  reason: 'Four of the last twenty things you finished were directed by him.',
  display_hint: 'portrait',
  cards: [cardSolaris, cardMirror, cardStalker],
}

/**
 * `reason: null` is a real state and not a hole to fill. A row assembled by a
 * `SELECT` has no explanation to give, and inventing one — "picked for you" —
 * would be exactly the fabrication the product's honesty rules forbid.
 */
export const rowRecentlyAdded: RowResponse = {
  slug: 'recently-added',
  title: 'Recently added',
  reason: null,
  display_hint: 'wide',
  cards: [cardTwinPeaks, cardMirror],
}

export const home: HomeResponse = {
  rows: [rowContinue, rowBecause, rowRecentlyAdded],
}

/**
 * An empty screen is a **200**, not an error and not a padded screen. `/home` is
 * a screen rather than a resource, so "this household has nothing" is a fact
 * about the household — and it has to stay distinguishable, because a "popular
 * titles" filler row produces a screen that looks personalised and is not.
 */
export const homeEmpty: HomeResponse = { rows: [] }
