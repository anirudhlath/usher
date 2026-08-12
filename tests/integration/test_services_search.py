"""`SearchService` against real Postgres, for the three things fakes cannot say.

The unit file holds the ranking arithmetic, driven through a scripted index so
that only ranking varies. What is here instead is everything that is a property
of a *statement*: how many are issued, what `episode_id IS NULL` costs and buys,
and whether the two definitions of "owned" -- the boost in `services/search.py`
and the `owned_only` filter in `adapters/search/postgres.py` -- are actually the
same predicate. A dict can be made to agree with itself; two SQL statements
written a task apart cannot.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import math
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.embedding import planted_pair
from tests.fakes.search_index import FakeSuggestIndex
from usher.adapters.search.postgres import PostgresSearchIndex, _predicates
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.search import PostgresTitleEmbeddingRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.taste import PostgresTasteRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.users import ensure_default_user
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.repository import StoredTaste, TitleEmbeddingUpsert
from usher.ports.search import SearchFilters, SearchMode
from usher.services.search import SearchService

SEEN_AT = datetime(2026, 8, 2, 3, 0, tzinfo=UTC)

# The two knobs the shipped indexes take. Fixed here rather than read from
# `Settings`, because `tests/integration/` builds no `Settings` and a value an
# operator can change is not what these cases are about.
_EF_SEARCH = 100
_RRF_K = 60


@pytest_asyncio.fixture
async def source(session: AsyncSession) -> AsyncIterator[Source]:
    row = Source(
        kind=SourceKind.EMBY,
        name="Attic Library",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{uuid.uuid4()}",
        device_id=str(uuid.uuid4()),
    )
    await PostgresSourceRepository(session).add(row)
    yield row


def _service(session: AsyncSession) -> SearchService:
    """The real index, the real repositories, and a *fake* suggest index.

    The suggest half has its own contract driver against real Postgres in
    `tests/integration/test_adapters_search_postgres.py`; re-running it through
    here would measure the same statement twice, and every case in this file is
    about the search path's hydration.
    """
    return SearchService(
        PostgresSearchIndex(session, ef_search=_EF_SEARCH, rrf_k=_RRF_K),
        FakeSuggestIndex(),
        PostgresTitleRepository(session),
        PostgresMediaItemRepository(session),
        PostgresWatchStateRepository(session),
        PostgresTasteRepository(session),
        PostgresTitleEmbeddingRepository(session),
        result_limit=100,
    )


_TASTE_MODEL = "fake:test-384"


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine over two unit vectors, for a case's own premise.

    A bare dot is a cosine only because both sides are unit before the cast and
    drift by at most 1.21e-04 after it; every case using this asserts a gap two
    orders of magnitude wider than that.
    """
    return sum(one * other for one, other in zip(left, right, strict=True))


async def _seed_title(
    session: AsyncSession,
    name: str,
    *,
    kind: TitleKind = TitleKind.MOVIE,
    overview: str | None = None,
) -> Title:
    title = Title(
        kind=kind,
        name=name,
        sort_name=name.casefold(),
        overview=overview,
        year=2019,
        enrichment_state=EnrichmentState.ENRICHED,
    )
    await PostgresTitleRepository(session).add(title)
    return title


async def _seed_copy(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    title_id: uuid.UUID,
    external_id: str,
    episode_id: uuid.UUID | None = None,
) -> None:
    await PostgresMediaItemRepository(session).upsert_many(
        [
            MediaItemUpsert(
                source_id=source_id,
                external_id=external_id,
                title_id=title_id,
                episode_id=episode_id,
                container="mkv",
                video_codec="hevc",
                audio_codec="truehd",
                width=3840,
                height=2160,
                hdr_format=None,
                audio_channels=8,
                file_size_bytes=1,
                runtime_seconds=9360,
                added_at=None,
                last_seen_at=SEEN_AT,
            )
        ]
    )


