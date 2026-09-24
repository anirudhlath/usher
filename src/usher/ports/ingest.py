"""DTOs that cross the ingest pipeline's service<->repository boundary."""

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
    heavily, so a TMDb ref without a kind names nothing; IMDb's `tt` ids are
    one global namespace, so an IMDb ref with a kind claims a distinction
    that does not exist.

    `value` is a string, not an int, so the same type serves TMDb's
    `90000550` and IMDb's `tt99000020`. The repository casts at the boundary,
    where it knows the column type.
    """

    provider: str
    value: str
    kind: TitleKind | None


@dataclass(frozen=True, slots=True)
class NameYearProbe:
    """PRD 03's match ladder, step 4: normalised name plus a year within +/-1.

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
    `usher.match.result` counter, which is how the TMDb search tier's yield
    against the requests it spends is answerable at all.
    """

    external_id: str
    title_id: uuid.UUID | None
    method: MatchMethod
    episode_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class MediaItemTarget:
    """What one stored `MediaItem` is matched to."""

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
    """What one batch of a walk did, from `IngestService.ingest_batch`."""

    inserted: int
    updated: int
    matched: int
    unmatched: int
    outcomes: tuple[MatchOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class WatchStateMerge:
    """One inbound watch record, on its way to `merge_from_source`.

    `play_count` and `last_played_at` default to `None`, meaning the read
    could not determine it -- carried down from `SourceWatchState` so the
    repository never reaches back into a port DTO it does not own. `0` is a
    positive claim and is written.

    `observed_at` is the run's start instant and carries the conflict rule:
    latest `updated_at` wins, so a stored row newer than this was written by
    something that knows more recent truth and is left alone.
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
    """One client-originated watch write, on its way to `WatchStateRepository.set_from_client`.

    The other direction from `WatchStateMerge`. No `observed_at`: that rule
    answers whether the walk saw something newer than what is stored, and a
    client write is never asked it -- `origin = api` always wins, because
    `trg_watch_states_set_updated_at` stamps every write with the instant it
    happened, later by construction than any walk that started before it.

    No `play_count`, `last_played_at` or `runtime_seconds`: a client reports
    what it did -- seek, mark played, mark unplayed -- not a count or a
    duration, and `set_from_client` derives the rest from `played`.
    """

    user_id: uuid.UUID
    title_id: uuid.UUID | None
    episode_id: uuid.UUID | None
    position_seconds: int
    played: bool


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What an availability sweep actually changed, and out of how many.

    `total` is the source's whole item count, available or not, which the
    sweep already counted for its own guard. A bare "3 retracted" is not an
    operational event: "3 of 4" and "3 of ninety thousand" want different
    responses, and `sync_runs.items_retracted` stores only the numerator.

    No `restored` count. Restoring an item that came back is `upsert_many`'s
    doing -- appearing in a walk is the evidence of availability -- and the
    sweep only ever sets `false`, so the field could only report zero.
    """

    retracted: int
    total: int


class AvailabilitySweepRefused(UsherPortError):
    """The sweep would retract more of a source than the ceiling permits, so it retracted nothing.

    `SourceAdapter.list_items` already guarantees a walk raises rather than
    truncating, and `ReconcileService` refuses to sweep after a run that
    raised. This covers the residual: a walk that *completes* and returns far
    less than the library holds -- an unmounted drive, a library removed by
    accident, a permissions change on the source's account. No adapter can
    tell that from a genuine mass deletion and Usher cannot undo one, so the
    sweep declines and says so.

    Carries the numbers, not just a message: the operator's next question is
    whether the library really shrank by that much.
    """

    def __init__(self, *, would_retract: int, total: int, ceiling: float) -> None:
        # `total or 1`: the only guard that raises this implies a non-empty
        # source, but a ZeroDivisionError inside the constructor of the error
        # that stops a sweep erasing a library would turn a refusal into a
        # crash.
        share = would_retract / (total or 1)
        super().__init__(
            f"refusing to mark {would_retract} of {total} items unavailable in one run "
            f"({share:.0%} exceeds the {ceiling:.0%} ceiling); "
            "nothing was retracted"
        )
        self.would_retract = would_retract
        self.total = total
        self.ceiling = ceiling
