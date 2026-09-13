"""`RotationService` -- two ciphers, one row at a time, and no plaintext key."""

import json
from collections.abc import Sequence

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from usher.db.repositories.credentials import build_cipher
from usher.ports.credentials import CredentialCiphertextStore
from usher.services.rotation import RotationReport, RotationService

OLD_KEY = SecretStr("old-" + "o" * 40)
NEW_KEY = SecretStr("new-" + "n" * 40)
# A third key nothing rotates to, for the row that decrypts under neither.
LOST_KEY = SecretStr("lost-" + "l" * 40)

# The value that must never reach a report, a log line or an exception
# message. Spelled once, and every case that asserts its absence asserts its
# *presence* in the seeded row first -- an absence claim about a value that
# was never there is satisfied by a service that stores nothing.
CANARY = "sup3rs3cret-emby-password"


def _blob(password: str = CANARY) -> bytes:
    """What `PostgresCredentialStore.put` encrypts: the JSON credential blob.

    Rotation never parses this -- it re-encrypts opaque bytes -- so the shape
    is here only so the canary sits where a real credential sits.
    """
    return json.dumps({"username": "usher", "password": password}).encode("utf-8")


class _Store(CredentialCiphertextStore):
    """In-memory ciphertext, in insertion order, with one event log.

    The log is shared with the commit callable on purpose: *"committed per
    row"* and *"committed once at the end"* write the identical rows and
    differ only in the **interleaving**, so a write counter and a commit
    counter that never meet cannot tell them apart.
    """

    def __init__(self, rows: dict[str, bytes]) -> None:
        self.rows = dict(rows)
        self.events: list[tuple[str, str]] = []

    async def list_refs(self) -> Sequence[str]:
        return list(self.rows)

    async def read_ciphertext(self, ref: str) -> bytes | None:
        return self.rows.get(ref)

    async def write_ciphertext(self, ref: str, ciphertext: bytes) -> None:
        self.rows[ref] = ciphertext
        self.events.append(("write", ref))

    async def commit(self) -> None:
        self.events.append(("commit", ""))


def _service(
    store: _Store, *, old: SecretStr = OLD_KEY, new: SecretStr = NEW_KEY
) -> RotationService:
    return RotationService(
        store=store,
        old_cipher=build_cipher(old),
        new_cipher=build_cipher(new),
        commit=store.commit,
    )


async def test_a_row_already_written_under_the_new_key_is_skipped_rather_than_double_encrypted() -> (  # noqa: E501
    None
):
    """The headline case, and (c) is the assertion with teeth.

    A service that re-encrypts an already-rotated row still leaves it
    readable, so (a) *both rows decrypt under the new key* and (b) *the report
    says `rotated=1, already=1`* are both satisfied by exactly the defect this
    case is named for -- Fernet's nonce is random, so a double encryption
    round-trips and simply produces different bytes. Only *the ciphertext did
    not change* can see it.

    Its premise is the row that genuinely moves: `before != after` for
    `ref-moves`, so *"the ciphertext did not change"* is a claim about
    `ref-already` rather than about a service that writes nothing at all.
    """
    old, new = build_cipher(OLD_KEY), build_cipher(NEW_KEY)
    moves, already = _blob("moves"), _blob("already")
    store = _Store({"ref-moves": old.encrypt(moves), "ref-already": new.encrypt(already)})
    before = dict(store.rows)

    report = await _service(store).rotate()

    # (a) both rows read under the new key afterwards.
    assert new.decrypt(store.rows["ref-moves"]) == moves
    assert new.decrypt(store.rows["ref-already"]) == already
    # The premise: the row that had to move did move.
    assert before["ref-moves"] != store.rows["ref-moves"], "no row was rotated at all"
    # (c) and the row that was already there is byte-identical.
    assert store.rows["ref-already"] == before["ref-already"]
    # (b)
    assert report.rotated == ("ref-moves",)
    assert report.already == ("ref-already",)
    assert report.refused == ()


async def test_every_rotated_row_is_committed_before_the_next_one_is_read() -> None:
    """Per-row commit, which is the opposite of K4's one-transaction restore and is deliberate.

    One transaction over N rows means an interrupted rotation leaves *every*
    row on the old key while the operator has already put the new one in their
    `.env` -- total credential loss on the next start. Per-row leaves a mixed
    state, and a mixed state is what re-running repairs.

    The assertion is the **interleaving**: `[write, write, commit]` and
    `[write, commit, write, commit]` produce identical rows, so counting is
    not enough.
    """
    old = build_cipher(OLD_KEY)
    store = _Store({"a": old.encrypt(_blob("a")), "b": old.encrypt(_blob("b"))})

    await _service(store).rotate()

    assert store.events == [("write", "a"), ("commit", ""), ("write", "b"), ("commit", "")]


async def test_a_row_under_neither_key_is_named_counted_and_left_exactly_as_it_was() -> None:
    """The row an operator has to re-enter.

    It must not be hidden behind a success, and it must not be *made worse* -- a service
    that wrote something onto it would destroy the one copy of a credential that a
    restored key could still have read.
    """
    old, lost = build_cipher(OLD_KEY), build_cipher(LOST_KEY)
    store = _Store({"ref-fine": old.encrypt(_blob("fine")), "ref-lost": lost.encrypt(_blob("x"))})
    before = dict(store.rows)

    report = await _service(store).rotate()

    assert report.rotated == ("ref-fine",)
    assert report.already == ()
    assert report.refused == ("ref-lost",)
    assert store.rows["ref-lost"] == before["ref-lost"]
    # And the refused row did not stop the one after it: `list_refs` orders by
    # ref, so `ref-fine` precedes `ref-lost` here -- the reverse ordering is
    # what the second store below is for.
    reversed_store = _Store(
        {"a-lost": lost.encrypt(_blob("x")), "b-fine": old.encrypt(_blob("fine"))}
    )
    assert (await _service(reversed_store).rotate()).rotated == ("b-fine",)


