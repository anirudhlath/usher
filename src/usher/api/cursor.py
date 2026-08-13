"""The opaque cursor: a wire artefact that carries a sort position and nothing
else.

PRD 07: *"Cursor-based (opaque, encodes sort position). Offset paging is not
offered."* The second half is measured rather than asserted --
`MediaItemRepository.list_unmatched`'s `OFFSET` is **43.7 ms at offset 0 and
388.9 ms at offset 1,126,574**, linear per page and quadratic to drain -- which
is why `RawPayloadStore.iterate` and `TitleEmbeddingRepository.list_stale`
already take a typed `after: uuid.UUID`. This module gives that habit a wire
form, and ADR-0034 records the three decisions behind it. In short:

**The cursor never reaches a port.** A repository keeps taking typed keyset
values; the base64 lives here because opacity is a *client-contract* concern.
A port that took a cursor would have to decode one, which means knowing the
sort vocabulary of the layer above it -- and `tests/unit/test_ports_pagination.py`
is what keeps that a fact rather than a habit.

**The cursor is not signed and carries no user.** It holds a version, the
sort-key values, and an 8-byte digest of the query it was minted for. Nothing
in it is secret and every position it names is one the same request reaches by
paging, so a forged cursor is not a capability -- it is a request for a page
the client could have asked for anyway, and the route's own authorisation is
still the thing that answers it. `Settings.secret_key` is deliberately not
read here. **The day a cursor grows a `user_id`, a household filter, or
anything else the route does not re-derive from the request, that stops being
true and this needs a MAC**; `CursorSpec`'s field list is pinned by a test for
exactly that reason.

**The digest is not security, it is coherence.** Without it, a cursor minted
under `sort=year` and replayed against `sort=name` decodes cleanly and
produces a plausible, wrong, silent page. With it, that is a
`400 invalid_cursor`. It is computed over the sort name and the filter state
-- values the client itself sent and is the only party ever holding the
cursor -- so it discloses nothing to anyone who did not already have it.

**Every refusal is a `400 invalid_cursor` problem document with a fixed
sentence.** A cursor is a submitted value, so `api/errors.py`'s rule binds it:
no `detail` here interpolates anything the client sent, and the refusal is
raised as a `ProblemException` rather than left to a pydantic validator, which
would answer 422 and echo the rejected cursor back under `input`.
"""

import base64
import binascii
import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from usher.api.dto.page import Page
from usher.api.dto.problem import ProblemCode
from usher.api.errors import ProblemException

#: Bumped when the payload's shape changes. A cursor minted by the previous
#: deployment is then refused rather than decoded against a component order
#: that has moved under it -- which is what lets a sort's keyset gain a
#: component without a `/v2` of the whole API.
CURSOR_VERSION: Final = 1

#: Eight bytes. Long enough that two of this API's sorts will not collide,
#: short enough that the cursor stays a short query parameter. It is a
#: coherence check and not a MAC, so the bar is accidental collision rather
#: than forgery -- see the module docstring.
_DIGEST_BYTES: Final = 8

# The payload's three members. Single letters because this rides in a query
# string on every page of every listing, and named here so the encoder and
# the decoder cannot drift.
_VERSION_KEY: Final = "v"
_DIGEST_KEY: Final = "q"
_KEYS_KEY: Final = "k"

# The refusal sentences. Fixed, distinct, and interpolating nothing the client
# submitted. Distinct because six causes rendered as one sentence are one
# refusal nobody can diagnose; fixed because the moment one renders a value,
# `api/errors.py`'s whole reason for existing is undone one parameter to the
# left.
_RESUME: Final = "Start from the first page."
_NOT_BASE64: Final = f"The cursor is not valid base64url text. {_RESUME}"
_NOT_A_PAYLOAD: Final = f"The cursor does not decode to a pagination cursor. {_RESUME}"
_WRONG_VERSION: Final = f"The cursor was minted by a different version of this API. {_RESUME}"
_WRONG_QUERY: Final = (
    "The cursor was minted for a different query. "
    "Start from the first page after changing the sort order or the filters."
)
_WRONG_ARITY: Final = f"The cursor does not carry this sort order's key. {_RESUME}"
_WRONG_TYPE: Final = f"The cursor's sort key is not the type this sort order uses. {_RESUME}"


