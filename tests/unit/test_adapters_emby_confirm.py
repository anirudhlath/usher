"""`EmbyAdapter.confirm_gone` on the wire: its rounds, its chunks, and what it will not read."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from tests.fakes.emby_server import FakeEmbyServer
from usher.adapters.emby.adapter import IDS_PER_REQUEST, EmbyAdapter
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortDataMalformed
from usher.ports.source import SourceItem, SourceItemKind

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
SOURCE = Source(
    id=new_id(),
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-1",
    device_id=str(new_id()),
)


def _movie(external_id: str) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=f"Movie {external_id}",
        kind=SourceItemKind.MOVIE,
        year=2021,
        container="mkv",
    )


def _adapter(
    server: FakeEmbyServer, answer: dict[str, object] | None = None
) -> tuple[EmbyAdapter, list[httpx.Request]]:
    """An adapter over `server`, every request it sends, and an optional canned `Ids` answer."""
    seen: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        # A turn of the loop per request, as a real one gives, so a deadline can fire.
        await asyncio.sleep(0)
        seen.append(request)
        if answer is not None and "Ids" in request.url.params:
            return httpx.Response(200, json=answer)
        return server.handle(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url=SOURCE.base_url)
    return EmbyAdapter(SOURCE, CREDENTIALS, client=client), seen


def _asked(seen: list[httpx.Request]) -> list[str]:
    """Each `Ids` request's ids, in the order the requests were sent."""
    return [request.url.params["Ids"] for request in seen if "Ids" in request.url.params]


async def test_a_served_version_its_sibling_hid_is_asked_again_and_kept() -> None:
    """One `Ids` request lists one version of a group, so the next round asks for the other."""
    server = FakeEmbyServer()
    for version in ("v0", "v1"):
        server.add_item(_movie(version), T0)
    server.group_versions("v0", "v1")
    adapter, seen = _adapter(server)
    try:
        gone = await adapter.confirm_gone(["v1", "v0"])
    finally:
        await adapter.aclose()
    assert gone == frozenset()
    assert _asked(seen) == ["v0,v1", "v1"]


async def test_an_id_nobody_asked_for_is_never_read_as_listed() -> None:
    """A listing naming only a stray id lists none of what was asked, so the asked id is gone.

    Counted as listed, the stray would keep every round non-empty, and the rounds would
    never end: hence the deadline.
    """
    stray = {"Items": [{"Id": "stray-1"}], "TotalRecordCount": 1}
    adapter, seen = _adapter(FakeEmbyServer(), answer=stray)
    try:
        async with asyncio.timeout(5):
            gone = await adapter.confirm_gone(["gone-1"])
    finally:
        await adapter.aclose()
    assert gone == {"gone-1"}
    assert _asked(seen) == ["gone-1"]


async def test_a_hundred_and_one_ids_go_as_two_requests_each_limited_to_its_chunk() -> None:
    """`Limit` is each chunk's own length: a default page would cut a full chunk short."""
    asked = [f"gone-{index:03d}" for index in range(IDS_PER_REQUEST + 1)]
    adapter, seen = _adapter(FakeEmbyServer())
    try:
        gone = await adapter.confirm_gone(asked)
    finally:
        await adapter.aclose()
    assert gone == frozenset(asked)
    limits = [request.url.params.get("Limit") for request in seen if "Ids" in request.url.params]
    assert limits == [str(IDS_PER_REQUEST), "1"]


async def test_a_body_without_items_raises_rather_than_reading_as_gone() -> None:
    adapter, _ = _adapter(FakeEmbyServer(), answer={"TotalRecordCount": 0})
    try:
        with pytest.raises(PortDataMalformed, match="no Items array"):
            await adapter.confirm_gone(["kept-1"])
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "entry",
    [{"Name": "Movie kept-1"}, {"id": "kept-1"}, {"Id": 1}, "kept-1"],
    ids=["no-id", "lowercase-id", "integer-id", "bare-string"],
)
async def test_an_entry_without_a_string_id_raises_rather_than_reading_as_gone(
    entry: object,
) -> None:
    """An entry with no readable `Id` may be the asked one, so the round cannot decide."""
    adapter, _ = _adapter(FakeEmbyServer(), answer={"Items": [entry], "TotalRecordCount": 1})
    try:
        with pytest.raises(PortDataMalformed, match="no string Id"):
            await adapter.confirm_gone(["kept-1"])
    finally:
        await adapter.aclose()


async def test_a_confirmation_is_timed_as_confirm_and_spanned_with_its_counts() -> None:
    """Timed as `list`, a hundred-id read would share a bucket with pages of a thousand."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    server = FakeEmbyServer()
    server.add_item(_movie("kept-1"), T0)
    adapter, seen = _adapter(server)
    try:
        gone = await adapter.confirm_gone(["kept-1", "gone-1"])
    finally:
        await adapter.aclose()
    assert gone == {"gone-1"}
    reads = [request for request in seen if request.url.path != "/Users/AuthenticateByName"]
    assert len(reads) == 2, "the premise: one round listed kept-1, the next listed nothing"
    spans = exporter.get_finished_spans()
    labels = [
        span.attributes["usher.op"]
        for span in spans
        if span.name == "source.request" and span.attributes is not None
    ]
    assert labels == ["confirm", "confirm"]
    [confirming] = [span for span in spans if span.name == "source.confirm_gone"]
    assert confirming.attributes is not None
    counts = (
        confirming.attributes["usher.confirm.asked"],
        confirming.attributes["usher.confirm.gone"],
    )
    assert counts == (2, 1)