async def _seed_episode_copy(
    session: AsyncSession, *, source_id: uuid.UUID, series_id: uuid.UUID, external_id: str
) -> uuid.UUID:
    """One `media_items` row shaped the way `IngestService` writes an episode:
    the *series'* `title_id` **and** the episode's own `episode_id`.

    Written through raw SQL rather than through `upsert_many` because
    `media_items.episode_id` is a real foreign key and this case does not care
    which episode it points at -- only that the row is not the series' own.

    Answers the episode's id, which the watch-state roll-up case writes against:
    a watch state on the *series* would make that case pass against the
    title-keyed read it exists to refuse.
    """
    season_id = uuid.uuid4()
    episode_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO seasons (id, title_id, season_number) "
            "VALUES (CAST(:id AS uuid), CAST(:title AS uuid), 1)"
        ),
        {"id": season_id, "title": series_id},
    )
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "VALUES (CAST(:id AS uuid), CAST(:title AS uuid), CAST(:season AS uuid), 1, 1)"
        ),
        {"id": episode_id, "title": series_id, "season": season_id},
    )
    await session.execute(
        text(
            "INSERT INTO media_items (id, source_id, title_id, episode_id, external_id, "
            "                         last_seen_at, available) "
            "VALUES (CAST(:id AS uuid), CAST(:source AS uuid), CAST(:title AS uuid), "
            "        CAST(:episode AS uuid), :external, :seen, true)"
        ),
        {
            "id": uuid.uuid4(),
            "source": source_id,
            "title": series_id,
            "episode": episode_id,
            "external": external_id,
            "seen": SEEN_AT,
        },
    )
    return episode_id


@contextmanager
def _record_statements(session: AsyncSession, sink: list[str]) -> Iterator[None]:
    """Capture SQL off `before_cursor_execute`, never transcribed.

    A hand-copied lookalike drifts and then reads like coverage, which is a
    failure this repository has recorded twice.
    """
    engine = session.get_bind().engine

    def _on_execute(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        sink.append(statement)

    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)


@pytest.mark.integration
async def test_a_search_costs_the_same_statements_at_5_hits_and_at_50(
    session: AsyncSession,
) -> None:
    """The N+1 this task's two port additions exist to delete, asserted the way
    M4's ingest cases assert it: hold the *shape* fixed and vary the thing that
    would multiply.

    Fails: `titles.get(hit.title_id)` per hit, and `media_items.list_for_title`
    per hit. The second is worse than N+1 -- a read on `media_items.title_id`
    alone is a read of the whole show, measured in this repository at 20,001
    rows / 22.901 ms / 402 buffers against 1 row / 0.251 ms / 21 buffers with
    `AND episode_id IS NULL`.

    Captured off `before_cursor_execute`, never transcribed.
    """
    for index in range(50):
        await _seed_title(session, f"Vacuum Study {index:02d}")
    await session.flush()
    service = _service(session)

    small: list[str] = []
    with _record_statements(session, small):
        few = await service.search("vacuum", limit=5)
    large: list[str] = []
    with _record_statements(session, large):
        many = await service.search("vacuum", limit=50)

    assert len(few.results) == 5
    assert len(many.results) == 50
    assert len(small) == len(large), (
        f"{len(large)} statements for 50 hits against {len(small)} for 5 -- "
        "hydration is per hit, which is the round-trip-per-item shape "
        "`list_by_ids` and `owned_title_ids` were added to delete"
    )


@pytest.mark.integration
async def test_ownership_counts_a_retracted_copy(session: AsyncSession, source: Source) -> None:
    """PRD 02's soft-delete availability. A copy the nightly sweep marked
    unavailable is still a copy you have, and a ranking that flips because a
    source went down moves search results for a reason unconnected to the
    query.

    This is also the case that pins the *shared* definition: the same predicate
    backs `SearchFilters.owned_only` in `PostgresSearchIndex`, and two
    definitions of owned is how a filtered list and a boosted list stop
    agreeing. Both halves are asserted here, in one case, because asserting
    either alone is what let them drift in the first place.
    """
    retracted = await _seed_title(session, "The Quiet Vacuum")
    await _seed_copy(session, source_id=source.id, title_id=retracted.id, external_id="retracted-1")
    await session.execute(
        text("UPDATE media_items SET available = false WHERE title_id = CAST(:id AS uuid)"),
        {"id": retracted.id},
    )
    await session.flush()

    media_items = PostgresMediaItemRepository(session)
    assert await media_items.owned_title_ids([retracted.id]) == {retracted.id}, (
        "the boost half stopped counting a retracted copy"
    )

    owned_only = await _service(session).search(
        "vacuum", filters=SearchFilters(owned_only=True), limit=10
    )
    assert [result.title_id for result in owned_only.results] == [retracted.id], (
        "the filter half stopped counting a retracted copy; the two definitions "
        "of owned have drifted and a filtered list and a boosted list now disagree"
    )
    assert [result.owned for result in owned_only.results] == [True]


