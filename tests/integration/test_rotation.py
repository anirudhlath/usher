"""`usher rotate-secret` against a real `source_credentials`.

`tests/unit/test_services_rotation.py` owns the ordering decisions against an
in-memory ciphertext store. What is here is what a dict cannot say:

- the rewrite really lands on the row, and the row afterwards is one
  `PostgresCredentialStore` built from the **new** key can read and one built
  from the old one cannot -- which is the whole of what rotation is for;
- a key `Settings` would refuse is refused **before the first row is
  touched**, read back on a second session, because *"nothing was written"*
  against the writer's own session is satisfied by a service that never
  committed at all;
- the per-row commit is visible to a second connection while the run is still
  going, which is what makes the interrupted-run argument true rather than
  aspirational.

**This module commits for real**, `test_restore.py`'s precedent and the same
argument: the suite's `session` fixture is a connection-bound transaction
that is rolled back, so committed state is not a question it can be asked.
So the sessions here come from an engine of their own, the assertions read on
a second session, and the file cleans up after itself in foreign-key order.

⚠️ Every row this file writes carries `SOURCE_NAME` or a `ROTATION_REF`
prefix, so the teardown deletes what this file created rather than emptying a
table another committing file shares.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.cli import main
from usher.config import get_settings
from usher.db.base import build_engine, build_session_factory
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


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one."""
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_slate(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Runs before as well as after: a previous run that died between the two
    would leave rows that make the next run's counts wrong, and a test whose
    isolation depends on the last one having finished cleanly is not
    isolated."""
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
    """The whole point of the command, asserted through the shipped reader
    rather than through the bytes.

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
    """The headline unit case's Postgres arm, and the assertion is the same
    one: a service that re-encrypted an already-rotated row would leave it
    perfectly readable, so only the bytes can see it.

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
    """A mixed table is the state an interrupted rotation leaves, and the
    refusal must not hide behind the rows that worked -- nor take them down
    with it, which is what one transaction over the whole table would do."""
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
    """Per-row commit, observed from a **second connection** while the run is
    still in flight.

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
    """Everything `usher rotate-secret` writes to stdout, over a table holding
    a rotatable row and an unopenable one, with the canary's presence in the
    stored row asserted first."""
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
    """`PostgresCredentialRotationStore.__init__` takes a session and nothing
    else, which is what makes an instance of it a thing that cannot leak a
    credential.

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
    """*An update, never an insert.* A rotation mints no rows, so a ref that
    vanished between the listing and the write must not reappear as a row with
    no source behind it -- which the foreign key would refuse anyway, loudly,
    in the middle of a run that was otherwise fine."""
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
    """`source_credentials` carries no `set_updated_at` trigger -- its model
    docstring says so, and named `PostgresCredentialStore` as the table's only
    writer until this class became the second one. So the stamp is this
    repository's to set, and a rotation that left it alone would make the
    column mean *"when the credential last changed"* on some rows and *"when
    it was last written"* on others."""
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
