"""`usher rotate-secret` against a real `source_credentials`."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.cli import main
from usher.config import get_settings
from usher.db.models.source import SourceRow
from usher.db.repositories.credentials import (
    PostgresCredentialRotationStore,
    PostgresCredentialStore,
    build_cipher,
)
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortDataMalformed
from usher.services.rotation import RotationReport, RotationService

OLD_KEY = SecretStr("rotation-case-old-" + "o" * 22)
NEW_KEY = SecretStr("rotation-case-new-" + "n" * 22)
LOST_KEY = SecretStr("rotation-case-lost-" + "l" * 21)

SOURCE_NAME = "rotation-case source"
REF_PREFIX = "rotation-case-"

# The password that must never reach a report, a printed line or an exception
# message. Every absence claim below asserts its presence in the stored row
# first, through the cipher that opens it.
CANARY = "sup3rs3cret-rotation-canary"

# The environment variable the command reads the new key out of. Exported by
# the case, never written into `.env`: measured 2026-08-25, the same name
# inside `.env` makes every entry point fail `extra="forbid"` and renders the
# key in the `ValidationError`'s `input_value=`.
VAR = "USHER_NEW_SECRET_KEY"

# Three canaries rather than one, because a rotation that moved row A's
# plaintext onto row B leaves every row perfectly decryptable and only
# *distinct* payloads can see it. Two would not do: a swap is its own
# inverse, so a two-row fixture cannot tell the permutation from its
# reverse (`testing-discipline.md`'s 3-cycle entry, in the payload domain).
CANARIES = ("sup3rs3cret-canary-one", "sup3rs3cret-canary-two", "sup3rs3cret-canary-three")


class _InterruptedRun(Exception):
    """How K8's drill spells "the operator killed the command".

    A signal is the obvious spelling and it is the wrong one: a killed
    process leaves no report, no exception and no frame to assert on, so a
    case built on one cannot tell "died after row 1" from "never started".
    An injected failure on the *second* row lands the run at the identical
    state -- row 1 committed, row 2 written and rolled back, row 3 never
    read -- and leaves something the case can be about.
    """


@pytest_asyncio.fixture(autouse=True)
async def clean_slate(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Runs before as well as after.

    a previous run that died between the two would leave rows that make the next run's
    counts wrong, and a test whose isolation depends on the last one having finished
    cleanly is not isolated.
    """
    await _purge(sessions)
    yield
    await _purge(sessions)


