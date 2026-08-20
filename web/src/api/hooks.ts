/**
 * One hook per Usher operation, typed off the generated schema.
 *
 * Every page in this app reaches the API through this file and nothing else,
 * so the set of exports here *is* the client's coverage of PRD 07's five
 * tables. An operation with no hook is an operation no screen can reach.
 */

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query'
import { request, UsherProblem, type Ok, type OkPost, type OkPut, type Schemas } from './client'

/* ---------------------------------------------------------------- Screens */

export type HomeResponse = Ok<'/home'>
export type RowResponse = Schemas['RowResponse']
export type RowCard = Schemas['RowCardResponse']

export function useHome() {
  return useQuery({
    queryKey: ['home'],
    queryFn: () => request<HomeResponse>('/home'),
    // The home screen is composed server-side over ten providers and is the
    // most expensive read in the API. Refetching it on every window focus
    // would recompose it for nothing.
    staleTime: 60_000,
  })
}

export type SearchResponse = Ok<'/search'>
export type SearchMode = Schemas['SearchMode']

/**
 * `fused` is the default, and it is a measurement rather than a preference.
 *
 * The default lives here now, in the hook, because the reference client kept
 * two of them — this hook said `full_text` and the search page's URL-parameter
 * reader said `fused` — so which lane ran depended on whether the caller passed
 * an argument. One default, in the place every caller reaches.
 *
 * It was NOT `fused` until 2026-08-19, and the reason is worth keeping:
 * a 1,300-query pre-registered bar (usher issue #21) measured fused at
 * **-17.0 points of recall@1** against `full_text` over titles drawn from the
 * skeleton population -- because RRF sums two reciprocal-rank terms over a
 * `FULL OUTER JOIN` and coalesces a missing lane to zero, so simply *having*
 * an embedding added score the ordering could not tell from relevance. A
 * skeleton's perfect lexical match was capped at `1/(k+1)` and lost to it.
 *
 * Issue #25's fix sorts an exact-name key inside the lexical CTE ahead of that
 * lane's LIMIT, which removes the mechanism for the case it fires on. Re-run
 * unchanged against the merged backend, same sample and same frozen bar:
 *
 *   skeleton stratum   -17.0 pts  ->  -1.8 pts
 *   discordant losses  521        ->  9
 *   never-retrieved    67         ->  0
 *   catalog net        -14.3 pts  ->  -0.47 pts, CI [-1.96, +1.02]
 *   embedded tier      +9.3 pts   ->  +11.0 pts
 *
 * So the net now straddles zero while fused wins outright on the ~10% of the
 * catalog carrying a vector -- a share that grows with every backfill. That is
 * what makes it the right seat, and what would make it the wrong seat again if
 * the catalog net moved back below zero.
 *
 * **The one case that still favours `full_text`** is an exact-name lookup of a
 * title sharing its name with an enriched one: `exact_name` ties, the sort
 * falls through to `score`, and the two-lane sum wins. That is the residue
 * #21 is narrowed to, and it is why the selector keeps all three lanes rather
 * than hiding the one the default replaced.
 */
export function useSearch(q: string, mode: SearchMode = 'fused', limit = 20) {
  return useQuery({
    queryKey: ['search', q, mode, limit],
    queryFn: () => request<SearchResponse>('/search', { query: { q, mode, limit } }),
    enabled: q.trim().length > 0,
  })
}

export type SuggestResponse = Ok<'/search/suggest'>
export type SuggestTier = Schemas['SuggestTier']

/**
 * The two-tier suggest (ADR-0031). `prefix` is the as-you-type tier; `fuzzy`
 * is the typo-tolerant one the 2026-08-03 gate showed cannot meet an
 * as-you-type latency budget on this catalog -- which is why the tier is a
 * parameter the UI exposes rather than a detail it hides.
 *
 * Both tiers get their own group header in the combobox (patterns.md §12):
 * they are two different queries, not a fallback chain, and presenting the
 * second as a continuation of the first would misdescribe both.
 */
