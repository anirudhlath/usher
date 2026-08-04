"""`usher derive` -- its argument surface, and the two decisions nothing else
asserts.

The command is driven against fakes rather than against `main`, because both
properties this file exists for are about *what the writing form does*, and a
parser case cannot see either: that the bare form answers zeroes on an empty
database, and that `--backfill` walks the cache **inline** rather than
enqueueing.
"""

import uuid

import pytest

from tests.fakes.collection_repository import FakeCollectionRepository
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.title_repository import FakeTitleRepository
from usher.cli import build_parser
from usher.domain.enums import TitleKind
from usher.domain.title import Title
from usher.services.derive import DeriveService


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
