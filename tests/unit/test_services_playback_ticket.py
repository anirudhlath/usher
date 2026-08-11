"""The ticket cipher: domain separation, the TTL boundary, and the no-oracle rule.

Every case here is deterministic. `Fernet.encrypt_at_time`/`decrypt_at_time`
take the instant as an argument, so expiry is an argument rather than a wait --
no `sleep`, no patched clock, and
`test_no_case_in_this_file_sleeps_or_patches_a_clock` is what keeps it that way
rather than the convention.

Two of this module's facts are measurements that refuted what the plan
predicted, and both are recorded on the cases that carry them:
`test_a_ticket_is_a_legal_path_segment_but_quote_safe_empty_is_not_a_no_op`
and `test_redeem_answers_none_rather_than_raising`'s `non-ascii` arm.
"""

import ast
import base64
import collections
import inspect
import pathlib
import string
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from usher.db.repositories.credentials import build_cipher
from usher.services import playback_ticket

# A realistic Emby direct-play URL, in the shape ADR-0012 records
# `build_stream_targets` producing: three query parameters, of which `api_key`
# is the credential the ticket exists to stop a client from holding.
_URL = (
    "https://emby.example.com/Videos/8f3c1e2a9b7d4f60a1c5e8d2b4a60739/stream.mkv"
    "?static=true&MediaSourceId=8f3c1e2a9b7d4f60a1c5e8d2b4a60739"
    "&api_key=3f9a1c7e5b2d48f0a6c3e9d1b7f42a58"
)

# 32 characters, which is `Settings.secret_key`'s own `min_length`.
_SECRET = SecretStr("0123456789abcdef0123456789abcdef")

_MINTED_AT = datetime(2026, 8, 11, 20, 30, 0, tzinfo=UTC)

# The plaintext lengths the padding tallies in this module's docstring, in
# ADR-0029's Evidence section and in
# `test_a_ticket_is_a_legal_path_segment_but_quote_safe_empty_is_not_a_no_op`
# are all computed over. Named once so the prose and the loop cannot drift --
# which is exactly how they drifted the first time.
_PADDING_SWEEP = range(1, 600)

_MODULE = pathlib.Path(inspect.getfile(playback_ticket))


def _module_tree(*, strip_docstrings: bool) -> ast.Module:
    """Parse the module under test.

    Docstrings are stripped by default because this module *argues* about
    `extract_timestamp` at length, and a scan over raw source would be
    answered by the prose explaining the decision rather than by the code
    keeping it -- the failure `.claude/rules/testing-discipline.md` records
    as "a `Forbidden not in source` scan fails on the module's own
    explanation".
    """
    tree = ast.parse(_MODULE.read_text())
    if not strip_docstrings:
        return tree
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        body = node.body
        leads_with_a_docstring = (
            bool(body)
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        )
        if leads_with_a_docstring:
            node.body = body[1:] or [ast.Pass()]
    return ast.fix_missing_locations(tree)


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(_module_tree(strip_docstrings=True)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in {_MODULE.name}")


def _called_names(tree: ast.AST) -> list[str]:
    return [ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)]


# --------------------------------------------------------------------------
# Domain separation, in both directions against one SecretStr.
# --------------------------------------------------------------------------


