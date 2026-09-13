"""Request and response shapes for the admin source routes."""

import uuid

from pydantic import AwareDatetime, BaseModel, Field, SecretStr

from usher.domain.enums import SourceKind
from usher.domain.jobs import JobKind
from usher.domain.source import Source
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


class SourceStatusResponse(BaseModel):
    """PRD 07's `GET /admin/sources/{id}/status`.

    `push_available` is `bool | None` and `null` means "not probed" -- see
    `SourceStatus`. An admin UI renders that as "unknown", which is the
    honest answer until M5's probe asserts on received messages.

    `is_administrator` is `bool | None` on the same three-valued pattern and
    for a sharper reason -- see `SourceStatus`. ADR-0012 accepts the risk
    that a source is configured with an Emby administrator account, whose
    token then rides in every playback URL and (from M5) opens a long-lived
    push socket; the recorded mitigation is PRD 03's "configure a normal
    user", which is guidance an operator can only follow if they can see
    which they did.

    `detail` is the adapter's own operator-facing status line, built from
    translated `usher.ports.errors` exceptions. Those carry a method, a
    path, and a transport error -- never a credential, never a
    `credentials_ref` -- which is what makes it safe to hand to a client
    verbatim.
    """

    reachable: bool
    authenticated: bool
    push_available: bool | None
    is_administrator: bool | None
    server_version: str | None
    detail: str | None

    @classmethod
    def of(cls, status: SourceStatus) -> "SourceStatusResponse":
        return cls(
            reachable=status.reachable,
            authenticated=status.authenticated,
            push_available=status.push_available,
            is_administrator=status.is_administrator,
            server_version=status.server_version,
            detail=status.detail,
        )


class SyncTriggerResponse(BaseModel):
    """`POST /admin/sources/{id}/sync`'s whole body.

    the enqueued job's identity, on the same shape
    `usher.api.dto.rows.RegenerateResponse` uses for `POST /admin/rows/regenerate` --
    both routes promise exactly one thing, that this row is on the queue at
    `JobPriority.DEMAND` or was already there, and `(kind, key)` is the only fact about
    it a reader can still act on.

    `key` is `"{source_id}:{lane}"`, never a bare source id --
    `usher.domain.jobs.JobKind.SYNC` says why the composite is deliberate.
    """

    kind: JobKind
    key: str
