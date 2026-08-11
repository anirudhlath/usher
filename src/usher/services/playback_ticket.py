"""The playback ticket: encrypt a stream URL under a subkey of `USHER_SECRET_KEY`.

[ADR-0029](../../../docs/prd/decisions/0029-the-playback-ticket-changes-the-artifact-not-the-grant.md)
is the decision; this is the primitive. `POST /titles/{id}/play` answers with
`https://usher/stream/{ticket}` instead of the source URL, and
`GET /stream/{ticket}` redeems one into a `302` whose `Location` is the real
target. What that changes is the *artifact*, not the grant --
[ADR-0012](../../../docs/prd/decisions/0012-playback-urls-carry-a-source-token.md)'s
own phrase, and its named M9 successor.

**Encrypted, not merely signed.** The payload *is* the Emby direct URL,
carrying the `api_key` that is the whole capability grant, so an
HMAC-signed-but-readable token would publish the credential it exists to hide.
Fernet is AES-128-CBC with an HMAC-SHA256 tag: confidentiality and
authenticity, which is both halves of what a ticket needs.

**Domain-separated from the credential store, and that is now measured rather
than promised.** `usher.db.repositories.credentials` derives its Fernet key
from the same `USHER_SECRET_KEY` with `info=b"usher.source-credentials.v1"`,
and its docstring already anticipated this module -- *"this subkey is
domain-separated from any other use a later milestone makes of
`USHER_SECRET_KEY`"*. The two `info` strings are the whole of the separation,
and `tests/unit/test_services_playback_ticket.py` asserts it in both
directions: a credential blob does not redeem as a ticket, and a ticket does
not decrypt as a credential blob.

**Pure functions in `services/`, not a class and not in `db/`: there is no
table.** The ticket is stateless by decision -- nothing is written anywhere,
`Fernet.decrypt_at_time` is what authenticates the timestamp, and the accepted
cost is that there is no revocation before expiry. A module rather than a
method for the reason M8 landed `curation_prompt.py` separately: an artefact
whose only real consumer is elsewhere gets no coverage unless a case opts in by
name, and a sweep that walks a service's control flow is blind to it.

**Rotating `USHER_SECRET_KEY` invalidates every outstanding ticket.** That is
correct rather than a bug -- tickets are short-lived, and the alternative would
be a rotation window during which a superseded key still mints working
redirects. Written here so it is not rediscovered as a defect.

**No TTL constant lives here.** `redeem`'s `ttl_seconds` is required and has no
default: this primitive does not get an opinion about how long a client takes
to press play. The constant, and the reasoning for its value, belong at the one
place that mints -- the `/play` route -- and PRD 08's
mechanism-before-the-setting rule is why it is a constant rather than
`USHER_PLAYBACK_TICKET_TTL_SECONDS`.

**Two measurements this module's tests carry, both of which refuted a
prediction, and both of which the redirect route inherits:**

- A `str` token outside ASCII makes `Fernet.decrypt_at_time` raise a bare
  `ValueError`, not `InvalidToken`, because it reaches `str.encode("ascii")`
  before any signature check. A percent-decoded path segment is exactly such a
  `str`, so `redeem` catches both and a non-ASCII ticket is a `None` rather
  than a 500.
- A ticket's alphabet is url-safe base64 *plus `=`*, and `=` is an RFC 3986
  sub-delim, hence a legal `pchar`: a ticket needs no encoding step to sit in a
  path segment. But `quote(ticket, safe="")` is **not** a no-op in general --
  it re-encodes `=` to `%3D`, and only 192 of the 599 plaintext lengths 1--599
  mint an unpadded token (200 mint one `=`, 207 mint two), so that spelling
  holds for under a third of them. `quote(ticket, safe="=")` is a no-op at
  every length. Those three tallies are **asserted** over that exact range by
  `test_a_ticket_is_a_legal_path_segment_but_quote_safe_empty_is_not_a_no_op`,
  not merely recorded here.
"""

import base64
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

__all__ = ["build_ticket_cipher", "mint", "redeem"]

_HKDF_INFO = b"usher.playback-ticket.v1"


def build_ticket_cipher(secret_key: SecretStr) -> Fernet:
    """Derive this deployment's ticket-encryption key.

    `credentials.build_cipher`'s shape exactly, with one `info` string
    changed: HKDF-SHA256 rather than a password-based KDF because the input is
    already high-entropy (`Settings.secret_key` enforces `min_length=32` and
    the documented way to produce it is `openssl rand -hex 32`), and the `info`
    string versioned so a future scheme change becomes a new derivation rather
    than a silent reinterpretation of old ciphertext.

    `get_secret_value()` is unwrapped exactly once, here, and the plaintext is
    never bound to a name -- only the derived key, which is an HKDF output and
    not the secret, outlives the call.
    """
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        secret_key.get_secret_value().encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def mint(cipher: Fernet, url: str, *, minted_at: datetime) -> str:
    """Encrypt `url` into a ticket stamped `minted_at`.

    The stamp rides inside the authenticated envelope, so a holder can neither
    read it nor move it. `minted_at` is an argument rather than a clock read so
    every expiry case is deterministic -- no `sleep`, no patched `time.time`.
    """
    return cipher.encrypt_at_time(url.encode("utf-8"), _epoch_seconds(minted_at)).decode("ascii")


def redeem(cipher: Fernet, token: str, *, now: datetime, ttl_seconds: int) -> str | None:
    """Answer the URL a ticket carries, or `None` if it will not be honoured.

    **Expired and forged answer the same thing, and that is a decision.**
    `Fernet.extract_timestamp` verifies the signature before returning the
    timestamp, so the distinction is genuinely available; it is deliberately
    not taken. *"This ticket expired"* confirms to a holder that the string was
    a real Usher-minted ticket, and the client's next move is identical either
    way -- ask `/play` again. Nothing raises, so there is no exception message
    for a URL to leak into.

    `ValueError` is caught beside `InvalidToken` for a measured reason: a `str`
    token outside ASCII reaches `str.encode("ascii")` inside the primitive
    before any signature check and raises a bare `ValueError`. A percent-decoded
    path segment is exactly such a `str`.

    The instant is converted *before* the `try`, deliberately. A naive `now` is
    a broken caller clock rather than a bad ticket, and collapsing the two into
    `None` would hide a bug in this project's own code behind a client-facing
    404.
    """
    current_time = _epoch_seconds(now)
    try:
        plaintext = cipher.decrypt_at_time(token, ttl=ttl_seconds, current_time=current_time)
    except (InvalidToken, ValueError):
        return None
    return plaintext.decode("utf-8")


def _epoch_seconds(moment: datetime) -> int:
    """Convert an aware `datetime` to the `int` the Fernet primitives take.

    One place, because the primitives take `int` seconds and the two silent
    failures either side of that are both worth refusing once rather than per
    caller: a naive `datetime.timestamp()` quietly means *local* time, and a
    `float` handed to `encrypt_at_time` truncates without complaint.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("a playback ticket instant must be an aware datetime")
    return int(moment.timestamp())
