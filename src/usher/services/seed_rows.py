"""Building a recently watched seed's "Because you watched" row from three lists."""

import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta

from loguru import logger
from opentelemetry import trace
from opentelemetry.trace import Span
from pydantic import AwareDatetime

from usher.domain.curation import LLMPurpose
from usher.domain.enums import EnrichmentState
from usher.domain.seed_rows import MIN_SEED_ROW_CARDS, SeedRow, SeedRowCard, SeedRowOutcome
from usher.domain.title import Title
from usher.ports.errors import PortDataMalformed, UsherPortError
from usher.ports.ingest import ProviderRef
from usher.ports.llm import LLMClient, LLMUsage
from usher.ports.metadata import MetadataProvider
from usher.ports.repository import (
    LLMCallRepository,
    NamedTitle,
    SeedRowRepository,
    TitleEmbeddingRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.scheduler import JobOutcome, ScheduledJob
from usher.services.llm_ledger import LLMLedger
from usher.services.rows.because_you_watched import SEED_WINDOW
from usher.services.seed_row_merge import merge_seed_row
from usher.services.seed_row_recall import (
    CLOSE_LIMIT,
    CLOSE_MIN_VOTES,
    RECALL_SCHEMA,
    RECALL_TEMPERATURE,
    parse_recall,
    recall_prompt,
    seed_row_recipe,
)

_tracer = trace.get_tracer("usher.seed_rows")

#: `SeedRowJob.name`. Stable, because it is a metric label and a span name.
SEED_ROWS_JOB_NAME = "seed_rows.build"

#: How long after one completed walk of the window the scheduler offers the next.
SEED_ROWS_PERIOD = timedelta(minutes=15)

_REBUILD_AFTER = timedelta(days=30)
_RETRY_AFTER = timedelta(days=7)


def is_due(row: SeedRow | None, *, recipe: str, now: AwareDatetime) -> bool:
    """Whether a seed's record asks for another build under `recipe` at `now`.

    A failed attempt decides alone: it is due once it is over seven days old or was made
    under another recipe, and until then it holds back a build that would otherwise be
    due, so a recipe change costs one attempt per seed rather than one per tick. Without
    one, a record is due with no cards, cards built by another recipe, or cards over
    thirty days old.
    """
    if row is None:
        return True
    if row.attempt_outcome is not SeedRowOutcome.BUILT:
        return row.attempt_recipe != recipe or now - row.attempted_at > _RETRY_AFTER
    return row.built_at is None or row.built_recipe != recipe or now - row.built_at > _REBUILD_AFTER


class SeedRowService:
    """One seed's row per `build`: one completion, one provider read, and one stored record.

    The only writer of `seed_rows`. The client is required, so a deployment with no model
    builds no service at all, as with `CurationService`.
    """

    def __init__(
        self,
        *,
        titles: TitleRepository,
        embeddings: TitleEmbeddingRepository,
        watch_states: WatchStateRepository,
        rows: SeedRowRepository,
        calls: LLMCallRepository,
        client: LLMClient,
        metadata: MetadataProvider | None,
        commit: Callable[[], Awaitable[None]],
        model: str,
        now: Callable[[], AwareDatetime],
    ) -> None:
        self._titles = titles
        self._embeddings = embeddings
        self._watch_states = watch_states
        self._rows = rows
        self._client = client
        self._metadata = metadata
        self._now = now
        self._clock = time.monotonic
        self._recipe = seed_row_recipe(model=model)
        self._spend = LLMLedger(
            ledger=calls,
            commit=commit,
            model=model,
            purpose=LLMPurpose.BECAUSE_YOU_WATCHED,
            now=now,
            clock=self._clock,
        )

    async def due_seeds(self, user_id: uuid.UUID) -> list[Title]:
        """The household's window of recent titles that need a build, most recent first.

        A seed the catalog no longer holds is dropped. Nothing due costs no catalog read.
        """
        recent = await self._watch_states.list_recent(user_id, limit=SEED_WINDOW)
        seed_ids = [watch.title_id for watch in recent]
        records = await self._rows.list_for(seed_ids)
        now = self._now()
        due = [
            seed_id
            for seed_id in seed_ids
            if is_due(records.get(seed_id), recipe=self._recipe, now=now)
        ]
        if not due:
            return []
        catalog = {title.id: title for title in await self._titles.list_by_ids(due)}
        return [catalog[seed_id] for seed_id in due if seed_id in catalog]

    async def build(self, seed: Title) -> SeedRow:
        """Build `seed`'s row now, due or not, and store what came of it.

        Unless it raises, exactly one record is saved and one ledger row settled, the
        record first so that one commit covers both. An unusable completion or too few
        cards records a failed attempt that keeps the last good cards. Any other
        `UsherPortError` is raised with nothing saved, though the ledger still records
        the completion, whose cost is already spent.
        """
        with _tracer.start_as_current_span("seed_rows.build") as span:
            span.set_attribute("usher.seed_rows.seed_id", str(seed.id))
            prompt = recall_prompt(
                name=seed.name, year=seed.year, overview=seed.overview, keywords=seed.keywords
            )
            started = self._clock()
            try:
                payload, usage = await self._client.complete_json(
                    prompt,
                    RECALL_SCHEMA,
                    purpose=LLMPurpose.BECAUSE_YOU_WATCHED,
                    temperature=RECALL_TEMPERATURE,
                )
            except PortDataMalformed as exc:
                return await self._fail(
                    seed, SeedRowOutcome.UNUSABLE, started, usage=None, error=_said(exc)
                )
            except UsherPortError as exc:
                span.set_attribute("usher.failed", True)
                await self._spend.settle(started, usage=None, error=_said(exc))
                raise
            try:
                named = parse_recall(payload)
            except PortDataMalformed as exc:
                return await self._fail(
                    seed, SeedRowOutcome.UNUSABLE, started, usage=usage, error=_said(exc)
                )
            try:
                cards = await self._cards(seed, named, span=span)
            except UsherPortError as exc:
                span.set_attribute("usher.failed", True)
                await self._spend.settle(
                    started, usage=usage, error=f"no row was built: {_said(exc)}"
                )
                raise
            span.set_attribute("usher.seed_rows.cards", len(cards))
            if len(cards) < MIN_SEED_ROW_CARDS:
                return await self._fail(
                    seed,
                    SeedRowOutcome.TOO_FEW,
                    started,
                    usage=usage,
                    error=f"{len(cards)} cards, fewer than the {MIN_SEED_ROW_CARDS} a row needs",
                )
            now = self._now()
            row = SeedRow(
                seed_title_id=seed.id,
                cards=cards,
                built_at=now,
                built_recipe=self._recipe,
                attempted_at=now,
                attempt_outcome=SeedRowOutcome.BUILT,
                attempt_recipe=self._recipe,
            )
            span.set_attribute("usher.seed_rows.outcome", row.attempt_outcome.value)
            await self._rows.save(row)
            await self._spend.settle(started, usage=usage, error=None)
            return row

    async def _cards(
        self, seed: Title, named: Sequence[NamedTitle], *, span: Span
    ) -> tuple[SeedRowCard, ...]:
        """The three lists, read in a fixed order, and the merge over the titles they hold."""
        resolved = await self._titles.resolve_names(named)
        named_ids = [resolved[probe] for probe in named if probe in resolved]
        recommended_ids = await self._recommended(seed)
        close_ids = await self._embeddings.nearest_known(
            seed.id, min_votes=CLOSE_MIN_VOTES, limit=CLOSE_LIMIT
        )
        union = list(dict.fromkeys([*named_ids, *recommended_ids, *close_ids]))
        # One read over all three: a skeleton reached by a provider id is dropped here,
        # and the votes that break the merge's ties come from the same rows.
        known = (
            {
                title.id: title.tmdb_vote_count or 0
                for title in await self._titles.list_by_ids(union)
                if title.enrichment_state is not EnrichmentState.SKELETON
            }
            if union
            else {}
        )
        named_ids = [one for one in named_ids if one in known]
        recommended_ids = [one for one in recommended_ids if one in known]
        close_ids = [one for one in close_ids if one in known]
        span.set_attribute("usher.seed_rows.named", len(named_ids))
        span.set_attribute("usher.seed_rows.recommended", len(recommended_ids))
        span.set_attribute("usher.seed_rows.close", len(close_ids))
        return merge_seed_row(
            seed_id=seed.id,
            named=named_ids,
            recommended=recommended_ids,
            close=close_ids,
            votes=known,
        )

    async def _recommended(self, seed: Title) -> list[uuid.UUID]:
        """The provider's first page for the seed, as catalog ids in the provider's order.

        Empty without a provider or a provider id, and when the provider has no record of
        the seed. Every other provider failure is raised.
        """
        if self._metadata is None or seed.tmdb_id is None:
            return []
        ref = ProviderRef(provider=self._metadata.name, value=str(seed.tmdb_id), kind=seed.kind)
        try:
            candidates = await self._metadata.recommendations(ref)
        except PortDataMalformed as exc:
            logger.info(
                "the metadata provider has no recommendations for seed {seed}: {error}",
                seed=seed.id,
                error=_said(exc),
            )
            return []
        provider_ids = [candidate.provider_id for candidate in candidates]
        if not provider_ids:
            return []
        resolved = await self._titles.resolve_tmdb_ids(seed.kind, provider_ids)
        return [resolved[one] for one in provider_ids if one in resolved]

    async def _fail(
        self,
        seed: Title,
        outcome: SeedRowOutcome,
        started: float,
        *,
        usage: LLMUsage | None,
        error: str,
    ) -> SeedRow:
        """Record a failed attempt over whatever the seed's record already held."""
        span = trace.get_current_span()
        span.set_attribute("usher.failed", True)
        span.set_attribute("usher.seed_rows.outcome", outcome.value)
        now = self._now()
        earlier = (await self._rows.list_for([seed.id])).get(seed.id)
        row = (
            SeedRow(
                seed_title_id=seed.id,
                attempted_at=now,
                attempt_outcome=outcome,
                attempt_recipe=self._recipe,
            )
            if earlier is None
            else earlier.evolve(
                attempted_at=now, attempt_outcome=outcome, attempt_recipe=self._recipe
            )
        )
        await self._rows.save(row)
        await self._spend.settle(started, usage=usage, error=error)
        logger.warning(
            "no row was built for seed {seed} ({outcome}): {error}",
            seed=seed.id,
            outcome=outcome.value,
            error=error,
        )
        return row


def _said(exc: BaseException) -> str:
    """Never a bare `str(exc)`, which is blank for an exception raised with no arguments."""
    return str(exc) or type(exc).__name__


#: One `SeedRowService` over one session, which its own builds commit.
SeedRowScope = Callable[[], AbstractAsyncContextManager[SeedRowService]]


class SeedRowJob(ScheduledJob):
    """Build every due seed in the household's window, one at a time."""

    name = SEED_ROWS_JOB_NAME

    def __init__(
        self,
        scope: SeedRowScope,
        *,
        user_id: Callable[[], Awaitable[uuid.UUID]],
        now: Callable[[], AwareDatetime],
    ) -> None:
        self._scope = scope
        self._user_id = user_id
        self._now = now
        self._done: datetime | None = None

    @property
    def period(self) -> timedelta:
        return SEED_ROWS_PERIOD

    async def last_done(self) -> datetime | None:
        """When this process last completed a run, or `None` until one has.

        Held in memory: the artefact is many records with no one completion time, and a
        restart's first run costs only the due check when nothing is due.
        """
        return self._done

    async def run(self) -> JobOutcome:
        """Build each due seed in recency order, in one scope.

        An unusable or short seed is recorded and the walk goes on. Anything else a build
        raises stops the walk there, and the scheduler offers the job again later.
        """
        user_id = await self._user_id()
        built: dict[SeedRowOutcome, int] = dict.fromkeys(SeedRowOutcome, 0)
        async with self._scope() as service:
            for seed in await service.due_seeds(user_id):
                row = await service.build(seed)
                built[row.attempt_outcome] += 1
        self._done = self._now()
        if any(built.values()):
            logger.info(
                "Because You Watched: {built} rows built, {too_few} too short, {unusable} unusable",
                built=built[SeedRowOutcome.BUILT],
                too_few=built[SeedRowOutcome.TOO_FEW],
                unusable=built[SeedRowOutcome.UNUSABLE],
            )
        return JobOutcome.DONE


__all__ = [
    "SEED_ROWS_JOB_NAME",
    "SEED_ROWS_PERIOD",
    "SeedRowJob",
    "SeedRowScope",
    "SeedRowService",
    "is_due",
]