export function useSuggest(q: string, tier: SuggestTier = 'prefix', limit = 10) {
  return useQuery({
    queryKey: ['suggest', q, tier, limit],
    queryFn: () => request<SuggestResponse>('/search/suggest', { query: { q, tier, limit } }),
    enabled: q.trim().length > 0,
    staleTime: 30_000,
  })
}

export type BrowseResponse = Ok<'/browse'>
export type BrowseSort = Schemas['BrowseSort']
export type BrowseItem = Schemas['BrowseItemResponse']
export type BrowseFacets = Schemas['BrowseFacetsResponse']

export type BrowseFilters = {
  sort?: BrowseSort
  genre?: string | null
  year?: number | null
  owned?: boolean | null
  facets?: boolean
  limit?: number
}

/**
 * Keyset paging, never an offset (ADR-0034). `next_cursor` carries a position
 * *and* a hash of the query, so changing a filter invalidates the cursor --
 * which is why the filters are part of the query key.
 *
 * `next_cursor ?? undefined` is what stops the list: TanStack reads `undefined`
 * as "there is no next page" and `null` as a legitimate page parameter, so
 * returning the API's `null` straight through would ask for page one forever.
 * The `null` still has to reach the UI, though — patterns.md §4 requires a
 * sentence when the list ends, because a silent stop is indistinguishable from
 * a bug — so read it off the last page's `next_cursor`, not off `hasNextPage`.
 */
