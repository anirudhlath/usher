"""Request and response shapes for the admin source routes."""

import uuid

from pydantic import AwareDatetime, BaseModel, Field, SecretStr

from usher.domain.enums import SourceKind
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus, WalkProgress, WalkStage
from usher.ports.source import SourceStatus


class SourceCreateRequest(BaseModel):
    kind: SourceKind
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: SecretStr


class SourceResponse(BaseModel):
    id: uuid.UUID
    kind: SourceKind
    name: str
    base_url: str
    # Not a secret, and useful: it is how an operator finds Usher's session
    # in Emby's own dashboard in order to revoke it.
    device_id: str
    enabled: bool
    supports_push: bool
    created_at: AwareDatetime

    @classmethod
    def of(cls, source: Source) -> "SourceResponse":
        return cls(
            id=source.id,
            kind=source.kind,
            name=source.name,
            base_url=source.base_url,
            device_id=source.device_id,
            enabled=source.enabled,
            supports_push=source.supports_push,
            created_at=source.created_at,
        )


class SyncRunResponse(BaseModel):
    """`last_sync`: a source's live whole-library walk, else its newest full or delta walk.

    `stage`, `units_done`, `units_total` and `items_expected` say where a
    whole-library walk's plan stands -- `walk_progress` -- and are `null` for a walk
    without one. `heartbeat_at` tells a live `running` walk from a dead one: a
    whole-library walk's writer moves it at least once a minute. `error` is the
    run's own sentence, built like `SourceStatusResponse.detail` from translated
    port errors.
    """

    kind: SyncRunKind
    status: SyncRunStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    heartbeat_at: AwareDatetime | None
    items_seen: int
    items_matched: int
    items_unmatched: int
    items_retracted: int
    error: str | None
    stage: WalkStage | None
    units_done: int | None
    units_total: int | None
    items_expected: int | None

    @classmethod
    def of(cls, run: SyncRun, walk: WalkProgress | None) -> "SyncRunResponse":
        return cls(
            kind=run.kind,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            heartbeat_at=run.heartbeat_at,
            items_seen=run.items_seen,
            items_matched=run.items_matched,
            items_unmatched=run.items_unmatched,
            items_retracted=run.items_retracted,
            error=run.error,
            stage=None if walk is None else walk.stage,
            units_done=None if walk is None else walk.units_done,
            units_total=None if walk is None else walk.units_total,
            items_expected=None if walk is None else walk.items_expected,
        )


class SourceStatusResponse(BaseModel):
    """PRD 07's `GET /admin/sources/{id}/status`.

    `push_available` is `bool | None` and `null` means "not probed" -- see
    `SourceStatus`. An admin UI renders that as "unknown".

    `is_administrator` is `bool | None` on the same three-valued pattern. A source
    configured with an Emby administrator account rides that token in every
    playback URL and opens a long-lived push socket; the mitigation is PRD 03's
    "configure a normal user", which an operator can only follow if they can see
    which they did.

    `detail` is the adapter's own operator-facing status line, built from
    translated `usher.ports.errors` exceptions. Those carry a method, a
    path, and a transport error -- never a credential, never a
    `credentials_ref` -- which is what makes it safe to hand to a client
    verbatim.

    `last_sync` is the source's live whole-library walk if it has one, else its newest
    full or delta walk -- never the watch lane's -- or `null` before the first.
    """

    reachable: bool
    authenticated: bool
    push_available: bool | None
    is_administrator: bool | None
    server_version: str | None
    detail: str | None
    last_sync: SyncRunResponse | None

    @classmethod
    def of(cls, status: SourceStatus, last_sync: SyncRunResponse | None) -> "SourceStatusResponse":
        return cls(
            reachable=status.reachable,
            authenticated=status.authenticated,
            push_available=status.push_available,
            is_administrator=status.is_administrator,
            server_version=status.server_version,
            detail=status.detail,
            last_sync=last_sync,
        )


class SyncTriggerResponse(BaseModel):
    """`POST /admin/sources/{id}/sync`'s whole body -- the enqueued job's identity.

    The route promises exactly one thing, that this row is on the queue at
    `JobPriority.DEMAND` or was already there, and `(kind, key)` is the only fact
    about it a reader can still act on.

    `key` is `"{source_id}:{lane}"`, never a bare source id --
    `usher.domain.jobs.JobKind.SYNC` says why the composite is deliberate.
    """

    kind: JobKind
    key: str