@pytest.mark.integration
async def test_a_series_owned_only_through_its_episodes_is_read_once(
    session: AsyncSession, source: Source
) -> None:
    """The bound named rather than implied. `owned_title_ids` carries
    `AND episode_id IS NULL`, so a library that reported episodes but never
    their series row reads as not-owned for that series -- the same bound
    `resolve_external_ids`' title branch already accepts, and the alternative
    is the 20,001-row read above. Asserted so the trade is visible if anyone
    later calls it a bug.

    Both sides again: the boost's read and the `owned_only` filter must give
    the same answer for this row shape, or a series appears in a filtered list
    with its badge off.
    """
    series = await _seed_title(session, "Vacuum Chamber Diaries", kind=TitleKind.SERIES)
    own_row = await _seed_title(session, "The Quiet Vacuum")
    await _seed_episode_copy(
        session, source_id=source.id, series_id=series.id, external_id="episode-1"
    )
    await _seed_copy(session, source_id=source.id, title_id=own_row.id, external_id="film-1")
    await session.flush()

    media_items = PostgresMediaItemRepository(session)
    assert await media_items.owned_title_ids([series.id, own_row.id]) == {own_row.id}

    owned_only = await _service(session).search(
        "vacuum", filters=SearchFilters(owned_only=True), limit=10
    )
    assert [result.title_id for result in owned_only.results] == [own_row.id]


@pytest.mark.integration
async def test_the_two_owned_predicates_are_the_same_string(session: AsyncSession) -> None:
    """The agreement asserted structurally rather than only behaviourally.

    Both cases above would still pass if the two statements happened to agree
    on the rows those cases seed and disagreed on a shape neither seeds. This
    reads the shipped SQL out of both modules and asserts they carry the same
    two clauses -- `episode_id IS NULL` present in both, `available` absent
    from both -- so a future edit to one is visible here rather than in a
    household's badge column.
    """
    from usher.db.repositories.media_item import _OWNED_TITLE_IDS

    filter_sql, _ = _predicates(SearchFilters(owned_only=True))
    both = ((filter_sql, "the owned_only filter"), (_OWNED_TITLE_IDS, "the owned boost"))
    for sql, where in both:
        assert "episode_id IS NULL" in sql, f"{where} lost the episode bound"
        assert "available" not in sql, (
            f"{where} filters on availability; PRD 02's soft delete means a "
            "retracted copy is still a copy you have"
        )


@pytest.mark.integration
async def test_a_hydrated_result_carries_the_row_and_not_just_an_id(
    session: AsyncSession, source: Source
) -> None:
    """Hydration through the real repository, where a `Title` is 31 columns and
    `search_document` is a deferred generated column the read must not touch.

    Fails: a `list_by_ids` whose `defer(..., raiseload=True)` reaches
    `_to_domain` -- which would be a `MissingGreenlet` rather than a wrong
    answer, and only against real SQLAlchemy.
    """
    title = await _seed_title(session, "The Quiet Vacuum", overview="A house nobody has entered.")
    await _seed_copy(session, source_id=source.id, title_id=title.id, external_id="film-1")
    await session.flush()

    answer = await _service(session).search("vacuum", limit=10)

    assert [result.name for result in answer.results] == ["The Quiet Vacuum"]
    assert answer.results[0].kind is TitleKind.MOVIE
    assert answer.results[0].year == 2019
    assert answer.results[0].owned is True
    assert answer.mode is SearchMode.FULL_TEXT
    assert answer.degraded is False


