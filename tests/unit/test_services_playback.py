"""`PlaybackService` against port fakes. No network, no database, no cipher.

Three things about the fixtures here are load-bearing rather than
incidental, and each of them is a defect this repository has already paid
for once:

- **The mint is never the identity.** `PlaybackService` takes
  `mint: Callable[[str], str]`, so a case injecting `lambda url: url` would
  satisfy every leak assertion below while the service published the
  source's credential verbatim. `RecordingMint` hands back an opaque
  `tkt<n>a` that is demonstrably not the URL it was given, and hands back a
  *fresh* one on every call -- so an implementation that minted twice for
  one URL fails the "both targets redeem the same string" assertion instead
  of quietly passing it.
- **The URL is deliberately tiny.** `tests/unit/test_ports_source.py`'s
  redaction probe uses one for the reason recorded in ADR-0012: loguru
  truncates a rendered value at ~128 characters, and a realistic Emby
  direct-play URL is long enough that its trailing `api_key` falls off the
  end -- so a leak assertion built on a real URL passes whether or not the
  leak exists.
- **Every ordering case asserts its own premise.** The fake mints a
  `MediaItem`'s id at the moment it stores it, so id order is insertion
  order; a fixture that seeded the right answer first would pass against a
  service that returned the copies in physical order. The two ordering
  cases below seed the *wrong* answer first and assert that they did.
"""

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest
from pydantic import SecretStr

from tests.fakes.credential_store import FakeCredentialStore
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.source_repository import FakeSourceRepository
from usher.domain.enums import HdrFormat, SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.ingest import MediaItemUpsert
from usher.ports.source import (
    INFUSE_SCHEME,
    SourceAdapter,
    SourceAdapterFactory,
    StreamTarget,
    StreamTargetKind,
    wrap_deep_link,
)
from usher.services.playback import (
    PlaybackResolution,
    PlaybackService,
    PlaybackStatus,
    PlaybackTarget,
)

CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))

# The token an implementation must never publish, and a URL short enough
# that a leak assertion over a rendered target can actually see it -- see
# the module docstring.
TOKEN = "tok-Zq7"
DIRECT_URL = f"https://e/a.mkv?api_key={TOKEN}"
SECOND_URL = f"https://f/b.mkv?api_key={TOKEN}"

T0 = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
LATER = T0 + timedelta(hours=1)
# Past every `last_seen_at` any case seeds, so `mark_unseen_unavailable`
# retracts whatever it is pointed at.
AFTER_EVERYTHING = T0 + timedelta(days=365)


# -- fakes -------------------------------------------------------------


class RecordingMint:
    """A mint that is demonstrably not the identity, and not memoised.

    Both properties are deliberate. An identity mint passes every
    token-absence assertion in this file against a service that substitutes
    nothing; a memoised mint passes the "one ticket per distinct source URL"
    assertion against a service that mints per target. This one records
    every call and answers a fresh ticket each time, so the memoisation has
    to live in the service or two cases fail.

    The alphabet is unreserved on purpose: `quote(ticket, safe="")` is the
    identity for these strings, so the deep-link assertions test containment
    of the ticket rather than of an encoding.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        return f"tkt{len(self.calls)}a"


@dataclass
class _Script:
    """What one source's adapter does when asked for stream targets."""

    targets: list[StreamTarget] = field(default_factory=list)
    error: Exception | None = None


