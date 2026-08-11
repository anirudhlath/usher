"""DTOs that cross the ingest pipeline's service<->repository boundary.

Separate from `usher.ports.repository` because that module is a list of
ABCs and this one is a vocabulary; separate from `usher.ports.source`
because nothing here is a source's concept. `usher.ports.metadata` imports
`ProviderRef` from here too, which settles one of its 🔶 markers -- a
provider reference is one idea, and having TMDb's integer id baked into one
signature and a string ref in another was the thing that marker complained
about.

Every dataclass here is `frozen=True` and therefore hashable, deliberately:
`MatchService` turns a batch of source items into sets of `ProviderRef` and
`NameYearProbe`, issues one query per set, and joins the answers back by
dict lookup. At 1,126,674 items the alternative is 1,126,674 round trips.

**Not only inbound, despite the module's name.** `WatchStateWrite`, beside
`WatchStateMerge` below, travels the opposite direction -- from a client
action route, through `WatchStateRepository.set_from_client`, rather than
from a source walk. It lives here anyway: this module is not "DTOs from a
source", it is every DTO that crosses into a repository this package owns,
and splitting the one client-originated member out into a module of its own
would be a distinction with no reader.
"""

import uuid
from dataclasses import dataclass

from pydantic import AwareDatetime

from usher.domain.enums import HdrFormat, MatchMethod, TitleKind
from usher.ports.errors import UsherPortError


@dataclass(frozen=True, slots=True)
class ProviderRef:
    """One provider's claim about an entity's identity.

    `kind` is `TitleKind` for a namespaced provider and `None` for a global
    one. TMDb keys movies and series in separate integer spaces that overlap
    on 26,968 ids (measured 2026-07-30), so a TMDb ref without a kind names
    nothing; IMDb's `tt` ids are one global namespace, so an IMDb ref with a
    kind would be claiming a distinction that does not exist. ADR-0011.

    `value` is a string, not an int, so the same type serves TMDb's
    `90000550` and IMDb's `tt99000020`. The repository casts at the boundary,
    where it knows the column type.
    """

    provider: str
    value: str
    kind: TitleKind | None


