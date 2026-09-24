"""The playback ticket: encrypt a stream URL under a subkey of `USHER_SECRET_KEY`."""

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

    Expired and forged answer the same thing, and that is a decision.
    `Fernet.extract_timestamp` verifies the signature before returning the
    timestamp, so the distinction is genuinely available; it is deliberately
    not taken. *"This ticket expired"* confirms to a holder that the string was
    a real Usher-minted ticket, and the client's next move is identical either
    way -- ask `/play` again. Nothing raises, so there is no exception message
    for a URL to leak into.

    `ValueError` is caught beside `InvalidToken` for a concrete reason: a `str`
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