class RecordingFactory(SourceAdapterFactory):
    """Counts what the service builds, asks and closes, per source.

    A ledger rather than an inference: "exactly one adapter per copy, and
    every one closed" is satisfied by an implementation that closes twice as
    many as it built, and by one that builds none at all.
    """

    def __init__(self) -> None:
        self.built: list[uuid.UUID] = []
        self.closed: list[uuid.UUID] = []
        self.asked: list[tuple[uuid.UUID, str]] = []
        self.scripts: dict[uuid.UUID, _Script] = {}

    def script(
        self,
        source: Source,
        *,
        targets: list[StreamTarget] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.scripts[source.id] = _Script(targets=targets or [], error=error)

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        self.built.append(source.id)
        return _ScriptedAdapter(source, self, self.scripts.get(source.id) or _Script())


class _ScriptedAdapter(FakeSourceAdapter):
    """A `FakeSourceAdapter` whose `stream_targets` is scripted outright.

    Seeding items and letting the fake build a URL would test the fake's
    URL construction; every case here is about what the service does with
    the targets it is handed, including ones no real adapter would emit.
    """

    def __init__(self, source: Source, factory: RecordingFactory, script: _Script) -> None:
        super().__init__(source)
        self._factory = factory
        self._script = script

    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        self._factory.asked.append((self._source.id, external_id))
        if self._script.error is not None:
            raise self._script.error
        return list(self._script.targets)

    async def aclose(self) -> None:
        self._factory.closed.append(self._source.id)
        await super().aclose()


class _UndecryptableStore(FakeCredentialStore):
    """A store whose rows are present and unreadable -- what a rotated
    `USHER_SECRET_KEY` leaves behind. The same shape
    `tests/unit/test_services_sources.py` uses, and deliberately not a mode
    on `FakeCredentialStore` itself: the contract suite runs against that
    fake, and a store that can be told to fail its own contract is a fake
    with a mode nothing in `src/` can produce."""

    async def get(self, ref: str) -> SourceCredentials | None:
        raise PortDataMalformed(
            "stored source credentials could not be decrypted", detail=f"credentials_ref={ref}"
        )


# -- fixtures ----------------------------------------------------------


class _Household:
    """Two repositories, a credential store, a recording factory and a
    recording mint, wired the way the composition root will wire them."""

    def __init__(self, credentials: FakeCredentialStore | None = None) -> None:
        self.title_id = new_id()
        self.episode_id = new_id()
        self.sources = FakeSourceRepository()
        self.credentials = credentials or FakeCredentialStore()
        self.media_items = FakeMediaItemRepository()
        self.factory = RecordingFactory()
        self.mint = RecordingMint()

    async def add_source(self, name: str, *, with_credentials: bool = True) -> Source:
        source = Source(
            kind=SourceKind.EMBY,
            name=name,
            base_url=f"https://{name.lower().replace(' ', '-')}.invalid",
            credentials_ref=f"ref-{name}",
            device_id=str(new_id()),
        )
        await self.sources.add(source)
        if with_credentials:
            await self.credentials.put(source.credentials_ref, CREDENTIALS, owner_id=source.id)
        return source

    async def add_copy(
        self,
        source: Source,
        *,
        external_id: str,
        last_seen_at: datetime = T0,
        of_episode: bool = False,
    ) -> None:
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=source.id,
                    external_id=external_id,
                    title_id=self.title_id,
                    episode_id=self.episode_id if of_episode else None,
                    container="mkv",
                    video_codec="hevc",
                    audio_codec="truehd",
                    width=3840,
                    height=2160,
                    hdr_format=HdrFormat.HDR10,
                    audio_channels=8,
                    file_size_bytes=1,
                    runtime_seconds=9360,
                    added_at=None,
                    last_seen_at=last_seen_at,
                )
            ]
        )

    async def retract(self, source: Source) -> None:
        """Soft-delete this source's copies, the only way a row becomes
        `available = false` (PRD 02, ADR-0015). `upsert_many` cannot seed
        one: appearing in a walk *is* the evidence of availability."""
        await self.media_items.mark_unseen_unavailable(
            source.id, seen_since=AFTER_EVERYTHING, max_retract_fraction=1.0
        )

    def service(self) -> PlaybackService:
        return PlaybackService(
            self.media_items, self.sources, self.credentials, self.factory, self.mint
        )


def _emby_shaped_targets(url: str) -> list[StreamTarget]:
    """The two targets `build_stream_targets` produces for one item.

    Built with the same `wrap_deep_link` that function calls rather than by
    importing it, so the deep link is byte-identical without a service test
    reaching into `usher.adapters.emby`.
    """
    return [
        StreamTarget(
            kind=StreamTargetKind.DIRECT,
            url=url,
            container="mkv",
            video_codec="hevc",
            audio="truehd_atmos_7_1",
            hdr_format=HdrFormat.HDR10,
            resolution="3840x2160",
            runtime_seconds=9360,
            resume_position_seconds=1840,
        ),
        StreamTarget(
            kind=StreamTargetKind.DEEP_LINK,
            url=wrap_deep_link(url),
            scheme=INFUSE_SCHEME,
        ),
    ]