export function useBrowse(filters: BrowseFilters) {
  return useInfiniteQuery({
    queryKey: ['browse', filters],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      request<BrowseResponse>('/browse', {
        query: {
          sort: filters.sort ?? 'name',
          genre: filters.genre ?? undefined,
          year: filters.year ?? undefined,
          owned: filters.owned ?? undefined,
          facets: filters.facets ?? false,
          limit: filters.limit ?? 24,
          cursor: pageParam ?? undefined,
        },
      }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
}

/* -------------------------------------------------------------- Resources */

export type TitleResponse = Ok<'/titles/{title_id}'>

/**
 * `searchId` is optional and is how a title view gets attributed back to the
 * search that produced it (`search_queries`). The route declares it and the
 * reference client never sent it, which is the difference between search
 * analytics that measure something and a table of queries nobody ever acted on.
 */
export function useTitle(id: string | undefined, searchId?: string) {
  return useQuery({
    queryKey: ['title', id, searchId ?? null],
    queryFn: () =>
      request<TitleResponse>(`/titles/${id}`, searchId ? { query: { search_id: searchId } } : {}),
    enabled: Boolean(id),
  })
}

export type SimilarResponse = Ok<'/titles/{title_id}/similar'>

/**
 * Three different absent states arrive on this one route and patterns.md §2
 * gives each its own treatment: `computed_at: null` is never-computed,
 * `neighbors: []` with a `computed_at` is computed-and-empty, and `stale: true`
 * means the list is real and its inputs moved — which is shown, not hidden.
 */
export function useSimilar(id: string | undefined) {
  return useQuery({
    queryKey: ['similar', id],
    queryFn: () => request<SimilarResponse>(`/titles/${id}/similar`),
    enabled: Boolean(id),
  })
}

export type EpisodeResponse = Ok<'/episodes/{episode_id}'>

export function useEpisode(id: string | undefined) {
  return useQuery({
    queryKey: ['episode', id],
    queryFn: () => request<EpisodeResponse>(`/episodes/${id}`),
    enabled: Boolean(id),
  })
}

export type SeasonsResponse = Ok<'/series/{title_id}/seasons'>

export function useSeasons(titleId: string | undefined) {
  return useQuery({
    queryKey: ['seasons', titleId],
    queryFn: () => request<SeasonsResponse>(`/series/${titleId}/seasons`),
    enabled: Boolean(titleId),
  })
}

export type SeasonEpisodesResponse = Ok<'/seasons/{season_id}/episodes'>

export function useSeasonEpisodes(seasonId: string | undefined, limit = 50) {
  return useInfiniteQuery({
    queryKey: ['season-episodes', seasonId, limit],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      request<SeasonEpisodesResponse>(`/seasons/${seasonId}/episodes`, {
        query: { limit, cursor: pageParam ?? undefined },
      }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: Boolean(seasonId),
  })
}

export type PersonResponse = Ok<'/people/{person_id}'>

export function usePerson(id: string | undefined) {
  return useQuery({
    queryKey: ['person', id],
    queryFn: () => request<PersonResponse>(`/people/${id}`),
    enabled: Boolean(id),
  })
}

export type CollectionResponse = Ok<'/collections/{collection_id}'>

/**
 * The one place in this product where a percentage is legitimate:
 * `owned_count` and `total_count` are both given, so the denominator is real
 * (patterns.md §14).
 */
export function useCollection(id: string | undefined) {
  return useQuery({
    queryKey: ['collection', id],
    queryFn: () => request<CollectionResponse>(`/collections/${id}`),
    enabled: Boolean(id),
  })
}

/* ---------------------------------------------------------------- Actions */

export type PlayResponse = OkPost<'/titles/{title_id}/play'>
export type PlayTarget = Schemas['PlayTargetResponse']

/**
 * Mints a short-lived opaque ticket that `GET /stream/{ticket}` 302s to the
 * real target (ADR-0029). The shareable artifact is the ticket, never a URL
 * carrying somebody's session token -- so the player must be pointed at
 * `/stream/{ticket}` (`streamPath` in `client.ts`) and never at `target.url`.
 *
 * The response body is a secret for as long as it is valid. It is redacted out
 * of the request journal at the record boundary; nothing else may render, copy
 * or log it (patterns.md §13).
 */
export function usePlayTitle() {
  return useMutation({
    // `search_id` is optional and is how a play gets attributed back to the
    // search that produced it (`search_queries`). Passing it when the user
    // arrived from a result is the difference between search analytics that
    // measure something and a table of queries nobody ever acted on.
    mutationFn: ({ titleId, searchId }: { titleId: string; searchId?: string }) =>
      request<PlayResponse>(`/titles/${titleId}/play`, {
        method: 'POST',
        ...(searchId ? { query: { search_id: searchId } } : {}),
      }),
  })
}

export type PlayEpisodeResponse = OkPost<'/episodes/{episode_id}/play'>

export function usePlayEpisode() {
  return useMutation({
    mutationFn: ({ episodeId, searchId }: { episodeId: string; searchId?: string }) =>
      request<PlayEpisodeResponse>(`/episodes/${episodeId}/play`, {
        method: 'POST',
        ...(searchId ? { query: { search_id: searchId } } : {}),
      }),
  })
}

export type WatchWriteRequest = Schemas['WatchWriteRequest']
export type WatchStateResponse = Schemas['WatchStateResponse']

export function useSetTitleWatchState() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ titleId, body }: { titleId: string; body: WatchWriteRequest }) =>
      request<OkPut<'/watch/titles/{title_id}'>>(`/watch/titles/${titleId}`, {
        method: 'PUT',
        body,
      }),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: ['title', v.titleId] })
      qc.invalidateQueries({ queryKey: ['home'] })
    },
  })
}

export function useSetEpisodeWatchState() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ episodeId, body }: { episodeId: string; body: WatchWriteRequest }) =>
      request<OkPut<'/watch/episodes/{episode_id}'>>(`/watch/episodes/${episodeId}`, {
        method: 'PUT',
        body,
      }),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: ['episode', v.episodeId] })
      qc.invalidateQueries({ queryKey: ['home'] })
    },
  })
}

/** `POST` marks played, `DELETE` marks unplayed -- two operations, one route. */
export function useMarkPlayed() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ titleId, played }: { titleId: string; played: boolean }) =>
      request<WatchStateResponse>(`/watch/titles/${titleId}/played`, {
        method: played ? 'POST' : 'DELETE',
      }),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: ['title', v.titleId] })
      qc.invalidateQueries({ queryKey: ['home'] })
    },
  })
}