class CursorType(StrEnum):
    """The wire tag of one keyset component.

    A tag rather than bare JSON values, because "the same typed sort key"
    is the contract: JSON cannot tell `1` from `1.0`, and a UUID and a
    timestamp are both strings to it. `NULL` is a tag a *value* may carry and
    never a type a spec may declare -- see `CursorSpec`.
    """

    STR = "s"
    INT = "i"
    FLOAT = "f"
    UUID = "u"
    DATETIME = "t"
    NULL = "n"


#: One component of a sort position. Deliberately not `bool`: `isinstance(True,
#: int)` is true, so a bool would ride as an integer and come back as `1`,
#: naming a position no row is at. A nullable sort column reaches `None`
#: instead, which is a position and is why `NULL` is a tag.
type CursorValue = str | int | float | uuid.UUID | dt.datetime | None


@dataclass(frozen=True, slots=True)
class CursorSpec:
    """One sort order's wire identity and keyset shape.

    Three fields, and the *absences* are the design. There is no `user`, no
    `household`, no offset and no page number: a cursor carries a sort
    position and nothing else, which is what makes it safe to leave unsigned.
    `tests/unit/test_api_cursor.py::test_a_spec_holds_no_household_and_no_secret`
    pins the field list so a fourth one has to argue for itself.

    `filters` is the rest of the query -- whatever else narrows the
    population. It is **not** carried in the cursor; only its digest is, and
    the digest is what refuses a cursor minted over `genre=horror` and
    replayed against `genre=comedy`. Rendered in sorted order, so a client
    that reorders its own query string on a retry does not lose its place.

    **`types` must end in a unique component, and this class refuses to exist
    otherwise.** A keyset over a non-unique column is not a total order, and
    the damage is silent: `RawPayloadStore.iterate`'s docstring already
    records that one bootstrap transaction stamps every row with the same
    `transaction_timestamp()`, so a page boundary inside that group drops the
    rest of it with nothing to say so. Three groups write keyset SQL
    independently this milestone; refusing it once here is cheaper than each
    of them remembering. Usher's unique component is always its UUIDv7
    primary key (ADR-0003), so the rule is spelled as that.
    """

    sort: str
    types: tuple[CursorType, ...]
    filters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sort:
            raise ValueError("a cursor spec needs a sort name; it is half of the query digest")
        if not self.types:
            raise ValueError("a cursor spec needs at least one keyset component")
        if CursorType.NULL in self.types:
            raise ValueError(
                "NULL is a value a component may take, not a type it may be declared as: "
                "a component that is always null is not a sort position"
            )
        if self.types[-1] is not CursorType.UUID:
            raise ValueError(
                "a keyset must be a total order, so its last component must be the UUIDv7 "
                f"primary key; {self.sort!r} ends in {self.types[-1].name}"
            )
        # Copied, then wrapped. The copy stops a caller mutating the dict it
        # handed over -- which would silently change the digest of a spec a
        # route holds as a module constant -- and the proxy stops the spec
        # mutating its own. Immutability only: `mappingproxy` delegates
        # `__hash__` to the dict it wraps, which is `None`, so this dataclass
        # is not hashable and does not claim to be (CLAUDE.md).
        object.__setattr__(self, "filters", MappingProxyType(dict(self.filters)))

    @property
    def digest(self) -> str:
        """Eight bytes over the sort name and the filter state.

        Not over `types`: an arity or a type mismatch has its own refusal, and
        folding them in here would collapse three diagnosable causes into one
        "different query".
        """
        material = "\x1f".join(
            [self.sort, *(f"{name}={value}" for name, value in sorted(self.filters.items()))]
        )
        return hashlib.blake2b(material.encode(), digest_size=_DIGEST_BYTES).hexdigest()


