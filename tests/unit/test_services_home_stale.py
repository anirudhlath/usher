"""Serve-stale-while-refreshing on the composed screen (PRD 06).

PRD 06:980 has said since M7 that rows are *"recomputed lazily and served
stale while refreshing, so the home screen never blocks on a slow row"*, and
M7 corrected the sentence rather than half-implementing it. This file is the
implementation's own suite, and its subject is the three constraints in the
feature's name, each of which has a different failure:

- **the screen never waits on it** -- a request that can end up awaiting the
  refresh is a latency regression wearing a cache's clothes, and it is
  *invisible* to any fixture whose refresher happens not to block. Asserted by
  driving `compose` by hand: `coro.send(None)` must raise `StopIteration`,
  which a coroutine that suspended anywhere cannot do. That is the shape
  `tests/unit/test_api_lanes.py` established for `LaneSupervisor.start`, and
  it is here for the same reason recorded in
  `.claude/rules/api-telemetry-and-lanes.md`: a deadlock-shaped case can only
  ever report a timeout, and a timeout is indistinguishable from a slow box.
- **serve stale** -- something decides how stale is too stale.
  `SCREEN_STALE_GRACE` is that bound, it sits beside `_SCREEN_TTL` where a
  reader looking up the 30 s finds it, and past `TTL + grace` an entry is a
  hard miss that is never served. Asserted by stepping the clock **exactly
  onto** the second boundary, which is the habit M5's surviving `stale_after`
  `<=` -> `<` mutation exists to teach.
- **the refresh is bounded** -- an unbounded background refresh is how a cache
  stampede melts the box, so the handover is a *bounded, deduplicating* queue
  and full means dropped rather than blocked. Dropping is safe because an
  entry past `TTL + grace` is a hard miss, so a dropped refresh degrades to
  the cost M7 already pays.

**The refresher is a synchronous callable, and that is the strongest available
spelling of the first constraint.** `Callable[[User], None]` has nothing to
await, so `await self._refresh(...)` -- the whole defect this task exists to
prevent -- is a mypy error at the gate rather than a case that has to be
lucky. `RowContext.affinities` is the precedent for a callable field on the
services side (`ports/rows.py`), and it is awaited because its *value* is the
product of three statements; this one hands a key over and returns.

**The grace window is gated on there being a refresher**, which is what keeps
the other half of the trade honest: a composer that opened the window with
nothing behind it would serve a stale screen and never replace it, which is
strictly worse than the miss it was avoiding. `usher home` is exactly that
caller and passes none.
"""

import asyncio
import dataclasses
import datetime as dt
import uuid
from collections.abc import Coroutine
from typing import Any

import pytest

from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.unit.rows import Library
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.rows import BuiltRow, RowCard
from usher.domain.watch import User
from usher.ports.rows import RowContext, ScoredRow
from usher.services.home import _SCREEN_TTL, SCREEN_STALE_GRACE, HomeService
from usher.services.rows.cache import RefreshQueue, RowCache

_START = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.UTC)


class _Clock:
    """A clock that only moves when a case moves it.

    `test_services_rows_cache.py`'s own fixture, copied rather than imported
    for the reason `test_telemetry_cache.py` copied it: a suite whose expiry
    boundaries depend on a sibling file's internals is a suite that breaks for
    reasons that have nothing to do with its subject.
    """

    def __init__(self) -> None:
        self.now = _START

    def advance(self, delta: dt.timedelta) -> None:
        self.now += delta

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def ctx() -> RowContext:
    return Library().context()


def _card(name: str) -> RowCard:
    return RowCard(
        title_id=uuid.uuid4(),
        kind=TitleKind.MOVIE,
        name=name,
        enrichment_state=EnrichmentState.SKELETON,
    )


def _provider(slug: str = "recently-added") -> FakeRowProvider:
    return FakeRowProvider(
        proposals=(ScoredRow(row=FakeRow(slug, cards=(_card(slug),)), score=0.9),),
        slug_prefix=slug,
    )


def _builds(provider: FakeRowProvider) -> int:
    row = provider.rows[0]
    assert isinstance(row, FakeRow)
    return row.builds


