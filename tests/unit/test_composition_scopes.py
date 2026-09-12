"""A narrow scope commits, or does not, by argument rather than by shape."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _itself(session: object) -> _Session:
    """The built object is the session itself, narrowed so `is` has two comparable operands."""
    assert isinstance(session, _Session)
    return session


def _sessions(session: _Session) -> Any:
    @asynccontextmanager
    async def open() -> AsyncIterator[_Session]:
        yield session

    return open


@pytest.mark.asyncio
async def test_a_committing_scope_commits_after_the_body() -> None:
    from usher.composition import scope

    session = _Session()
    opened = scope(_sessions(session), _itself, commit=True)

    async with opened() as held:
        assert session.commits == 0
        assert held is session

    assert session.commits == 1


@pytest.mark.asyncio
async def test_a_raise_inside_the_body_leaves_it_uncommitted() -> None:
    from usher.composition import scope

    session = _Session()
    opened = scope(_sessions(session), _itself, commit=True)

    with pytest.raises(RuntimeError):
        async with opened():
            raise RuntimeError("the chunk failed")

    assert session.commits == 0


@pytest.mark.asyncio
async def test_a_non_committing_scope_never_commits() -> None:
    from usher.composition import scope

    session = _Session()
    opened = scope(_sessions(session), _itself, commit=False)

    async with opened():
        pass

    assert session.commits == 0
