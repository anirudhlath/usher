# ADR-0029 — The playback ticket changes the artifact, not the grant

**Status:** Accepted — the M9 successor
[0012](0012-playback-urls-carry-a-source-token.md) named, implementing its
option 1

## Context

[0012](0012-playback-urls-carry-a-source-token.md) recorded a contradiction
this project could not have both halves of. [07](../07-client-api.md) says
*"Usher supplies complete information and never proxies bytes"*;
[08](../08-operations.md) says *"No credential ever reaches a client"*. Emby
authenticates `/Videos/{id}/stream.{container}` and neither a `<video>` element
nor a deep link can set a header, so v1 shipped the token in
`StreamTarget.url`, deliberately and with the risk written down.

ADR-0012 named two successors and preferred the first: **a playback ticket**,
where `POST /titles/{id}/play` answers `https://usher/stream/{opaque}` and
Usher redeems it into a `302`. The second — a per-client scoped token — needs a
client identity that does not exist until authentication does, and
authentication is out for all of M9.

**ADR-0012 was also exact about what the preferred option does not do**, and
that exactness is the thing this ADR must not quietly drop: *"Neither option
below removes the credential from the wire."*

## Decision

**A ticket is a Fernet token over an HKDF-SHA256 subkey of `USHER_SECRET_KEY`,
domain-separated with `info=b"usher.playback-ticket.v1"`, carrying the source
URL as its plaintext, stateless, and redeemed into a `302`.**

Four parts, each of which a reasonable person could have decided differently:

**1. Encrypted, not merely signed.** The payload *is* the Emby direct URL
carrying `api_key`. An HMAC-signed-but-readable token — a JWT with no
encryption, the obvious cheap choice — would publish the credential the ticket
exists to hide, while looking opaque to anyone who did not base64-decode it.
Fernet is AES-128-CBC with an HMAC-SHA256 tag, which is both halves.

**2. A subkey, not the secret, and separated by `info`.**
`usher.db.repositories.credentials` already derives a Fernet key from
`USHER_SECRET_KEY` with `info=b"usher.source-credentials.v1"`, and its
docstring already promised that subkey was *"domain-separated from any other
use a later milestone makes"*. This is that milestone. The two `info` strings
are the whole of the separation, and it is asserted in both directions rather
than assumed.

**3. Stateless, so there is no revocation before expiry.** Nothing is written
to any table. `Fernet.decrypt_at_time(token, ttl=…)` authenticates the
timestamp that is already inside the authenticated envelope, so a ticket needs
no store to expire — and therefore has no store an operator could delete a row
from. **This is a real cost, accepted rather than solved.** The alternative is
a `playback_tickets` table with a nightly sweep, and it buys revocation of an
artifact that is already short-lived while adding a write to the hot path of
every play. [08](../08-operations.md)'s existing answer for a compromised
deployment — rotate `USHER_SECRET_KEY`, which invalidates every outstanding
ticket at once — is the coarse revocation that does exist.

**4. Expired and forged answer the same thing.** `Fernet.extract_timestamp`
verifies the signature *before* returning the timestamp, so a distinguishing
error is genuinely available and is deliberately not taken. *"This ticket
expired"* confirms to a holder that the string was a real Usher-minted ticket,
and the client's next move is identical either way: ask `/play` again. The
redemption path answers `None` for both and raises nothing, so there is no
exception message for a URL to leak into.

## Consequences

**What it changes is the artifact, not the grant** — ADR-0012's own phrase. A
`302` puts the real URL in `Location`, which the client reads by definition;
the token still reaches it. What the client *stores, renders, caches, or pastes
into a chat* becomes an opaque, short-lived string instead of a working
credential, and that is a genuine reduction, because most leaks are leaks of
the artifact.

**It is weakest for the `deep_link` target, and that is not a corner case.**
The deep link hands the ticket to a third-party player, which follows the
redirect and then holds the real URL exactly as it does today. For that target
the reduction is close to nil — Usher's own response body no longer carries the
credential, and the player's process does. An ADR that only claimed the win
would be the version a future reader would be right to distrust.

**Rotating `USHER_SECRET_KEY` invalidates every outstanding ticket.** Correct
rather than a bug — tickets are short-lived, and the alternative is a rotation
window in which a superseded key still mints working redirects. Recorded in the
module docstring so it is not rediscovered as a defect.

