"""PRD 03 stage 4's queued half, against port fakes. No database, no model.

`FakeEmbedder` is a hash, so **no case here asserts relevance** -- that is its
documented divergence and a test ignoring it is a defect in the test. What is
asserted is plumbing: what gets written, what gets called, and what happens the
second time. Relevance is asserted only where a real model runs.

`FakeTitleEmbeddingRepository` has no `halfvec` quantisation, no width
constraint and no SQL predicate, so the dimension check is tested directly
rather than through a write, and the drain is an integration case.
"""

import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from tests.fakes.embedding import FakeEmbedder
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import Job, JobKind
from usher.domain.title import Title
from usher.ports.embedding import Embedder
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.services.handlers import index_handler
from usher.services.index import IndexService
from usher.services.search import compose_document


class _WrongWidthEmbedder(FakeEmbedder):
    """Declares 384 and returns 512 -- a model swap that changed width.

    Deliberately a *lying* embedder rather than one built at another
    dimension: `dimension` is what the column was sized from, so the failure
    worth catching is the two disagreeing, not both being 512.
    """

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = await super().embed(texts)
        return [[*vector, *vector[:128]] for vector in vectors]


class _TwoVectorEmbedder(FakeEmbedder):
    """Returns two vectors for one text -- an implementation that expanded a
    batch internally, which is how title *n*'s vector lands on title *m*."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = await super().embed(texts)
        return [*vectors, *vectors]


class _UnreachableEmbedder(FakeEmbedder):
    """The model file is gone, or the process cannot reach it. Retryable."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise PortUnavailable("the model is not loaded")


@pytest.fixture
def titles() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def embeddings() -> FakeTitleEmbeddingRepository:
    return FakeTitleEmbeddingRepository()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def commits() -> list[int]:
    return []


@pytest.fixture
def service(
    titles: FakeTitleRepository,
    embeddings: FakeTitleEmbeddingRepository,
    embedder: FakeEmbedder,
    commits: list[int],
) -> IndexService:
    return _service(titles, embeddings, embedder, commits)


def _service(
    titles: FakeTitleRepository,
    embeddings: FakeTitleEmbeddingRepository,
    embedder: Embedder,
    commits: list[int],
) -> IndexService:
    async def commit() -> None:
        commits.append(1)

    return IndexService(titles=titles, embeddings=embeddings, embedder=embedder, commit=commit)


async def _given(titles: FakeTitleRepository, **rest: Any) -> Title:
    """A synthetic enriched movie in the repository. Every value invented."""
    fields: dict[str, Any] = {
        "kind": TitleKind.MOVIE,
        "name": "The Quiet Vacuum",
        "sort_name": "quiet vacuum, the",
        "year": 2019,
        "overview": "A caretaker inventories a house nobody has entered since 1974.",
        "enrichment_state": EnrichmentState.ENRICHED,
    }
    fields.update(rest)
    title = Title(**fields)
    await titles.add(title)
    return title


async def test_indexing_writes_the_vector_the_model_and_the_fingerprint(
    titles: FakeTitleRepository,
    embeddings: FakeTitleEmbeddingRepository,
    embedder: FakeEmbedder,
    service: IndexService,
) -> None:
    """All three columns, because the vector alone cannot be checked for
    staleness.

    Fails: an implementation storing the embedding and leaving
    `model_name`/`source_fingerprint` at their defaults. Every search still
    works, every vector is still a vector, and the stale predicate matches
    every row forever -- so the backfill never drains and the gauge never
    reaches zero, with nothing raising.
    """
    title = await _given(titles)

    await service.index(title.id)

    stored = await embeddings.get(title.id)
    assert stored is not None
    assert stored.model_name == embedder.model_name
    assert stored.source_fingerprint == compose_document(title).fingerprint
    assert stored.embedding is not None
    assert len(stored.embedding) == embedder.dimension


async def test_indexing_commits(
    titles: FakeTitleRepository, service: IndexService, commits: list[int]
) -> None:
    """The handler runs inside `JobWorker`'s transaction and the row has to
    be durable before `complete` deletes the job. Without this the work is
    done, the job is gone, and the vector is not there."""
    title = await _given(titles)

    await service.index(title.id)

    assert commits == [1]