@pytest.mark.integration
async def test_a_household_costs_exactly_two_more_statements_and_it_names_them(
    session: AsyncSession,
) -> None:
    """The read count, against real SQL rather than against a counter.

    Three statements without a household -- the retrieval, `list_by_ids`,
    `owned_title_ids`. **Five** with one: those three plus
    `played_title_ids` and `TasteRepository.latest`, one each, whatever the hit
    count -- and **no vector read at all**, because this household has no
    stored centroid, which is the shipped state of every deployment whose
    worker has never run.

    Fails: a per-hit household read, which is the N+1 the batch port exists to
    delete and which answers identically; a household read issued when there is
    no household, which costs two statements per search on every caller that
    has none; and a `list_for_titles` gated on the *row* rather than on the
    *centroid*, which puts a `title_id IN (...)` over the whole candidate set
    on every search of every un-indexed deployment and answers `{}`.

    `_record_statements` captures off `before_cursor_execute` and nothing is
    transcribed, so the count is the count Postgres saw. The three per-table
    assertions are what say the extra statements are the ones this pair of
    tasks added rather than a second hydration read that happens to make the
    arithmetic work.
    """
    for index in range(8):
        await _seed_title(session, f"Vacuum Study {index:02d}")
    await session.flush()
    service = _service(session)
    household = uuid.uuid4()

    without: list[str] = []
    with _record_statements(session, without):
        anonymous = await service.search("vacuum", limit=8)
    with_one: list[str] = []
    with _record_statements(session, with_one):
        theirs = await service.search("vacuum", limit=8, user_id=household)

    assert len(anonymous.results) == 8, "the premise: there are more hits than statements"
    assert len(theirs.results) == 8
    assert len(without) == 3, f"three statements without a household: {without}"
    assert len(with_one) == 5, f"five statements with one: {with_one}"
    assert [one for one in without if "watch_states" in one] == []
    assert [one for one in without if "user_taste" in one] == []
    assert len([one for one in with_one if "watch_states" in one]) == 1
    assert len([one for one in with_one if "user_taste" in one]) == 1
    assert [one for one in with_one if "title_embeddings" in one] == []


@pytest.mark.integration
async def test_a_stored_centroid_ranks_a_search_on_a_process_that_holds_no_model(
    session: AsyncSession,
) -> None:
    """PRD 05's sixth term end to end, over the two `halfvec` round trips no
    fake can express — the centroid's and the candidates'.

    Nothing here holds an `Embedder`. `_service` builds none, and neither does
    `api/deps.get_search_service`; the centroid is written the way a worker
    writes one and *read* through `TasteRepository.latest`, which is the whole
    of what this task closed. Fails: a term routed through
    `TasteService.centroid`, which answers `None` with no embedder and would
    leave the two rows tied.

    The angle is planted rather than hoped for, and the premise is read back
    **through the repository** — after the `halfvec` cast, whose measured max
    round-trip cosine error is 1.21e-04, three orders of magnitude below the
    0.5 gap seeded here.

    The vector read is asserted to be **scoped and issued once**: this is the
    six-statement arm of the count case above — the retrieval plus the service's
    five port reads, where a household with no stored centroid pays five — and
    the model name on the wire is what stops a mid-swap deployment blending two
    spaces.
    """
    near = await _seed_title(session, "Vacuum Study Alpha")
    far = await _seed_title(session, "Vacuum Study Beta")
    household = await ensure_default_user(session)
    axis, near_vector = planted_pair(math.pi / 3)
    _, far_vector = planted_pair(math.pi / 2)
    embeddings = PostgresTitleEmbeddingRepository(session)
    await embeddings.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=near.id,
                embedding=tuple(near_vector),
                model_name=_TASTE_MODEL,
                source_fingerprint="0" * 32,
            ),
            TitleEmbeddingUpsert(
                title_id=far.id,
                embedding=tuple(far_vector),
                model_name=_TASTE_MODEL,
                source_fingerprint="1" * 32,
            ),
        ]
    )
    taste = PostgresTasteRepository(session)
    await taste.put(
        StoredTaste(
            user_id=household,
            centroid=tuple(axis),
            model_name=_TASTE_MODEL,
            source_watermark=None,
            title_count=12,
            computed_at=SEEN_AT,
        )
    )
    await session.flush()

    stored = await taste.latest(household)
    assert stored is not None and stored.centroid is not None, (
        "the premise: the row is readable without a model name"
    )
    seen = await embeddings.list_for_titles([near.id, far.id], model_name=stored.model_name)
    assert _dot(stored.centroid, seen[near.id]) > _dot(stored.centroid, seen[far.id]) + 1e-2, (
        "the premise: the gap survives the halfvec cast by three orders of "
        "magnitude more than its 1.21e-04 round-trip error"
    )

    service = _service(session)
    statements: list[str] = []
    with _record_statements(session, statements):
        answer = await service.search("vacuum study", limit=10, user_id=household)

    scores = {one.title_id: one.score for one in answer.results}
    assert set(scores) == {near.id, far.id}
    assert scores[near.id] > scores[far.id]
    assert len(statements) == 6, f"six statements when a centroid exists: {statements}"
    vector_reads = [one for one in statements if "title_embeddings" in one]
    assert len(vector_reads) == 1
    assert "model_name" in vector_reads[0], (
        "the vector read must be scoped by the model the stored centroid names"
    )