/* ------------------------------------------------------------------ Admin */

export type SourceResponse = Schemas['SourceResponse']
export type SourceCreateRequest = Schemas['SourceCreateRequest']
export type SourceStatusResponse = Ok<'/admin/sources/{source_id}/status'>

export function useSources() {
  return useQuery({
    queryKey: ['sources'],
    queryFn: () => request<Ok<'/admin/sources'>>('/admin/sources'),
  })
}

/**
 * `is_administrator: true` in this body is a **risk surface, not a success**
 * (patterns.md §13), and `device_id` on the source is deliberately visible: it
 * is how an operator finds and revokes Usher's session in Emby's own dashboard.
 */
export function useSourceStatus(id: string | undefined) {
  return useQuery({
    queryKey: ['source-status', id],
    queryFn: () => request<SourceStatusResponse>(`/admin/sources/${id}/status`),
    enabled: Boolean(id),
  })
}

/**
 * The one request in this client that carries a real credential. It never
 * reaches the journal — see `REDACTED_KEYS` — and the API never returns it.
 */
export function useCreateSource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SourceCreateRequest) =>
      request<OkPost<'/admin/sources'>>('/admin/sources', { method: 'POST', body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sources'] }),
  })
}

/**
 * The only irreversible action in the product: watch state survives a source
 * deletion, availability does not. patterns.md §5 reserves `requireTyped` for
 * exactly this one.
 */
export function useDeleteSource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => request<null>(`/admin/sources/${id}`, { method: 'DELETE' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sources'] }),
  })
}

export type SyncKind = 'full' | 'delta'
export type SyncTriggerResponse = OkPost<'/admin/sources/{source_id}/sync'>

/** 202. "Queued", never "Done" — see `JobHandle` below. */
export function useSyncSource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, kind }: { id: string; kind?: SyncKind }) =>
      request<SyncTriggerResponse>(`/admin/sources/${id}/sync`, {
        method: 'POST',
        ...(kind ? { query: { kind } } : {}),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['source-status'] }),
  })
}

export type UnmatchedResponse = Ok<'/admin/unmatched'>
export type UnmatchedItem = Schemas['UnmatchedItemResponse']
export type ResolveUnmatchedRequest = Schemas['ResolveUnmatchedRequest']

export function useUnmatched(sourceId?: string, limit = 50) {
  return useInfiniteQuery({
    queryKey: ['unmatched', sourceId ?? null, limit],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      request<UnmatchedResponse>('/admin/unmatched', {
        query: { source_id: sourceId, limit, cursor: pageParam ?? undefined },
      }),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
}

export function useResolveUnmatched() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ResolveUnmatchedRequest }) =>
      request<OkPost<'/admin/unmatched/{media_item_id}/resolve'>>(`/admin/unmatched/${id}/resolve`, {
        method: 'POST',
        body,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['unmatched'] }),
  })
}

export type BootstrapStatusResponse = Ok<'/admin/bootstrap/status'>
export type BootstrapPhase = Schemas['BootstrapPhase']
export type ImportRun = Schemas['ImportRunResponse']

/**
 * `options` is a partial because the poll cadence is the *caller's* decision
 * and patterns.md §8 makes it a conditional one: status costs ~0.33 s and is
 * uncached, so poll every 10 s and **only while at least one run is
 * `running`*. A surface with nothing running says "idle — not polling" rather
 * than polling invisibly forever.
 */
export function useBootstrapStatus(options?: Partial<UseQueryOptions<BootstrapStatusResponse>>) {
  return useQuery({
    queryKey: ['bootstrap-status'],
    queryFn: () => request<BootstrapStatusResponse>('/admin/bootstrap/status'),
    ...options,
  })
}

export type BootstrapTriggerResponse = OkPost<'/admin/bootstrap/{phase}'>