def _rendered(resolution: PlaybackResolution) -> str:
    """Every field of every returned target, as a string.

    `dataclasses.asdict` deliberately: ADR-0012 measured that it, `astuple`,
    `vars()`, `json.dumps(asdict(...))` and pydantic's `dump_json` all return
    `StreamTarget.url` in full -- the field-access paths `__repr__`'s
    redaction cannot close. A leak assertion written over `repr()` would be
    satisfied by the redaction rather than by the substitution.
    """
    return json.dumps([asdict(entry) for entry in resolution.targets], default=str)


# -- the ticket substitution -------------------------------------------


async def test_a_deep_link_carries_the_ticket_and_never_the_source_url() -> None:
    """The headline: both targets carry the ticket, neither carries the URL.

    Three assertions, and the first is a positive control -- an
    implementation that returned nothing at all would also have no token in
    its output, so "the direct target's url *is* the ticket" is what makes
    the two absence assertions evidence rather than decoration.

    The deep-link half asserts containment of the ticket itself. D1 measured
    that `quote(ticket, safe="")` is *not* a no-op in general (it re-encodes
    `=`, and only 32% of plaintext lengths mint an unpadded token), so a
    case written as if the encoding proved something would be asserting a
    fact about this fixture's alphabet rather than about the service.
    """
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.PLAYABLE
    assert [entry.target.kind for entry in resolution.targets] == [
        StreamTargetKind.DIRECT,
        StreamTargetKind.DEEP_LINK,
    ]
    direct, deep = (entry.target for entry in resolution.targets)
    ticket = "tkt1a"
    assert direct.url == ticket
    assert ticket in deep.url
    rendered = _rendered(resolution)
    assert TOKEN not in rendered
    assert DIRECT_URL not in rendered
    assert quote(DIRECT_URL, safe="") not in rendered


async def test_the_direct_target_keeps_every_fact_a_client_chooses_on() -> None:
    """Substituting the URL must not substitute the target.

    PRD 07: Usher "supplies complete information and never proxies bytes",
    and a client picks between targets on container, codec, HDR format and
    resolution. An implementation that rebuilt a bare `StreamTarget(kind,
    url)` would pass every leak assertion in this file.
    """
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))

    resolution = await household.service().for_title(household.title_id)

    direct = resolution.targets[0].target
    assert direct.container == "mkv"
    assert direct.video_codec == "hevc"
    assert direct.audio == "truehd_atmos_7_1"
    assert direct.hdr_format is HdrFormat.HDR10
    assert direct.resolution == "3840x2160"
    assert direct.runtime_seconds == 9360
    assert direct.resume_position_seconds == 1840
    assert resolution.targets[1].target.scheme == INFUSE_SCHEME


async def test_one_ticket_is_minted_per_distinct_source_url() -> None:
    """ "Both targets redeem the same string" is a claim about the mint.

    The fixture's mint answers a fresh ticket every call, so a service that
    minted per *target* would hand the deep link a ticket the direct target
    does not carry -- and the deep-link containment assertion above would
    already fail. This one pins the count directly, because the memoisation
    is also what stops one play spending two encryptions per copy.
    """
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))

    await household.service().for_title(household.title_id)

    assert household.mint.calls == [DIRECT_URL]


