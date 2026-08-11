"""D5 -- the leak pins ADR-0012 names and says nothing tests: an RFC 9457
`detail`, `RowCache`, and the success body itself now that D3 has substituted
a ticket for every source URL.

**Every pin asserts the serializer ran before asserting the token is
absent.** A response nothing built also carries no token, so each case below
proves the surface under test actually produced output -- a 503 with a real
`code` and a non-empty `detail`, a screen the cache actually cached, a body
whose targets are real ticket URLs -- before it asks whether the token is in
it. That ordering is the whole method (D5's own title), not decoration:
`.claude/rules/mutation-sweeps.md:561`'s finding is *"a `sink == []` assertion
is a false green wherever the fixture makes the logging impossible"*, and the
same shape applies to every absence assertion in this file.

**Every case uses a deliberately tiny URL, `https://e/a.mkv?api_key=tok-Zq7`.**
ADR-0012 measured that loguru truncates a rendered value at ~128 characters,
so a leak probe built on a realistic Emby URL passes whether or not the
redaction or the substitution exists -- `tests/unit/test_ports_source.py`
already keeps this discipline one layer down, over `StreamTarget.__repr__`
directly; this file keeps it one layer up, over the whole response the API
builds from one.

**D1 measured that a ticket's plaintext *is* the URL**, so "the token is
absent" here always means the *source* token (`TOKEN`, or the whole
`DIRECT_URL`) -- never the ticket, which legitimately appears in the body and
is the artifact the client is meant to hold.

Two of ADR-0012's three named leak surfaces -- the telemetry attribute
(including `HTTPXClientInstrumentor`'s `url.full`) and the log sink -- need a
*real* outbound call to be a meaningful pin rather than a vacuous one (no fake
adapter ever makes one), so they live in
`tests/integration/test_playback_leaks.py` against the real `EmbyAdapter`
instead of here.
"""

import ast
import inspect
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr

import usher.api.dto.playback as playback_dto
from tests.fakes.credential_store import FakeCredentialStore
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.source_repository import FakeSourceRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.unit.rows import USER, Library
from usher.api.app import create_app
from usher.api.deps import (
    get_credential_store,
    get_media_item_repository,
    get_row_context,
    get_source_adapter_factory,
    get_source_repository,
    get_title_repository,
)
from usher.config import Settings
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortUnavailable
from usher.ports.ingest import MediaItemUpsert
from usher.ports.source import SourceAdapter, SourceAdapterFactory, StreamTarget, StreamTargetKind

SECRET_KEY = "0123456789abcdef0123456789abcdef"
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)

# Short, distinctive and deliberately tiny -- see the module docstring.
TOKEN = "tok-Zq7"
DIRECT_URL = f"https://e/a.mkv?api_key={TOKEN}"


# -- fakes and fixtures -------------------------------------------------


class _ScriptedAdapter(FakeSourceAdapter):
    """A `FakeSourceAdapter` whose `stream_targets` is scripted outright, or
    which raises whatever it was scripted with -- the module docstring's
    tiny-URL discipline needs a target this file wrote, not one the fake's
    own URL construction produced."""

    def __init__(
        self, source: Source, targets: Sequence[StreamTarget], error: Exception | None
    ) -> None:
        super().__init__(source)
        self._targets = list(targets)
        self._error = error

    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        if self._error is not None:
            raise self._error
        return list(self._targets)


class _ScriptedFactory(SourceAdapterFactory):
    def __init__(self) -> None:
        self._scripts: dict[uuid.UUID, tuple[list[StreamTarget], Exception | None]] = {}

    def script(
        self,
        source: Source,
        *,
        targets: Sequence[StreamTarget] = (),
        error: Exception | None = None,
    ) -> None:
        self._scripts[source.id] = (list(targets), error)

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        targets, error = self._scripts.get(source.id, ([], None))
        return _ScriptedAdapter(source, targets, error)


class _Household:
    """The five ports the playback graph reads. Trimmed from
    `test_api_playback.py`'s own fixture of the same name -- this file needs
    no episodes and only ever one copy per case."""

    def __init__(self) -> None:
        self.titles = FakeTitleRepository()
        self.media_items = FakeMediaItemRepository()
        self.sources = FakeSourceRepository()
        self.credentials = FakeCredentialStore()
        self.factory = _ScriptedFactory()

    async def add_title(self) -> uuid.UUID:
        title = Title(kind=TitleKind.MOVIE, name="Example Movie", sort_name="Example Movie")
        await self.titles.add(title)
        return title.id

    async def add_source(self, name: str = "Living Room Emby") -> Source:
        source = Source(
            kind=SourceKind.EMBY,
            name=name,
            base_url=f"https://{name.lower().replace(' ', '-')}.invalid",
            credentials_ref=f"ref-{name}",
            device_id=str(new_id()),
        )
        await self.sources.add(source)
        await self.credentials.put(source.credentials_ref, CREDENTIALS, owner_id=source.id)
        return source

    async def add_copy(self, source: Source, *, title_id: uuid.UUID) -> str:
        external_id = f"emby-{source.name}-{title_id}"
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=source.id,
                    external_id=external_id,
                    title_id=title_id,
                    episode_id=None,
                    container="mkv",
                    video_codec="hevc",
                    audio_codec=None,
                    width=None,
                    height=None,
                    hdr_format=None,
                    audio_channels=None,
                    file_size_bytes=1,
                    runtime_seconds=None,
                    added_at=None,
                    last_seen_at=SEEN_AT,
                )
            ]
        )
        return external_id


