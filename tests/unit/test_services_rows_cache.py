"""The in-process row and screen caches (PRD 06).

**In-process, per worker, and it dies with the process.** Said here because a
reader assumes otherwise: this is a dict in the server, not Redis. On the
deployment this project ships that is exactly one cache -- `compose.yml` runs
one `usher` service and its `CMD` runs one uvicorn worker.

**The two silent bugs this file exists to make loud.** A key collision serves
one household's screen to another, which is unreachable at one user and
*unreachable is not impossible*; and a TTL that never expires serves last
week's screen with no error anywhere. Neither raises, neither logs, and neither
is visible on a screen that looks right.
"""

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.unit.rows import Library
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.rows import BuiltRow, DisplayHint, RowCard, RowFamily
from usher.ports.rows import RowContext, ScoredRow
from usher.services.home import HomeService
from usher.services.rows.cache import Freshness, RowCache

_TTL = dt.timedelta(seconds=30)
_START = dt.datetime(2026, 8, 4, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """`usher.cache.hits`/`.misses`' own file (`test_telemetry_cache.py`) is
    where the metric is exercised in depth; this fixture is here only for the
    one boundary case below, so the expiry habit `stale_after` teaches is
    checked against the metric too, in the same place it is checked against
    the returned value."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


def _cache_points(reader: InMemoryMetricReader, name: str) -> list[tuple[dict[str, object], float]]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    found: list[tuple[dict[str, object], float]] = []
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    # `usher.cache.hits`/`.misses` are counters (`Sum`), so
                    # every point reaching this loop is a `NumberDataPoint`
                    # carrying `.value` -- narrowed rather than asserted, so
                    # a rename to a histogram fails here on the type rather
                    # than on an `AttributeError` mid-run.
                    assert isinstance(point, NumberDataPoint)
                    found.append((dict(point.attributes or {}), float(point.value)))
    return found


class _Clock:
    """A clock that only moves when a case moves it.

    The alternative -- `datetime.now(UTC)` inside the cache -- makes both TTL
    cases unassertable: there is no way to reach an expiry without sleeping,
    and a case that sleeps 30 s is a case nobody runs.
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


# A card built twice from the same name must be the *same* card: these cases
# compare a cached value against a freshly built one, and a `new_id()` per call
# makes every such comparison fail for a reason that has nothing to do with the
# cache.
_NAMESPACE = uuid.UUID("00000000-0000-7000-8000-00000000ca11")


def _card(name: str) -> RowCard:
    return RowCard(
        title_id=uuid.uuid5(_NAMESPACE, name),
        kind=TitleKind.MOVIE,
        name=name,
        enrichment_state=EnrichmentState.SKELETON,
    )


def _row(name: str, *, slug: str = "continue-watching") -> BuiltRow:
    return BuiltRow(
        slug=slug,
        title="Continue Watching",
        family=RowFamily.SOURCE,
        display_hint=DisplayHint.LANDSCAPE,
        ttl=_TTL,
        cards=(_card(name),),
    )


def _screen(name: str) -> tuple[BuiltRow, ...]:
    return (_row(name),)


def test_two_users_never_share_a_composed_screen(clock: _Clock) -> None:
    """**Unreachable at one user, and unreachable is not impossible.**

    v1 mints one user, so a key that omitted `user_id` would work today and
    serve one household's screen to another the day PRD 07's authentication
    seam is replaced -- with no error, no log line and no metric. What makes
    the key correct is that it is built from the `user_id` the request
    resolved, never from a module constant or an implicit current user.

    Kills a key spelled `slug` alone, and a key spelled `("screen",)`.
    """
    cache, alice, bob = RowCache(clock=clock), uuid.uuid4(), uuid.uuid4()

    cache.put_screen(alice, _screen("alice"), ttl=_TTL)

    assert cache.get_screen(bob) is None
    assert cache.get_screen(alice) == _screen("alice")


def test_a_row_cache_key_carries_both_the_user_and_the_slug(clock: _Clock) -> None:
    cache, alice, bob = RowCache(clock=clock), uuid.uuid4(), uuid.uuid4()

    cache.put_row(alice, "continue-watching", _row("alice"), ttl=_TTL)

    assert cache.get_row(bob, "continue-watching") is None
    assert cache.get_row(alice, "next-up") is None
    assert cache.get_row(alice, "continue-watching") == _row("alice")


def test_an_expired_entry_is_recomputed_rather_than_served(clock: _Clock) -> None:
    """A TTL that never expires serves a screen from last week and nothing
    anywhere reports it. The mutation is one deleted branch."""
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_screen(user, _screen("old"), ttl=_TTL)

    clock.advance(dt.timedelta(seconds=31))

    assert cache.get_screen(user) is None


def test_an_entry_exactly_at_its_expiry_is_expired(
    clock: _Clock, meter_reader: InMemoryMetricReader
) -> None:
    """**Steps the clock *onto* the boundary, not past it.**

    M5's mutation sweep recorded the `stale_after` `<=` -> `<` mutation
    surviving for precisely this reason: every case in that file stepped past
    the boundary rather than onto it, so both spellings agreed on every input
    the suite offered. One second of drift in a 30 s TTL is invisible; the
    habit that hides it is not.

    **And an entry found expired is a `usher.cache.misses` point, not a
    `usher.cache.hits` one** -- it is a rebuild, not a serve. The wrong
    implementation this rules out records the miss on `<=` correctly but the
    *metric* on whether `entry` was found at all, which an expired-but-present
    entry would still count as a hit.
    """
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_screen(user, _screen("old"), ttl=_TTL)
    cache.put_row(user, "continue-watching", _row("old"), ttl=_TTL)

    clock.advance(_TTL)

    assert cache.get_screen(user) is None
    assert cache.get_row(user, "continue-watching") is None

    misses = _cache_points(meter_reader, "usher.cache.misses")
    hits = _cache_points(meter_reader, "usher.cache.hits")
    assert {attrs["cache"] for attrs, _ in misses} == {"screen", "row"}
    assert hits == []


def test_a_stale_serve_is_a_hit_labelled_stale(
    clock: _Clock, meter_reader: InMemoryMetricReader
) -> None:
    """**The label a served-stale read gets, and the argument for it.**

    A **hit**, because the request was served out of the cache and paid no
    rebuild: counting it a miss would make `usher.cache.hits` say a compose
    happened when none did, on the series a dashboard reads as "requests that
    avoided a compose". But *not a plain hit*, because a plain hit hides
    exactly what serve-stale trades away -- the household is looking at data
    older than the TTL, and nothing else in PRD 10 would say so.

    `freshness` is on the hits counter only. A miss served nothing, so it has
    no freshness to report, and the pair stays at four series.

    The wrong implementations this rules out: a stale serve counted as a miss
    (which is also what the whole feature *not working* looks like), and a
    stale serve counted under the same series as a fresh one.
    """
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_screen(user, _screen("old"), ttl=_TTL)

    clock.advance(_TTL)
    assert cache.read_screen(user, grace=dt.timedelta(seconds=60)).freshness is Freshness.STALE

    hits = _cache_points(meter_reader, "usher.cache.hits")
    assert [(attrs.get("cache"), attrs.get("freshness"), value) for attrs, value in hits] == [
        ("screen", "stale", 1.0)
    ]
    assert _cache_points(meter_reader, "usher.cache.misses") == []


def test_the_screen_read_has_three_states_and_the_grace_is_the_callers(
    clock: _Clock,
) -> None:
    """Fresh, stale-inside-`grace`, absent -- and **the grace is a parameter,
    not a property of the dict.**

    The only reader entitled to a stale answer is one that can arrange for the
    entry to be replaced, which is what makes `HomeService`'s "no refresher,
    no grace" gate expressible at all. A cache that decided for itself would
    hand `usher home` a screen it has nothing to refresh with, and that is a
    stale screen served forever -- worse than the miss it avoided, and silent.
    """
    cache, user = RowCache(clock=clock), uuid.uuid4()
    grace = dt.timedelta(seconds=60)

    assert cache.read_screen(user, grace=grace).freshness is Freshness.ABSENT

    cache.put_screen(user, _screen("fresh"), ttl=_TTL)
    assert cache.read_screen(user, grace=grace).freshness is Freshness.FRESH

    clock.advance(_TTL)
    stale = cache.read_screen(user, grace=grace)
    assert stale.freshness is Freshness.STALE
    assert stale.screen == _screen("fresh"), "a stale read still hands back the value"
    assert cache.read_screen(user).freshness is Freshness.ABSENT, (
        "and with no grace the same entry is a plain miss"
    )


def test_an_entry_exactly_at_the_end_of_its_grace_is_a_hard_miss(clock: _Clock) -> None:
    """**Stepped exactly onto the second boundary, not past it.**

    `TTL + grace` is the instant a stale entry stops being servable, and `>=`
    against `>` there is the same one-keystroke mutation M5's sweep recorded
    surviving on `stale_after`: invisible to every case that steps past. Past
    it the entry is *removed*, not merely refused -- the screen half is
    bounded only by the `users` table, so an entry nobody will ever be served
    again is dead weight per household.
    """
    cache, user = RowCache(clock=clock), uuid.uuid4()
    grace = dt.timedelta(seconds=60)
    cache.put_screen(user, _screen("old"), ttl=_TTL)

    clock.advance(_TTL + grace - dt.timedelta(seconds=1))
    assert cache.read_screen(user, grace=grace).freshness is Freshness.STALE

    clock.advance(dt.timedelta(seconds=1))
    assert cache.read_screen(user, grace=grace).freshness is Freshness.ABSENT
    assert cache.size == 0, "and it was removed rather than left to accumulate"


def test_the_grace_is_read_at_the_read_and_not_baked_in_at_the_write(
    clock: _Clock,
) -> None:
    """A grace applied at `put_screen` -- folded into the stored
    `expires_at` -- reads identically at every assertion above and is a
    different feature: the entry would then be *fresh* for `TTL + grace`, so
    nothing would ever be stale, no refresh would ever be scheduled, and the
    screen a household sees would simply live 90 s instead of 30.

    What distinguishes the two is a reader with **no** grace looking at the
    same entry a second before the TTL expires and a second after.
    """
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_screen(user, _screen("old"), ttl=_TTL)

    clock.advance(_TTL)

    assert cache.get_screen(user) is None, "the stored expiry is the TTL, with no grace in it"


def test_a_live_entry_is_served_without_recomputing(clock: _Clock) -> None:
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_screen(user, _screen("fresh"), ttl=_TTL)

    clock.advance(dt.timedelta(seconds=29))

    assert cache.get_screen(user) == _screen("fresh")


def test_the_row_cache_is_bounded_because_its_key_space_is_the_catalog(
    clock: _Clock,
) -> None:
    """`because-you-watched-<seed>` is one slug per seed, so an unevicted dict
    keyed by `(user_id, slug)` grows with the household's watch history -- and
    expired entries are only *read* past, never removed, so the TTL reclaims
    nothing. Same cardinality hazard as the `provider` metric label, one layer
    over, and here it is a leak."""
    cache, user = RowCache(clock=clock, max_entries=64), uuid.uuid4()

    for index in range(500):
        cache.put_row(user, f"because-you-watched-{index}", _row(str(index)), ttl=_TTL)

    assert cache.size <= 64


def test_eviction_takes_the_soonest_to_expire_first(clock: _Clock) -> None:
    """A ceiling that evicted the *newest* entry would leave a cache that
    never serves anything it was just asked to store -- bounded, and useless,
    with no symptom but a miss rate nothing in M7 measures
    (`usher.cache.hits` is M9's)."""
    cache, user = RowCache(clock=clock, max_entries=2), uuid.uuid4()

    cache.put_row(user, "expires-first", _row("a"), ttl=dt.timedelta(seconds=1))
    cache.put_row(user, "expires-later", _row("b"), ttl=dt.timedelta(seconds=300))
    cache.put_row(user, "expires-last", _row("c"), ttl=dt.timedelta(seconds=600))

    assert cache.get_row(user, "expires-first") is None
    assert cache.get_row(user, "expires-later") is not None
    assert cache.get_row(user, "expires-last") is not None


def test_invalidating_a_user_leaves_another_users_entries_alone(clock: _Clock) -> None:
    cache, alice, bob = RowCache(clock=clock), uuid.uuid4(), uuid.uuid4()
    cache.put_screen(alice, _screen("alice"), ttl=_TTL)
    cache.put_row(bob, "continue-watching", _row("bob"), ttl=_TTL)
    cache.put_screen(bob, _screen("bob"), ttl=_TTL)

    cache.invalidate(alice, ("continue-watching",))

    assert cache.get_screen(bob) == _screen("bob")
    assert cache.get_row(bob, "continue-watching") == _row("bob")


def test_invalidating_a_row_also_drops_the_screen_that_contained_it(clock: _Clock) -> None:
    """The composed screen is a *composition of rows*, so a row whose inputs
    moved leaves the screen carrying a stale copy of it. Dropping the row and
    keeping the screen is the subtle half of this bug: the next request is a
    screen cache hit and the invalidation had no visible effect at all."""
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_row(user, "continue-watching", _row("old"), ttl=_TTL)
    cache.put_screen(user, _screen("old"), ttl=_TTL)

    cache.invalidate(user, ("continue-watching",))

    assert cache.get_row(user, "continue-watching") is None
    assert cache.get_screen(user) is None


def test_enriching_a_title_drops_every_cached_row_that_names_it(clock: _Clock) -> None:
    """A row is built from titles, so a title that changed leaves every row
    holding it stale -- the card carries `name`, `year`, `enrichment_state` and
    `artwork`, and enrichment rewrites all four.

    The household that never cached the title keeps both halves, which is what
    makes this a statement about the *title* rather than a `clear()` wearing a
    narrower name.
    """
    cache = RowCache(clock=clock)
    alice, bob = uuid.uuid4(), uuid.uuid4()
    cache.put_row(alice, "because-you-watched", _row("enriched"), ttl=_TTL)
    cache.put_screen(alice, _screen("enriched"), ttl=_TTL)
    cache.put_row(bob, "because-you-watched", _row("untouched"), ttl=_TTL)
    cache.put_screen(bob, _screen("untouched"), ttl=_TTL)

    cache.invalidate_titles((_card("enriched").title_id,))

    assert cache.get_row(alice, "because-you-watched") is None
    assert cache.get_screen(alice) is None
    assert cache.get_row(bob, "because-you-watched") == _row("untouched")
    assert cache.get_screen(bob) == _screen("untouched")


def test_an_enrichment_reaches_every_household_that_cached_the_title(clock: _Clock) -> None:
    """`invalidate` takes a household because a play button is one household's
    act. Enrichment is a **catalog** write -- the same title is stale on every
    screen holding it at once -- so this one takes no `user_id`, and a
    per-household spelling would leave the second household serving the first's
    already-repaired staleness for the rest of its TTL.
    """
    cache = RowCache(clock=clock)
    for user in (uuid.uuid4(), uuid.uuid4()):
        cache.put_row(user, "because-you-watched", _row("enriched"), ttl=_TTL)
        cache.put_screen(user, _screen("enriched"), ttl=_TTL)

    cache.invalidate_titles((_card("enriched").title_id,))

    assert cache.size == 0


def test_a_screen_naming_the_title_goes_even_when_its_row_was_never_cached(
    clock: _Clock,
) -> None:
    """Both halves are scanned, not just the row half. A screen is stored whole
    (`put_screen` takes the composed tuple), so a row can reach a screen without
    ever being written to the row half -- and dropping only the row half would
    leave the next request a screen cache hit carrying the stale card, which is
    `invalidate`'s own recorded subtle bug arriving through the other door.
    """
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_screen(user, _screen("enriched"), ttl=_TTL)

    cache.invalidate_titles((_card("enriched").title_id,))

    assert cache.get_screen(user) is None


def test_invalidating_no_titles_drops_nothing(clock: _Clock) -> None:
    """The premise that keeps the three cases above about titles: an empty
    batch is the shape a job with nothing enriched hands over, and a `clear()`
    behind this name would satisfy every assertion of theirs while emptying a
    cache no write had staled."""
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_row(user, "because-you-watched", _row("enriched"), ttl=_TTL)
    cache.put_screen(user, _screen("enriched"), ttl=_TTL)

    cache.invalidate_titles(())

    assert cache.get_row(user, "because-you-watched") == _row("enriched")
    assert cache.get_screen(user) == _screen("enriched")


def test_clearing_empties_both_halves(clock: _Clock) -> None:
    """`usher home --repeat` clears between runs, because a repeat that
    measured cache hits would report a number near zero and mean nothing."""
    cache, user = RowCache(clock=clock), uuid.uuid4()
    cache.put_row(user, "continue-watching", _row("x"), ttl=_TTL)
    cache.put_screen(user, _screen("x"), ttl=_TTL)

    cache.clear()

    assert cache.size == 0
    assert cache.get_screen(user) is None


# --- the composer's own use of it ----------------------------------------


@pytest.fixture
def ctx() -> RowContext:
    return Library().context()


def _provider(slug: str, *, score: float) -> FakeRowProvider:
    return FakeRowProvider(
        proposals=(ScoredRow(row=FakeRow(slug, cards=(_card(slug),)), score=score),),
        slug_prefix=slug,
    )


def _builds(provider: FakeRowProvider) -> int:
    row = provider.rows[0]
    assert isinstance(row, FakeRow)
    return row.builds


async def test_a_second_compose_inside_the_window_rebuilds_nothing(
    ctx: RowContext, clock: _Clock
) -> None:
    """The screen cache doing its job, asserted through the composer rather
    than through the dict -- a cache nothing reads is a dict."""
    cache = RowCache(clock=clock)
    provider = _provider("recently-added", score=0.9)
    service = HomeService(providers=[provider], cache=cache)

    first = await service.compose(ctx)
    second = await service.compose(ctx)

    assert first == second
    assert _builds(provider) == 1
    assert len(provider.contexts) == 1, "a screen hit must not re-propose either"


async def test_a_compose_after_the_screen_expires_builds_again(
    ctx: RowContext, clock: _Clock
) -> None:
    """The other half, and the one that fails against a TTL that never
    expires: without it the case above is satisfied by a cache that never
    lets go."""
    cache = RowCache(clock=clock)
    provider = _provider("recently-added", score=0.9)
    service = HomeService(providers=[provider], cache=cache)

    await service.compose(ctx)
    clock.advance(dt.timedelta(seconds=31))
    await service.compose(ctx)

    assert len(provider.contexts) == 2


async def test_a_row_survives_the_screen_expiring_because_its_own_ttl_is_longer(
    ctx: RowContext, clock: _Clock
) -> None:
    """PRD 06 caches at two layers, and this is why: the screen is ~30 s and a
    similarity row is hours. A composer that only cached the screen would
    rebuild every row on every screen miss, which is the expensive half of the
    work done on a 30 s cycle for rows whose inputs move in days."""
    cache = RowCache(clock=clock)
    provider = FakeRowProvider(
        proposals=(
            ScoredRow(
                row=FakeRow(
                    "because-you-watched-dune",
                    family=RowFamily.SIMILARITY,
                    ttl=dt.timedelta(hours=6),
                    cards=(_card("Dune"),),
                ),
                score=0.8,
            ),
        ),
        slug_prefix="because-you-watched",
    )
    service = HomeService(providers=[provider], cache=cache)

    await service.compose(ctx)
    clock.advance(dt.timedelta(seconds=31))
    await service.compose(ctx)

    assert len(provider.contexts) == 2, "the screen must have expired"
    assert _builds(provider) == 1, "the row's own six-hour TTL had not expired"