async def test_a_deep_link_is_paired_by_containment_and_not_by_position() -> None:
    """The pairing is keyed, and the adapter's order is scrambled to say so.

    This repository has recorded the positional failure twice --
    `SourceEvent.watch_states` is "keyed by `external_id` rather than
    aligned by position", and M5's `zip` of a matched subset against a whole
    batch published item A's position under item B's id
    (`services/push.py:203-219`). The deep link here arrives *first* and
    wraps the *second* direct URL, so any implementation that pairs by index
    hands it the wrong ticket.
    """
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")
    first, second = _emby_shaped_targets(DIRECT_URL)[0], _emby_shaped_targets(SECOND_URL)[0]
    deep_for_second = StreamTarget(
        kind=StreamTargetKind.DEEP_LINK, url=wrap_deep_link(SECOND_URL), scheme=INFUSE_SCHEME
    )
    household.factory.script(source, targets=[deep_for_second, first, second])

    resolution = await household.service().for_title(household.title_id)

    # Premise: the two directs got different tickets, so "carries the right
    # one" is a distinguishable claim.
    by_kind = {entry.target.kind: entry.target for entry in resolution.targets}
    directs = [
        entry.target for entry in resolution.targets if entry.target.kind is StreamTargetKind.DIRECT
    ]
    assert len(directs) == 2
    assert directs[0].url != directs[1].url
    ticket_for_second = directs[1].url
    assert ticket_for_second in by_kind[StreamTargetKind.DEEP_LINK].url
    assert directs[0].url not in by_kind[StreamTargetKind.DEEP_LINK].url
    rendered = _rendered(resolution)
    assert TOKEN not in rendered
    assert quote(SECOND_URL, safe="") not in rendered


async def test_a_deep_link_that_wraps_no_visible_direct_url_is_dropped() -> None:
    """Passing it through publishes exactly the token the ticket hides.

    The adapter returns `[deep, direct, direct2]` where the deep link wraps
    a URL neither direct target carries. Dropped, not paired: an
    implementation that fell back on position would emit three targets and
    fail the count, and one that passed the deep link through unchanged
    would fail the token-absence assertion.
    """
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")
    invisible = f"https://g/c.mkv?api_key={TOKEN}"
    orphan = StreamTarget(
        kind=StreamTargetKind.DEEP_LINK, url=wrap_deep_link(invisible), scheme=INFUSE_SCHEME
    )
    first, second = _emby_shaped_targets(DIRECT_URL)[0], _emby_shaped_targets(SECOND_URL)[0]
    household.factory.script(source, targets=[orphan, first, second])

    resolution = await household.service().for_title(household.title_id)

    assert [entry.target.kind for entry in resolution.targets] == [
        StreamTargetKind.DIRECT,
        StreamTargetKind.DIRECT,
    ]
    rendered = _rendered(resolution)
    assert TOKEN not in rendered
    assert quote(invisible, safe="") not in rendered


async def test_a_deep_link_is_paired_across_the_source_that_produced_it() -> None:
    """Two sources, two tickets, and neither deep link may cross over.

    The memoisation is one dict for the whole resolution, which is what lets
    a containment match reach a URL another copy produced -- and is exactly
    why the match has to be by the longest containment rather than by any.
    """
    household = _Household()
    first = await household.add_source("Attic Emby")
    second = await household.add_source("Living Room Emby")
    await household.add_copy(first, external_id="e1")
    await household.add_copy(second, external_id="e2")
    household.factory.script(first, targets=_emby_shaped_targets(DIRECT_URL))
    household.factory.script(second, targets=_emby_shaped_targets(SECOND_URL))

    resolution = await household.service().for_title(household.title_id)

    per_source: dict[str, list[StreamTarget]] = {
        entry.source_name: [] for entry in resolution.targets
    }
    for entry in resolution.targets:
        per_source[entry.source_name].append(entry.target)
    for targets in per_source.values():
        direct, deep = targets
        assert direct.url in deep.url
    assert per_source["Attic Emby"][0].url != per_source["Living Room Emby"][0].url