def test_a_token_minted_under_the_credential_subkey_does_not_redeem_as_a_ticket() -> None:
    """`credentials.py`'s docstring promised this subkey was "domain-separated
    from any other use a later milestone makes of `USHER_SECRET_KEY`". This is
    that later milestone, and this is where the promise becomes a measurement.

    The positive control is not decoration. A `redeem` that answers `None` for
    everything passes the negative assertion perfectly, so the case first
    proves the ticket cipher redeems *its own* token before proving it refuses
    the credential store's -- the two ciphers differ only in `info`, and both
    are built from the same `SecretStr`.

    **The foreign token is stamped with `encrypt_at_time` at the same instant,
    and that is what makes this a test of the key.** Written with a plain
    `credential_cipher.encrypt()` it was a test of the *clock*: `encrypt`
    stamps with the wall clock, `_MINTED_AT` is a fixed literal, and the two
    were 24,507 seconds apart on the day this was measured -- so the TTL
    refused the token before the key was ever consulted. Planting
    `_HKDF_INFO = b"usher.source-credentials.v1"` (which collapses the two
    ciphers into one) left that spelling **green**, and it is the mutation
    this case exists to kill. `.claude/rules/testing-discipline.md`'s "a
    rejection is not an assertion", arriving at the milestone's own named
    failing test.
    """
    credential_cipher = build_cipher(_SECRET)
    ticket_cipher = playback_ticket.build_ticket_cipher(_SECRET)

    # The premise: one secret, two ciphers, and the ticket cipher works.
    mine = playback_ticket.mint(ticket_cipher, _URL, minted_at=_MINTED_AT)
    assert playback_ticket.redeem(ticket_cipher, mine, now=_MINTED_AT, ttl_seconds=60) == _URL

    foreign = credential_cipher.encrypt_at_time(
        _URL.encode("utf-8"), int(_MINTED_AT.timestamp())
    ).decode("ascii")

    # The premise: the foreign token is inside the TTL window, so the only
    # thing left that can refuse it is the subkey it was minted under.
    assert credential_cipher.decrypt_at_time(
        foreign, ttl=60, current_time=int(_MINTED_AT.timestamp())
    ) == _URL.encode("utf-8")

    assert playback_ticket.redeem(ticket_cipher, foreign, now=_MINTED_AT, ttl_seconds=60) is None


def test_a_ticket_does_not_decrypt_under_the_credential_stores_own_cipher() -> None:
    """The mirror arm. One arm alone is satisfied by a cipher that decrypts
    nothing at all, which is why the separation is asserted from both sides.
    """
    credential_cipher = build_cipher(_SECRET)
    ticket_cipher = playback_ticket.build_ticket_cipher(_SECRET)

    # The premise: the credential cipher round-trips its own payload.
    own = credential_cipher.encrypt(b"a stored credential")
    assert credential_cipher.decrypt(own) == b"a stored credential"

    ticket = playback_ticket.mint(ticket_cipher, _URL, minted_at=_MINTED_AT)

    with pytest.raises(InvalidToken):
        credential_cipher.decrypt(ticket.encode("ascii"))


def test_one_secret_always_derives_the_same_ticket_cipher() -> None:
    """Without this, the two separation cases above could pass because
    `build_ticket_cipher` is nondeterministic rather than because the `info`
    string separates anything -- a cipher freshly randomised per call refuses
    every foreign token for the wrong reason.
    """
    first = playback_ticket.build_ticket_cipher(_SECRET)
    second = playback_ticket.build_ticket_cipher(SecretStr(_SECRET.get_secret_value()))

    ticket = playback_ticket.mint(first, _URL, minted_at=_MINTED_AT)

    assert playback_ticket.redeem(second, ticket, now=_MINTED_AT, ttl_seconds=60) == _URL


