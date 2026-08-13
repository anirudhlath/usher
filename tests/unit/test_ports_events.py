"""The client event channel's vocabulary.

Brought forward from the SSE task, because `PushApplyService` publishes and
`services/` may depend only on `domain/` and `ports/` -- so the port has to
exist before the first service that calls it, not after.
"""

import uuid

from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher, NullEventPublisher


def test_every_event_kind_is_something_this_process_emits() -> None:
    """PRD 10's rule for metrics, applied to a client contract: "A documented
    metric nothing emits is a dashboard panel that is permanently empty, and
    nothing distinguishes that from a healthy zero." An SSE event type nothing
    emits is worse -- a client writes a handler for it and waits forever.

    **M7 added the fifth, `row.invalidated`, in the same commit as its
    publisher** (`PushApplyService`), which is this rule pointed the other way:
    a member with no publisher is the handler that waits forever, and a
    publisher with no member is a `KeyError` inside a response that has already
    answered 200.

    **M9's E7 added the sixth, `bootstrap.progress`, on the same terms.** Its
    absence was justified by a premise E5 removed: bootstrap ran only in the
    CLI process while the bus is in-process, so there was no channel from one
    to the other. `JobKind.BOOTSTRAP` put the work on the worker lane, which
    in the shipped default is the API process holding this bus, and the member
    lands in the same commit as `BootstrapService`'s publisher. What has not
    changed is the split deployment: with `usher work` in its own container
    the frames reach a `NullEventPublisher`, which is the degradation
    `title.updated` has had since M5 rather than a new one.

    Renamed from `..._is_something_m5_emits`: the rule is about *this process*,
    not about one milestone, and a guard whose name pins a milestone is one the
    next milestone edits without reading.
    """
    assert {kind.value for kind in ClientEventKind} == {
        "title.updated",
        "watchstate.updated",
        "row.invalidated",
        "sync.progress",
        "bootstrap.progress",
        "resync_required",
    }


def test_an_event_may_be_scoped_to_a_title_or_to_nothing() -> None:
    """PRD 07: "Subscriptions are scoped by query (`?titles=id1,id2`) so a
    detail screen isn't woken by unrelated churn." A `sync.progress` event
    belongs to no title and must reach only unfiltered subscribers."""
    title_id = uuid.uuid4()
    assert ClientEvent(kind=ClientEventKind.TITLE_UPDATED, title_id=title_id).title_id == title_id
    assert ClientEvent(kind=ClientEventKind.SYNC_PROGRESS, data={"seen": 1}).title_id is None


def test_an_episode_event_carries_its_series_title_for_filtering() -> None:
    """A client watching a series subscribes with the *series'* title id --
    it has no episode ids until it fetches the season. So an episode event
    carries both, and the filter matches on `title_id`."""
    title_id, episode_id = uuid.uuid4(), uuid.uuid4()
    event = ClientEvent(
        kind=ClientEventKind.WATCHSTATE_UPDATED, title_id=title_id, episode_id=episode_id
    )
    assert (event.title_id, event.episode_id) == (title_id, episode_id)


async def test_the_null_publisher_accepts_everything_and_does_nothing() -> None:
    """`usher work` as a standalone process publishes nowhere, and that has
    to be expressible without a branch in `EnrichService`."""
    await NullEventPublisher().publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED))


def test_publish_is_the_ports_only_method() -> None:
    """Subscription is not on the port. A Postgres `LISTEN/NOTIFY`
    implementation publishes with an `INSERT`/`NOTIFY` and subscribes with a
    dedicated connection whose lifecycle has nothing in common with an
    in-memory queue's -- putting both on one ABC would force the second
    implementation to satisfy a shape drawn around the first."""
    assert EventPublisher.__abstractmethods__ == frozenset({"publish"})