async def test_a_prefix_of_another_copys_url_does_not_steal_its_deep_link() -> None:
    """Containment alone is ambiguous when one URL is a prefix of another.

    Two tokens where one is a prefix of the other -- `tok-Zq7` and
    `tok-Zq77` -- is not a contrived shape: Emby session tokens are a fixed
    alphabet with no delimiter after them, and the longer URL's
    percent-encoded form contains the shorter one's in full. The pairing
    therefore takes the *longest* match, and this case fails against a
    first-match implementation.
    """
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")
    short = f"https://e/a.mkv?api_key={TOKEN}"
    longer = f"https://e/a.mkv?api_key={TOKEN}7"
    assert quote(short, safe="") in quote(longer, safe="")
    household.factory.script(
        source,
        targets=[
            _emby_shaped_targets(short)[0],
            _emby_shaped_targets(longer)[0],
            StreamTarget(
                kind=StreamTargetKind.DEEP_LINK, url=wrap_deep_link(longer), scheme=INFUSE_SCHEME
            ),
        ],
    )

    resolution = await household.service().for_title(household.title_id)

    directs = [
        entry.target for entry in resolution.targets if entry.target.kind is StreamTargetKind.DIRECT
    ]
    deep = next(
        entry.target
        for entry in resolution.targets
        if entry.target.kind is StreamTargetKind.DEEP_LINK
    )
    assert directs[1].url in deep.url
    assert directs[0].url not in deep.url


# -- three outcomes, not two -------------------------------------------


async def test_a_second_source_serves_when_the_first_raises() -> None:
    """A partial degradation is still an answer.

    PRD 08: "a degraded subsystem narrows functionality; it never fails a
    request local state can answer." One source down and another holding the
    file is a narrower answer, not a 503.
    """
    household = _Household()
    down = await household.add_source("Attic Emby")
    up = await household.add_source("Living Room Emby")
    await household.add_copy(down, external_id="e1")
    await household.add_copy(up, external_id="e2")
    household.factory.script(down, error=PortUnavailable(f"GET /Items failed: {TOKEN}"))
    household.factory.script(up, targets=_emby_shaped_targets(SECOND_URL))

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.PLAYABLE
    assert {entry.source_name for entry in resolution.targets} == {"Living Room Emby"}
    assert TOKEN not in _rendered(resolution)


async def test_every_source_raising_is_unavailable_rather_than_an_empty_list() -> None:
    """The other half of the pair above, and it needs its own case.

    "A second source serves when the first raises" is satisfied by an
    implementation that swallows every error and answers with whatever it
    collected -- which for a household whose sources are all down is an
    empty list, i.e. "you do not own this". That is the wrong answer and the
    one PRD 07's worked example of an RFC 9457 envelope exists for.
    """
    household = _Household()
    first = await household.add_source("Attic Emby")
    second = await household.add_source("Living Room Emby")
    await household.add_copy(first, external_id="e1")
    await household.add_copy(second, external_id="e2")
    household.factory.script(first, error=PortUnavailable("connect timed out"))
    household.factory.script(second, error=PortUnavailable("connect timed out"))

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.UNAVAILABLE
    assert resolution.targets == ()


async def test_a_source_answering_with_no_targets_is_not_playable() -> None:
    """`[]` is "no way to play this", explicitly not an error.

    `SourceAdapter.stream_targets` documents it -- a series or season
    folder, or a media source with no container -- and the two outcomes have
    to be distinguishable by the value the route branches on rather than by
    a message, because a message is not a branch.
    """
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")
    household.factory.script(source, targets=[])

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.NOT_PLAYABLE
    assert resolution.targets == ()
    # Nothing for a route to read prose out of: the two empty-handed outcomes
    # differ by the value alone. `assert status is not UNAVAILABLE` was the
    # obvious spelling and mypy refuses it as a non-overlapping identity
    # check -- it is already narrowed by the line above, so it could not fail.
    assert resolution.detail is None


async def test_a_household_holding_no_copy_at_all_is_not_playable() -> None:
    """The catalog holds 1,271,138 titles and the one measured source holds
    1,126,789 items, so "on no source" is the ordinary answer rather than an
    error -- and nothing is built, because there is nothing to ask."""
    household = _Household()
    await household.add_source("Living Room Emby")

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.NOT_PLAYABLE
    assert household.factory.built == []


async def test_a_failure_beside_an_empty_answer_is_unavailable() -> None:
    """The case the plan leaves open, decided here so the route inherits it.

    One source answers `[]` and another is down. "Not playable" is a claim
    about the household's whole holding and can only be made from a complete
    answer; a source that raised is a source that did not answer, so the
    claim is unsupported and the honest reply is the retryable one.
    """
    household = _Household()
    empty = await household.add_source("Attic Emby")
    down = await household.add_source("Living Room Emby")
    await household.add_copy(empty, external_id="e1")
    await household.add_copy(down, external_id="e2")
    household.factory.script(empty, targets=[])
    household.factory.script(down, error=PortUnavailable("connect timed out"))

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.UNAVAILABLE
    assert resolution.detail is not None
    assert "Living Room Emby" in resolution.detail
    assert "Attic Emby" not in resolution.detail


