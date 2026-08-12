"""The review queue on the wire: `GET /admin/unmatched` and
`POST /admin/unmatched/{id}/resolve`.

Driven through a real `create_app()` with three dependencies overridden -- the
media-item, title and episode repositories -- so the router, the DTOs, A3's
cursor codec, A2's problem envelope and FastAPI's own path, query and body
parsing all sit on the path a request takes. Only the Postgres reads are stood
in for; `tests/integration/test_admin_unmatched.py` is what runs those, and it
is the only place the keyset's NULL boundary is *silently* wrong rather than
loudly wrong -- a NULL cannot poison a comparison in Python.

**Every ordering case here seeds the newest item first**, so the ids
`FakeMediaItemRepository` mints run the opposite way to `added_at`. Without
that, a UUIDv7 primary key makes `ORDER BY id DESC` and
`ORDER BY added_at DESC NULLS LAST, id DESC` agree by accident and no
assertion in this file could tell them apart. Each such case asserts that
premise itself.
"""

import ast
import inspect
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

import usher.api.routers.unmatched as unmatched_module
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.api.app import create_app
from usher.api.cursor import CursorSpec, CursorType, encode_cursor
from usher.api.deps import (
    get_episode_repository,
    get_media_item_repository,
    get_title_repository,
)
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE, ProblemCode
from usher.api.dto.unmatched import UnmatchedItemResponse
from usher.config import Settings
from usher.domain.enums import HdrFormat, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id
from usher.domain.source import MediaItem
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert

SOURCE_ID = new_id()
OTHER_SOURCE_ID = new_id()
SEEN_AT = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
NEWER = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
OLDER = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def media_items() -> FakeMediaItemRepository:
    return FakeMediaItemRepository()


@pytest.fixture
def titles() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def episodes() -> FakeEpisodeRepository:
    return FakeEpisodeRepository()


@pytest.fixture
def app(
    media_items: FakeMediaItemRepository,
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_media_item_repository] = lambda: media_items
    built.dependency_overrides[get_title_repository] = lambda: titles
    built.dependency_overrides[get_episode_repository] = lambda: episodes
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _upsert(
    external_id: str,
    *,
    source_id: uuid.UUID = SOURCE_ID,
    added_at: datetime | None = None,
    title_id: uuid.UUID | None = None,
) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=source_id,
        external_id=external_id,
        title_id=title_id,
        episode_id=None,
        container="mkv",
        video_codec="hevc",
        audio_codec="truehd",
        width=3840,
        height=2160,
        hdr_format=HdrFormat.HDR10,
        audio_channels=6,
        file_size_bytes=68_719_476_736,
        runtime_seconds=7200,
        added_at=added_at,
        last_seen_at=SEEN_AT,
    )


async def _given(
    media_items: FakeMediaItemRepository, rows: Sequence[MediaItemUpsert]
) -> dict[str, MediaItem]:
    """Seed in the order given -- which is what lets a case make the minted
    UUIDv7s disagree with `added_at` on purpose."""
    await media_items.upsert_many(rows)
    stored: dict[str, MediaItem] = {}
    for row in rows:
        found = await media_items.get_by_external_id(row.source_id, row.external_id)
        assert found is not None
        stored[row.external_id] = found
    return stored


async def _given_title(titles: FakeTitleRepository, name: str) -> Title:
    title = Title(kind=TitleKind.SERIES, name=name, sort_name=name)
    await titles.add(title)
    return title


async def _given_episode(
    episodes: FakeEpisodeRepository, title_id: uuid.UUID, number: int = 1
) -> Episode:
    season = Season(title_id=title_id, season_number=1)
    await episodes.upsert_seasons([season])
    episode = Episode(
        title_id=title_id,
        season_id=season.id,
        season_number=1,
        episode_number=number,
        name=f"Episode {number}",
    )
    await episodes.upsert_episodes([episode])
    return episode