@pytest.mark.parametrize(
    ("secret", "expected_key"),
    [
        pytest.param(
            "0123456789abcdef0123456789abcdef",
            "7eNLFj9N37Dz57gB9Lu15_yDFjb7X2tDPoXrV_JgRhE=",
            id="ascii",
        ),
        pytest.param(
            "un-très-long-mot-de-passe-àéîöü-π-2026",
            "8LGjBFc-v4DxotPkAyxWap6vL2fZuUcdRQmYC1aZvfs=",
            id="non-ascii",
        ),
    ],
)
def test_the_subkey_derivation_is_pinned_by_a_known_answer(secret: str, expected_key: str) -> None:
    """**Without this, the whole derivation is unpinned.** Found by the sweep,
    refuting the plan's prediction that changing `salt=None` to a literal salt
    "fails the round-trip": it survives all 3,007 unit cases, because every
    case builds *both* the cipher and the token through `build_ticket_cipher`,
    so a consistently-applied change to the derivation moves every key
    together and nothing can see it. The same is true of `info`, of the
    hash, and of the secret's encoding -- `_HKDF_INFO` is killed only by the
    one value that *collides* with the credential store's subkey, and
    `usher.playback-ticket.v2` would survive exactly as the salt does.

    A known-answer test is the standard answer and the only one that works:
    the expected key is a literal, computed once and written down, **not**
    re-derived here -- re-deriving would restate the same five parameters in a
    second file and move with any mutation applied to both.

    What it pins, therefore: `salt=None`, `info=b"usher.playback-ticket.v1"`,
    `algorithm=SHA256`, `length=32`, and `.encode("utf-8")` of the secret. A
    change to any of them is a scheme change that invalidates every
    outstanding ticket on deploy, which is precisely why the module's `info`
    string is versioned -- so such a change is a new derivation rather than a
    silent reinterpretation.

    **The `non-ascii` arm is why there are two, and it closes a survivor the
    `ascii` arm alone cannot see.** `.encode("utf-8")` mutated to
    `.encode("latin-1")` survived the whole suite against the hex secret,
    because the two codecs agree on every ASCII byte -- the identity-element
    family again, in the codec domain. Nothing constrains `USHER_SECRET_KEY`
    to ASCII (`Settings` enforces `min_length=32` and nothing else; the hex
    form is documentation, not a validator), so an operator with a passphrase
    is a reachable state where the two derive different keys, and one holding
    any character above U+00FF is a state where `latin-1` raises
    `UnicodeEncodeError` at cipher construction. With this arm the mutation
    fails on the key.
    """
    pinned = Fernet(expected_key)

    ticket = playback_ticket.mint(
        playback_ticket.build_ticket_cipher(SecretStr(secret)), _URL, minted_at=_MINTED_AT
    )

    redeemed = pinned.decrypt_at_time(ticket, ttl=60, current_time=int(_MINTED_AT.timestamp()))
    assert redeemed == _URL.encode("utf-8")


def test_a_different_secret_redeems_nothing() -> None:
    """PRD 08's rotation consequence, as a property rather than a docstring:
    rotating `USHER_SECRET_KEY` invalidates every outstanding ticket.
    """
    minted = playback_ticket.build_ticket_cipher(_SECRET)
    rotated = playback_ticket.build_ticket_cipher(SecretStr("fedcba9876543210fedcba9876543210"))

    ticket = playback_ticket.mint(minted, _URL, minted_at=_MINTED_AT)

    assert playback_ticket.redeem(minted, ticket, now=_MINTED_AT, ttl_seconds=60) == _URL
    assert playback_ticket.redeem(rotated, ticket, now=_MINTED_AT, ttl_seconds=60) is None


# --------------------------------------------------------------------------
# The TTL, which is the primitive's own feature.
# --------------------------------------------------------------------------


def test_a_ticket_is_redeemable_one_second_inside_its_ttl_and_not_one_second_outside() -> None:
    """Both sides of the boundary, positive first -- an implementation that
    redeems nothing must not be able to pass the expiry half.

    `ttl_seconds` is passed explicitly because `redeem` has no default for it:
    this module does not get an opinion about how long a client takes to press
    play, and the constant that does live at the route that mints.
    """
    cipher = playback_ticket.build_ticket_cipher(_SECRET)
    ticket = playback_ticket.mint(cipher, _URL, minted_at=_MINTED_AT)
    ttl = 60

    inside = _MINTED_AT + timedelta(seconds=ttl - 1)
    outside = _MINTED_AT + timedelta(seconds=ttl + 1)

    assert playback_ticket.redeem(cipher, ticket, now=inside, ttl_seconds=ttl) == _URL
    assert playback_ticket.redeem(cipher, ticket, now=outside, ttl_seconds=ttl) is None