@pytest.mark.integration
async def test_a_watched_episode_lifts_its_series_in_a_search(
    session: AsyncSession, source: Source
) -> None:
    """The roll-up, and it is the trap a fake cannot see.

    `played_title_ids` reaches a series through
    `COALESCE(ws.title_id, e.title_id)`, so a household that has finished an
    *episode* has played the series. Fails: a title-keyed read, which returns a
    films-only answer -- correct-looking, correctly ordered, and silently
    unpersonalised for every television household there is.

    The premise is asserted twice over: the two hits carry equal index scores,
    so the relevance term cancels and the watch-state term is the only thing
    left; and the watch state written is against the **episode**, never against
    the series row the assertion is about.
    """
    series = await _seed_title(session, "Vacuum Chamber Diaries", kind=TitleKind.SERIES)
    film = await _seed_title(session, "The Quiet Vacuum")
    episode_id = await _seed_episode_copy(
        session, source_id=source.id, series_id=series.id, external_id="episode-1"
    )
    household = await ensure_default_user(session)
    watch_states = PostgresWatchStateRepository(session)
    await watch_states.merge_from_source(
        [
            WatchStateMerge(
                user_id=household,
                title_id=None,
                episode_id=episode_id,
                position_seconds=0,
                played=True,
                runtime_seconds=2700,
                observed_at=SEEN_AT,
                play_count=1,
                last_played_at=SEEN_AT,
            )
        ]
    )
    await session.flush()
    assert await watch_states.played_title_ids(household, [series.id, film.id]) == {series.id}, (
        "the premise: the roll-up sees the episode, and the film is genuinely unplayed"
    )

    service = _service(session)
    anonymous = {
        one.title_id: one.score for one in (await service.search("vacuum", limit=10)).results
    }
    theirs = {
        one.title_id: one.score
        for one in (await service.search("vacuum", limit=10, user_id=household)).results
    }

    assert set(anonymous) == {series.id, film.id} == set(theirs)
    # Scores rather than positions, because the two names do not tie on
    # `ts_rank` and a rank-0/rank-1 gap is 0.35 against a watch-state weight of
    # 0.02 -- an ordering assertion here would be an assertion about full-text
    # scoring. What the household changes is the *gap*, and it changes it in
    # both directions at once: the series gains the boost, and the film gains a
    # present-and-zero signal that renormalises its score down.
    assert theirs[series.id] > anonymous[series.id]
    assert theirs[film.id] < anonymous[film.id]
    assert theirs[series.id] - theirs[film.id] > anonymous[series.id] - anonymous[film.id]


@pytest.mark.integration
async def test_a_search_that_matches_nothing_costs_no_hydration(session: AsyncSession) -> None:
    """The empty-candidate guard, where it is observable. Fails: a `_rank` that
    issues `list_by_ids([])` and `owned_title_ids([])` anyway -- two statements
    per keystroke on a search box whose query has not matched yet, which is
    most keystrokes."""
    await _seed_title(session, "The Quiet Vacuum")
    await session.flush()
    service = _service(session)

    seen: list[str] = []
    with _record_statements(session, seen):
        answer = await service.search("thereisnosuchword", limit=10)

    assert answer.results == ()
    assert len(seen) == 1, f"{len(seen)} statements for a search that matched nothing: {seen}"