def _without_suspending(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive `coro` one step and require it to have finished.

    **This is the case with teeth for "the screen never waits on it."** A
    coroutine that reached any suspension point hands back a future here
    instead of raising `StopIteration`, and it does so *deterministically* --
    where a case that awaited a refresher parked on a gate could only ever
    report a timeout, which is what a slow box also looks like. Closed on the
    failure path so the failure is one assertion rather than one assertion and
    a "coroutine was never awaited" warning.
    """
    try:
        coro.send(None)
    except StopIteration as finished:
        return finished.value
    coro.close()
    raise AssertionError(
        "compose suspended before returning: the request awaited the refresh, "
        "which is the latency regression serve-stale exists to avoid"
    )


# -- the constraint in the middle of the title ------------------------------


async def test_a_stale_screen_is_served_without_waiting_for_the_refresh(
    ctx: RowContext, clock: _Clock
) -> None:
    """The clock stepped **onto** `_SCREEN_TTL` -- expired, and inside the
    grace window -- and a refresh queue nothing is draining, so the refresh
    that gets scheduled can never complete during this case.

    At HEAD this fails because an expired entry is popped and the request pays
    a full compose: `_builds` is 2 and the provider was re-proposed. Against a
    naive implementation that awaits the refresh it fails at
    `_without_suspending`, by construction rather than by timing out.
    """
    cache = RowCache(clock=clock)
    queue = RefreshQueue()
    provider = _provider()
    service = HomeService(providers=[provider], cache=cache, refresh=queue.schedule)

    warm = await service.compose(ctx)
    clock.advance(_SCREEN_TTL)

    served = _without_suspending(service.compose(ctx))

    assert served == warm, "the stale screen itself must come back, not a rebuild"
    assert _builds(provider) == 1, "the refresh must not have run on the request's turn"
    assert len(provider.contexts) == 1, "a stale serve must not re-propose either"
    assert queue.depth == 1, "the key must have been handed to the refresher"
    assert queue.pending == frozenset({ctx.user.id})


async def test_a_fresh_screen_schedules_no_refresh(ctx: RowContext, clock: _Clock) -> None:
    """The control the case above needs. Without it, a composer that scheduled
    a refresh on *every* read -- turning one request per TTL into one refresh
    per request, which is the stampede the bound exists to prevent -- passes
    every assertion in this file."""
    cache = RowCache(clock=clock)
    queue = RefreshQueue()
    service = HomeService(providers=[_provider()], cache=cache, refresh=queue.schedule)

    await service.compose(ctx)
    clock.advance(_SCREEN_TTL - dt.timedelta(seconds=1))
    await service.compose(ctx)

    assert queue.depth == 0


# -- how stale is too stale -------------------------------------------------


async def test_past_the_grace_window_the_entry_is_a_hard_miss_and_is_never_served(
    ctx: RowContext, clock: _Clock
) -> None:
    """**Stepped exactly onto the second boundary, not past it.**

    `_SCREEN_TTL + SCREEN_STALE_GRACE` is the instant a stale entry stops
    being servable, and `>=` versus `>` there is the same one-keystroke
    mutation M5's sweep recorded surviving on `stale_after` -- invisible to
    every case that steps past. A hard miss rebuilds, and it must *not* also
    schedule a refresh: the request already paid for the rebuild, so a
    scheduled one is a second full compose for a screen that is now fresh.
    """
    cache = RowCache(clock=clock)
    queue = RefreshQueue()
    provider = _provider()
    service = HomeService(providers=[provider], cache=cache, refresh=queue.schedule)

    await service.compose(ctx)
    clock.advance(_SCREEN_TTL + SCREEN_STALE_GRACE)
    await service.compose(ctx)

    assert _builds(provider) == 2, "past the grace window the request rebuilds"
    assert queue.depth == 0, "a hard miss has nothing to refresh -- it just rebuilt"


async def test_one_second_inside_the_grace_window_is_still_served_stale(
    ctx: RowContext, clock: _Clock
) -> None:
    """The other side of that boundary, so "hard miss" cannot be satisfied by
    a grace window of zero -- which is what deleting the feature looks like
    and which the case above alone would pass."""
    cache = RowCache(clock=clock)
    queue = RefreshQueue()
    provider = _provider()
    service = HomeService(providers=[provider], cache=cache, refresh=queue.schedule)

    warm = await service.compose(ctx)
    clock.advance(_SCREEN_TTL + SCREEN_STALE_GRACE - dt.timedelta(seconds=1))

    assert _without_suspending(service.compose(ctx)) == warm
    assert _builds(provider) == 1


async def test_a_composer_with_no_refresher_never_serves_stale(
    ctx: RowContext, clock: _Clock
) -> None:
    """**The grace window is gated on having somewhere to send the key.**

    A composer that opened the window with nothing behind it would serve a
    stale screen and never replace it -- strictly worse than the miss it
    avoided, and silent. `usher home` is that caller: the process ends when
    the command does, so there is nothing to run a scheduled refresh.

    Kills a grace window applied unconditionally, which every other case in
    this file passes because every other case injects a refresher.
    """
    cache = RowCache(clock=clock)
    provider = _provider()
    service = HomeService(providers=[provider], cache=cache)

    await service.compose(ctx)
    clock.advance(_SCREEN_TTL)
    await service.compose(ctx)

    assert len(provider.contexts) == 2, "with no refresher an expired screen is a plain miss"


# -- bounded, and full means dropped ---------------------------------------


async def test_two_reads_over_one_stale_key_schedule_one_refresh(
    ctx: RowContext, clock: _Clock
) -> None:
    """The deduplication, at the queue's own level -- **and the second read is
    still served the stale screen.**

    That second half is not decoration. `read_screen` returning the stale value
    *and popping the entry* is a one-line mutation that satisfies every other
    assertion in this file: the first read is served, the key is queued once,
    nothing is dropped -- and the next request is **cold**, so the household
    pays the full compose serve-stale exists to spare it, once per refresh, for
    as long as the lane is behind. Measured: without the two assertions below
    that mutation survived this whole file and only surfaced in
    `tests/unit/test_api_lanes.py`, as a *hang*.

    **This is a count, and a count is not a concurrency claim.** What it rules
    out is a queue with no dedup at all; the claim that matters -- that a
    request arriving while a refresh is *in flight* schedules nothing -- needs
    observed overlap and lives in `tests/unit/test_api_lanes.py`, where there
    is a lane to be in flight.
    """
    cache = RowCache(clock=clock)
    queue = RefreshQueue()
    provider = _provider()
    service = HomeService(providers=[provider], cache=cache, refresh=queue.schedule)

    warm = await service.compose(ctx)
    clock.advance(_SCREEN_TTL)
    _without_suspending(service.compose(ctx))
    second = _without_suspending(service.compose(ctx))

    assert second == warm, "the entry must be left in place for the next reader"
    assert len(provider.contexts) == 1, (
        "the second read re-proposed, so the stale entry was consumed rather "
        "than served -- the next request after a stale serve must not be cold"
    )
    assert queue.depth == 1
    assert queue.dropped == 0, "the second read was deduplicated, not dropped"


async def test_a_full_queue_drops_the_key_and_the_request_still_does_not_wait(
    ctx: RowContext, clock: _Clock
) -> None:
    """**Full means dropped, never blocked.**

    `asyncio.Queue.put` on a full queue suspends, and a request path that
    suspended there would block on exactly the load that filled it -- the
    stampede, arriving through the mechanism built to prevent it. `put_nowait`
    is the whole difference and it is invisible to any case whose queue never
    fills.

    Dropping is safe rather than merely tolerable: the entry is still inside
    its grace window, and once past it the next request takes a hard miss and
    rebuilds -- the cost M7 already pays.
    """
    cache = RowCache(clock=clock)
    queue = RefreshQueue(maxsize=1)
    queue.schedule(User(id=uuid.uuid4(), name="someone else"))
    assert queue.depth == 1, "the queue must really be full before the read that drops"

    service = HomeService(providers=[_provider()], cache=cache, refresh=queue.schedule)
    warm = await service.compose(ctx)
    clock.advance(_SCREEN_TTL)

    assert _without_suspending(service.compose(ctx)) == warm
    assert queue.dropped == 1
    assert queue.depth == 1
    assert ctx.user.id not in queue.pending


async def test_the_key_stays_pending_until_the_refresh_says_it_is_done(
    ctx: RowContext, clock: _Clock
) -> None:
    """**Taking a key off the queue does not clear it.**

    The dedup window has to cover the refresh itself, not just the wait for
    one: cleared at `take()`, a second request arriving mid-refresh schedules
    a second full compose over the same key, which is the stampede this bound
    exists to prevent and which no count-based case can see. The lane's
    `finally` calls `done`, so a refresh that raises still releases the key.
    """
    cache = RowCache(clock=clock)
    queue = RefreshQueue()
    service = HomeService(providers=[_provider()], cache=cache, refresh=queue.schedule)

    await service.compose(ctx)
    clock.advance(_SCREEN_TTL)
    _without_suspending(service.compose(ctx))

    taken = await asyncio.wait_for(queue.take(), timeout=1.0)
    assert taken.user == ctx.user
    assert queue.pending == frozenset({ctx.user.id}), "still in flight, still deduplicated"

    _without_suspending(service.compose(ctx))
    assert queue.depth == 0, "a key already being refreshed must not be queued again"

    queue.done(ctx.user.id)
    assert queue.pending == frozenset()


# -- what the refresh actually does ----------------------------------------


async def test_rebuild_ignores_the_cached_screen_and_replaces_it(
    ctx: RowContext, clock: _Clock
) -> None:
    """`rebuild` is the refresh's entry point and it is a *different* method
    from `compose` for one reason: a refresh that went through the ordinary
    read would find its own stale entry, serve it to itself, and schedule
    another refresh -- a lane spinning on one key forever, with the household
    still looking at the same stale screen.

    **The row cache underneath is still consulted**, which is PRD 06's two
    layers doing what they exist for: 30 s past the screen's TTL the row's own
    60 s has not moved, so the refresh re-proposes and re-orders without
    re-hydrating. A refresh that dropped the row half would make every screen
    expiry pay the expensive phase for rows whose inputs move in hours.
    """
    cache = RowCache(clock=clock)
    queue = RefreshQueue()
    provider = _provider()
    service = HomeService(providers=[provider], cache=cache, refresh=queue.schedule)

    await service.compose(ctx)
    clock.advance(_SCREEN_TTL)
    _without_suspending(service.compose(ctx))

    rebuilt = await service.rebuild(ctx)

    assert len(provider.contexts) == 2, "the refresh really re-proposed"
    assert _builds(provider) == 1, "and reused the row, whose own TTL had not expired"
    assert queue.depth == 1, "and scheduled nothing of its own"
    read = cache.read_screen(ctx.user.id)
    assert read.screen == rebuilt
    assert read.freshness.value == "fresh", "the refreshed screen is live again"


async def test_a_stale_serve_costs_no_taste_read(clock: _Clock) -> None:
    """`RowContext.affinities` is a callable so that a screen the cache can
    answer never pays the three statements behind it (`api/deps.py`
    `_Affinities`). A *stale* serve is a screen the cache answered, so it owes
    them no more than a fresh one does -- and a serve-stale path that resolved
    the context eagerly would put the whole genre-affinity read back in front
    of the answer it already has.
    """
    reads = 0

    async def affinities() -> tuple[()]:
        nonlocal reads
        reads += 1
        return ()

    ctx = dataclasses.replace(Library().context(), affinities=affinities)
    cache = RowCache(clock=clock)
    service = HomeService(providers=[_provider()], cache=cache, refresh=RefreshQueue().schedule)

    await service.compose(ctx)
    before = reads
    clock.advance(_SCREEN_TTL)
    _without_suspending(service.compose(ctx))

    assert reads == before, "a stale serve read the household's taste"


# -- the shape of the value that crosses into the lane ----------------------


def test_the_queue_hands_over_the_user_the_request_already_resolved() -> None:
    """**A frozen domain value, never the `RowContext`.**

    The context holds ten repositories bound to the request's `AsyncSession`,
    which `get_session` commits and closes when the handler returns -- so a
    queue carrying one would hand a background task either a closed session or
    a live one shared with a request, which is the `AsyncSession` concurrency
    hazard ADR-0025 refuses one layer up and which *usually works*. The `User`
    is immutable and is what the refresh needs to build a context of its own.
    """
    queue = RefreshQueue()
    user = User(id=uuid.uuid4(), name="default", is_default=True)

    queue.schedule(user)

    assert queue.depth == 1


def test_a_screen_stale_by_a_negative_ttl_is_still_inside_the_grace_window() -> None:
    """The shape `tests/integration/test_rows_refresh.py` plants with, pinned
    here so that file's premise is not an assumption about arithmetic it
    cannot step a clock to check: a real wall clock cannot be advanced, so the
    integration case makes an entry that is *already* expired instead."""
    cache = RowCache(clock=lambda: _START)
    user = uuid.uuid4()
    screen: tuple[BuiltRow, ...] = ()

    cache.put_screen(user, screen, ttl=-dt.timedelta(seconds=1))

    assert cache.read_screen(user, grace=SCREEN_STALE_GRACE).freshness.value == "stale"
    assert cache.get_screen(user) is None, "and it is not fresh to a reader without a grace"