async def test_a_malformed_payload_from_one_copy_does_not_abort_the_others() -> None:
    """`except UsherPortError`, not `except PortUnavailable`.

    A source that answered with a payload this adapter could not parse has
    failed *that copy*; narrowing the clause would let the exception escape
    the loop and turn a household's other, working source into a 500.
    """
    household = _Household()
    malformed = await household.add_source("Attic Emby")
    up = await household.add_source("Living Room Emby")
    await household.add_copy(malformed, external_id="e1")
    await household.add_copy(up, external_id="e2")
    household.factory.script(malformed, error=PortDataMalformed("MediaSources was not a list"))
    household.factory.script(up, targets=_emby_shaped_targets(SECOND_URL))

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.PLAYABLE
    assert {entry.source_name for entry in resolution.targets} == {"Living Room Emby"}


async def test_a_source_with_no_stored_credentials_is_unavailable() -> None:
    """A misconfigured source cannot serve, and cannot answer "you do not
    own this" on the household's behalf either. Answered without building an
    adapter, exactly as `SourceService.status` does: there is nothing to
    authenticate with, so a probe could only spend a 1-5 s upstream round
    trip to learn what local state already knows."""
    household = _Household()
    source = await household.add_source("Living Room Emby", with_credentials=False)
    await household.add_copy(source, external_id="e1")

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.UNAVAILABLE
    assert household.factory.built == []


async def test_a_credential_that_no_longer_decrypts_is_unavailable() -> None:
    """A rotated `USHER_SECRET_KEY`, or a row restored from a backup taken
    under a different one. `CredentialStore.get` raises `PortDataMalformed`
    for this rather than answering `None`, and the raise must not escape as
    a 500."""
    household = _Household(credentials=_UndecryptableStore())
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.UNAVAILABLE
    assert household.factory.built == []


async def test_a_copy_whose_source_row_has_gone_is_skipped_not_failed() -> None:
    """`media_items.source_id` is `ON DELETE CASCADE`, so a source deleted
    between the two reads leaves a copy naming a row that is already gone.
    `TitleReadService` renders that as "Unknown source"; here there is
    nothing to build an adapter from, and calling it a failure would report
    a 503 for a row that is on its way out anyway."""
    household = _Household()
    ghost = Source(
        kind=SourceKind.EMBY,
        name="Deleted Emby",
        base_url="https://gone.invalid",
        credentials_ref="ref-gone",
        device_id=str(new_id()),
    )
    await household.add_copy(ghost, external_id="e1")

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.NOT_PLAYABLE
    assert household.factory.built == []


# -- the detail an operator reads --------------------------------------


async def test_the_unavailable_detail_names_the_source_and_not_the_exception() -> None:
    """A fixed sentence plus the source's name, never `str(exc)`.

    `SourceService.status` draws the same line for the same reason: an
    upstream's own message quotes what it choked on, and what it choked on
    here is a URL with a token in it. The fake raises with the token in its
    message deliberately, so an implementation that interpolated the
    exception fails on the token rather than on taste.
    """
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")
    household.factory.script(source, error=PortUnavailable(f"GET {DIRECT_URL} failed"))

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.UNAVAILABLE
    assert resolution.detail is not None
    assert "Living Room Emby" in resolution.detail
    assert TOKEN not in resolution.detail
    assert DIRECT_URL not in resolution.detail