def over_fetch(limit: int) -> int:
    """How many rows to ask the repository for, to serve `limit` of them.

    One more, not one page more. That single extra row is the whole difference
    between "this page is full" and "there is more", and it is what stops a
    population whose size is an exact multiple of the limit from minting a
    cursor to nothing -- a client must never have to make a request to learn
    it is finished. The row is read and never served: `paginate` truncates.
    """
    return limit + 1


def encode_cursor(values: Sequence[CursorValue], *, spec: CursorSpec) -> str:
    """Mint the cursor naming the position `values` is at.

    Raises `ValueError`, never a `ProblemException`: everything this can
    refuse is a mistake made *here*, by the route, with nothing a client
    submitted involved. Unpadded `urlsafe` base64, so the token needs no
    percent-encoding and survives every client's query-string encoder
    unchanged.
    """
    if len(values) != len(spec.types):
        raise ValueError(
            f"{spec.sort!r} takes {len(spec.types)} keyset components, given {len(values)}"
        )
    keys = [
        _to_wire(value, declared=declared)
        for declared, value in zip(spec.types, values, strict=True)
    ]
    payload = {
        _VERSION_KEY: CURSOR_VERSION,
        _DIGEST_KEY: spec.digest,
        _KEYS_KEY: keys,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(raw: str, *, spec: CursorSpec) -> tuple[CursorValue, ...]:
    """The position a cursor names, as the same typed values it was minted
    from.

    Raises `ProblemException` -- `400 invalid_cursor` -- for every malformed
    input, in the order the causes can be told apart: a cursor that is not
    base64 cannot be checked for a version, and one minted for another query
    is not an arity problem even when its arity also differs.
    """
    payload = _payload(raw)
    if payload.get(_VERSION_KEY) != CURSOR_VERSION:
        raise _invalid(_WRONG_VERSION)
    if payload.get(_DIGEST_KEY) != spec.digest:
        raise _invalid(_WRONG_QUERY)
    keys = payload.get(_KEYS_KEY)
    if not isinstance(keys, list) or len(keys) != len(spec.types):
        raise _invalid(_WRONG_ARITY)
    return tuple(
        _from_wire(entry, declared=declared)
        for declared, entry in zip(spec.types, keys, strict=True)
    )


def paginate[RowT, ItemT](
    fetched: Sequence[RowT],
    *,
    limit: int,
    spec: CursorSpec,
    keys: Callable[[RowT], Sequence[CursorValue]],
    item: Callable[[RowT], ItemT],
) -> Page[ItemT]:
    """Turn `over_fetch(limit)` rows into a page of at most `limit`.

    `fetched` is what the repository answered when asked for `over_fetch(limit)`
    rows. More than `limit` of them means there is another page; exactly
    `limit` means this was the last one, and reading *that* as "there is more"
    is the off-by-one this whole design exists to remove.

    Two callables rather than one, and the split is not cosmetic. `keys` reads
    the **row**, because a sort key is very often a column the wire shape does
    not carry (`popularity`, `added_at`); `item` produces the DTO, and is
    never run on the over-fetched row, whose only job is to answer "is there
    more" before being discarded.
    """
    kept = list(fetched[:limit])
    exhausted = len(fetched) <= limit
    next_cursor = None if exhausted or not kept else encode_cursor(keys(kept[-1]), spec=spec)
    return Page(items=[item(row) for row in kept], next_cursor=next_cursor)


def _invalid(detail: str) -> ProblemException:
    """One line for a route to adopt, and the reason `ProblemCode` already
    carries `INVALID_CURSOR`: `api/errors.py`'s status table cannot map this,
    because no *status* implies it -- a 400 is not always a bad cursor."""
    return ProblemException(status_code=400, code=ProblemCode.INVALID_CURSOR, detail=detail)


def _payload(raw: str) -> Mapping[str, Any]:
    """base64url -> JSON -> a mapping, refusing at each step separately."""
    padded = raw + "=" * (-len(raw) % 4)
    try:
        # `validate=True`, and it is load-bearing rather than pedantic:
        # `base64.urlsafe_b64decode` **discards** every character outside the
        # alphabet by default, so `!!not-base64!!` decodes to plausible
        # garbage and is refused two steps later as "not a payload". The
        # cause a client is told is then the wrong one.
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise _invalid(_NOT_BASE64) from exc
    try:
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid(_NOT_A_PAYLOAD) from exc
    if not isinstance(parsed, dict):
        # A bare `5` or `[1, 2]` is valid JSON and is not a cursor. Without
        # this the `.get` calls below would be an `AttributeError`, i.e. a
        # 500 for a value a client typed.
        raise _invalid(_NOT_A_PAYLOAD)
    return parsed


def _to_wire(value: CursorValue, *, declared: CursorType) -> list[Any]:
    """One keyset component as `[tag, value]`.

    The `bool` arm is first and is not defensive tidiness: `isinstance(True,
    int)` is true, so without it a `bool` falls into the integer arm, rides as
    `1`, and names a sort position no row is at.
    """
    if value is None:
        return [CursorType.NULL.value, None]
    if isinstance(value, bool):
        raise ValueError(
            "a bool is not a sort position: it would ride as an integer and come back as 1"
        )
    tag, wire = _tagged(value)
    if tag is not declared:
        raise ValueError(f"this component is declared {declared.name}, given {tag.name}")
    return [tag.value, wire]


def _tagged(value: str | int | float | uuid.UUID | dt.datetime) -> tuple[CursorType, Any]:
    if isinstance(value, str):
        return CursorType.STR, value
    if isinstance(value, uuid.UUID):
        return CursorType.UUID, str(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise ValueError(
                "a naive datetime is not a sort position: it renders identically to an aware "
                "one minus its offset, so the position it names depends on the reader's zone"
            )
        return CursorType.DATETIME, value.isoformat()
    if isinstance(value, int):
        return CursorType.INT, value
    if isinstance(value, float):
        return CursorType.FLOAT, value
    raise ValueError(f"{type(value).__name__} is not a sort position this codec can carry")


def _from_wire(entry: Any, *, declared: CursorType) -> CursorValue:
    """One `[tag, value]` back to the typed value it was minted from.

    Every failure here is `_WRONG_TYPE`, including a malformed entry: a
    component that is not a two-element `[tag, value]` pair is a component
    whose type cannot be read at all.
    """
    if not isinstance(entry, list) or len(entry) != 2:
        raise _invalid(_WRONG_TYPE)
    tag, wire = entry
    if tag == CursorType.NULL.value:
        if wire is not None:
            raise _invalid(_WRONG_TYPE)
        return None
    if tag != declared.value:
        raise _invalid(_WRONG_TYPE)
    # `bool` is refused on every arm for the reason `_to_wire` refuses it:
    # JSON's `true` is an `int` to `isinstance` and would decode to 1.
    if isinstance(wire, bool):
        raise _invalid(_WRONG_TYPE)
    match declared:
        case CursorType.STR:
            if not isinstance(wire, str):
                raise _invalid(_WRONG_TYPE)
            return wire
        case CursorType.INT:
            if not isinstance(wire, int):
                raise _invalid(_WRONG_TYPE)
            return wire
        case CursorType.FLOAT:
            if not isinstance(wire, float):
                raise _invalid(_WRONG_TYPE)
            return wire
        case CursorType.UUID:
            if not isinstance(wire, str):
                raise _invalid(_WRONG_TYPE)
            try:
                return uuid.UUID(wire)
            except ValueError as exc:
                raise _invalid(_WRONG_TYPE) from exc
        case CursorType.DATETIME:
            if not isinstance(wire, str):
                raise _invalid(_WRONG_TYPE)
            try:
                parsed = dt.datetime.fromisoformat(wire)
            except ValueError as exc:
                raise _invalid(_WRONG_TYPE) from exc
            if parsed.tzinfo is None:
                raise _invalid(_WRONG_TYPE)
            return parsed
        case CursorType.NULL:  # pragma: no cover - refused by CursorSpec
            raise _invalid(_WRONG_TYPE)


__all__ = [
    "CURSOR_VERSION",
    "CursorSpec",
    "CursorType",
    "CursorValue",
    "decode_cursor",
    "encode_cursor",
    "over_fetch",
    "paginate",
]
