"""`usher set-credentials` against a real `sources` and `source_credentials`."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.emby_server import FakeEmbyServer
from usher.adapters.emby.adapter import EmbyAdapter
from usher.cli import main
from usher.config import get_settings
from usher.db.models.source import MediaItemRow, SourceRow
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.source import PostgresSourceRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortAuthFailed

KEY = SecretStr("credentials-case-key-" + "k" * 19)

SOURCE_NAME = "credentials-case source"
REF_PREFIX = "credentials-case-"

# The username differs as well as the password, so a command that stored only
# the password still fails to log in below.
OLD_USERNAME, OLD_PASSWORD = "old-account", "old-password"
NEW_USERNAME, NEW_PASSWORD = "new-account-canary", "n3w-password-canary"

USERNAME_VAR = "CASE_SOURCE_USERNAME"
PASSWORD_VAR = "CASE_SOURCE_PASSWORD"


@pytest_asyncio.fixture(autouse=True)
async def clean_slate(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Before as well as after, for `test_rotation.py`'s reason: a run that died between them."""
    await _purge(sessions)
    yield
    await _purge(sessions)


async def _purge(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # `ON DELETE CASCADE` from `sources` takes each case's credentials and
        # media items with it; the first delete is for a ref left without one.
        await session.execute(
            text("DELETE FROM source_credentials WHERE ref LIKE :prefix"),
            {"prefix": f"{REF_PREFIX}%"},
        )
        await session.execute(
            text("DELETE FROM sources WHERE name LIKE :name"), {"name": f"{SOURCE_NAME}%"}
        )
        await session.commit()


@pytest.fixture
def command_environment(monkeypatch: pytest.MonkeyPatch, postgres_url: str) -> Iterator[None]:
    """What `usher set-credentials` reads: the deployment, and the new login under two names."""
    monkeypatch.setenv("USHER_DATABASE_URL", postgres_url)
    monkeypatch.setenv("USHER_SECRET_KEY", KEY.get_secret_value())
    monkeypatch.setenv(USERNAME_VAR, NEW_USERNAME)
    monkeypatch.setenv(PASSWORD_VAR, NEW_PASSWORD)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def _main(argv: list[str]) -> None:
    """`usher.cli.main` on a thread of its own, since `asyncio.run` refuses a running loop."""
    await asyncio.to_thread(main, argv)


def _argv(source_name: str) -> list[str]:
    return [
        "set-credentials",
        "--source",
        source_name,
        "--username-env",
        USERNAME_VAR,
        "--password-env",
        PASSWORD_VAR,
    ]


async def _seed(
    sessions: async_sessionmaker[AsyncSession], ref: str, *, name: str, enabled: bool = True
) -> uuid.UUID:
    """One source and its old login, written through the shipped store and committed."""
    source_id = new_id()
    async with sessions() as session:
        session.add(
            SourceRow(
                id=source_id,
                kind=SourceKind.EMBY,
                name=name,
                base_url="https://emby.invalid",
                credentials_ref=ref,
                device_id=str(new_id()),
                enabled=enabled,
            )
        )
        await session.flush()
        await PostgresCredentialStore(session, KEY).put(
            ref,
            SourceCredentials(username=OLD_USERNAME, password=SecretStr(OLD_PASSWORD)),
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


async def _source(sessions: async_sessionmaker[AsyncSession], source_id: uuid.UUID) -> Source:
    async with sessions() as session:
        source = await PostgresSourceRepository(session).get(source_id)
    assert source is not None
    return source


@asynccontextmanager
async def _adapter_on_the_stored_row(
    sessions: async_sessionmaker[AsyncSession], server: FakeEmbyServer, source_id: uuid.UUID
) -> AsyncIterator[EmbyAdapter]:
    """An adapter built from the source and the login as stored, read on a session of its own.

    So the claim is about what the next process to build one would find, and not about
    anything the command still held.
    """
    async with sessions() as session:
        source = await PostgresSourceRepository(session).get(source_id)
        assert source is not None
        credentials = await PostgresCredentialStore(session, KEY).get(source.credentials_ref)
        assert credentials is not None
    async with httpx.AsyncClient(transport=server.transport(), base_url=source.base_url) as client:
        adapter = EmbyAdapter(source, credentials, client=client)
        try:
            yield adapter
        finally:
            await adapter.aclose()


async def _logs_in(
    sessions: async_sessionmaker[AsyncSession], server: FakeEmbyServer, source_id: uuid.UUID
) -> bool:
    async with _adapter_on_the_stored_row(sessions, server, source_id) as adapter:
        return (await adapter.verify()).authenticated


@pytest.mark.usefixtures("command_environment")
async def test_it_overwrites_the_stored_row_so_a_new_adapter_logs_in_with_the_new_password(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The command's purpose, asserted through the adapter that next reads the row.

    The server knows only the new login, so the premise is that the stored one fails
    against it.
    """
    server = FakeEmbyServer(username=NEW_USERNAME, password=NEW_PASSWORD)
    name = f"{SOURCE_NAME} overwritten"
    source_id = await _seed(sessions, f"{REF_PREFIX}overwritten", name=name)
    async with _adapter_on_the_stored_row(sessions, server, source_id) as adapter:
        with pytest.raises(PortAuthFailed):
            await adapter.get_item("absent")

    await _main(_argv(name))

    assert await _logs_in(sessions, server, source_id) is True


@pytest.mark.usefixtures("command_environment")
async def test_it_keeps_the_sources_id_and_its_rows(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """What registering the source again would lose: a second source, away from the first's rows."""
    name = f"{SOURCE_NAME} kept"
    ref = f"{REF_PREFIX}kept"
    source_id = await _seed(sessions, ref, name=name)
    item_id = new_id()
    async with sessions() as session:
        session.add(
            MediaItemRow(
                id=item_id,
                source_id=source_id,
                external_id="item-1",
                container="mkv",
                width=3840,
                height=2160,
                added_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            )
        )
        await session.commit()
    source_before = await _source(sessions, source_id)
    item_before = await _item(sessions, item_id)
    ciphertext_before = await _ciphertext(sessions, ref)

    await _main(_argv(name))

    assert await _ciphertext(sessions, ref) != ciphertext_before, "the premise: it wrote"
    async with sessions() as session:
        ours = [
            source
            for source in await PostgresSourceRepository(session).list_all()
            if source.name.startswith(SOURCE_NAME)
        ]
    assert ours == [source_before]
    assert await _item(sessions, item_id) == item_before


async def _item(sessions: async_sessionmaker[AsyncSession], item_id: uuid.UUID) -> dict[str, Any]:
    """Every column of one `media_items` row, so "unchanged" covers the columns nobody named."""
    async with sessions() as session:
        row = await session.execute(
            text("SELECT * FROM media_items WHERE id = :id"), {"id": item_id}
        )
        return dict(row.mappings().one())


@pytest.mark.usefixtures("command_environment")
async def test_it_prints_neither_value_and_says_to_restart(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    """The success line names the source and the restart, and nothing of the login."""
    name = f"{SOURCE_NAME} printed"
    ref = f"{REF_PREFIX}printed"
    await _seed(sessions, ref, name=name)

    await _main(_argv(name))

    captured = capsys.readouterr()
    # The premise: both values were read and stored, so their absence below is
    # about values this process held.
    async with sessions() as session:
        stored = await PostgresCredentialStore(session, KEY).get(ref)
    assert stored is not None
    assert (stored.username, stored.password.get_secret_value()) == (NEW_USERNAME, NEW_PASSWORD)
    assert (
        f"usher set-credentials: stored new credentials for {name}. Restart Usher: running "
        "lanes and the worker's cached adapters hold the old password until then."
    ) in captured.out.splitlines()
    for stream in (captured.out, captured.err):
        assert NEW_USERNAME not in stream
        assert NEW_PASSWORD not in stream


@pytest.mark.usefixtures("command_environment")
async def test_a_name_matching_no_source_is_refused_and_writes_nothing(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    """A strict prefix of the stored name, so the match has to be exact to refuse it.

    Its premise is the last arm: the same command naming the source moves the row.
    """
    name = f"{SOURCE_NAME} present"
    ref = f"{REF_PREFIX}present"
    await _seed(sessions, ref, name=name)
    before = await _ciphertext(sessions, ref)
    given = SOURCE_NAME
    assert name.startswith(given) and name != given, "the premise: a near miss"

    with pytest.raises(SystemExit) as exit_info:
        await _main(_argv(given))

    captured = capsys.readouterr()
    assert str(exit_info.value) == "usher set-credentials: no source has the name given to --source"
    assert given not in captured.out + captured.err + str(exit_info.value)
    assert await _ciphertext(sessions, ref) == before

    await _main(_argv(name))

    assert await _ciphertext(sessions, ref) != before


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.usefixtures("command_environment")
async def test_a_name_matching_two_sources_is_refused_and_writes_nothing(
    sessions: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str], count: int
) -> None:
    """`sources.name` is not unique, and the command cannot tell which source was meant.

    Three as well as two, so the count is read off the matches. Its premise is the last
    arm: with the others renamed, the same command moves the first row and only that one.
    """
    name = f"{SOURCE_NAME} shared"
    refs = [f"{REF_PREFIX}shared-{number}" for number in range(count)]
    ids = [await _seed(sessions, ref, name=name) for ref in refs]
    before = {ref: await _ciphertext(sessions, ref) for ref in refs}

    with pytest.raises(SystemExit) as exit_info:
        await _main(_argv(name))

    captured = capsys.readouterr()
    assert str(exit_info.value) == (
        f"usher set-credentials: {count} sources have the name given to --source, and this "
        "command cannot tell them apart"
    )
    assert name not in captured.out + captured.err + str(exit_info.value)
    assert {ref: await _ciphertext(sessions, ref) for ref in refs} == before

    async with sessions() as session:
        await session.execute(
            text("UPDATE sources SET name = name || ' renamed ' || id WHERE id = ANY(:ids)"),
            {"ids": ids[1:]},
        )
        await session.commit()
    await _main(_argv(name))

    moved = [await _ciphertext(sessions, ref) != before[ref] for ref in refs]
    assert moved == [True] + [False] * (count - 1)


@pytest.mark.usefixtures("command_environment")
async def test_a_disabled_source_can_have_its_credentials_stored(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`selected_sources` skips a disabled source, and a disabled source must still be fixable."""
    server = FakeEmbyServer(username=NEW_USERNAME, password=NEW_PASSWORD)
    name = f"{SOURCE_NAME} disabled"
    source_id = await _seed(sessions, f"{REF_PREFIX}disabled", name=name, enabled=False)
    assert (await _source(sessions, source_id)).enabled is False, "the premise: it is disabled"
    assert await _logs_in(sessions, server, source_id) is False, "the premise: the old login"

    await _main(_argv(name))

    assert await _logs_in(sessions, server, source_id) is True