async def test_the_detail_names_every_source_that_failed_once_each() -> None:
    """Two copies on one source that is down must not name it twice, and a
    second failing source must not be silently dropped from the sentence an
    operator reads."""
    household = _Household()
    first = await household.add_source("Attic Emby")
    second = await household.add_source("Living Room Emby")
    await household.add_copy(first, external_id="e1")
    await household.add_copy(first, external_id="e2")
    await household.add_copy(second, external_id="e3")
    household.factory.script(first, error=PortUnavailable("connect timed out"))
    household.factory.script(second, error=PortUnavailable("connect timed out"))

    resolution = await household.service().for_title(household.title_id)

    assert resolution.detail is not None
    assert resolution.detail.count("Attic Emby") == 1
    assert resolution.detail.count("Living Room Emby") == 1


# -- ranking -----------------------------------------------------------


async def test_an_available_copy_is_offered_before_an_unavailable_one() -> None:
    """PRD 02's soft delete means a retracted copy may still play, so an
    unavailable one is a fallback rather than an exclusion -- and the
    fixture is built so that neither `ORDER BY id` nor `ORDER BY
    last_seen_at` could produce the right answer by accident.

    The retracted copy is seeded *first*, so it holds the lower UUIDv7, and
    it is given the *newer* `last_seen_at`. Only `available DESC` puts the
    available copy in front. That is the shape M7 was missing when five
    orderings went untested.
    """
    household = _Household()
    retracted = await household.add_source("Attic Emby")
    live = await household.add_source("Living Room Emby")
    await household.add_copy(retracted, external_id="e1", last_seen_at=LATER)
    await household.retract(retracted)
    await household.add_copy(live, external_id="e2", last_seen_at=T0)
    household.factory.script(retracted, targets=_emby_shaped_targets(DIRECT_URL))
    household.factory.script(live, targets=_emby_shaped_targets(SECOND_URL))

    copies = {
        copy.source_id: copy
        for copy in await household.media_items.list_for_title(household.title_id)
    }
    # Premises, all three: the ordering keys genuinely disagree, so this
    # case cannot pass by physical order or by recency.
    assert copies[retracted.id].available is False
    assert copies[live.id].available is True
    assert copies[retracted.id].id < copies[live.id].id
    assert copies[retracted.id].last_seen_at > copies[live.id].last_seen_at

    resolution = await household.service().for_title(household.title_id)

    assert [entry.source_name for entry in resolution.targets][:1] == ["Living Room Emby"]
    assert {entry.source_name for entry in resolution.targets} == {
        "Living Room Emby",
        "Attic Emby",
    }


async def test_a_household_holding_only_unavailable_copies_still_gets_targets() -> None:
    """A sweep that over-retracted must not be able to tell a household it
    owns nothing. `mark_unseen_unavailable` sets `available = false` and
    deletes nothing (ADR-0015), and the file is very often still there."""
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1")
    await household.retract(source)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))

    copies = await household.media_items.list_for_title(household.title_id)
    assert [copy.available for copy in copies] == [False]

    resolution = await household.service().for_title(household.title_id)

    assert resolution.status is PlaybackStatus.PLAYABLE
    assert len(resolution.targets) == 2


async def test_the_freshest_of_two_available_copies_is_offered_first() -> None:
    """The second ordering key, with its own premise: the stale copy is
    seeded first and therefore holds the lower id, so `ORDER BY id` and
    `ORDER BY last_seen_at DESC` disagree and only the second is right."""
    household = _Household()
    stale = await household.add_source("Attic Emby")
    fresh = await household.add_source("Living Room Emby")
    await household.add_copy(stale, external_id="e1", last_seen_at=T0)
    await household.add_copy(fresh, external_id="e2", last_seen_at=LATER)
    household.factory.script(stale, targets=_emby_shaped_targets(DIRECT_URL))
    household.factory.script(fresh, targets=_emby_shaped_targets(SECOND_URL))

    copies = {
        copy.source_id: copy
        for copy in await household.media_items.list_for_title(household.title_id)
    }
    assert copies[stale.id].id < copies[fresh.id].id
    assert copies[stale.id].last_seen_at < copies[fresh.id].last_seen_at

    resolution = await household.service().for_title(household.title_id)

    assert next(entry.source_name for entry in resolution.targets) == "Living Room Emby"


# -- adapter lifetime ---------------------------------------------------


