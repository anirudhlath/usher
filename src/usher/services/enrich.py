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
from usher.domain.title import Title
from usher.ports.errors import PortDataMalformed, UsherPortError
from usher.ports.ingest import ProviderRef
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
    "community_rating",
    "vote_count",
    "popularity",
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
        *,
        cache_max_age_days: int = 30,
        now: Callable[[], AwareDatetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._titles = titles
        self._episodes = episodes
        self._payloads = payloads
        self._provider = provider
        self._commit = commit
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
        enriched = self._merged(title, result)
        await self._titles.update(enriched)
        await self._store_hierarchy(result)
        await self._commit()
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

    def _merged(self, title: Title, result: EnrichmentResult) -> Title:
        """The stored title with what the provider supplied written over it.

        `.evolve()`, never `model_copy(update=)`: every `usher.domain` model
        is frozen and `model_copy` skips validation entirely, so it can hand
        back a `Title` carrying an out-of-range `community_rating` that
        pydantic then serialises without complaint.
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