@pytest.fixture
def household() -> _Household:
    return _Household()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key=SECRET_KEY,
        push_enabled=False,
        worker_enabled=False,
    )


@pytest.fixture
def app(household: _Household, settings: Settings) -> FastAPI:
    """The shipped app, with the playback ports replaced by `household` and
    `GET /home` wired over an otherwise-empty `Library` -- the row-cache pin
    needs both routers live in one app so `RowCache` can be observed across
    both.

    `get_row_cache` is deliberately **not** overridden: `app.state.row_cache`
    is the one this file reads back, which is what makes the structural sweep
    over `app.state` mean anything -- a cache built only for the test would
    prove nothing about what the shipped app actually shares between the two
    routers.
    """
    built = create_app(settings)
    built.dependency_overrides[get_title_repository] = lambda: household.titles
    built.dependency_overrides[get_media_item_repository] = lambda: household.media_items
    built.dependency_overrides[get_source_repository] = lambda: household.sources
    built.dependency_overrides[get_credential_store] = lambda: household.credentials
    built.dependency_overrides[get_source_adapter_factory] = lambda: household.factory
    built.dependency_overrides[get_row_context] = lambda: Library().context()
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _direct_target(url: str = DIRECT_URL) -> StreamTarget:
    return StreamTarget(kind=StreamTargetKind.DIRECT, url=url, container="mkv")


# -- pin 1: the RFC 9457 detail ------------------------------------------


async def test_the_503_detail_never_carries_the_upstream_messages_own_token(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """ADR-0012's first named leak surface: an RFC 9457 `detail` built from
    an upstream's own message.

    The fake raises `PortUnavailable` whose message *contains* the tiny URL,
    deliberately -- exactly what a real transport error does when it quotes
    the request it choked on. `PlaybackService._copy_targets` and
    `api/routers/playback.py`'s `_answer` both document that the client-facing
    `detail` is a fixed sentence plus the source's own name and never
    `str(exc)`; this is the case that proves it rather than trusts the
    docstring.
    """
    title_id = await household.add_title()
    source = await household.add_source()
    await household.add_copy(source, title_id=title_id)
    household.factory.script(
        source, error=PortUnavailable(f"connection refused fetching {DIRECT_URL}")
    )

    response = await client.post(f"/titles/{title_id}/play")

    assert response.status_code == 503
    body = response.json()
    # The positive control: the serializer ran and said something.
    assert body["code"] == "source_unavailable"
    assert body["detail"], "the premise: the 503 carries a detail at all"
    assert source.name in body["detail"]
    # The absence: the upstream's own message, quoting the tiny URL, never
    # reaches the client.
    assert TOKEN not in response.text
    assert DIRECT_URL not in response.text


# -- pin 2: RowCache -------------------------------------------------------


def _cache_shaped_dicts(app: FastAPI) -> list[tuple[str, dict[object, object]]]:
    """Every `dict`-valued attribute of every object `app.state` holds.

    A structural sweep rather than a hard-coded `row_cache` reach-in, per
    D5's acceptance bar: any object parked on `app.state` that keeps its own
    entries in a plain `dict` is "cache-shaped" for this purpose, so a second
    cache another group adds later is swept without this file naming it.
    `State.__setattr__` (Starlette) stores every attribute in one private
    dict, `_state` -- reading that rather than `getattr`-guessing names is
    what makes the sweep exhaustive over whatever the app actually holds.
    """
    state: dict[str, object] = app.state.__dict__.get("_state", {})
    found: list[tuple[str, dict[object, object]]] = []
    for name, value in state.items():
        attrs = getattr(value, "__dict__", None)
        if not isinstance(attrs, dict):
            continue
        for attr_name, attr_value in attrs.items():
            if isinstance(attr_value, dict):
                found.append((f"{name}.{attr_name}", attr_value))
    return found


async def test_the_row_cache_never_stores_a_token_or_a_ticket(
    client: httpx.AsyncClient, household: _Household, app: FastAPI
) -> None:
    """ADR-0012's second named leak surface, scoped to the cache this
    application actually holds: `RowCache` (`services/rows/cache.py:94`), a
    two-dict store of built rows and composed screens -- not a group-A HTTP
    cache over `GET /titles/{id}`, which does not exist (group A declines
    conditional GET there).

    Warms the cache through `GET /home` first -- the positive control is
    that `RowCache.size` actually grew and a screen entry exists for this
    request's user, so the "unchanged after play" assertion below is a
    statement about the cache rather than about a cache nothing ever wrote
    to. Then plays and redeems, and re-reads: `RowCache.size` must not have
    moved (nothing on the playback path writes to it), and a structural
    sweep of every dict-shaped attribute on `app.state` -- not just
    `row_cache`'s own two dicts -- must not carry the token or the ticket.
    """
    cache = app.state.row_cache
    assert cache.size == 0, "the premise: nothing has warmed the cache yet"

    home = await client.get("/home")
    assert home.status_code == 200
    warmed_size = cache.size
    assert warmed_size > 0, "the premise: GET /home actually populated the cache"
    assert cache.get_screen(USER.id) is not None, "the premise: this user's screen is cached"

    title_id = await household.add_title()
    source = await household.add_source()
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=[_direct_target()])

    played = await client.post(f"/titles/{title_id}/play")
    assert played.status_code == 200
    ticket_url = played.json()["targets"][0]["url"]
    assert ticket_url.startswith("http://test/stream/"), "the premise: a real ticket was minted"
    redeemed = await client.get(ticket_url)
    assert redeemed.status_code == 302, "the premise: the ticket actually redeemed"

    assert cache.size == warmed_size, "playback moved the row/screen cache"
    ticket = ticket_url.rsplit("/", 1)[-1]
    for label, mapping in _cache_shaped_dicts(app):
        for entry in mapping.values():
            rendered = repr(entry)
            assert TOKEN not in rendered, f"{label} carries the source token: {rendered!r}"
            assert ticket not in rendered, f"{label} carries the ticket: {rendered!r}"