@dataclass(frozen=True, slots=True)
class NameYearProbe:
    """PRD 03 stage 3: normalised name plus a year within +/-1.

    `name` is passed exactly as the source gave it; the repository applies
    the same `lower()` the `ix_titles_name_lower_year` expression index is
    built on. Normalising here instead would put the index's definition in
    two places, which is how they diverge.
    """

    name: str
    year: int | None
    kind: TitleKind


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """What one source item resolved to, and by which tier.

    `method` is not diagnostics: it is the label on PRD 10's
    `usher.match.result` counter, which is how "is the TMDb-search tier
    earning its rate limit" is answerable at all.
    """

    external_id: str
    title_id: uuid.UUID | None
    method: MatchMethod
    episode_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class MediaItemTarget:
    """What one stored `MediaItem` is matched to.

    Read in two directions, and the asymmetry between them is the point.
    Coming *out* of `MediaItemRepository.resolve_targets` this is what the
    row holds, and an episode's row holds **both** ids: `IngestService`
    writes `title_id` (the series' canonical title) and `episode_id`
    together, because a client browsing a season wants both. Going *in* to
    `resolve_external_ids` it is a watch-state target, where
    `watch_states`' own `num_nonnulls(title_id, episode_id) = 1` CHECK means
    exactly one is set.

    So the two are not interchangeable, and the collapse from the first to
    the second (`episode_id` wins; a title-only target must not match an
    episode row) belongs to whoever is merging watch state --
    `usher.services.watch_sync`, which is the only caller and states the
    rule where it is legible. Handing `merge_from_source` a pair with both
    ids set raises `PortDataMalformed` by contract, which at 999,827
    episodes would abort a batch of five thousand states over 89% of the
    library.

    Hashable (frozen) because it is a dict key in both directions.
    """

    title_id: uuid.UUID | None
    episode_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class MediaItemUpsert:
    """One row for the staged `media_items` upsert.

    `last_seen_at` has no default. It is the availability sweep's only
    input, and it must be the *run's* start instant rather than each row's
    own write instant -- a per-row `now()` would make the sweep's
    `last_seen_at < run.started_at` comparison race against the batch it is
    sweeping over.

    `title_id`/`episode_id` are `None` for an unmatched item, which is a
    legitimate and common state (PRD 02) -- but the upsert statement must
    never write a `None` *over* a stored value, or the nightly walk erases
    every manual review-queue resolution. That is the repository's
    `COALESCE`, not this DTO's problem, and it has its own contract case.
    """

    source_id: uuid.UUID
    external_id: str
    title_id: uuid.UUID | None
    episode_id: uuid.UUID | None
    container: str | None
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    hdr_format: HdrFormat | None
    audio_channels: int | None
    file_size_bytes: int | None
    runtime_seconds: int | None
    added_at: AwareDatetime | None
    last_seen_at: AwareDatetime


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What one batch of a walk did, from `IngestService.ingest_batch`.

    `inserted`/`updated` are `BulkWriteResult`'s two counts, restated rather
    than nested so the common read (`result.inserted`) stays one attribute
    deep. `matched`/`unmatched` are the *outcome* counts, and they are here
    because `SyncRun` carries `items_matched`/`items_unmatched` and the
    alternative was a `list_unmatched` query per batch to recover a number
    the batch already knew.

    They do not have to sum to `inserted + updated`: a batch may legitimately
    contain the same `(source_id, external_id)` twice (`list_items`' own
    contract permits it), which is two outcomes and one row.

    `outcomes` is one per item, in the order they were given, *after* episode
    attachment -- so an episode's outcome here carries the title and episode
    ids it was hung off, which the match stage on its own never knows. It is
    returned rather than kept internal because it is the only place the
    per-item resolution is expressible: the counters above are sums, and the
    method label on PRD 10's `usher.ingest.items` counter is not something a
    caller can recover from them.
    """

    inserted: int
    updated: int
    matched: int
    unmatched: int
    outcomes: tuple[MatchOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class WatchStateMerge:
    """One inbound watch record, on its way to `merge_from_source`.

    `play_count` and `last_played_at` default to `None` and `None` means
    "this read could not determine it" -- ADR-0014, carried one layer down
    from `SourceWatchState` so the repository never has to reach back into a
    port DTO it does not own. `0` is a positive claim and is written.

    `observed_at` is the run's start instant, and it is the conflict rule:
    PRD 03 says "latest `updated_at` wins", so a stored row whose
    `updated_at` is newer than this was written by something that knows
    more recent truth (a client, through `origin = api`) and is left alone.
    """

    user_id: uuid.UUID
    title_id: uuid.UUID | None
    episode_id: uuid.UUID | None
    position_seconds: int
    played: bool
    runtime_seconds: int | None
    observed_at: AwareDatetime
    play_count: int | None = None
    last_played_at: AwareDatetime | None = None


@dataclass(frozen=True, slots=True)
class WatchStateWrite:
    """One client-originated watch write, on its way to
    `WatchStateRepository.set_from_client`.

    The other direction from `WatchStateMerge`, immediately above. No
    `observed_at`: `merge_from_source`'s conflict rule exists to answer "did
    the walk that produced this see something newer than what's stored",
    and a client write is never asked that question -- `origin = api`
    always wins, because `trg_watch_states_set_updated_at` (a
    `BEFORE UPDATE` trigger assigning `now()` unconditionally) stamps every
    write with the instant it actually happened, which is by construction
    later than any walk that started before it.

    No `play_count`, no `last_played_at`, no `runtime_seconds`: a client
    reports what it did -- seek to a position, mark played, mark unplayed --
    not a play count or a duration, and `set_from_client` derives the two
    former from `played` itself. See its docstring.
    """

    user_id: uuid.UUID
    title_id: uuid.UUID | None
    episode_id: uuid.UUID | None
    position_seconds: int
    played: bool


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What an availability sweep actually changed, and out of how many.

    `total` is the source's whole item count -- available or not -- which
    the sweep has already counted to evaluate its own guard, so reporting it
    is free. It is here because "3 retracted" is not an operational event on
    its own: "3 of 4" and "3 of 94,438" want different responses, and
    `sync_runs.items_retracted` only stores the numerator.

    There is deliberately **no `restored` count**. Restoring an item that
    came back is `upsert_many`'s doing -- appearing in a walk *is* the
    evidence of availability -- and the sweep only ever sets `false`
    (ADR-0015), so a `restored` field on this DTO could only ever report
    zero. An always-zero field that the port's own docstring describes as
    meaningful is worse than an absent one.
    """

    retracted: int
    total: int


class AvailabilitySweepRefused(UsherPortError):
    """The sweep would have retracted more of a source than the configured
    ceiling permits, so it retracted nothing.

    `SourceAdapter.list_items`' contract already guarantees a walk raises
    rather than truncating, and `ReconcileService` already refuses to sweep
    after a run that raised. This covers the residual those two do not: a
    walk that *completes* and returns far less than the library holds -- an
    unmounted drive, a library an operator removed by accident, a
    permissions change on the source's own account. There is no way for an
    adapter to tell that from a genuine mass deletion, and there is no way
    for Usher to undo one, so the sweep declines and says so.

    Carries the numbers rather than only a message, because the operator's
    next question is "did my library really shrink by that much" and the
    answer is arithmetic.
    """

    def __init__(self, *, would_retract: int, total: int, ceiling: float) -> None:
        # `total or 1`: the one guard that raises this today only fires when
        # at least one row is stale, which implies a non-empty source -- but
        # a ZeroDivisionError thrown from inside the constructor of the error
        # that exists to stop a sweep from erasing a library would replace a
        # refusal with a crash, and there is no reading of that trade worth
        # taking. An empty source reports 0%.
        share = would_retract / (total or 1)
        super().__init__(
            f"refusing to mark {would_retract} of {total} items unavailable in one run "
            f"({share:.0%} exceeds the {ceiling:.0%} ceiling); "
            "nothing was retracted"
        )
        self.would_retract = would_retract
        self.total = total
        self.ceiling = ceiling