export function useStartBootstrap() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (phase: BootstrapPhase) =>
      request<BootstrapTriggerResponse>(`/admin/bootstrap/${phase}`, {
        method: 'POST',
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['bootstrap-status'] }),
  })
}

export type RowProviderResponse = Schemas['RowProviderResponse']

export function useRowProviders() {
  return useQuery({
    queryKey: ['row-providers'],
    queryFn: () => request<Ok<'/admin/rows/providers'>>('/admin/rows/providers'),
  })
}

export function useSetRowProvider() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ slug, enabled }: { slug: string; enabled: boolean }) =>
      request<OkPut<'/admin/rows/providers/{slug}'>>(`/admin/rows/providers/${slug}`, {
        method: 'PUT',
        body: { enabled },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['row-providers'] })
      qc.invalidateQueries({ queryKey: ['home'] })
    },
  })
}

export function useRegenerateRows() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => request<OkPost<'/admin/rows/regenerate'>>('/admin/rows/regenerate', { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['home'] }),
  })
}

/**
 * What all three 202-shaped admin actions answer with, and **there is no route
 * to look the key up** (patterns.md §6, §15 item 3). The idiom is therefore to
 * name what was queued, print the key in mono so an operator can paste it into
 * a log search, and point at the surface where evidence will appear. Never
 * "Done", never "Saved", never a bare checkmark.
 */
export type JobHandle = SyncTriggerResponse | BootstrapTriggerResponse | OkPost<'/admin/rows/regenerate'>

/* ------------------------------------------------------------------- Meta */

export type ReadinessResponse = Ok<'/health/ready'>
export type LivenessResponse = Ok<'/health'>

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: () => request<LivenessResponse>('/health'),
    refetchInterval: 15_000,
  })
}

export function useReadiness() {
  return useQuery({
    queryKey: ['readiness'],
    queryFn: () => request<ReadinessResponse>('/health/ready'),
    refetchInterval: 15_000,
    // A 503 from readiness is information, not a failure to retry into.
    retry: false,
  })
}

/**
 * A rejected readiness poll still carries a readiness document.
 *
 * `/health/ready` is one of exactly two routes exempt from Usher's RFC 9457
 * envelope, and the reason is visible here: its real consumers gate on the
 * status code and never parse the body, so the 503 keeps the *same*
 * `ReadinessResponse` shape and reports which check failed. `client.ts` turns
 * any non-2xx into an `UsherProblem` carrying the parsed body, so a 503 is a
 * degraded render rather than a page that disappears.
 *
 * A 503 whose body did *not* parse as a readiness document is a genuine
 * failure and belongs in the error treatment; that is the `null` return.
 */
export function readinessFromError(error: unknown): ReadinessResponse | null {
  if (!(error instanceof UsherProblem)) return null
  const body = error.body
  if (body === null || typeof body !== 'object') return null
  const checks: unknown = Reflect.get(body, 'checks')
  const lanes: unknown = Reflect.get(body, 'lanes')
  const status: unknown = Reflect.get(body, 'status')
  if (checks === null || typeof checks !== 'object') return null
  if (lanes === null || typeof lanes !== 'object') return null
  if (typeof status !== 'string') return null
  return {
    status,
    checks: {
      database: Reflect.get(checks, 'database') === true,
      migrations: Reflect.get(checks, 'migrations') === true,
    },
    lanes: {
      push: readStringArray(Reflect.get(lanes, 'push')),
      worker: Reflect.get(lanes, 'worker') === true,
    },
  }
}

function readStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v: unknown): v is string => typeof v === 'string') : []
}

export type AttributionResponse = Ok<'/meta/attribution'>

/**
 * A licensing requirement rather than a credit roll: IMDb and TMDb both
 * require their strings to be shown, and Usher ships importers rather than
 * data precisely so that stays true.
 */
export function useAttribution() {
  return useQuery({
    queryKey: ['attribution'],
    queryFn: () => request<AttributionResponse>('/meta/attribution'),
    staleTime: Infinity,
  })
}