async def test_indexing_the_same_title_twice_writes_the_same_row_and_embeds_once(
    titles: FakeTitleRepository,
    embeddings: FakeTitleEmbeddingRepository,
    embedder: FakeEmbedder,
    service: IndexService,
) -> None:
    """PRD 08's "redelivery is safe by construction", plus the half that
    makes it free. `JobWorker.recover()` requeues an abandoned claim,
    so a process killed between the handler returning and `complete`
    committing produces exactly this.

    Fails: an implementation that re-embeds on every delivery. It is
    *correct*, and at ~83 texts/s a requeued backfill would re-run the whole
    enriched tier. Asserted on `embedder.calls`, never on wall time.
    """
    title = await _given(titles)
    await service.index(title.id)
    first = await embeddings.get(title.id)
    calls = len(embedder.calls)

    await service.index(title.id)

    assert await embeddings.get(title.id) == first
    assert len(embedder.calls) == calls, "a redelivered index job re-embedded"


async def test_a_changed_overview_is_re_embedded(
    titles: FakeTitleRepository,
    embeddings: FakeTitleEmbeddingRepository,
    service: IndexService,
) -> None:
    """The mirror, and why the skip is a fingerprint comparison rather than
    `if stored is not None: return`. That version passes every idempotence
    case and then never updates a vector again -- the milestone's own failure
    mode, a stale index that does not raise, it answers.
    """
    title = await _given(titles)
    await service.index(title.id)
    before = await embeddings.get(title.id)
    await titles.update(title.evolve(overview="Two sisters share one inherited grudge."))

    await service.index(title.id)

    after = await embeddings.get(title.id)
    assert before is not None and after is not None
    assert after.source_fingerprint != before.source_fingerprint
    assert after.embedding != before.embedding


async def test_a_degenerate_title_is_written_with_a_null_embedding_rather_than_skipped(
    titles: FakeTitleRepository,
    embeddings: FakeTitleEmbeddingRepository,
    embedder: FakeEmbedder,
    service: IndexService,
) -> None:
    """**The case that catches the non-draining backfill.**

    This project has shipped that bug once: the watch-history repair carried
    the walk's `observed_at`, was refused by the very row it existed to
    repair, wrote nothing, and left that row matching `played AND play_count
    = 0` for good.

    Here the same shape is one `return` away. An implementation that sees
    `document.is_degenerate` and returns without writing leaves the title
    matching the stale predicate forever -- re-claimed by every backfill
    pass, counted by `usher.search.embeddings.stale` on every scrape, with a
    handler that completes successfully every single time. Nothing raises,
    nothing parks, and the queue churns permanently on rows that can never
    succeed.

    Three assertions, and the third is the one the bug slips past: a row
    exists, its embedding is NULL, and its fingerprint is the fingerprint of
    the degenerate text -- so it *stops matching the predicate* rather than
    merely being present.
    """
    title = await _given(titles, name=" ", sort_name=" ", year=None, overview=None)

    await service.index(title.id)

    stored = await embeddings.get(title.id)
    assert stored is not None, "a refused title was skipped, not written"
    assert stored.embedding is None
    assert stored.source_fingerprint == compose_document(title).fingerprint
    assert embedder.calls == [], "a degenerate document was sent to the model"


async def test_a_refused_title_is_re_indexed_once_when_it_gains_content(
    titles: FakeTitleRepository,
    embeddings: FakeTitleEmbeddingRepository,
    embedder: FakeEmbedder,
    service: IndexService,
) -> None:
    """The other half of the drain: refused is not permanent. An
    implementation writing a constant fingerprint for every refusal passes
    the case above and then never re-claims the title however much
    enrichment gives it.

    "Once", not "every pass": the third `index` call is the redelivery, and
    it must not embed again.
    """
    title = await _given(titles, name=" ", sort_name=" ", year=None, overview=None)
    await service.index(title.id)
    repaired = title.evolve(name="Ledgerhand", sort_name="ledgerhand", overview="A house.")
    await titles.update(repaired)

    await service.index(title.id)
    stored = await embeddings.get(title.id)
    await service.index(title.id)

    assert stored is not None
    assert stored.embedding is not None
    assert stored.source_fingerprint == compose_document(repaired).fingerprint
    assert len(embedder.calls) == 1


async def test_a_title_that_no_longer_exists_completes_rather_than_parks(
    embeddings: FakeTitleEmbeddingRepository, embedder: FakeEmbedder, service: IndexService
) -> None:
    """`handlers.py`'s stated rule, and the deliberate divergence from
    `EnrichService`, which raises `PortDataMalformed` here. An `index` job's
    other producer is a sweep over the whole enriched tier, so a title
    deleted between sweep and claim is routine -- and a park per deleted
    title fills the review list with tombstones.
    """
    await service.index(uuid.uuid4())

    assert embeddings.rows == {}
    assert embedder.calls == []