def test_the_ttl_is_measured_from_when_the_ticket_was_minted() -> None:
    """Kills a `mint` that stamps the token with the wrong instant -- the
    stamp is inside the authenticated envelope, so nothing else can observe
    it. Two tickets for the same URL, minted an hour apart, read at one
    instant under one TTL: the older is expired and the newer is not.
    """
    cipher = playback_ticket.build_ticket_cipher(_SECRET)
    old = playback_ticket.mint(cipher, _URL, minted_at=_MINTED_AT - timedelta(hours=1))
    fresh = playback_ticket.mint(cipher, _URL, minted_at=_MINTED_AT)

    assert playback_ticket.redeem(cipher, fresh, now=_MINTED_AT, ttl_seconds=60) == _URL
    assert playback_ticket.redeem(cipher, old, now=_MINTED_AT, ttl_seconds=60) is None


# --------------------------------------------------------------------------
# The no-oracle rule: expired and forged answer the same thing.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shape",
    ["garbage", "truncated", "expired", "empty", "non-ascii", "another-secret"],
)
def test_redeem_answers_none_rather_than_raising(shape: str) -> None:
    """Expired and forged are deliberately indistinguishable. `Fernet.
    extract_timestamp` verifies the signature *before* handing back the
    timestamp, so the distinction is genuinely available and is not taken:
    "this ticket expired" confirms to a holder that the string was a real
    Usher-minted ticket, and the client's next move is identical either way.

    `non-ascii` is the arm the plan did not name and it is the one with teeth.
    Measured on cryptography 49.0.0: `Fernet.decrypt_at_time` raises a bare
    `ValueError("string argument should contain only ASCII characters")` --
    **not** `InvalidToken` -- for a `str` token outside ASCII, because it
    reaches `str.encode("ascii")` before any signature check. A percent-decoded
    path segment is exactly such a `str`, so `except InvalidToken` alone turns
    `GET /stream/t%C3%ADck%C3%A9t` into a 500. D3 and D4 inherit this.
    """
    cipher = playback_ticket.build_ticket_cipher(_SECRET)
    ticket = playback_ticket.mint(cipher, _URL, minted_at=_MINTED_AT)
    now = _MINTED_AT

    token = {
        "garbage": "not-a-ticket",
        "truncated": ticket[:-12],
        "expired": ticket,
        "empty": "",
        "non-ascii": "tíckét",
        "another-secret": playback_ticket.mint(
            playback_ticket.build_ticket_cipher(SecretStr("z" * 32)), _URL, minted_at=_MINTED_AT
        ),
    }[shape]
    if shape == "expired":
        now = _MINTED_AT + timedelta(days=1)

    # The premise: the same call shape answers the URL for a good ticket, so a
    # `None` below is the token being refused rather than the arguments being
    # wrong.
    assert playback_ticket.redeem(cipher, ticket, now=_MINTED_AT, ttl_seconds=60) == _URL

    assert playback_ticket.redeem(cipher, token, now=now, ttl_seconds=60) is None


def test_the_module_never_asks_whether_a_ticket_merely_expired() -> None:
    """Pins the no-oracle decision structurally rather than leaving it in
    prose. Scanned over a docstring-stripped tree, so the paragraph above
    explaining why `extract_timestamp` is not used cannot answer the scan on
    the code's behalf.
    """
    source = ast.unparse(_module_tree(strip_docstrings=True))

    assert "extract_timestamp" not in source


# --------------------------------------------------------------------------
# The token as a URL path segment -- facts D3 and D4 depend on.
# --------------------------------------------------------------------------