async def test_the_rotation_report_names_refs_and_never_a_credential() -> None:
    """PRD 08's *"credentials are never logged"*.

    at the one command whose job is to handle every stored credential in the deployment.

    The canary's **presence in the seeded row is asserted first**, so the
    absence claim below is about a value that could have appeared.
    """
    old, lost = build_cipher(OLD_KEY), build_cipher(LOST_KEY)
    store = _Store({"ref-a": old.encrypt(_blob()), "ref-b": lost.encrypt(_blob())})
    # The premise. Rotation is the only thing in this process holding a key
    # that opens `ref-a`, so this is where the canary provably is.
    assert CANARY.encode("utf-8") in old.decrypt(store.rows["ref-a"])

    report = await _service(store).rotate()

    rendered = repr(report)
    assert "ref-a" in rendered and "ref-b" in rendered
    assert CANARY not in rendered
    assert OLD_KEY.get_secret_value() not in rendered
    assert NEW_KEY.get_secret_value() not in rendered


async def test_a_second_run_over_a_fully_rotated_table_writes_nothing() -> None:
    """*Re-running is the recovery.

    and it needs no ledger.* A run over a table that is already on the new key is a no-
    op reporting *N already rotated*, and the way that is true is that every row is
    tried with the **new** cipher first.
    """
    old = build_cipher(OLD_KEY)
    store = _Store({"a": old.encrypt(_blob("a")), "b": old.encrypt(_blob("b"))})

    first = await _service(store).rotate()
    after_first = dict(store.rows)
    store.events.clear()
    second = await _service(store).rotate()

    assert first.rotated == ("a", "b")
    assert second.rotated == () and second.already == ("a", "b")
    assert store.events == [], "the second run wrote or committed something"
    assert store.rows == after_first


async def test_a_run_over_a_half_rotated_table_finishes_it() -> None:
    """What an interrupted rotation leaves, and what the next run does with it.

    The mixed state is the *point* of the per-row commit, so it needs a case rather than
    only a docstring.
    """
    old, new = build_cipher(OLD_KEY), build_cipher(NEW_KEY)
    store = _Store({"a": new.encrypt(_blob("a")), "b": old.encrypt(_blob("b"))})

    report = await _service(store).rotate()

    assert report.rotated == ("b",) and report.already == ("a",)
    assert new.decrypt(store.rows["a"]) == _blob("a")
    assert new.decrypt(store.rows["b"]) == _blob("b")


async def test_a_ref_deleted_between_the_listing_and_the_read_is_not_a_refusal() -> None:
    """`list_refs` and the per-row read are two statements with a commit between them.

    so a source deleted through the admin API mid-rotation is reachable rather than
    hypothetical -- and it is an absence, not a row whose key is lost.
    """
    old = build_cipher(OLD_KEY)
    store = _Store({"gone": old.encrypt(_blob()), "here": old.encrypt(_blob())})
    del store.rows["gone"]

    report = await _service(store).rotate()

    assert report.rotated == ("here",)
    assert report.refused == ()
    assert report.already == ()


def test_the_service_holds_two_ciphers_and_nothing_that_is_a_key() -> None:
    """*The rotation service holds two `Fernet` objects and no plaintext key.*.

    `build_cipher` unwraps `get_secret_value()` exactly once, inside itself,
    and retains only the HKDF output -- which is the property that makes
    holding two ciphers at once safe. A constructor taking `SecretStr` would
    look identical from every behavioural case in this file.
    """
    service = _service(_Store({}))
    held = vars(service)

    assert isinstance(held["_old_cipher"], Fernet)
    assert isinstance(held["_new_cipher"], Fernet)
    assert not [
        name for name, value in held.items() if isinstance(value, SecretStr | str | bytes)
    ], "the service is holding something that could be a key"


def test_neither_cipher_carries_the_key_it_was_derived_from() -> None:
    """The claim `build_cipher`'s docstring makes, asserted rather than read.

    Its premise is that the cipher really is this key's: a `Fernet` built from
    an unrelated key would satisfy the absence trivially.
    """
    cipher = build_cipher(NEW_KEY)
    # Premise: same key in, same cipher out.
    assert build_cipher(NEW_KEY).decrypt(cipher.encrypt(b"probe")) == b"probe"

    material = cipher._signing_key + cipher._encryption_key
    assert len(material) == 32, "the derived key is not the shape this asserts about"
    assert NEW_KEY.get_secret_value().encode("utf-8") not in material


def test_a_report_is_frozen() -> None:
    """`RotationReport` is the only record of what a rotation did.

    and the refused list is what an operator works from.
    """
    report = RotationReport(rotated=("a",), already=(), refused=("b",))
    with pytest.raises(Exception):  # noqa: B017  frozen dataclass raises FrozenInstanceError
        report.rotated = ()  # type: ignore[misc]
