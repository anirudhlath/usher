"""PRD 03 stage 3: one canonical title, filled in from a metadata provider.

**The tier and the failure are orthogonal, and that is ADR-0008 rather than
a preference.** `EnrichmentState` has no `FAILED` member: the three rungs are
`skeleton | stub | enriched` and a failed attempt records
`Title.enrichment_error` while leaving the tier exactly where it was. A
skeleton whose enrichment failed is still a perfectly usable skeleton — its
genres, rating and runtime did not stop being true — and the next attempt
needs to know which rung it is working from.

**Every tier comparison goes through `ENRICHMENT_RANK`.** `EnrichmentState`
is a `StrEnum`, so members compare lexicographically:
`EnrichmentState.ENRICHED > EnrichmentState.SKELETON` is `False`, and so is
`ENRICHED > STUB`. A guard spelled as a direct comparison therefore does not
"sometimes downgrade" — it never promotes anything at all, silently, which is
the shape of bug the rank map exists to make unspellable.

**Fields the provider supplied win; fields it did not are left alone.** TMDb
serves plenty of entities carrying little more than an id, and a merge that
wrote every field would blank what a source already knew on the one title
least able to spare it. `field_provenance` accumulates rather than being
replaced, so an id an earlier provider supplied keeps its attribution
(PRD 02: "so a second metadata provider can be added later without
ambiguity").

**The payload is cached before it is used, and read before the provider is
asked.** ADR-0016: `raw_payloads` holds provider responses so M7 and M9 can
derive `Person`/`Credit`/`Collection`/`Image` with no second network call.
The freshness window is a *ceiling* under TMDb's six-month caching term, not
a target — refetching every title on every attempt is how a retry storm
becomes a rate limit.

`commit` is injected because `services/` may depend only on `domain/` and
`ports/` (ADR-0009), and a session is neither.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from opentelemetry import metrics, trace
from pydantic import AwareDatetime

from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState
from usher.domain.episode import Episode
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.errors import PortDataMalformed, UsherPortError
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.ingest import ProviderRef
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.metadata import EnrichmentResult, MetadataProvider
from usher.ports.repository import EpisodeRepository, RawPayloadStore, TitleRepository

_tracer = trace.get_tracer("usher.enrich")
_meter = metrics.get_meter("usher.enrich")
# PRD 10's name, not a shortened one: its dashboard 3 panel ("enrichment
# throughput and p50/p99") and its "enrichment SLA missed" alert both query
# `usher.enrichment.latency`, and a metric emitted under a near-miss name is
# a permanently empty panel that nothing distinguishes from a healthy zero.
# Labelled `outcome` rather than PRD 10's original `trigger`: nothing in M4
# enriches on demand (`JobPriority.DEMAND` is defined and unused until M5),
# so a `trigger` label would carry one constant value, while a failure's
# latency and a success's are genuinely different populations. PRD 10 is
# corrected rather than approximated.
_enrich_duration = _meter.create_histogram(
    "usher.enrichment.latency", unit="s", description="Wall time per title enrichment"
)
_enriched = _meter.create_counter(
    "usher.enrich.result", unit="1", description="Enrichment attempts, by outcome"
)

# Which `Title` column a provider addresses a title by, and whether that id
# space is namespaced by kind. The same three-row table
# `usher.services.matching._PROVIDER_TIERS` holds, restated rather than
# imported: there it is a match ladder ordered by confidence, here it is a
# lookup keyed by provider name, and collapsing the two would make one of
# them read as the other's leftovers.
_PROVIDER_ID_FIELDS: dict[str, tuple[str, bool]] = {
    "tmdb": ("tmdb_id", True),
    "imdb": ("imdb_id", False),
    "tvdb": ("tvdb_id", False),
}

# What a provider is allowed to overwrite. Deliberately enumerated rather
# than derived from the result's own `field_provenance`: driving the merge
# off the provider's bookkeeping means a mapper that forgot one provenance
# entry silently stops merging that field, and nothing would ever say so.
#
# Absent, each for its own reason: `id` and `kind` are identity and the id
# space the fetch was made in; `collection_id` is a FK to a table M7 creates;
# `enrichment_state`/`enrichment_error`/`enriched_at` are this service's;
# `field_provenance` is merged rather than assigned; `created_at`/`updated_at`
# belong to the store.
_ENRICHABLE: tuple[str, ...] = (
    "tmdb_id",
    "imdb_id",
    "tvdb_id",
    "name",
    "original_name",
    "sort_name",
    "year",
    "release_date",
    "end_year",
    "overview",
    "tagline",
    "runtime_minutes",
    "status",
    "genres",
    "keywords",
    "original_language",
    "spoken_languages",
    "origin_countries",
    "content_rating",
    "tmdb_vote_average",
    "tmdb_vote_count",
    "tmdb_popularity",
)

# The `kind` half of a `raw_payloads` key when a provider's id space is not
# namespaced (IMDb's `tt` ids are one global namespace). A non-empty string
# rather than `""`: the column is `NOT NULL` and a key nobody can read is
# worse than a slightly odd word.
_GLOBAL_ID_SPACE = "global"


class EnrichService:
    def __init__(
        self,
        titles: TitleRepository,
        episodes: EpisodeRepository,
        payloads: RawPayloadStore,
        provider: MetadataProvider,
        commit: Callable[[], Awaitable[None]],
        events: EventPublisher,
        *,
        queue: JobQueue,
        cache_max_age_days: int = 30,
        now: Callable[[], AwareDatetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._titles = titles
        self._episodes = episodes
        self._payloads = payloads
        self._provider = provider
        self._commit = commit
        # Required, never a default. A shared `NullEventPublisher()` sitting
        # in a signature is a mutable-looking default that happens to be
        # stateless today, and every other collaborator here is required --
        # the two composition roots supply `NullEventPublisher()` where they
        # mean it, and say why.
        self._events = events
        # Required and keyword-only, for the same reason `events` is: a
        # defaulted queue in this signature is how one composition root ends
        # up enqueueing index work into an object nothing ever claims from,
        # while the other works. It must be the *same* queue `MatchService`
        # and `IngestService` hold, which only a composition root can know.
        self._queue = queue
        self._max_age = timedelta(days=cache_max_age_days)
        # Injected, the way `EmbySession`, `TmdbClient` and `TMDbIdDataset`
        # all inject theirs: the cache-expiry branch is otherwise only
        # reachable by waiting a month, and `enriched_at` is a column an
        # operator reads.
        self._now = now

    async def enrich(self, title_id: uuid.UUID) -> Title:
        """Fill one title in from the provider. Raises `UsherPortError`.

        Deliberately re-raises rather than absorbing: `JobWorker` is the only
        thing that knows `PortDataMalformed` parks immediately and every other
        port error backs off, and it learns which by catching the exception.
        Absorbing it here would complete the job and lose the work silently.
        """
        started = time.perf_counter()
        outcome = "failed"
        with _tracer.start_as_current_span("enrich.title") as span:
            span.set_attribute("usher.title_id", str(title_id))
            title = await self._titles.get(title_id)
            if title is None:
                # No error row to write -- there is no row. Malformed rather
                # than not-found so the job parks instead of retrying a key
                # that names a deleted title five more times.
                raise PortDataMalformed("no such title to enrich", detail=str(title_id))
            try:
                enriched = await self._apply(title)
            except UsherPortError as exc:
                await self._record_failure(title, exc)
                span.set_attribute("usher.failed", True)
                _enriched.add(1, {"outcome": "failed"})
                _enrich_duration.record(time.perf_counter() - started, {"outcome": "failed"})
                raise
            outcome = "enriched"
            span.set_attribute("usher.enrichment_state", enriched.enrichment_state.value)
        _enriched.add(1, {"outcome": outcome})
        _enrich_duration.record(time.perf_counter() - started, {"outcome": outcome})
        return enriched

    async def _apply(self, title: Title) -> Title:
        ref = self._ref_for(title)
        payload = await self._payload_for(ref)
        result = self._provider.to_result(payload, title.id)
        changed = self._changes(result)
        enriched = self._merged(title, result, changed)
        await self._titles.update(enriched)
        await self._store_hierarchy(result)
        await self._commit()
        # PRD 03's fourth stage, enqueued from the third. **After the commit**,
        # and that ordering is the one here with a wrong answer and no error
        # attached: a worker claiming this job reads `titles` in a different
        # transaction, so a job enqueued before the commit can run against the
        # pre-enrichment row, fingerprint the old text, store a vector of the
        # old text -- and then *stop matching the stale predicate*, because the
        # fingerprint agrees with what it embedded. A permanently stale vector
        # the backfill will never re-claim, produced by the enqueue that exists
        # to keep it fresh.
        #
        # Success path only. A failure leaves the tier where it was (ADR-0008)
        # and changes no text, so the fingerprint is unchanged and the job
        # would complete without embedding -- one claim per attempt of a
        # backoff schedule, for nothing.
        #
        # `BACKFILL`, the floor: nothing a client renders depends on a search
        # document. It is also the sweep's priority, which is what makes a
        # re-enqueue write zero rows (`_ENQUEUE`'s `WHERE jobs.priority <
        # excluded.priority`) rather than rewriting the row.
        #
        # Flushes, never commits (`JobQueue`'s contract), so this row lands in
        # the transaction `JobWorker` closes with `complete(job.id)`: "this
        # enrich job is done" and "an index job exists" commit together.
        #
        # `enriched.id`, not `title.id`. `_merged` preserves the id today, so
        # the two are the same object and the mutation between them survives
        # every test -- it is written this way so a future `_merged` that
        # re-mints an id cannot silently index the wrong row.
        #
        # **A failure here propagates**, deliberately. After a successful
        # commit the session is healthy, so this is close to unreachable --
        # but catching and logging would be a silently lost index job, this
        # milestone's failure mode in miniature. The cost of propagating is
        # one `enrichment_error` on an already-enriched title (the tier is
        # untouched) that the next attempt clears, and the retry is cheap
        # because `_payload_for` reads the cache.
        #
        # **No second client event, and that is boundary call 5 rather than an
        # omission.** PRD 09 asks M6 to publish `title.updated` "through the
        # `EventPublisher` port M5 built rather than inventing a channel" --
        # and it is published immediately below, already. Nothing a client
        # renders depends on the search document or the embedding, so a
        # `title.indexed` would be an event with no consumer, which
        # `ports/events.py` names: "no member nothing emits". Do not add one.
        #
        # **One call, two requests, and that is not tidiness.** `enqueue` is a
        # staged write -- a temp DDL, a COPY and one `INSERT ... SELECT ... ON
        # CONFLICT` -- so a second `await self._queue.enqueue([...])` here is a
        # second full staging cycle per enriched title, on the path M6 already
        # had to fix once for exactly this shape of cost. Two requests in one
        # list is one cycle and one statement. Everything above applies to both
        # requests and is not restated.
        #
        # The two are deliberately not ordered against each other. `DERIVE`
        # writes `credit_names`, which is an input to `compose_document`, so a
        # title whose `INDEX` job is claimed first embeds without its cast and
        # is re-claimed once `DERIVE` moves its fingerprint. One wasted embed
        # per enriched title at ~115 tokens, and the only lever is a
        # `JobPriority` rung that does not exist between `BACKFILL` and `NEW`
        # -- promoting `DERIVE` to `NEW` would put it ahead of a `match` queue
        # that is hundreds of thousands of jobs deep on a first bootstrap.
        await self._queue.enqueue(
            [
                JobRequest(kind=JobKind.INDEX, key=str(enriched.id), priority=JobPriority.BACKFILL),
                JobRequest(
                    kind=JobKind.DERIVE, key=str(enriched.id), priority=JobPriority.BACKFILL
                ),
            ]
        )
        # PRD 03's read-through loop, closed: "Completion publishes a
        # `title.updated` event on a Server-Sent Events channel; clients patch
        # in place."
        #
        # **After the commit**, because a client patches by refetching the
        # fields named below and would otherwise read the row this
        # transaction has not written yet. On the success path only: a
        # failure records `enrichment_error` and leaves the tier exactly
        # where it was (ADR-0008), so "this changed" would send a client to
        # refetch an identical stub -- once per attempt of a backoff
        # schedule.
        await self._events.publish(
            ClientEvent(
                kind=ClientEventKind.TITLE_UPDATED,
                title_id=enriched.id,
                # The fields a client can patch without refetching the whole
                # title (PRD 07: "Title id + changed fields | Patch in
                # place"). `["*"]` turns "patch in place" back into
                # "refetch", one request later.
                data={"fields": [*sorted(changed), "enrichment_state"]},
            )
        )
        return enriched

    def _ref_for(self, title: Title) -> ProviderRef:
        """How this provider addresses this title, or `PortDataMalformed`.

        A title carrying no id this provider understands is not a transient
        failure: there is nothing to fetch and no amount of waiting changes
        that, so the job parks on its first attempt rather than spending five
        rate-limited ones discovering the same thing.
        """
        field, kind_scoped = _PROVIDER_ID_FIELDS.get(self._provider.name, ("", False))
        value = getattr(title, field, None) if field else None
        if value is None:
            raise PortDataMalformed(
                f"title carries no {self._provider.name} id to enrich from", detail=str(title.id)
            )
        return ProviderRef(
            provider=self._provider.name,
            value=str(value),
            # ADR-0011: a TMDb ref without a kind names two things. The
            # provider rejects one, and it is right to.
            kind=title.kind if kind_scoped else None,
        )

    async def _payload_for(self, ref: ProviderRef) -> dict[str, Any]:
        """The cached response if it is inside the freshness window, else a
        fresh fetch, cached on the way through.

        The window is a ceiling under TMDb's six-month caching term rather
        than a target. Both halves matter: never refetching leaves a catalog
        that cannot learn a film got a sequel, and always refetching turns a
        retry storm into a rate limit.
        """
        space = ref.kind.value if ref.kind is not None else _GLOBAL_ID_SPACE
        cached = await self._payloads.get(ref.provider, space, ref.value)
        if cached is not None and cached[1] >= self._now() - self._max_age:
            return cached[0]
        payload = await self._provider.fetch(ref)
        await self._payloads.put(ref.provider, space, ref.value, payload)
        return payload

    def _changes(self, result: EnrichmentResult) -> dict[str, Any]:
        """What the provider actually supplied, keyed by field.

        Hoisted out of `_merged` rather than recomputed, because it is also
        what `title.updated` names on the wire -- and two copies of "which
        fields moved" is how the event comes to disagree with the row.
        """
        changes: dict[str, Any] = {}
        for field in _ENRICHABLE:
            value = getattr(result.title, field)
            # `None` and `()` both mean "this response did not say", never
            # "blank it". `0` and `False` are positive claims and are kept --
            # the same distinction ADR-0014 draws one lane over.
            if value is None or value == ():
                continue
            changes[field] = value
        return changes

    def _merged(self, title: Title, result: EnrichmentResult, changes: dict[str, Any]) -> Title:
        """The stored title with what the provider supplied written over it.

        `.evolve()`, never `model_copy(update=)`: every `usher.domain` model
        is frozen and `model_copy` skips validation entirely, so it can hand
        back a `Title` carrying an out-of-range `community_rating` that
        pydantic then serialises without complaint.
        """
        return title.evolve(
            **changes,
            # `ENRICHMENT_RANK`, never a direct comparison. `ENRICHED` is the
            # top rung so `max` always chooses it today; spelling it as a max
            # over the rank is what keeps this correct when a fourth rung or a
            # second provider tier arrives.
            enrichment_state=max(
                (title.enrichment_state, EnrichmentState.ENRICHED),
                key=lambda state: ENRICHMENT_RANK[state],
            ),
            # Cleared on success. A stale error on an enriched title reads as
            # "this is broken" on every dashboard that renders it.
            enrichment_error=None,
            enriched_at=self._now(),
            field_provenance={**title.field_provenance, **result.title.field_provenance},
        )

    async def _store_hierarchy(self, result: EnrichmentResult) -> None:
        """Seasons then episodes, each in one statement, with the season ids
        **read back** rather than trusted.

        The read-back is not defensive. `to_result` mints a fresh UUIDv7 per
        `Season`, and a season the catalog already holds keeps the id it was
        inserted with — so an episode carrying the minted id names no row and
        fails on `fk_episodes_season_id_seasons`, on the *second* enrichment
        rather than the first. `IngestService._ensure_seasons` re-reads for
        exactly this reason; no port fake can see it (a dict has no foreign
        keys), which is why it is asserted directly.
        """
        if not result.seasons:
            # A movie. Two round trips per title against 94,438 of them, on a
            # catalog that is two thirds films, for nothing.
            return
        await self._episodes.upsert_seasons(result.seasons)
        season_ids = await self._episodes.resolve_seasons(
            [(one.title_id, one.season_number) for one in result.seasons]
        )
        rows: list[Episode] = []
        for episode in result.episodes:
            stored_id = season_ids.get((episode.title_id, episode.season_number))
            if stored_id is None:
                # The upsert wrote it and the resolve did not find it: not
                # reachable through either implementation. Left out rather
                # than asserted on, for the reason `IngestService` states --
                # a raise here costs the whole enrichment, and a missing
                # episode is recoverable on the next pass.
                logger.warning(
                    "season {number} of {title_id} was written but did not resolve",
                    number=episode.season_number,
                    title_id=episode.title_id,
                )
                continue
            rows.append(episode.evolve(season_id=stored_id))
        if rows:
            await self._episodes.upsert_episodes(rows)

    async def _record_failure(self, title: Title, exc: UsherPortError) -> None:
        """ADR-0008's whole point, in four lines: the error is recorded and
        the tier is untouched.

        Committed before the caller re-raises, because `JobWorker` parks the
        job on the exception and the reason has to be readable somewhere an
        operator looks -- PRD 02's enrichment dashboard reads
        `enrichment_error`, not the queue.
        """
        # `str(exc)`, never the exception object and never a payload: PRD 08's
        # credentials-are-never-logged rule applies to a column an operator
        # reads.
        await self._titles.update(title.evolve(enrichment_error=str(exc)))
        await self._commit()
        logger.warning(
            "enrichment of {title_id} failed, tier stays {state}: {error}",
            title_id=title.id,
            state=title.enrichment_state.value,
            error=str(exc),
        )