async def _purge(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # `source_credentials.source_id` is `ON DELETE CASCADE`, so the
        # sources delete takes the credentials with it -- the explicit one
        # above it is for a ref whose source a case removed by hand.
        await session.execute(
            text("DELETE FROM source_credentials WHERE ref LIKE :prefix"),
            {"prefix": f"{REF_PREFIX}%"},
        )
        await session.execute(
            text("DELETE FROM sources WHERE name LIKE :name"), {"name": f"{SOURCE_NAME}%"}
        )
        await session.commit()


async def _seed(
    sessions: async_sessionmaker[AsyncSession], ref: str, key: SecretStr, *, password: str = CANARY
) -> uuid.UUID:
    """One source and its credential, encrypted under `key` and committed.

    Written through the shipped `PostgresCredentialStore` rather than by a raw
    `INSERT`, so the ciphertext a rotation meets is the ciphertext the
    application writes -- including the JSON blob shape, which is what puts
    the canary where a real password sits.
    """
    source_id = new_id()
    async with sessions() as session:
        # The ORM row rather than a hand-written `INSERT`: the column list of
        # `sources` is not this file's subject, and transcribing it is how a
        # fixture breaks on a migration that has nothing to do with it.
        session.add(
            SourceRow(
                id=source_id,
                kind=SourceKind.EMBY,
                name=f"{SOURCE_NAME} {ref}",
                base_url="https://emby.invalid",
                credentials_ref=ref,
                device_id=str(new_id()),
            )
        )
        await session.flush()
        await PostgresCredentialStore(session, key).put(
            ref,
            SourceCredentials(username="usher", password=SecretStr(password)),
            owner_id=source_id,
        )
        await session.commit()
    return source_id


async def _ciphertext(sessions: async_sessionmaker[AsyncSession], ref: str) -> bytes:
    async with sessions() as session:
        row = await session.execute(
            text("SELECT ciphertext FROM source_credentials WHERE ref = :ref"), {"ref": ref}
        )
        return bytes(row.scalar_one())


async def _main(argv: list[str]) -> None:
    """`usher.cli.main` from inside an async test.

    `main` ends at `asyncio.run`, which refuses to start a second loop on a
    thread that already has one -- so a case that drives the real command from
    an `async def` has to hand it a thread of its own. Nothing else about the
    command changes: `monkeypatch.setenv` is process-wide and `capsys`
    replaces `sys.stdout` for every thread.
    """
    await asyncio.to_thread(main, argv)


async def _rotate(
    sessions: async_sessionmaker[AsyncSession],
    *,
    old: SecretStr = OLD_KEY,
    new: SecretStr = NEW_KEY,
) -> RotationReport:
    """One rotation on a session of its own, closed before anything is read.

    The session is not reused for the assertions on purpose: a reader sharing
    the writer's session would see uncommitted rows, and every claim in this
    file about what a *restart* would find would pass against a service that
    never committed.
    """
    async with sessions() as session:
        return await RotationService(
            store=PostgresCredentialRotationStore(session),
            old_cipher=build_cipher(old),
            new_cipher=build_cipher(new),
            commit=session.commit,
        ).rotate()


async def test_the_rotated_row_reads_under_the_new_key_and_no_longer_under_the_old_one(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point of the command.

    asserted through the shipped reader rather than through the bytes.

    Both directions, because only one of them is the *change*: a rotation that
    wrote nothing satisfies "the new key reads it" if the two keys happened to
    derive the same cipher, and a rotation that corrupted the row satisfies
    "the old key cannot" on its own.
    """
    ref = f"{REF_PREFIX}a"
    await _seed(sessions, ref, OLD_KEY)
    before = await _ciphertext(sessions, ref)
    # The premise, through the cipher that opens it: the canary really is in
    # this row, so every absence claim below is about a value that was there.
    assert CANARY.encode("utf-8") in build_cipher(OLD_KEY).decrypt(before)

    report = await _rotate(sessions)

    assert report.rotated == (ref,) and report.already == () and report.refused == ()
    assert await _ciphertext(sessions, ref) != before, "the row did not move"
    async with sessions() as session:
        found = await PostgresCredentialStore(session, NEW_KEY).get(ref)
        assert found is not None
        assert found.username == "usher"
        assert found.password.get_secret_value() == CANARY
        with pytest.raises(PortDataMalformed) as exc_info:
            await PostgresCredentialStore(session, OLD_KEY).get(ref)
    assert exc_info.value.detail == f"credentials_ref={ref}"
    assert CANARY not in str(exc_info.value)


async def test_a_row_already_on_the_new_key_is_left_byte_identical(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The headline unit case's Postgres arm, and the assertion is the same one.

    a service that re-encrypted an already-rotated row would leave it perfectly
    readable, so only the bytes can see it.

    Its premise is the row that genuinely moves.
    """
    moved, kept = f"{REF_PREFIX}moves", f"{REF_PREFIX}stays"
    await _seed(sessions, moved, OLD_KEY)
    await _seed(sessions, kept, NEW_KEY)
    before_moved = await _ciphertext(sessions, moved)
    before_kept = await _ciphertext(sessions, kept)

    report = await _rotate(sessions)

    assert report.rotated == (moved,) and report.already == (kept,)
    assert await _ciphertext(sessions, moved) != before_moved, "no row was rotated at all"
    assert await _ciphertext(sessions, kept) == before_kept


async def test_a_row_no_key_opens_is_refused_and_the_rest_of_the_table_still_rotates(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A mixed table is the state an interrupted rotation leaves.

    and the refusal must not hide behind the rows that worked -- nor take them down with
    it, which is what one transaction over the whole table would do.
    """
    fine, lost = f"{REF_PREFIX}fine", f"{REF_PREFIX}zlost"
    await _seed(sessions, fine, OLD_KEY)
    await _seed(sessions, lost, LOST_KEY)
    before_lost = await _ciphertext(sessions, lost)

    report = await _rotate(sessions)

    assert report.rotated == (fine,)
    assert report.refused == (lost,)
    assert await _ciphertext(sessions, lost) == before_lost, "the refused row was written to"
    async with sessions() as session:
        assert await PostgresCredentialStore(session, NEW_KEY).get(fine) is not None


async def test_each_rotated_row_is_committed_before_the_next_one_is_written(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Per-row commit, observed from a **second connection** while the run is still in flight.

    That is the claim the unit case's event log cannot make: an event log
    proves the service *called* commit between rows, and this proves the row
    is durable at that moment, which is what "an interruption leaves a mixed
    state" actually means.

    The commit callable is wrapped rather than replaced, so what is measured
    is the shipped session's own commit.
    """
    first, second = f"{REF_PREFIX}m1", f"{REF_PREFIX}m2"
    await _seed(sessions, first, OLD_KEY)
    await _seed(sessions, second, OLD_KEY)
    before_second = await _ciphertext(sessions, second)
    seen: list[bytes] = []

    async with sessions() as session:
        service_session = session

        async def commit_and_peek() -> None:
            await service_session.commit()
            # A connection of its own: the writer's session can see its own
            # uncommitted work, so reading there would prove nothing.
            seen.append(await _ciphertext(sessions, second))

        await RotationService(
            store=PostgresCredentialRotationStore(session),
            old_cipher=build_cipher(OLD_KEY),
            new_cipher=build_cipher(NEW_KEY),
            commit=commit_and_peek,
        ).rotate()

    assert len(seen) == 2, "two rows rotated and this did not see two commits"
    # After the *first* row's commit, the second row is still untouched --
    # which is what makes the first observation a statement about a partial
    # state rather than about the end of the run.
    assert seen[0] == before_second
    assert seen[1] != before_second
    async with sessions() as session:
        assert await PostgresCredentialStore(session, NEW_KEY).get(first) is not None


async def test_a_new_key_settings_would_refuse_is_refused_before_any_row_is_touched(
    sessions: async_sessionmaker[AsyncSession],
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance criterion, and it is asserted **from a second session**.

    A rotation to a key `Settings` would refuse bricks the next start, so the
    refusal has to arrive before the first write rather than from pydantic at
    the next boot. This drives the real `usher rotate-secret` end to end --
    `main`, the parser, `_dispatch`, the environment read -- against a real
    database holding a real credential, and then reads the row back on a
    connection the command never had.

    Its premise is that the row is genuinely rotatable: the same command with
    an acceptable key moves it, in the same test, so *"nothing was written"*
    is not a statement about a table nothing could have written to.
    """
    ref = f"{REF_PREFIX}guarded"
    await _seed(sessions, ref, OLD_KEY)
    before = await _ciphertext(sessions, ref)

    monkeypatch.setenv("USHER_DATABASE_URL", postgres_url)
    monkeypatch.setenv("USHER_SECRET_KEY", OLD_KEY.get_secret_value())
    monkeypatch.setenv(VAR, "far-too-short")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exit_info:
            await _main(["rotate-secret", "--new-key-env", VAR])
        message = str(exit_info.value)
        assert "secret_key" in message
        assert "far-too-short" not in message
        assert await _ciphertext(sessions, ref) == before, "a refused key still wrote a row"

        # The premise: this row was rotatable all along.
        monkeypatch.setenv(VAR, NEW_KEY.get_secret_value())
        await _main(["rotate-secret", "--new-key-env", VAR])
    finally:
        get_settings.cache_clear()

    assert await _ciphertext(sessions, ref) != before


async def test_the_command_prints_refs_and_never_a_credential_or_a_key(
    sessions: async_sessionmaker[AsyncSession],
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Everything `usher rotate-secret` writes to stdout.

    over a table holding a rotatable row and an unopenable one, with the canary's
    presence in the stored row asserted first.
    """
    fine, lost = f"{REF_PREFIX}printed", f"{REF_PREFIX}zunopenable"
    await _seed(sessions, fine, OLD_KEY)
    await _seed(sessions, lost, LOST_KEY)
    assert CANARY.encode("utf-8") in build_cipher(OLD_KEY).decrypt(
        await _ciphertext(sessions, fine)
    ), "the premise: the canary is in the row this run decrypts"

    monkeypatch.setenv("USHER_DATABASE_URL", postgres_url)
    monkeypatch.setenv("USHER_SECRET_KEY", OLD_KEY.get_secret_value())
    monkeypatch.setenv(VAR, NEW_KEY.get_secret_value())
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exit_info:
            await _main(["rotate-secret", "--new-key-env", VAR])
    finally:
        get_settings.cache_clear()

    printed = capsys.readouterr().out + str(exit_info.value)
    # The refused ref is named -- that is the row an operator has to act on,
    # and a count cannot say which it was. The rotated one is a count, which
    # is the asymmetry `_print_rotation_report`'s docstring argues for.
    assert lost in printed
    assert "rotated     1" in printed and "refused     1" in printed
    assert CANARY not in printed
    assert OLD_KEY.get_secret_value() not in printed
    assert NEW_KEY.get_secret_value() not in printed
    # Not just the password: the username is half a credential, and the JSON
    # blob is the shape a naive "print what we decrypted" would emit.
    assert "username" not in printed


async def test_the_rotation_store_needs_no_key_and_the_reader_still_does(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`PostgresCredentialRotationStore.__init__` takes a session and nothing else.

    which is what makes an instance of it a thing that cannot leak a credential.

    The control is the sibling: `PostgresCredentialStore` *does* take a key,
    so this is a statement about a deliberate asymmetry rather than about a
    constructor nobody passes anything to.
    """
    ref = f"{REF_PREFIX}opaque"
    await _seed(sessions, ref, OLD_KEY)

    async with sessions() as session:
        store = PostgresCredentialRotationStore(session)
        assert list(vars(store)) == ["_session"]
        blob = await store.read_ciphertext(ref)
        assert blob is not None
        assert CANARY.encode("utf-8") not in blob
        assert await store.list_refs() == [ref]
        assert await store.read_ciphertext(f"{REF_PREFIX}absent") is None


async def test_writing_to_a_ref_the_table_does_not_hold_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """*An update.

    never an insert.* A rotation mints no rows, so a ref that vanished between the
    listing and the write must not reappear as a row with no source behind it -- which
    the foreign key would refuse anyway, loudly, in the middle of a run that was
    otherwise fine.
    """
    async with sessions() as session:
        await PostgresCredentialRotationStore(session).write_ciphertext(
            f"{REF_PREFIX}never-existed", b"anything"
        )
        await session.commit()
    async with sessions() as session:
        rows = await session.execute(
            text("SELECT count(*) FROM source_credentials WHERE ref LIKE :prefix"),
            {"prefix": f"{REF_PREFIX}%"},
        )
        assert rows.scalar_one() == 0


async def test_the_write_moves_updated_at(sessions: async_sessionmaker[AsyncSession]) -> None:
    """`source_credentials` carries no `set_updated_at` trigger.

    its model docstring says so, and named `PostgresCredentialStore` as the table's only
    writer until this class became the second one.

    So the stamp is this repository's to set, and a rotation that left it alone would
    make the column mean *"when the credential last changed"* on some rows and *"when it
    was last written"* on others.
    """
    ref = f"{REF_PREFIX}stamped"
    await _seed(sessions, ref, OLD_KEY)
    async with sessions() as session:
        # Backdate through raw SQL: the table has no `BEFORE UPDATE` trigger,
        # so this really does stick, which is the fact under test.
        await session.execute(
            text(
                "UPDATE source_credentials SET updated_at = now() - interval '1 day' "
                "WHERE ref = :ref"
            ),
            {"ref": ref},
        )
        await session.commit()
    before = await _updated_at(sessions, ref)

    await _rotate(sessions)

    assert await _updated_at(sessions, ref) > before


async def _updated_at(sessions: async_sessionmaker[AsyncSession], ref: str) -> datetime:
    async with sessions() as session:
        row = await session.execute(
            text("SELECT updated_at FROM source_credentials WHERE ref = :ref"), {"ref": ref}
        )
        stamp = row.scalar_one()
        assert isinstance(stamp, datetime)
        return stamp


# --------------------------------------------------------------------------- K8's
# drill, run for real on 2026-08-26 against a scratch `pgvector/pgvector:pg17` and
# transcribed into `docs/runbooks/rotation.md`.


async def _seed_three(
    sessions: async_sessionmaker[AsyncSession], *, key: SecretStr = OLD_KEY, tag: str = "row"
) -> tuple[str, ...]:
    """Three rows under one key, one distinct canary each, in `ref` order.

    `list_refs` is `ORDER BY ref`, so the returned order is the order a
    rotation meets them in -- which is what lets a case say *"the run died on
    the second row"* and mean a particular row.
    """
    refs = tuple(f"{REF_PREFIX}{tag}{number}" for number in (1, 2, 3))
    for ref, canary in zip(refs, CANARIES, strict=True):
        await _seed(sessions, ref, key, password=canary)
    return refs


@contextmanager
def _command_environment(
    monkeypatch: pytest.MonkeyPatch,
    postgres_url: str,
    *,
    secret_key: SecretStr,
    new_key: SecretStr,
) -> Iterator[None]:
    """The environment `usher rotate-secret` reads, and nothing else.

    `USHER_SECRET_KEY` is a parameter rather than a constant because the whole
    of K8's ordering finding is what happens when it is already the *new* key
    -- see `test_rotating_after_the_key_was_already_changed_...` below.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", postgres_url)
    monkeypatch.setenv("USHER_SECRET_KEY", secret_key.get_secret_value())
    monkeypatch.setenv(VAR, new_key.get_secret_value())
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def _opens(
    sessions: async_sessionmaker[AsyncSession], key: SecretStr, ref: str
) -> str | None:
    """The password a store built from `key` reads out of `ref`.

    or `None` if that store cannot open the row.

    A **separately-constructed** store per call, which is the point: a claim
    about a row's ciphertext must not be able to pass because one long-lived
    reader happened to hold the right cipher.
    """
    async with sessions() as session:
        try:
            found = await PostgresCredentialStore(session, key).get(ref)
        except PortDataMalformed:
            return None
    return None if found is None else found.password.get_secret_value()


async def test_three_rows_rotate_together_and_each_keeps_its_own_credential(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """K8 arm 1.

    The happy path over more than one row, asserted per row.

    A one-row case cannot see a rotation that re-encrypts the *wrong*
    plaintext onto a row: every row still opens under the new key, the report
    still counts three, and only distinct payloads can tell them apart. Three
    is the smallest fixture that can, because a two-row permutation is its own
    inverse.
    """
    refs = await _seed_three(sessions)
    before = {ref: await _ciphertext(sessions, ref) for ref in refs}
    # The premise the per-row assertion rests on: the three payloads really
    # are distinguishable from each other.
    assert len(set(CANARIES)) == 3

    report = await _rotate(sessions)

    assert report.rotated == refs and report.already == () and report.refused == ()
    for ref in refs:
        assert await _ciphertext(sessions, ref) != before[ref], f"{ref} did not move"
    for ref, canary in zip(refs, CANARIES, strict=True):
        assert await _opens(sessions, NEW_KEY, ref) == canary, f"{ref} came back as another row"


async def test_an_interrupted_rotation_leaves_a_mixed_table_that_a_second_run_finishes(
    sessions: async_sessionmaker[AsyncSession],
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """K8 arms 2 and 3: the interruption, and the re-run that is its recovery.

    **The interruption is an injected failure and not a signal**, for
    `_InterruptedRun`'s reason. It is injected on the `commit` callable --
    the shipped constructor seam -- on the **second** row, so the run lands
    exactly where a `kill -9` would: row 1 committed, row 2's `UPDATE` issued
    and rolled back by the session's own close, row 3 never read.

    **The mixed state is read through two separately-constructed stores**, one
    per key. That is what makes the claim about the *ciphertext* rather than
    about a cipher some reader was still holding: a single store asked twice
    would answer from whatever key it was built with, and a service that had
    written nothing at all would satisfy "the old key still opens rows 2
    and 3".

    Its premise, asserted before the injection: **all three rows open under
    the old key**, so the mixed table is a thing this run produced and not a
    thing the fixture was.
    """
    refs = await _seed_three(sessions, tag="mix")
    first, second, third = refs
    before = {ref: await _ciphertext(sessions, ref) for ref in refs}
    for ref, canary in zip(refs, CANARIES, strict=True):
        assert await _opens(sessions, OLD_KEY, ref) == canary, "the premise: all three open"

    commits = 0

    async with sessions() as session:
        service_session = session

        async def commit_until_the_second_row() -> None:
            nonlocal commits
            commits += 1
            if commits == 2:
                # Before the commit, not after: the row 2 `UPDATE` is issued
                # and never made durable, which is the state a killed process
                # leaves and the state the rollback below has to undo.
                raise _InterruptedRun("the operator killed the command")
            await service_session.commit()

        with pytest.raises(_InterruptedRun):
            await RotationService(
                store=PostgresCredentialRotationStore(session),
                old_cipher=build_cipher(OLD_KEY),
                new_cipher=build_cipher(NEW_KEY),
                commit=commit_until_the_second_row,
            ).rotate()

    assert commits == 2, "the run did not reach the second row, so nothing was interrupted"
    interrupted = await _ciphertext(sessions, first)
    assert interrupted != before[first], "the first row never landed"
    assert await _ciphertext(sessions, second) == before[second], "the killed write was durable"
    assert await _ciphertext(sessions, third) == before[third]

    # The mixed table, through one store per key. Both directions on both
    # sides: "the new key opens row 1" is satisfied by two keys deriving one
    # cipher unless the old key is also asked and refused.
    assert await _opens(sessions, NEW_KEY, first) == CANARIES[0]
    assert await _opens(sessions, OLD_KEY, first) is None
    for ref, canary in ((second, CANARIES[1]), (third, CANARIES[2])):
        assert await _opens(sessions, OLD_KEY, ref) == canary
        assert await _opens(sessions, NEW_KEY, ref) is None

    # Both refusals name the row and neither names what is in it.
    async with sessions() as session:
        with pytest.raises(PortDataMalformed) as on_the_old_key:
            await PostgresCredentialStore(session, OLD_KEY).get(first)
        with pytest.raises(PortDataMalformed) as on_the_new_key:
            await PostgresCredentialStore(session, NEW_KEY).get(second)
    for caught, ref in ((on_the_old_key, first), (on_the_new_key, second)):
        assert caught.value.detail == f"credentials_ref={ref}"
        assert ref in str(caught.value)
        assert not any(canary in str(caught.value) for canary in CANARIES)
        assert "username" not in str(caught.value)

    # Arm 3: the recovery is the same command again, and it is the **shipped**
    # command rather than the service, because "re-run it" is what a runbook
    # tells an operator to do.
    with _command_environment(monkeypatch, postgres_url, secret_key=OLD_KEY, new_key=NEW_KEY):
        await _main(["rotate-secret", "--new-key-env", VAR])

    printed = capsys.readouterr().out
    assert "rotated     2" in printed
    assert "already     1" in printed
    assert "refused     0" in printed
    assert not any(canary in printed for canary in CANARIES)
    # The row a previous run had already moved is skipped rather than
    # re-encrypted -- and only the bytes can see that, because a
    # double-encrypted row reads back perfectly.
    assert await _ciphertext(sessions, first) == interrupted, "row 1 was re-encrypted"
    for ref, canary in zip(refs, CANARIES, strict=True):
        assert await _opens(sessions, NEW_KEY, ref) == canary


async def test_a_row_no_key_can_open_is_named_and_the_other_two_still_rotate(
    sessions: async_sessionmaker[AsyncSession],
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """K8 arm 4, through the shipped command over three rows.

    The service-level sibling
    (`test_a_row_no_key_opens_is_refused_and_the_rest_of_the_table_still_rotates`)
    makes the same argument over two rows and a row encrypted under a *third*
    key. This one is the operator-facing half and a different input: bytes
    that are not a Fernet token at all, the shape a truncated column or a bad
    restore produces, driven through `usher rotate-secret` so the refusal is
    read off the report an operator actually sees.

    **The other two rotating is the load-bearing half.** A rotation that
    aborted on the bad row would leave a table more mixed than the one it
    started with, and the operator holding a key that opens some unknown
    subset of it.
    """
    refs = await _seed_three(sessions, tag="bad")
    first, broken, last = refs
    async with sessions() as session:
        # Not a Fernet token: no version byte, no HMAC, nothing either cipher
        # can even attempt. Written through the shipped writer so the row is
        # the shape the column really holds.
        await PostgresCredentialRotationStore(session).write_ciphertext(broken, b"not-a-token")
        await session.commit()
    corrupted = await _ciphertext(sessions, broken)

    with (
        _command_environment(monkeypatch, postgres_url, secret_key=OLD_KEY, new_key=NEW_KEY),
        pytest.raises(SystemExit) as exit_info,
    ):
        await _main(["rotate-secret", "--new-key-env", VAR])

    printed = capsys.readouterr().out + str(exit_info.value)
    assert broken in printed, "the row an operator has to re-enter was not named"
    assert "rotated     2" in printed and "refused     1" in printed
    assert not any(canary in printed for canary in CANARIES)
    # The *partial* arm, and it is the control for the saturated case below:
    # two rows rotated, so the old key was right and this row really is
    # unreadable. Here the destructive advice is the correct advice.
    assert "re-entered" in printed and "POST /admin/sources" in printed
    assert "no credential was lost" not in printed.lower()
    # Left exactly as it was: writing onto it would destroy the one copy a
    # restored key could still have read.
    assert await _ciphertext(sessions, broken) == corrupted
    for ref, canary in ((first, CANARIES[0]), (last, CANARIES[2])):
        assert await _opens(sessions, NEW_KEY, ref) == canary


async def test_rotating_after_the_key_was_already_changed_refuses_every_row_and_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """K8's ordering finding.

    which is the whole reason `rotation.md` states an order rather than a list of steps.

    An operator who edits `.env` **first** and restarts has a deployment whose
    `USHER_SECRET_KEY` is already the new key -- so `cli._rotate` builds
    `old_cipher` from *that*, both ciphers are the same cipher, and every row
    still on the previous key opens under neither. The command refuses all of
    them, writes nothing, and exits non-zero. That is the good case; the bad
    one is an operator who reads `refused 3` as *"the credentials are
    corrupt"* and re-enters them, which is the recovery for a problem they do
    not have.

    Its premise is the second half: the **same rows**, the **same command**,
    with only the order corrected, rotate. So "refused 3" is a statement about
    the ordering rather than about a table nothing could have rotated.
    """
    refs = await _seed_three(sessions, tag="ord")
    before = {ref: await _ciphertext(sessions, ref) for ref in refs}

    with (
        _command_environment(monkeypatch, postgres_url, secret_key=NEW_KEY, new_key=NEW_KEY),
        pytest.raises(SystemExit) as exit_info,
    ):
        await _main(["rotate-secret", "--new-key-env", VAR])

    printed = capsys.readouterr().out + str(exit_info.value)
    assert "rotated     0" in printed and "already     0" in printed
    assert "refused     3" in printed
    for ref in refs:
        assert ref in printed
        assert await _ciphertext(sessions, ref) == before[ref], "a refused run still wrote"
    # 🔴 And the advice the operator is given, end to end through the shipped
    # command rather than through `_rotation_refusal` alone: this state is the
    # ordering mistake, every credential is intact, and telling the operator to
    # re-enter them destroys working state to fix a problem they do not have.
    assert "USHER_SECRET_KEY" in printed
    assert "no credential was lost" in printed.lower()
    assert "re-entered" not in printed
    assert "re-register" not in printed

    # The premise: only the order was wrong.
    with _command_environment(monkeypatch, postgres_url, secret_key=OLD_KEY, new_key=NEW_KEY):
        await _main(["rotate-secret", "--new-key-env", VAR])

    assert "rotated     3" in capsys.readouterr().out
    for ref, canary in zip(refs, CANARIES, strict=True):
        assert await _opens(sessions, NEW_KEY, ref) == canary