async def test_exactly_one_adapter_is_built_per_copy_and_every_one_is_closed() -> None:
    """One adapter is one connection pool, and `/play` is a client route.

    `SourceService.status`' comment is the reason verbatim: "a status
    endpoint a dashboard polls would otherwise leak one per call". Asserted
    against the factory's ledger rather than inferred from the answer --
    an implementation that closed nothing produces an identical response.
    """
    household = _Household()
    first = await household.add_source("Attic Emby")
    second = await household.add_source("Living Room Emby")
    await household.add_copy(first, external_id="e1")
    await household.add_copy(first, external_id="e2")
    await household.add_copy(second, external_id="e3")
    household.factory.script(first, targets=_emby_shaped_targets(DIRECT_URL))
    household.factory.script(second, targets=_emby_shaped_targets(SECOND_URL))

    await household.service().for_title(household.title_id)

    assert len(household.factory.built) == 3
    assert sorted(household.factory.closed) == sorted(household.factory.built)
    assert {external_id for _, external_id in household.factory.asked} == {"e1", "e2", "e3"}


async def test_an_adapter_whose_source_raised_is_closed_too() -> None:
    """The `finally`, and the mutation it exists for. Moving `aclose()` out
    of it leaks exactly one connection pool per unreachable source, on the
    route a client retries."""
    household = _Household()
    down = await household.add_source("Attic Emby")
    up = await household.add_source("Living Room Emby")
    await household.add_copy(down, external_id="e1")
    await household.add_copy(up, external_id="e2")
    household.factory.script(down, error=PortUnavailable("connect timed out"))
    household.factory.script(up, targets=_emby_shaped_targets(SECOND_URL))

    await household.service().for_title(household.title_id)

    assert household.factory.built == [down.id, up.id]
    assert sorted(household.factory.closed) == sorted([down.id, up.id])


# -- episodes -----------------------------------------------------------


async def test_an_episode_is_resolved_through_its_own_copies() -> None:
    """`list_for_episode`, not `list_for_title`.

    `list_for_title` carries `AND episode_id IS NULL`, which is exactly what
    makes it useless here -- an episode's row is precisely one of the rows
    that clause excludes. A service that reached for the title read would
    answer "not playable" for every episode in the catalog, and 999,927 of
    the one measured library's 1,126,789 items are episodes.
    """
    household = _Household()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, external_id="e1", of_episode=True)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))

    # Premise: the same title read answers nothing for this copy, so the
    # case cannot pass against an implementation that used it.
    assert await household.media_items.list_for_title(household.title_id) == []

    resolution = await household.service().for_episode(household.episode_id)

    assert resolution.status is PlaybackStatus.PLAYABLE
    assert resolution.targets[0].target.url == "tkt1a"
    assert TOKEN not in _rendered(resolution)


async def test_an_episode_with_no_copy_is_not_playable() -> None:
    household = _Household()
    await household.add_source("Living Room Emby")

    resolution = await household.service().for_episode(household.episode_id)

    assert resolution.status is PlaybackStatus.NOT_PLAYABLE
    assert household.factory.built == []


# -- the outcome DTO ----------------------------------------------------


def test_targets_and_status_cannot_disagree() -> None:
    """`PlaybackResolution` refuses the two states a route could not render:
    a playable answer with nothing to play, and a failed one carrying
    targets. The same shape `SourceStatus.__post_init__` uses, and for the
    same reason -- the invariant belongs on the DTO rather than in every
    caller that branches on it."""
    one = PlaybackTarget(
        source_id=new_id(),
        source_name="Living Room Emby",
        target=StreamTarget(kind=StreamTargetKind.DIRECT, url="tkt1a"),
    )
    # The premise: the pairing this DTO does accept is constructible, so the
    # two refusals below are about the invariant rather than about the
    # arguments being wrong.
    assert PlaybackResolution(status=PlaybackStatus.PLAYABLE, targets=(one,)).targets == (one,)
    for status in (PlaybackStatus.UNAVAILABLE, PlaybackStatus.NOT_PLAYABLE):
        with pytest.raises(ValueError, match="targets"):
            PlaybackResolution(status=status, targets=(one,))
    with pytest.raises(ValueError, match="targets"):
        PlaybackResolution(status=PlaybackStatus.PLAYABLE)
