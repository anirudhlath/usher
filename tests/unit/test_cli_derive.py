"""`usher derive` -- its argument surface, and the two decisions nothing else
asserts.

The command is driven against fakes rather than against `main`, because both
properties this file exists for are about *what the writing form does*, and a
parser case cannot see either: that the bare form answers zeroes on an empty
database, and that `--backfill` walks the cache **inline** rather than
enqueueing.
"""

import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from tests.fakes.collection_repository import FakeCollectionRepository
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.image_repository import FakeImageRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.title_repository import FakeTitleRepository
from usher.cli import _derive, build_parser
from usher.config import Settings
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.services.derive import DerivationReport, DeriveService


def _cli_settings() -> Settings:
    return Settings(database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher", secret_key="0" * 32)


@contextlib.asynccontextmanager
async def _no_session(_: Settings) -> AsyncIterator[None]:
    """`_session_for` without the engine. The claim under test is what the
    command *prints*, and opening a real connection would make it a claim
    about Postgres -- `tests/unit/test_cli.py` takes the same shape for the
    same reason."""
    yield None


def test_derive_parses_with_its_defaults() -> None:
    args = build_parser().parse_args(["derive"])
    assert args.command == "derive"
    assert args.backfill is False
    assert args.limit == 0
    # 500 rather than `index`'s 1000: a page here carries whole JSONB payloads
    # rather than title ids, and 500 TMDb detail responses at ~8 kB is ~4 MB
    # in flight.
    assert args.page_size == 500


def test_derive_backfill_is_opt_in() -> None:
    """The bare form only reads, so it is safe on a production box while
    diagnosing something. A `--backfill` that defaulted to on would make the
    diagnostic a write."""
    assert build_parser().parse_args(["derive", "--backfill"]).backfill is True


class _Fakes:
    def __init__(self) -> None:
        self.payloads = FakeRawPayloadStore()
        self.titles = FakeTitleRepository()
        self.people = FakePersonRepository()
        self.credits = FakeCreditRepository(self.people, self.titles)
        self.collections = FakeCollectionRepository()
        self.images = FakeImageRepository()
        self.queue = FakeJobQueue()

    def service(self) -> DeriveService:
        async def commit() -> None:
            return None

        return DeriveService(
            payloads=self.payloads,
            provider=FakeMetadataProvider(),
            titles=self.titles,
            people=self.people,
            credits=self.credits,
            collections=self.collections,
            images=self.images,
            commit=commit,
        )


@pytest.fixture
def fakes() -> _Fakes:
    return _Fakes()


async def test_derive_reports_zeroes_against_an_empty_database(fakes: _Fakes) -> None:
    """PRD 08's rule, at the five reads the bare form makes: *every one of
    them has to work against an empty database*.

    The arithmetic hazard in this command is the coverage ratio, which is why
    the report prints two counts and no percentage --
    `titles_with_credits / cached_payloads` is `0/0` on exactly the deployment
    that rule exists for, and a `ZeroDivisionError` on a diagnostic is the
    empty install's first experience of this milestone.

    Asserted through the ports the command reads rather than through its
    stdout, so a formatting change cannot silently satisfy it.
    """
    assert await fakes.payloads.count("tmdb") == 0
    assert await fakes.credits.count_titles_with_credits() == 0
    assert await fakes.people.count() == 0
    assert await fakes.collections.count() == 0


async def test_derive_backfill_writes_no_job_rows(fakes: _Fakes) -> None:
    """**The task's second decision, and nothing else asserts it.**

    `usher index --backfill` enqueues one job per stale title because the
    worker owns the model, and a CLI that embedded would load 65 MB of ONNX in
    a process whose job is to print two numbers. Derivation needs none of
    that -- no model, no network call, no rate limit -- so the queue would buy
    ordering, retry and backoff for work that needs none of the three, and
    2k-10k `jobs` rows plus one `get` per row to do what one page-walk does.

    The wrong implementation this kills is the one that copies `_index`'s
    backfill wholesale. It is not a failure anyone would notice: the jobs
    drain, the derivation happens, and the only symptom is a queue that grew
    by the size of the enriched tier and a walk that read every payload twice.
    """
    title = Title(
        kind=TitleKind.MOVIE,
        name="An Invented Film",
        sort_name="An Invented Film",
        tmdb_id=90000700,
    )
    await fakes.titles.add(title)
    await fakes.payloads.put(
        "tmdb",
        "movie",
        "90000700",
        {
            "id": 90000700,
            "title": "An Invented Film",
            "credits": {
                "cast": [
                    {
                        "id": 93000801,
                        "name": "Someone Invented",
                        "original_name": "Someone Invented",
                        "credit_id": f"{93000801:024d}",
                        "order": 0,
                    }
                ]
            },
        },
    )

    report = await fakes.service().derive_all()

    assert report.titles_derived == 1
    assert len(await fakes.credits.list_for_title(title.id)) == 1
    # `depth()` reports every kind, so the assertion is the *sum*: an
    # equality against `{}` passes for a reason that has nothing to do with
    # this command.
    assert sum((await fakes.queue.depth()).values()) == 0, "the walk is inline; nothing is enqueued"


async def test_the_report_counts_titles_rather_than_credit_rows(fakes: _Fakes) -> None:
    """`count_titles_with_credits`, and the name is the assertion.

    An operator running this asks "did my library get derived". A report
    counting credit *rows* answers "412,000 credits", which one
    heavily-credited film moves by fifty and which has no relationship to the
    denominator printed beside it.
    """
    title = Title(
        kind=TitleKind.MOVIE,
        name="An Invented Film",
        sort_name="An Invented Film",
        tmdb_id=90000710,
    )
    await fakes.titles.add(title)
    await fakes.payloads.put(
        "tmdb",
        "movie",
        "90000710",
        {
            "id": 90000710,
            "title": "An Invented Film",
            "credits": {
                "cast": [
                    {
                        "id": 93000900 + order,
                        "name": f"Billed {order}",
                        "original_name": f"Billed {order}",
                        "credit_id": f"{93000900 + order:024d}",
                        "order": order,
                    }
                    for order in range(4)
                ]
            },
        },
    )

    await fakes.service().derive_all()

    assert await fakes.credits.count_titles_with_credits() == 1
    assert await fakes.people.count() == 4


async def test_a_derive_job_key_that_is_not_a_uuid_parks_rather_than_killing_the_worker(
    fakes: _Fakes,
) -> None:
    """`_title_id`'s whole reason, now for a third kind.

    `uuid.UUID("not-a-uuid")` raises a `ValueError`, and `JobWorker`
    deliberately lets anything that is not a `UsherPortError` propagate -- *a
    bug in a handler is not an upstream failure*. So an unparseable key would
    take the worker process down instead of parking one job, and every other
    job in the queue with it.

    The conversion is shared with `enrich` and `index` rather than written a
    third time: three converters are three chances for one of them to raise
    the wrong type.
    """
    from usher.domain.jobs import Job, JobKind
    from usher.ports.errors import PortDataMalformed
    from usher.services.handlers import derive_handler

    handler = derive_handler(fakes.service())

    with pytest.raises(PortDataMalformed):
        await handler(Job(kind=JobKind.DERIVE, key="not-a-uuid"))


async def test_a_derive_job_for_a_deleted_title_completes(fakes: _Fakes) -> None:
    """The control beside the case above, and it is what makes that one about
    *parsing* rather than about "derive raises".

    `raw_payloads` outlives `titles`, so a job naming a title deleted since it
    was enqueued is ordinary. `handlers.py`'s standing rule: a job for work
    that has since become impossible **completes** rather than parks, because
    parking fills the review list with things that are simply gone.
    """
    from usher.domain.jobs import Job, JobKind
    from usher.services.handlers import derive_handler

    handler = derive_handler(fakes.service())

    await handler(Job(kind=JobKind.DERIVE, key=str(uuid.uuid4())))


async def test_the_backfill_report_prints_every_count_the_walk_produced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**A number a report does not print is a number nobody sees**, and this
    command is the only surface `images_written` has.

    The five lines are asserted together rather than only the new one, because
    the wrong implementation here is not a missing line -- it is a line printed
    against the wrong field, and `images written: 4` is indistinguishable from
    `collections linked: 4` unless both are pinned in one place. Every count in
    the report is therefore distinct, so no two lines can be satisfied by the
    same attribute.

    **A count, never a ratio**, for the reason the bare form's docstring
    gives one screen up: `images_written / payloads_read` is `0/0` on the empty
    database PRD 08 requires every command to work against.
    """
    report = DerivationReport(
        payloads_read=6,
        titles_derived=5,
        people_written=4,
        credits_written=3,
        collections_written=2,
        images_written=1,
    )

    class _Service:
        async def derive_all(self, *, page_size: int, limit: int) -> DerivationReport:
            return report

    async def _a_provider(_: Settings) -> tuple[object, Callable[[], Awaitable[None]]]:
        async def _close() -> None:
            return None

        return object(), _close

    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.build_pipeline", lambda *_, **__: object())
    monkeypatch.setattr("usher.cli.metadata_provider", _a_provider)
    monkeypatch.setattr("usher.cli.build_derive_service", lambda *_: _Service())

    await _derive(_cli_settings(), backfill=True, limit=0, page_size=500)

    assert capsys.readouterr().out.splitlines() == [
        "payloads read: 6",
        "titles derived: 5",
        "people written: 4",
        "credits written: 3",
        "collections linked: 2",
        "images written: 1",
    ]


async def test_a_derivation_that_found_no_artwork_still_prints_the_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**Zero is the number this line exists to print.** `images` joined
    `*_APPEND_TO_RESPONSE` in M4, so most of a real cache predates it, and an
    operator's first `usher derive --backfill` will report far fewer images
    than titles. A report that suppressed the line when it was zero would make
    "the cache is old" and "the write is broken" look identical -- and the
    second is what an operator would assume, because the line they saw last
    time is gone.
    """

    class _Service:
        async def derive_all(self, *, page_size: int, limit: int) -> DerivationReport:
            return DerivationReport(payloads_read=9, titles_derived=9)

    async def _a_provider(_: Settings) -> tuple[object, Callable[[], Awaitable[None]]]:
        async def _close() -> None:
            return None

        return object(), _close

    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.build_pipeline", lambda *_, **__: object())
    monkeypatch.setattr("usher.cli.metadata_provider", _a_provider)
    monkeypatch.setattr("usher.cli.build_derive_service", lambda *_: _Service())

    await _derive(_cli_settings(), backfill=True, limit=0, page_size=500)

    assert "images written: 0" in capsys.readouterr().out.splitlines()