def test_a_ticket_is_a_legal_path_segment_but_quote_safe_empty_is_not_a_no_op() -> None:
    """**The plan's measurement is right at one length and wrong as a rule,
    and D3's deep-link assertion is the thing that would have been ratified by
    the difference.**

    Measured on cryptography 49.0.0. A Fernet token is
    `base64url(1 + 8 + 16 + ciphertext + 32)` bytes, and the ciphertext is
    AES-CBC padded to a 16-byte block -- so the encoded length, and with it the
    base64 `=` padding, moves in bands of 16 plaintext characters. The plan's
    sample is a 184-character URL minting a 332-character token for which
    `quote(token, safe="") == token`; that reproduces exactly, and it holds
    only for the 176--191 band, whose 249-byte token happens to encode with
    **no** padding at all. A realistic Emby URL straddles the boundary -- the
    same URL on a host one character shorter is 175 characters and mints a
    padded token.

    What is true at every length is the claim that actually matters: `=` is an
    RFC 3986 sub-delim and therefore a legal `pchar`, so a ticket needs no
    encoding step to sit in `GET /stream/{ticket}`. It is `quote`'s
    conservative default that is not a no-op, not the URI grammar.

    **The three tallies are asserted, not narrated, and that is a review
    finding rather than a preference.** This case shipped with the prose citing
    a 1--599 sweep as its evidence and a loop that ran `range(1, 200)` -- a
    third of it, tallying 64/64/71, with `padded > 0` as the only check. The
    property was true and the numbers were right, but nothing in the suite
    computed them, so a change that moved the distribution in the untested
    200--599 band would have left this green while contradicting the sentence
    beside it. The loop is now the range the numbers come from and the counts
    are assertions; 599 mints cost **6.4 ms**, which is what makes the choice
    between narrating and enforcing an easy one.
    """
    cipher = playback_ticket.build_ticket_cipher(_SECRET)
    alphabet = set(string.ascii_letters + string.digits + "-_=")

    padding = collections.Counter[int]()
    for length in _PADDING_SWEEP:
        ticket = playback_ticket.mint(cipher, "u" * length, minted_at=_MINTED_AT)

        assert set(ticket) <= alphabet, f"a ticket for {length} characters left the alphabet"
        assert quote(ticket, safe="=") == ticket, f"safe='=' re-encoded a {length}-character ticket"
        padding[ticket.count("=")] += 1

    # The distribution the module docstring and ADR-0029 both cite, computed
    # over exactly the range they name. Every length lands in one of the three
    # bands, so the tallies sum to the sweep and none of them can be zero.
    assert padding[0] == 192, f"unpadded tally moved: {padding[0]}"
    assert padding[1] == 200, f"one-`=` tally moved: {padding[1]}"
    assert padding[2] == 207, f"two-`=` tally moved: {padding[2]}"
    assert sum(padding.values()) == len(_PADDING_SWEEP)

    # ... and therefore the headline claim, derived rather than restated: the
    # spelling D3 must not use holds for under a third of lengths.
    assert padding[0] / len(_PADDING_SWEEP) == pytest.approx(0.32, abs=0.005)

    plan_sample = "u" * 184
    plan_ticket = playback_ticket.mint(cipher, plan_sample, minted_at=_MINTED_AT)
    assert len(plan_ticket) == 332
    assert quote(plan_ticket, safe="") == plan_ticket

    one_band_down = playback_ticket.mint(cipher, "u" * 175, minted_at=_MINTED_AT)
    assert len(one_band_down) == 312
    assert quote(one_band_down, safe="") != one_band_down


# --------------------------------------------------------------------------
# The boundary conversion, and the secret's one unwrapping.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("call", ["mint", "redeem"])
def test_a_naive_datetime_is_refused_rather_than_read_as_local_time(call: str) -> None:
    """`encrypt_at_time` takes `int` seconds. A naive `datetime.timestamp()`
    silently means "local time", which would put a ticket's stamp hours from
    where the caller meant -- so the conversion refuses one.

    The `redeem` arm doubles as the case that pins *where* the conversion
    happens: `redeem` swallows `ValueError` to turn a non-ASCII token into
    `None`, so a conversion moved inside that `try` would answer `None` for a
    broken caller clock and hide the bug. This case demands a raise.
    """
    cipher = playback_ticket.build_ticket_cipher(_SECRET)
    naive = datetime(2026, 8, 11, 20, 30, 0)  # the defect under test: no tzinfo

    with pytest.raises(ValueError, match="aware"):
        if call == "mint":
            playback_ticket.mint(cipher, _URL, minted_at=naive)
        else:
            ticket = playback_ticket.mint(cipher, _URL, minted_at=_MINTED_AT)
            playback_ticket.redeem(cipher, ticket, now=naive, ttl_seconds=60)


