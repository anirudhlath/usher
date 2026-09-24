"""PRD 03 stage 4, application side: what gets embedded, and what gets ranked."""

import asyncio
import hashlib
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from loguru import logger
from opentelemetry import metrics
from pydantic import AwareDatetime

from usher.domain.ids import new_id
from usher.domain.search import SearchResult
from usher.domain.title import Title
from usher.ports.embedding import Embedder
from usher.ports.errors import UsherPortError
from usher.ports.repository import (
    MediaItemRepository,
    SearchQueryRecord,
    SearchQueryRepository,
    TasteRepository,
    TitleEmbeddingRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.search import (
    SearchFilters,
    SearchHit,
    SearchIndex,
    SearchMode,
    SearchRequest,
    SuggestIndex,
    SuggestTier,
)
from usher.services.query_expansion import QueryExpansionService

_meter = metrics.get_meter("usher.search")

# PRD 10's names, byte for byte, and neither is shortened or pluralised by analogy with
# anything.
_search_duration = _meter.create_histogram(
    "usher.search.duration", unit="s", description="Wall time per search, by mode"
)
# A histogram rather than a counter, and the label says why: the useful question is the
# *distribution* of result-set sizes per mode -- how often FUSED comes back empty, how
# often it saturates the limit -- not a running total nobody plots.
_search_results = _meter.create_histogram(
    "usher.search.results", unit="1", description="Results returned per search, by mode"
)

# **The two-tier suggest boundary is a bucket problem before it is a naming problem**,
# and this is the only histogram in the project that says so.
_SUGGEST_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

# PRD 10's names for the type-ahead path, byte for byte and on the same terms as the
# pair above.
_suggest_duration = _meter.create_histogram(
    "usher.suggest.duration",
    unit="s",
    description="Wall time per answered keystroke, by suggest tier",
    explicit_bucket_boundaries_advisory=_SUGGEST_BUCKETS,
)
# **Results and not hits**, which is a distinction this path has and `search`
# does not: `suggest` drops a hit whose title `list_by_ids` did not return, so
# the two numbers differ exactly when the index and the catalog disagree. The
# hydrated count is the one a client painted, so it is the one plotted.
_suggest_results = _meter.create_histogram(
    "usher.suggest.results", unit="1", description="Results returned per keystroke, by tier"
)

# The two separators, named because they are load-bearing rather than
# cosmetic. `_SECTION` is `_FINGERPRINT_SQL`'s `CHR(10)` and `_ITEM` is
# `usher_array_text`'s `array_to_string($1, ' ')`. A change to either is a
# change to every fingerprint in the catalog.
_SECTION = "\n"
_ITEM = " "


@dataclass(frozen=True, slots=True)
class EmbeddingDocument:
    """One title as an embedder sees it, plus the hash of exactly that.

    Deliberately not `ports.search.SearchDocument`, which is a retrieval
    document with weight classes aimed at `index_many`. Sharing a type would
    invite the fingerprint being computed over the weighted form, which is the
    one way to get this wrong that nothing downstream can detect.

    `is_degenerate` is a flag on a fully-formed document, never an absence. A
    refused title still gets a `title_embeddings` row carrying this
    `fingerprint` and a `NULL` embedding, so it stops matching the stale
    predicate and starts matching the `embedding IS NULL` one a diagnostic
    counts. Returning `None` here would leave the caller nothing to write and
    the title re-claimed by every backfill pass forever.
    """

    text: str
    fingerprint: str
    is_degenerate: bool


def compose_document(title: Title, *, credits: Sequence[str] = ()) -> EmbeddingDocument:
    """The text this title embeds as, and the `md5` of that text."""
    text = _SECTION.join(
        (
            title.name,
            title.original_name or "",
            _ITEM.join(credits),
            title.overview or "",
            title.tagline or "",
            _ITEM.join(title.genres),
            _ITEM.join(title.keywords),
        )
    )
    return EmbeddingDocument(
        text=text,
        # `usedforsecurity=False` is required, not decorative: ruff's `S` rules flag
        # `hashlib.md5` as S324, and the flag is the honest statement -- this is a
        # content hash for change detection and nothing about it is a security boundary.
        fingerprint=hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest(),
        is_degenerate=not text.strip(),
    )


#: One `SearchQueryRepository` per batch, committed on a clean exit. A callable
#: returning a context manager rather than a session factory, so this module
#: still imports no SQLAlchemy.
SearchQueryBatch = Callable[[], AbstractAsyncContextManager[SearchQueryRepository]]


class SearchQueryBuffer:
    """Keystroke rows, taken off the path that produced them.

    `submit` appends and returns; `drain` -- one task per process, started by
    the composition root -- writes what has accumulated, one transaction per
    batch. What a request pays is an append, which is what lets
    `USHER_SEARCH_SUGGEST_ANALYTICS` ship on.

    **Bounded twice, by rows and by the characters they carry, and a full
    buffer drops rather than blocks.** Back-pressure would let an analytics row
    slow down the answer it is about, which is the property this class exists
    to remove; `q` has no maximum length, so a row bound alone would let one
    caller hold arbitrary memory per keystroke.

    **`flush` is a barrier**: it returns with nothing pending and nothing in
    flight, which is what `aclose` and a reader of the table both need.

    Nothing here escapes: a refused row loses that row and an unwritable batch
    loses that batch, both at `ERROR`, because a drain that raises stops
    recording silently.
    """

    def __init__(
        self,
        batches: SearchQueryBatch,
        *,
        capacity: int = 1024,
        batch: int = 64,
        budget: int = 64 * 1024,
    ) -> None:
        self._batches = batches
        self._capacity = capacity
        self._batch = batch
        self._budget = budget
        self._pending: deque[SearchQueryRecord] = deque()
        self._held = 0
        self._dropped = 0
        self._stopped = False
        self._submitted = asyncio.Event()
        # Held across the pop as well as the write, which is what makes `flush`
        # a barrier rather than a drain of whatever happens to be left.
        self._writing = asyncio.Lock()

    def submit(self, record: SearchQueryRecord) -> bool:
        """Take the row, or refuse it because the buffer is full.

        Never awaits and never raises, so a caller can treat recording as free.
        """
        if len(self._pending) >= self._capacity or self._held + len(record.query) > self._budget:
            # Reported once per run of drops rather than once per keystroke:
            # the shape this defends against is a database that is down, and on
            # this route that is a log line per character typed. `flush` reports
            # the count when the buffer next empties.
            self._dropped += 1
            if self._dropped == 1:
                logger.error(
                    "the {surface} analytics buffer is full; keystrokes are unrecorded",
                    surface=record.surface.value,
                )
            return False
        self._pending.append(record)
        self._held += len(record.query)
        self._submitted.set()
        return True

    async def drain(self) -> None:
        """Write submitted rows until `aclose` stops it -- the root's task."""
        while not self._stopped:
            await self._submitted.wait()
            self._submitted.clear()
            await self.flush()

    async def flush(self) -> None:
        """Write everything submitted, and wait for a batch already in flight.

        A barrier on both counts. A caller that only drained the deque would
        return while the drain task held a batch of its own -- which is a
        shutdown that cancels an INSERT, and a reader that races one.
        """
        async with self._writing:
            while self._pending:
                batch = [
                    self._pending.popleft() for _ in range(min(self._batch, len(self._pending)))
                ]
                self._held -= sum(len(record.query) for record in batch)
                await self._write(batch)
            if self._dropped:
                logger.error(
                    "{count} analytics rows were dropped while the buffer was full",
                    count=self._dropped,
                )
                self._dropped = 0

    async def aclose(self) -> None:
        """Stop the drain and write what is left.

        Told to stop rather than cancelled: `CancelledError` is not an
        `Exception`, so a cancel landing inside the write escapes the guard
        below it and rolls that batch back.
        """
        self._stopped = True
        self._submitted.set()
        await self.flush()

    async def _write(self, records: Sequence[SearchQueryRecord]) -> None:
        try:
            async with self._batches() as queries:
                for record in records:
                    try:
                        await queries.record(record)
                    except UsherPortError as exc:
                        # One refused row loses one row, `_write_row`'s rule:
                        # the repository refuses inside a SAVEPOINT, so the
                        # rest of the batch still commits.
                        logger.error(
                            "the {surface} analytics row was refused: {error}",
                            surface=record.surface.value,
                            error=str(exc) or type(exc).__name__,
                        )
        except Exception as exc:
            # Wider than anywhere else in this module and deliberately so: this
            # runs in a task nobody awaits, so an escaping exception stops the
            # writer for the life of the process with nothing on the wire.
            logger.error(
                "{count} analytics rows were lost: {error}",
                count=len(records),
                error=str(exc) or type(exc).__name__,
            )


@dataclass(frozen=True, slots=True)
class SearchAnalytics:
    """`search_queries`' write side: the repository, and the commit for it."""

    queries: SearchQueryRepository
    commit: Callable[[], Awaitable[None]]
    #: Where a keystroke row goes on a root that has a drain for it. `None` on
    #: one that does not -- `usher suggest` is a command typed once, and an
    #: `INSERT` it waits for is a cost nobody is paying per character.
    buffer: SearchQueryBuffer | None = None


class SemanticSearchUnavailable(Exception):
    """This deployment has no `Embedder`, so a semantic query cannot be served.

    Deliberately not a `UsherPortError`: that family means "every error a port
    implementation may raise", and nothing failed here -- the deployment is
    configured without a model and said so once, at startup. Filed there it
    would land in the `except UsherPortError` arms that mean "an upstream is
    broken", where the response is a retry and no retry can help. And not a
    `ValueError`, which a caller wrapping a search would catch alongside every
    argument failure in the call.
    """


# `k / (k + rank)`, and k is 1 rather than `search_rrf_k`'s 60.
_RELEVANCE_K = 1.0

# Popularity squashed to [0, 1) by `p / (p + midpoint)`: bounded, monotone, and --
# unlike a min-max over the candidate set -- independent of which other rows came back.
_POPULARITY_MIDPOINT = 10.0

# Age squashed to (0, 1] by `1 / (1 + age / midpoint)` -- `_popularity_term`'s shape
# exactly, and for its reasons: bounded, monotone, and independent of which other rows
# came back, so a wrong constant moves a score by at most its weight rather than
# reshuffling a list.
_RECENCY_MIDPOINT_YEARS = 25.0

# Julian years, so a leap year is not a discontinuity in an age. The term is
# monotone in days and nothing downstream reads the age itself, so the third
# decimal place of this divisor cannot reach a result.
_DAYS_IN_YEAR = 365.25

# PRD 05's six ranking terms, all of them.
_WEIGHTS: dict[str, float] = {
    "relevance": 0.70,
    "popularity": 0.15,
    "owned": 0.15,
    "played": 0.02,
    "recency": 0.02,
    "taste": 0.005,
}


@dataclass(frozen=True, slots=True)
class SearchAnswer:
    """Ranked results, plus what actually ran."""

    results: tuple[SearchResult, ...] = ()
    requested_mode: SearchMode = SearchMode.FULL_TEXT
    mode: SearchMode = SearchMode.FULL_TEXT
    semantic_coverage: float = 0.0
    expanded_query: str | None = None
    search_id: uuid.UUID | None = None

    @property
    def degraded(self) -> bool:
        """The request was served in a narrower mode than it asked for."""
        return self.mode is not self.requested_mode


class SearchService:
    """PRD 05's two stages, in order: retrieve, then rank."""

    def __init__(
        self,
        index: SearchIndex,
        prefix_suggestions: SuggestIndex,
        fuzzy_suggestions: SuggestIndex,
        titles: TitleRepository,
        media_items: MediaItemRepository,
        watch_states: WatchStateRepository,
        taste: TasteRepository,
        embeddings: TitleEmbeddingRepository,
        *,
        result_limit: int,
        embedder: Embedder | None = None,
        expander: QueryExpansionService | None = None,
        analytics: SearchAnalytics | None = None,
        suggest_analytics: bool = True,
        now: Callable[[], AwareDatetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._index = index
        # Two `SuggestIndex` implementations, both required. The argument
        # `embedder` and `expander` make one parameter over -- a capability an
        # operator may not have installed -- does not transfer: both indexes are
        # btree/GIN reads over tables `m09a` creates unconditionally, so "built or
        # not built" has no state left to express.
        self._tiers: dict[SuggestTier, SuggestIndex] = {
            SuggestTier.PREFIX: prefix_suggestions,
            SuggestTier.FUZZY: fuzzy_suggestions,
        }
        self._titles = titles
        self._media_items = media_items
        # Not optional, unlike the two collaborators below it: a deployment
        # without a household is not a state this project has -- PRD 01's
        # authentication seam is a singleton row, and both callers resolve one
        # before they search. What is optional is the *argument* to `search`,
        # because a caller may legitimately have no household to speak for.
        self._watch_states = watch_states
        # Not an `Embedder` and not a `TasteService`, and both absences are the
        # point: the taste term needs a centroid, computing one is a job rather
        # than a request, so what reaches the blend is a centroid some *other*
        # process wrote -- `TasteRepository.latest`, one indexed single-row probe.
        self._taste = taste
        # Read only when a centroid was found, and scoped by the model that
        # wrote it. See `_rank`.
        self._embeddings = embeddings
        self._result_limit = result_limit
        # Injected for the reason every clock in `services/` is: the recency
        # term is a function of the instant it is scored at, and a term read
        # off the wall clock is one no case can pin an age against.
        self._now = now
        # The interval clock, a second callable rather than a second reading of
        # `now`: `_now` is a wall clock answering an `AwareDatetime` because
        # `search_queries.at` is a timestamp somebody will join against, while this
        # is a monotone counter whose epoch is unspecified.
        self._clock = clock
        # Optional, and a deployment without it still has search: full-text and
        # trigram are PRD 05's catalog-lookup tier and serve the whole catalog
        # with no model at all.
        self._embedder = embedder
        # Optional on the same terms and off by default twice: `USHER_LLM_ENABLED`
        # is `false`, so `composition.build_pipeline` is handed no client; and
        # `USHER_QUERY_EXPANSION_ENABLED` is `false` even when it is handed one.
        self._expander = expander
        # Optional, and the reason is a *caller* state rather than a deployment
        # state -- which is the difference from the two suggest indexes above.
        self._analytics = analytics
        # A second switch beside it, narrowing one surface rather than the
        # collaborator: `analytics=None` is a caller inside a unit of work it
        # does not own and turns off both writers, where this is an operator
        # turning off the keystroke one. Collapsing them would make "do not
        # record keystrokes" also mean "do not record searches".

        # It defaults the same way here as in `Settings`, because every shipped
        # construction goes through `composition.build_search_service` -- so a
        # disagreement is invisible until somebody builds a `SearchService` by
        # hand, which is what every unit case does.
        self._suggest_analytics = suggest_analytics

    @property
    def records_suggestions(self) -> bool:
        """Whether an answered keystroke produces a `search_queries` row.

        Published so a request boundary can decide whether to resolve a
        household: the row is the only thing on the suggest path that needs
        one, and resolving it is a `users` read per keystroke.
        """
        return self._analytics is not None and self._suggest_analytics

    async def search(
        self,
        query: str,
        *,
        mode: SearchMode = SearchMode.FULL_TEXT,
        limit: int = 20,
        # `SearchFilters()` as a default would be ruff B008 (a call in a
        # default) even though the value is frozen. The sentinel is the
        # spelling, not the reason.
        filters: SearchFilters | None = None,
        # A keyword here and deliberately not a `SearchFilters` field. PRD 05 says
        # `SearchFilters` is a closed vocabulary with no user field, and the practical
        # half of that is `usher search`'s `--filter`-shaped flags and `GET /search`'s
        # query string: a household reachable from a query string is a household any
        # caller can claim to be.
        user_id: uuid.UUID | None = None,
    ) -> SearchAnswer:
        """Retrieve, then rank.

        Raises `SemanticSearchUnavailable`.
        """
        requested = mode
        # Refused before the model, not after.
        if not query.strip():
            return SearchAnswer(requested_mode=requested, mode=requested)

        started = self._clock()
        # Resolved once and used twice -- by the coverage probe below and by the
        # request -- so the population the guard checked is the population the
        # search runs over. Two spellings of the same default would be two
        # populations the day either grew a branch.
        applied = filters or SearchFilters()
        vector: tuple[float, ...] | None = None
        expanded: str | None = None
        if mode is not SearchMode.FULL_TEXT:
            if self._embedder is None:
                if mode is SearchMode.SEMANTIC:
                    # Narrowing this to full-text is not narrowing. The caller
                    # asked the one question full-text cannot answer and would
                    # get a plausible answer to a different one.
                    raise SemanticSearchUnavailable(
                        "semantic search needs an embedding model; this deployment has none"
                    )
                # FUSED, on the other hand, still has a whole lane left, and
                # PRD 08 says a degraded subsystem narrows rather than fails.
                # The narrowing is carried in the answer, not hidden in it.
                mode = SearchMode.FULL_TEXT
            else:
                # One completion, immediately in front of the embed, and its position
                # is the cost argument: inside this `else` it is bought only by a
                # search that was going to embed something, so `full_text`, a
                # deployment with no model and a blank query all pay nothing.
                if (
                    self._expander is not None
                    and await self._index.semantic_coverage(applied) > 0.0
                ):
                    expanded = await self._expander.expand(query)
                vector = tuple(
                    (await self._embedder.embed([query if expanded is None else expanded]))[0]
                )

        outcome = await self._index.search(
            SearchRequest(
                # The typed words, never the rewrite: only the vector is computed
                # from an expansion, so under RRF the lexical lane goes on matching what
                # the viewer actually wrote while the semantic lane matches the
                # paraphrase.
                query=query,
                # A ceiling, not a default: every candidate becomes a hydrated
                # row in application code, so an unclamped limit is a scan.
                limit=min(limit, self._result_limit),
                mode=mode,
                filters=applied,
                query_vector=vector,
            )
        )
        answer = SearchAnswer(
            results=await self._rank(outcome.hits, user_id=user_id),
            requested_mode=requested,
            mode=mode,
            # Passed through, never recomputed. It is the fraction of the
            # *filtered population* that had a vector; derived from the hits it
            # would read 1.0 whenever every returned hit had one.
            semantic_coverage=outcome.semantic_coverage,
            # Reported, never silently substituted. This is the string that
            # was embedded whenever it is not `None`, so a caller can print it
            # beside the results; `usher search` does. A field that echoed the
            # typed query when nothing was expanded would put a line on every
            # search of every deployment and mean nothing.
            expanded_query=expanded,
        )
        # After the rank, not around the retrieval alone: PRD 05 splits the two stages
        # and an operator asking "why is search slow" is asking about the answer, not
        # about half of it.
        elapsed = self._clock() - started
        labels = {"mode": mode.value}
        _search_duration.record(elapsed, labels)
        _search_results.record(len(answer.results), labels)
        # Outside the window, deliberately: an INSERT inside it would be counted as
        # search latency by both the histogram and the row itself.
        return replace(
            answer,
            search_id=await self._record_search(
                query, mode=mode, user_id=user_id, results=len(answer.results), elapsed=elapsed
            ),
        )

    async def _record_search(
        self,
        query: str,
        *,
        mode: SearchMode,
        user_id: uuid.UUID | None,
        results: int,
        elapsed: float,
    ) -> uuid.UUID | None:
        """One `search_queries` row for one answered search, and its commit.

        Answers the row's own id, or `None` when no row was written -- the value
        `SearchAnswer.search_id` carries and therefore what `GET /search` echoes.
        """
        analytics = self._analytics
        if analytics is None or user_id is None:
            return None
        return await _write_row(
            analytics,
            SearchQueryRecord(
                id=new_id(),
                at=self._now(),
                user_id=user_id,
                query=query,
                mode=mode,
                result_count=results,
                latency_ms=_ms(elapsed),
            ),
        )

    async def _record_suggest(
        self,
        prefix: str,
        *,
        tier: SuggestTier,
        user_id: uuid.UUID | None,
        results: int,
        elapsed: float,
    ) -> None:
        """One `search_queries` row for one answered keystroke."""
        analytics = self._analytics
        if analytics is None or user_id is None or not self._suggest_analytics:
            return
        record = SearchQueryRecord(
            id=new_id(),
            at=self._now(),
            user_id=user_id,
            query=prefix,
            mode=SearchMode.FULL_TEXT,
            result_count=results,
            latency_ms=_ms(elapsed),
            tier=tier,
        )
        # Handed over rather than written where a root has a drain: the
        # keystroke is answered and there is no id to publish, so nothing here
        # has any reason to wait for the row. `_record_search` does wait,
        # because `SearchAnswer.search_id` is only honest once the row exists.
        if analytics.buffer is not None:
            analytics.buffer.submit(record)
            return
        await _write_row(analytics, record)

    async def suggest(
        self,
        prefix: str,
        limit: int = 10,
        *,
        tier: SuggestTier,
        # `None`-able and mirroring `search`'s, and the default is the decision: a
        # household is not a thing this path *uses* -- there is no blend here -- it
        # is a thing the row *needs*, because `search_queries.user_id` is
        # `NOT NULL` behind `ON DELETE RESTRICT`.
        user_id: uuid.UUID | None = None,
    ) -> tuple[SearchResult, ...]:
        """Type-ahead candidates from one tier, hydrated and not re-ranked."""
        if not prefix.strip():
            return ()
        started = self._clock()
        hits = await self._tiers[tier].suggest(prefix, limit=min(limit, self._result_limit))
        by_id = {
            title.id: title
            for title in await self._titles.list_by_ids([hit.title_id for hit in hits])
        }
        owned = await self._media_items.owned_title_ids(list(by_id))
        results = tuple(
            _result(by_id[hit.title_id], owned=hit.title_id in owned, score=hit.score)
            for hit in hits
            if hit.title_id in by_id
        )
        elapsed = self._clock() - started
        # The timed interval is the whole method and not the tier call, because
        # hydration is what a client waits for and it is the same two reads for both
        # tiers by construction -- so a window around the tier call alone would
        # report the half of the cost that does not differ between the two things
        # the label distinguishes.
        _suggest_duration.record(elapsed, {"tier": tier.value})
        _suggest_results.record(len(results), {"tier": tier.value})
        await self._record_suggest(
            prefix,
            # Passed through, never re-derived. The row has to name the index
            # that ran, and the only thing that knows which one that was is the
            # argument that selected it out of `self._tiers`.
            tier=tier,
            user_id=user_id,
            results=len(results),
            elapsed=elapsed,
        )
        return results

    async def _rank(
        self, hits: Sequence[SearchHit], *, user_id: uuid.UUID | None
    ) -> tuple[SearchResult, ...]:
        """PRD 05 stage 2, over one already-retrieved candidate set.

        Three reads with a household and two without, regardless of hit count --
        which is the whole reason `list_by_ids`, `owned_title_ids` and
        `played_title_ids` exist in the batch shape they do. A per-hit spelling
        answers identically and costs a statement a hit.

        `played_title_ids` rolls a watched episode up to its series through
        `COALESCE(ws.title_id, e.title_id)`, which is what keeps this from
        being a films-only answer for a television household -- the roll-up is
        the port's, and re-deriving it here would be a second definition of
        "seen".
        """
        if not hits:
            return ()
        titles = {
            title.id: title
            for title in await self._titles.list_by_ids([hit.title_id for hit in hits])
        }
        owned = await self._media_items.owned_title_ids(list(titles))
        # Not read at all without a household, rather than read with a
        # placeholder: `played_title_ids` is scoped by `user_id`, so an invented
        # one is a statement per search answering about a user nobody is.
        played = (
            frozenset[uuid.UUID]()
            if user_id is None
            else await self._watch_states.played_title_ids(user_id, list(titles))
        )
        stored = None if user_id is None else await self._taste.latest(user_id)
        centroid = None if stored is None else stored.centroid
        vectors: dict[uuid.UUID, tuple[float, ...]] = (
            {}
            if stored is None or centroid is None
            else await self._embeddings.list_for_titles(list(titles), model_name=stored.model_name)
        )
        today = self._now().date()
        ranks = _dense_ranks(hits)
        results = [
            _result(
                titles[hit.title_id],
                owned=hit.title_id in owned,
                score=_blend(
                    relevance=_RELEVANCE_K / (_RELEVANCE_K + rank),
                    popularity=_popularity_term(titles[hit.title_id].tmdb_popularity),
                    owned=1.0 if hit.title_id in owned else 0.0,
                    # PRD 05: a small boost, never a demotion. A search is
                    # overwhelmingly a re-find intent, so demoting what the household
                    # has finished buries the exact film they just named.
                    played=None if user_id is None else (1.0 if hit.title_id in played else 0.0),
                    recency=_recency_term(titles[hit.title_id], today=today),
                    # `None` rather than 0.0 in both absent cases.
                    taste=_taste_term(centroid, vectors.get(hit.title_id)),
                ),
            )
            for hit, rank in zip(hits, ranks, strict=True)
            # Dropped, not raised: a title deleted between the index write and
            # this read is ordinary, and `titles[hit.title_id]` is a KeyError
            # -- a 500 on a search because one row went away.
            if hit.title_id in titles
        ]
        # Ties broken by id. Falling back to the index's own order is not an order
        # -- `UPDATE ... RETURNING` hands rows back in heap order on a small table
        # -- and a search that reorders equal-scoring rows between two identical
        # calls cannot be paginated.
        results.sort(key=lambda result: (-result.score, result.title_id))
        return tuple(results)


def _dense_ranks(hits: Sequence[SearchHit]) -> list[int]:
    """Positions: equal index scores share one, an exact name match sits alone."""
    ranks: list[int] = []
    rank = 0
    previous: tuple[bool, float] | None = None
    for hit in hits:
        key = (hit.exact_name, hit.score)
        if previous is not None and key != previous:
            rank += 1
        ranks.append(rank)
        previous = key
    return ranks


def _popularity_term(popularity: float | None) -> float | None:
    """`p / (p + midpoint)`, or `None` when the catalog carries no popularity.

    `None` is not 0.0. `titles.tmdb_popularity` is null for every title TMDb's
    daily export has never described -- all of a `--phase imdb` catalog and most
    of a `--phase all` one -- and `popularity or 0.0` would rank an unknown title
    identically to an unpopular one, burying the whole un-enriched catalog
    beneath the enriched tier while looking like arithmetic and raising nothing.
    `_blend` drops an absent signal from numerator and denominator, so each title
    is scored on what is known about it.
    """
    if popularity is None:
        return None
    return popularity / (popularity + _POPULARITY_MIDPOINT)


def _recency_term(title: Title, *, today: date) -> float | None:
    """`1 / (1 + age / midpoint)`, or `None` when the title carries no date."""
    released = title.release_date or (None if title.year is None else date(title.year, 1, 1))
    if released is None:
        return None
    age_years = max((today - released).days, 0) / _DAYS_IN_YEAR
    return 1.0 / (1.0 + age_years / _RECENCY_MIDPOINT_YEARS)


def _taste_term(
    centroid: tuple[float, ...] | None, vector: tuple[float, ...] | None
) -> float | None:
    """`max(0, cos)` held inside `[0, 1]`, or `None` when there is nothing to compare."""
    if centroid is None or vector is None or len(vector) != len(centroid):
        return None
    dot = sum(one * other for one, other in zip(centroid, vector, strict=True))
    norms = math.sqrt(sum(value * value for value in centroid)) * math.sqrt(
        sum(value * value for value in vector)
    )
    if norms == 0.0:
        # Unreachable through `list_for_titles`, which never hands back a zero
        # vector, and guarded for `_normalise`'s reason one module over: a
        # `ZeroDivisionError` in a ranking function is the kind of thing that
        # becomes reachable the day somebody relaxes a refusal, and it would
        # arrive as a 500 on a search.
        return None
    return min(1.0, max(0.0, dot / norms))


def _blend(**signals: float | None) -> float:
    """A weighted mean over the signals that are actually present.

    An absent signal leaves both the numerator and the denominator, so a title
    with no popularity is scored on what is known about it rather than penalised
    for what is not. At equal relevance, unknown popularity ranks above a stored
    zero, and an undated title above one carrying an old year.

    Written as a sum over an explicit signal list -- the same skeleton
    `SimilarityService` uses -- so landing a term is adding a term and a weight
    in both places rather than rewriting two scorers.

    It is scale-invariant, which is what lets new terms arrive without moving old
    scores: multiplying every weight by the same factor changes nothing, and
    adding a term changes only the rows where that term is *present*.
    """
    total = 0.0
    applied = 0.0
    for name, value in signals.items():
        if value is None:
            continue
        total += _WEIGHTS[name] * value
        applied += _WEIGHTS[name]
    return total / applied if applied else 0.0


def _ms(seconds: float) -> int:
    """`search_queries.latency_ms`, which the column requires to be `>= 0`.

    `time.perf_counter` is non-decreasing by contract, so a negative delta is
    unreachable with the shipped clock and only an injected one can produce it.
    Without the clamp a backwards clock is a `RepositoryConflict` on the path
    that has just answered a search correctly.
    """
    return max(0, int(seconds * 1000))


async def _write_row(analytics: SearchAnalytics, record: SearchQueryRecord) -> uuid.UUID | None:
    """Write one `search_queries` row and make it durable, or say it did not."""
    try:
        await analytics.queries.record(record)
        await analytics.commit()
    except UsherPortError as exc:
        logger.error(
            "the {surface} analytics row was refused; this request is unrecorded: {error}",
            surface=record.surface.value,
            error=str(exc) or type(exc).__name__,
        )
        return None
    return record.id


def _result(title: Title, *, owned: bool, score: float) -> SearchResult:
    return SearchResult(
        title_id=title.id,
        kind=title.kind,
        name=title.name,
        year=title.year,
        popularity=title.tmdb_popularity,
        owned=owned,
        score=score,
    )


__all__ = [
    "EmbeddingDocument",
    "SearchAnalytics",
    "SearchAnswer",
    "SearchQueryBatch",
    "SearchQueryBuffer",
    "SearchService",
    "SemanticSearchUnavailable",
    "compose_document",
]