async def test_a_wrong_width_vector_parks(
    titles: FakeTitleRepository, embeddings: FakeTitleEmbeddingRepository, commits: list[int]
) -> None:
    """A model swap that silently changes width writes vectors a
    `halfvec(384)` column rejects, and the rejection arrives one statement
    later naming a column, not a model. `PortDataMalformed` rather than
    retryable: no backoff makes a 512-wide model return 384 floats.

    Tested here rather than through a write because
    `FakeTitleEmbeddingRepository` has no width constraint at all -- that is
    its second documented divergence, and it is why this check is the
    service's rather than the database's.
    """
    service = _service(titles, embeddings, _WrongWidthEmbedder(), commits)
    title = await _given(titles)

    with pytest.raises(PortDataMalformed):
        await service.index(title.id)

    assert embeddings.rows == {}


async def test_a_batch_that_comes_back_the_wrong_length_parks(
    titles: FakeTitleRepository, embeddings: FakeTitleEmbeddingRepository, commits: list[int]
) -> None:
    """One text in, one vector out. An implementation returning two is one
    that expanded the batch internally, which is how title *n*'s vector lands
    on title *m* -- and taking `vectors[0]` regardless would store a vector
    for the wrong text with nothing raising anywhere.
    """
    service = _service(titles, embeddings, _TwoVectorEmbedder(), commits)
    title = await _given(titles)

    with pytest.raises(PortDataMalformed):
        await service.index(title.id)


async def test_an_unreachable_model_is_retryable_rather_than_parked(
    titles: FakeTitleRepository, embeddings: FakeTitleEmbeddingRepository, commits: list[int]
) -> None:
    """The other column of the disposition table. A model that is not loaded
    is a `PortUnavailable`, which `JobWorker` backs off rather than parks --
    retrying genuinely fixes it, and parking would need a human to release
    work whose only problem was a restart.

    The service re-raises rather than absorbing: `JobWorker` is the only
    thing that knows which error means which, and it learns by catching.
    """
    service = _service(titles, embeddings, _UnreachableEmbedder(), commits)
    title = await _given(titles)

    with pytest.raises(PortUnavailable):
        await service.index(title.id)

    assert embeddings.rows == {}


async def test_the_handler_converts_the_key(
    titles: FakeTitleRepository,
    embeddings: FakeTitleEmbeddingRepository,
    service: IndexService,
) -> None:
    """`_title_id`, reused rather than reimplemented, so the conversion is in
    one place for both title-keyed kinds."""
    title = await _given(titles)

    await index_handler(service)(Job(kind=JobKind.INDEX, key=str(title.id)))

    assert await embeddings.get(title.id) is not None


async def test_the_handler_parks_an_unparseable_key(service: IndexService) -> None:
    """`uuid.UUID("not-a-uuid")` raises `ValueError`, which is not a
    `UsherPortError`, and `JobWorker` lets those propagate on purpose -- so an
    unconverted key takes the worker process down instead of parking one job.
    """
    with pytest.raises(PortDataMalformed):
        await index_handler(service)(Job(kind=JobKind.INDEX, key="not-a-uuid"))


async def test_a_model_swap_re_embeds_a_title_whose_text_did_not_change(
    titles: FakeTitleRepository,
    embeddings: FakeTitleEmbeddingRepository,
    commits: list[int],
) -> None:
    """The `model_name` half of the skip, which the rest of this file cannot
    see because every other case runs one embedder.

    Measured, and this is why the column exists: the same checkpoint served by
    sentence-transformers and by fastembed produces vectors whose max pairwise
    delta is 1.41e-03 -- 6x the halfvec quantisation error -- so the two are
    not interchangeable without a re-embed. Recording the runtime alongside
    the checkpoint is what makes a swap invalidate every stored vector through
    the stale predicate rather than through a migration somebody remembers.

    Fails: dropping `stored.model_name == self._embedder.model_name` from the
    skip. The text has not moved, so the fingerprint matches, so the swapped
    deployment skips every title it already has -- and goes on serving vectors
    from the previous runtime forever while the gauge reads zero stale.

    Asserted on `model_name` and on the embed call rather than on the vector:
    `FakeEmbedder` hashes its input and ignores its own name, so the two
    runtimes here return *identical* vectors. That is the harder case and the
    honest one -- a comparison of vectors would pass against the mutation.
    """
    before = FakeEmbedder(model_name="fake:runtime-a")
    after = FakeEmbedder(model_name="fake:runtime-b")
    title = await _given(titles)
    await _service(titles, embeddings, before, commits).index(title.id)

    await _service(titles, embeddings, after, commits).index(title.id)

    stored = await embeddings.get(title.id)
    assert stored is not None
    assert stored.model_name == "fake:runtime-b"
    assert after.calls != [], "a model swap left the previous runtime's vectors in place"