def test_the_secret_is_unwrapped_once_and_never_bound_to_a_name() -> None:
    """CLAUDE.md's rule, and `credentials.py:28-32`'s, as a structural
    assertion: `get_secret_value()` is called exactly once in the whole
    module, inside `build_ticket_cipher`, and its result is an argument rather
    than an assignment -- so no plaintext copy of `USHER_SECRET_KEY` outlives
    the derivation.
    """
    whole = _module_tree(strip_docstrings=True)
    unwraps = [name for name in _called_names(whole) if name.endswith("get_secret_value")]
    assert len(unwraps) == 1, f"expected one unwrapping, found {unwraps}"

    builder = _function("build_ticket_cipher")
    assert [n for n in _called_names(builder) if n.endswith("get_secret_value")] == unwraps

    def _holds_the_secret(node: ast.AST) -> bool:
        return any(name.endswith("get_secret_value") for name in _called_names(node))

    for node in ast.walk(builder):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            value = node.value
            if value is None:
                continue
            # `derived = HKDF(...).derive(secret.get_secret_value()...)` is
            # fine -- `derived` holds an HKDF output. What is refused is
            # binding the plaintext itself, i.e. an assignment whose value
            # *is* the unwrapping or a method call directly on it.
            if isinstance(value, ast.Call) and _holds_the_secret(value.func):
                raise AssertionError(f"the plaintext is bound by `{ast.unparse(node)}`")
            if _holds_the_secret(value) and not any(
                name.endswith("derive") for name in _called_names(value)
            ):
                raise AssertionError(f"the plaintext is bound by `{ast.unparse(node)}`")


def test_mint_stamps_the_token_and_redeem_checks_the_stamp() -> None:
    """The two `_at_time` primitives are what make every expiry case above
    deterministic. A `mint` that reached for `encrypt` would still round-trip
    and would silently stamp the token with the wall clock, which no fixture
    could then place either side of a boundary.
    """
    assert "encrypt_at_time" in " ".join(_called_names(_function("mint")))
    assert "decrypt_at_time" in " ".join(_called_names(_function("redeem")))

    minting = _called_names(_function("mint"))
    assert not any(name.endswith("cipher.encrypt") for name in minting)


def test_no_case_in_this_file_sleeps_or_patches_a_clock() -> None:
    """The acceptance criterion, as a check rather than as a habit. Every
    expiry case takes its instant as an argument, so nothing here needs to
    wait for one or to lie about one -- and a future case that reached for
    either would be re-introducing the nondeterminism `encrypt_at_time` exists
    to remove.
    """
    tree = ast.parse(pathlib.Path(__file__).read_text())
    called = _called_names(tree)

    assert not [name for name in called if name.split(".")[-1] == "sleep"]
    assert not [name for name in called if name.split(".")[-1] == "setattr"]

    arguments = {
        argument.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for argument in node.args.args
    }
    assert "monkeypatch" not in arguments
    assert "freezer" not in arguments


def test_the_module_carries_the_rotation_consequence_in_its_docstring() -> None:
    """The risk the plan asks to be written down where it will not be
    rediscovered as a bug: rotating `USHER_SECRET_KEY` invalidates every
    outstanding ticket. `test_a_different_secret_redeems_nothing` is the
    behaviour; this is the sentence that stops it reading as a defect.
    """
    doc = playback_ticket.__doc__ or ""

    assert "USHER_SECRET_KEY" in doc
    assert "rotat" in doc.lower()


def test_the_ticket_cipher_is_a_fernet_over_a_thirty_two_byte_subkey() -> None:
    """`Fernet` refuses a key that is not 32 url-safe-base64-encoded bytes, so
    `length=32` is load-bearing at construction rather than at use. Pinned by
    building the key the module builds and handing it to `Fernet` directly.
    """
    cipher = playback_ticket.build_ticket_cipher(_SECRET)
    assert isinstance(cipher, Fernet)

    with pytest.raises(ValueError, match="32 url-safe base64-encoded bytes"):
        Fernet(base64.urlsafe_b64encode(b"\x00" * 16))