async def _walk(
    client: httpx.AsyncClient, *, limit: int, params: dict[str, str] | None = None
) -> tuple[list[str], int]:
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        query = {"limit": str(limit), **(params or {})}
        if cursor is not None:
            query["cursor"] = cursor
        response = await client.get("/admin/unmatched", params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        pages += 1
        seen.extend(entry["external_id"] for entry in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return seen, pages
        assert pages < 20, "the walk did not terminate"


async def test_the_queue_is_newest_first_and_the_undated_ones_are_last(
    client: httpx.AsyncClient, media_items: FakeMediaItemRepository
) -> None:
    """`added_at DESC NULLS LAST, id DESC`, rendered.

    Seeded newest-first on purpose, so the minted ids run the opposite way to
    the dates -- the two premises below are what make this an ordering case
    rather than a membership one. Under `ORDER BY id DESC` the undated item,
    seeded last and therefore holding the largest id, would come *first*;
    under Postgres's own NULLS-FIRST default for a descending sort it would
    too, which is the same wrong answer reached two different ways.
    """
    stored = await _given(
        media_items,
        [
            _upsert("newer", added_at=NEWER),
            _upsert("older", added_at=OLDER),
            _upsert("undated", added_at=None),
        ],
    )
    assert stored["newer"].id < stored["older"].id < stored["undated"].id, (
        "the premise: the ids run the opposite way to the dates, so `ORDER BY id DESC` "
        "and this order cannot agree by accident"
    )
    assert NEWER > OLDER

    body = (await client.get("/admin/unmatched")).json()

    assert [entry["external_id"] for entry in body["items"]] == ["newer", "older", "undated"]
    assert body["items"][2]["added_at"] is None
    assert body["next_cursor"] is None


async def test_a_queue_entry_carries_the_source_id_the_operator_needs_to_find_the_file(
    client: httpx.AsyncClient, media_items: FakeMediaItemRepository
) -> None:
    """The one place a source's own item id is on the wire, and the reason is
    that an operator resolves an unmatched file by finding it on their own
    server. `usher unmatched` has printed it since M4.

    Also the shape assertion: every declared field is rendered, derived from
    the model rather than from a list this case keeps in step by hand.
    """
    await _given(media_items, [_upsert("emby-item-4471", added_at=NEWER)])

    entry = (await client.get("/admin/unmatched")).json()["items"][0]

    assert entry["external_id"] == "emby-item-4471"
    assert entry["source_id"] == str(SOURCE_ID)
    assert entry["available"] is True
    assert set(entry) == set(UnmatchedItemResponse.model_fields)


async def test_a_page_that_exactly_exhausts_the_queue_carries_no_next_cursor(
    client: httpx.AsyncClient, media_items: FakeMediaItemRepository
) -> None:
    """The off-by-one, and the only arrangement that can see it.

    Six items at `limit=3` makes the second page **full and final**. Under the
    naive "the page is full so there is more" spelling the response mints a
    cursor to nothing and the client spends a third request to learn it is
    finished -- while the partition case below, seven items at the same limit,
    stays green because `7 % 3 != 0`. `over_fetch(limit)` is what tells them
    apart.
    """
    await _given(media_items, [_upsert(f"orphan-{index}", added_at=NEWER) for index in range(6)])

    first = (await client.get("/admin/unmatched", params={"limit": "3"})).json()
    assert first["next_cursor"] is not None
    second = (
        await client.get("/admin/unmatched", params={"limit": "3", "cursor": first["next_cursor"]})
    ).json()

    assert len(second["items"]) == 3, "the premise: the last page is exactly full"
    assert second["next_cursor"] is None, "and it is the last one"


async def test_walking_the_cursor_serves_every_item_exactly_once_over_more_than_one_page(
    client: httpx.AsyncClient, media_items: FakeMediaItemRepository
) -> None:
    """Seven items at `limit=3`: the partition case, whose `7 % 3 != 0` is why
    it cannot see the off-by-one above. Both dated and undated, so the walk
    crosses the boundary between the two groups.

    `pages > 1` is asserted because a route that ignored `limit` and served
    everything at once satisfies the set assertion perfectly.
    """
    seeded = await _given(
        media_items,
        [
            *(_upsert(f"dated-{index}", added_at=NEWER) for index in range(4)),
            *(_upsert(f"undated-{index}", added_at=None) for index in range(3)),
        ],
    )

    seen, pages = await _walk(client, limit=3)

    assert pages == 3
    assert sorted(seen) == sorted(seeded)
    assert len(seen) == len(set(seen)), f"an item was served twice: {seen}"


async def test_the_source_filter_narrows_the_queue_and_the_cursor_remembers_which(
    client: httpx.AsyncClient, media_items: FakeMediaItemRepository
) -> None:
    """`?source_id=` is a filter and rides in the cursor's digest, so a cursor
    minted over one source and replayed against another is a `400
    invalid_cursor` rather than a plausible, wrong, silent page of the other
    source's backlog starting after *this* source's second item."""
    await _given(
        media_items,
        [
            _upsert("mine-a", added_at=NEWER),
            _upsert("mine-b", added_at=OLDER),
            _upsert("theirs", source_id=OTHER_SOURCE_ID, added_at=NEWER),
        ],
    )

    scoped = (
        await client.get("/admin/unmatched", params={"source_id": str(SOURCE_ID), "limit": "1"})
    ).json()
    assert [entry["external_id"] for entry in scoped["items"]] == ["mine-a"]
    assert scoped["next_cursor"] is not None

    replayed = await client.get(
        "/admin/unmatched",
        params={"source_id": str(OTHER_SOURCE_ID), "cursor": scoped["next_cursor"]},
    )

    assert replayed.status_code == 400
    assert replayed.json()["code"] == ProblemCode.INVALID_CURSOR.value


async def test_dropping_the_source_filter_mid_walk_is_refused_rather_than_silently_widened(
    client: httpx.AsyncClient, media_items: FakeMediaItemRepository
) -> None:
    """The other direction of the same digest, and the one a client reaches by
    accident: a cursor minted with a filter, replayed with none. An unfiltered
    read resuming from a filtered position is a page of a different population
    whose every row still looks right."""
    await _given(
        media_items,
        [
            _upsert("mine-a", added_at=NEWER),
            # A second item on the same source, so `limit=1` leaves this
            # source with more to serve and a cursor really is minted.
            _upsert("mine-b", added_at=OLDER),
            _upsert("theirs", source_id=OTHER_SOURCE_ID),
        ],
    )
    scoped = (
        await client.get("/admin/unmatched", params={"source_id": str(SOURCE_ID), "limit": "1"})
    ).json()
    assert scoped["next_cursor"] is not None

    replayed = await client.get("/admin/unmatched", params={"cursor": scoped["next_cursor"]})

    assert replayed.status_code == 400
    assert replayed.json()["code"] == ProblemCode.INVALID_CURSOR.value


async def test_a_tampered_cursor_is_refused_in_the_envelope_and_never_echoed(
    client: httpx.AsyncClient, media_items: FakeMediaItemRepository
) -> None:
    """A3's refusal, at a real route for the first time on this path.

    The `detail` names the rule and never the submitted value -- which is
    `api/errors.py`'s whole reason for existing, and is why the codec raises a
    `ProblemException` rather than letting a pydantic validator answer 422
    with the rejected cursor echoed back under `input`.
    """
    await _given(media_items, [_upsert("orphan", added_at=NEWER)])
    tampered = "!!not-base64!!"

    response = await client.get("/admin/unmatched", params={"cursor": tampered})

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == ProblemCode.INVALID_CURSOR.value
    assert body["instance"] == "/admin/unmatched"
    assert tampered not in body["detail"]
    # The control: the value really is one that could have appeared, and the
    # path it was submitted on is not the thing being asserted about.
    assert tampered not in response.text
    assert body["detail"]


async def test_a_foreign_cursor_carrying_the_wrong_key_type_is_refused(
    client: httpx.AsyncClient, media_items: FakeMediaItemRepository
) -> None:
    """A well-formed cursor minted by another listing. It decodes cleanly as
    base64 and as JSON, and it is still not this sort's key -- which is the
    failure a digest and a type check exist for and a base64 check cannot
    see."""
    await _given(media_items, [_upsert("orphan", added_at=NEWER)])
    foreign = encode_cursor(
        (7, new_id()),
        spec=CursorSpec(sort="episode_number", types=(CursorType.INT, CursorType.UUID)),
    )

    response = await client.get("/admin/unmatched", params={"cursor": foreign})

    assert response.status_code == 400
    assert response.json()["code"] == ProblemCode.INVALID_CURSOR.value


async def test_a_page_size_past_the_ceiling_is_refused_rather_than_clamped(
    client: httpx.AsyncClient,
) -> None:
    """`MAX_LIMIT` is what stops a client asking for the whole of a library
    that has never run a match pass -- 1,126,789 items on the one measured
    source. Refused rather than silently clamped, so a client that asked for
    more learns that it did."""
    assert (await client.get("/admin/unmatched", params={"limit": "0"})).status_code == 422
    over = await client.get(
        "/admin/unmatched", params={"limit": str(unmatched_module.MAX_LIMIT + 1)}
    )
    assert over.status_code == 422
    assert over.json()["code"] == ProblemCode.VALIDATION_FAILED.value


async def test_resolving_an_item_attaches_the_title_and_takes_it_off_the_queue(
    client: httpx.AsyncClient,
    media_items: FakeMediaItemRepository,
    titles: FakeTitleRepository,
) -> None:
    stored = await _given(media_items, [_upsert("orphan", added_at=NEWER)])
    title = await _given_title(titles, "A Resolved Film")

    response = await client.post(
        f"/admin/unmatched/{stored['orphan'].id}/resolve", json={"title_id": str(title.id)}
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(stored["orphan"].id),
        "title_id": str(title.id),
        "episode_id": None,
    }
    assert await media_items.list_unmatched(SOURCE_ID) == []


async def test_resolving_to_an_episode_writes_both_ids_which_is_what_the_cli_could_not(
    client: httpx.AsyncClient,
    media_items: FakeMediaItemRepository,
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
) -> None:
    """The argument `usher.cli._unmatched` said this route would grow. Both
    ids land on the row, which is the shape `ports/ingest.py`'s
    `MediaItemTarget` documents for an episode's `media_items` row: its
    series' `title_id` **and** its own `episode_id`."""
    stored = await _given(media_items, [_upsert("orphan", added_at=NEWER)])
    series = await _given_title(titles, "A Resolved Series")
    episode = await _given_episode(episodes, series.id)

    response = await client.post(
        f"/admin/unmatched/{stored['orphan'].id}/resolve",
        json={"title_id": str(series.id), "episode_id": str(episode.id)},
    )

    assert response.status_code == 200
    assert response.json()["episode_id"] == str(episode.id)
    written = await media_items.get_by_external_id(SOURCE_ID, "orphan")
    assert written is not None
    assert (written.title_id, written.episode_id) == (series.id, episode.id)


async def test_an_unknown_media_item_is_a_404_that_names_no_resource_in_its_code(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """`attach_title`'s boolean is what answers this -- the port returns
    whether a row changed precisely so a caller can say 404 rather than claim
    to have resolved something that does not exist. The code is the generic
    `not_found`: RFC 9457's `instance` carries the path, which names the
    missing item more precisely than a code could."""
    title = await _given_title(titles, "A Real Title")
    missing = new_id()

    response = await client.post(
        f"/admin/unmatched/{missing}/resolve", json={"title_id": str(title.id)}
    )

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == ProblemCode.NOT_FOUND.value
    assert body["instance"] == f"/admin/unmatched/{missing}/resolve"


async def test_an_unknown_title_is_refused_and_the_item_stays_on_the_queue(
    client: httpx.AsyncClient, media_items: FakeMediaItemRepository
) -> None:
    """Read back rather than inferred from the status code: "it answered 422"
    is also what a route that wrote the row and then failed a lookup
    produces."""
    stored = await _given(media_items, [_upsert("orphan", added_at=NEWER)])

    response = await client.post(
        f"/admin/unmatched/{stored['orphan'].id}/resolve", json={"title_id": str(new_id())}
    )

    assert response.status_code == 422
    assert response.json()["code"] == ProblemCode.VALIDATION_FAILED.value
    still_there = await media_items.list_unmatched(SOURCE_ID)
    assert [one.external_id for one in still_there] == ["orphan"]


async def test_an_unknown_episode_is_refused_and_the_item_stays_on_the_queue(
    client: httpx.AsyncClient,
    media_items: FakeMediaItemRepository,
    titles: FakeTitleRepository,
) -> None:
    stored = await _given(media_items, [_upsert("orphan", added_at=NEWER)])
    series = await _given_title(titles, "A Real Series")

    response = await client.post(
        f"/admin/unmatched/{stored['orphan'].id}/resolve",
        json={"title_id": str(series.id), "episode_id": str(new_id())},
    )

    assert response.status_code == 422
    assert response.json()["code"] == ProblemCode.VALIDATION_FAILED.value
    assert [one.external_id for one in await media_items.list_unmatched(SOURCE_ID)] == ["orphan"]


async def test_an_episode_of_another_title_is_refused_and_the_item_stays_on_the_queue(
    client: httpx.AsyncClient,
    media_items: FakeMediaItemRepository,
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
) -> None:
    """The check nothing downstream would have made. `attach_title` writes
    what it is given, `media_items` has no CHECK tying `title_id` to
    `episode_id`, and an episode row is *supposed* to carry its series' title
    beside its own episode -- so a file pointed at episode 1 of a different
    series is a valid row every read on this port answers with.

    Both titles are real and both are series, so the only thing separating the
    accepted resolution from this one is the relation between them.
    """
    stored = await _given(media_items, [_upsert("orphan", added_at=NEWER)])
    wanted = await _given_title(titles, "The Series An Operator Meant")
    other = await _given_title(titles, "Some Other Series")
    stray = await _given_episode(episodes, other.id)
    assert stray.title_id != wanted.id, "the premise: the episode belongs to the other title"

    response = await client.post(
        f"/admin/unmatched/{stored['orphan'].id}/resolve",
        json={"title_id": str(wanted.id), "episode_id": str(stray.id)},
    )

    assert response.status_code == 422
    assert response.json()["code"] == ProblemCode.VALIDATION_FAILED.value
    written = await media_items.get_by_external_id(SOURCE_ID, "orphan")
    assert written is not None
    assert (written.title_id, written.episode_id) == (None, None)


async def test_no_refusal_echoes_an_id_the_client_submitted(
    client: httpx.AsyncClient,
    media_items: FakeMediaItemRepository,
    titles: FakeTitleRepository,
    episodes: FakeEpisodeRepository,
) -> None:
    """A2's control, applied to the two refusals this route raises itself.

    `instance` carries the request *path*, so the media item's id is on the
    wire by design and is not what this is about -- what must not appear is a
    value from the **body**, which is where the credential rule lives and
    where a `detail` that interpolated one would put it.
    """
    stored = await _given(media_items, [_upsert("orphan", added_at=NEWER)])
    wanted = await _given_title(titles, "The Series An Operator Meant")
    other = await _given_title(titles, "Some Other Series")
    stray = await _given_episode(episodes, other.id)
    ghost = new_id()

    unknown = await client.post(
        f"/admin/unmatched/{stored['orphan'].id}/resolve", json={"title_id": str(ghost)}
    )
    mismatch = await client.post(
        f"/admin/unmatched/{stored['orphan'].id}/resolve",
        json={"title_id": str(wanted.id), "episode_id": str(stray.id)},
    )

    assert str(ghost) not in unknown.text
    assert str(stray.id) not in mismatch.text
    # The control: these ids do reach the wire when the request succeeds, so
    # the absences above are a property of the refusals rather than of the
    # values being unrenderable.
    accepted = await client.post(
        f"/admin/unmatched/{stored['orphan'].id}/resolve",
        json={"title_id": str(other.id), "episode_id": str(stray.id)},
    )
    assert accepted.status_code == 200
    assert str(stray.id) in accepted.text


def test_the_router_enqueues_nothing_and_invalidates_nothing() -> None:
    """The module docstring's claim, held structurally rather than by review.

    Resolving writes `media_items.title_id` and nothing reads that column to
    build a job, a neighbour list or a cached screen -- so a re-derive or a
    cache clear here would be work with no consumer, and `usher unmatched
    --resolve` does neither. Asserted on the module's identifiers because
    "the response looked the same" is also what a route that enqueued and
    discarded the result produces.

    Over a docstring-stripped `ast.unparse`, so the prose above -- which names
    every one of these on purpose -- cannot satisfy or trip the scan.
    """
    tree = ast.parse(inspect.getsource(unmatched_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    source = ast.unparse(ast.fix_missing_locations(tree))
    for forbidden in ("JobQueue", "JobRequest", "JobKind", "enqueue", "RowCache", "invalidate"):
        assert forbidden not in source, f"the review-queue router names {forbidden}"