**`/play`'s response body now carries no credential at all**, which retires an
exception rather than narrowing it. The M9 spec's acceptance criterion — *"no
credential in any response body that is not `POST /play`'s deliberate one"* —
was written against the pre-ticket design and inherited ADR-0012's text. With
the ticket, that exception is **empty**.

**ADR-0012's field-access rules become testable and stay in force.**
`StreamTarget.__repr__` and `redact_query` are unchanged and still hold; what
changes is that the three serializer paths ADR-0012 recorded as leaking
`StreamTarget.url` with *"no test currently pins them"* now have a reason to be
pinned, because the URL is no longer meant to reach a client by any route.

**No `USHER_PLAYBACK_TICKET_TTL_SECONDS`.** [08](../08-operations.md)'s
mechanism-before-the-setting rule cuts against a setting here: nobody has
measured how long a client sits between receiving a target and following it,
and a setting whose default is a guess is a guess with a config key on it. The
TTL is a named constant at the one place that mints, and the redemption
primitive requires it as an argument with no default — the cipher does not get
an opinion about how long a client takes to press play.

**No byte proxying.** `GET /stream/{ticket}` is a `302`; the client fetches the
target itself, so [07](../07-client-api.md)'s *"never proxies bytes"* is
untouched.

## Evidence

Measured on the installed `cryptography` **49.0.0**, 2026-08-11.

**The primitives make expiry deterministic.** `Fernet.encrypt_at_time(data,
current_time: int)` and `Fernet.decrypt_at_time(token, ttl: int, current_time:
int)` both exist, so minting and redeeming take the instant as an argument and
every expiry case is an argument rather than a wait — no `sleep`, no patched
clock. Verified on both sides of one boundary: redeemable at `minted_at + ttl -
1`, `InvalidToken` at `minted_at + ttl + 1`.

**Domain separation holds in both directions** against one `SecretStr`: a blob
encrypted by `credentials.build_cipher` does not redeem as a ticket, and a
ticket handed to that cipher's `decrypt` raises `InvalidToken`. One arm alone
is satisfied by a cipher that decrypts nothing, which is why both are asserted.

**A ticket is a legal path segment, and the obvious way to check that is
wrong.** A Fernet token is `base64url(1 + 8 + 16 + ciphertext + 32)` bytes with
the ciphertext AES-CBC-padded to a 16-byte block, so its length — and with it
its base64 `=` padding — moves in bands of 16 plaintext characters. A realistic
184-character Emby direct URL mints a **332-character** token, and for that
token `quote(ticket, safe="") == ticket`.

**That no-op is a property of the band, not of the token**, and this is the
measurement that refuted the prediction this ADR was written from. Over
plaintext lengths 1–599: **192 mint an unpadded token, 200 mint one `=`, 207
mint two**, so `quote(ticket, safe="")` is a no-op for **32%** of lengths and
re-encodes `=` to `%3D` for the other 68%. Those three tallies are asserted
over that exact range by
`test_a_ticket_is_a_legal_path_segment_but_quote_safe_empty_is_not_a_no_op`
rather than recorded as a one-off — they shipped as narration beside a loop
that ran a third of the range, and a review caught it. The 176–191 band the 184-character
sample lands in is one of the unpadded ones; the *same URL on a host one
character shorter* is 175 characters and mints a padded 312-character token for
which the no-op fails. What is true at every length is the claim that matters:
the alphabet is url-safe base64 plus `=`, `=` is an RFC 3986 sub-delim and
therefore a legal `pchar`, so a ticket needs no encoding step — and
`quote(ticket, safe="=")` is a no-op at all 599 lengths measured.

**A non-ASCII token raises `ValueError`, not `InvalidToken`.** `decrypt_at_time`
reaches `str.encode("ascii")` before any signature check, so a `str` outside
ASCII raises a bare `ValueError("string argument should contain only ASCII
characters")`. A percent-decoded path segment is exactly such a `str`, so
catching `InvalidToken` alone would turn `GET /stream/t%C3%ADck%C3%A9t` into a
500 rather than a refusal. Both are caught. Garbage, truncated, empty,
expired, wrong-secret and non-ASCII all answer the same thing and raise
nothing.

**The secret is unwrapped once.** `get_secret_value()` appears exactly once in
the module, inside the key derivation, and the plaintext is never bound to a
name that outlives the call — `credentials.py`'s rule, asserted structurally
rather than by convention.