# -- pin 4: the success body ------------------------------------------------


async def test_the_success_body_never_carries_the_source_url_the_ticket_replaced(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """The load-bearing fourth pin. ADR-0012 was written when `/play`'s
    response *was* a serialization of `StreamTarget` and the token in the
    body was the point -- with D3's ticket that is no longer true, and "the
    body carries no source URL" is now a property a regression could quietly
    reverse with nothing else noticing (an unsubstituted target still
    round-trips through every DTO field, still 200s, still looks like a
    working response).
    """
    title_id = await household.add_title()
    source = await household.add_source()
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=[_direct_target()])

    response = await client.post(f"/titles/{title_id}/play")

    assert response.status_code == 200
    body = response.json()
    urls = [target["url"] for target in body["targets"]]
    # The positive control: the serializer produced at least one real ticket
    # URL on this host, not an empty list satisfying every absence check for
    # free.
    assert urls, "the premise: the serializer produced targets"
    assert all(url.startswith("http://test/stream/") for url in urls)

    redeemed = await client.get(urls[0])
    assert redeemed.status_code == 302
    assert redeemed.headers["location"] == DIRECT_URL, (
        "the premise: the minted ticket redeems through the real cipher to exactly "
        "the tiny source URL"
    )

    # The absence: neither the token nor the whole URL's percent-encoded form
    # -- the shape a parameter-name-matching redaction sails past, per
    # `redact_query`'s own docstring -- appears anywhere in the body.
    assert TOKEN not in response.text
    assert quote(DIRECT_URL, safe="") not in response.text


# -- the structural pin -----------------------------------------------------


def _without_docstrings(tree: ast.Module) -> ast.Module:
    """`tree` with every docstring removed, so a name scan reads code only.
    Same helper as `tests/unit/test_api_rows.py`'s `_without_prose`, kept as
    its own copy here for the reason that file's own docstring gives for not
    sharing one: independence from a sibling test file's own scan."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return tree


def test_the_playback_dto_module_names_no_bulk_serializer() -> None:
    """ADR-0012 measured six bulk-dump paths -- `dataclasses.asdict`,
    `astuple`, `__dict__`, `vars()`, `json.dumps(asdict(...))`, and pydantic's
    `TypeAdapter(StreamTarget).dump_json`/`dump_python` -- all returning
    `StreamTarget.url` verbatim. `api/dto/playback.py`'s own module docstring
    argues at length that every field is named one at a time for exactly this
    reason, which is what makes a raw substring scan worthless here: the
    docstring itself names every one of these words. Scanned with docstrings
    stripped via `ast.unparse`, so a scan that would pass by "fixing" the
    explanation instead of the code fails honestly.
    """
    source = inspect.getsource(playback_dto)
    tree = ast.parse(source)
    code = ast.unparse(_without_docstrings(tree))
    assert "PlayTargetResponse" in code, "the docstring strip took the module with it"
    for forbidden in ("asdict", "astuple", "vars", "__dict__", "model_dump", "TypeAdapter"):
        assert forbidden not in code, f"api/dto/playback.py names {forbidden}"
