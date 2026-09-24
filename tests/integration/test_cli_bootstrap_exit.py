"""`usher bootstrap`'s exit status, through the real entry point and a real PostgreSQL.

The defect this pins was observed on a clean checkout: `--phase crosswalk` printed a
link count and exited 0 while `bootstrap-status` showed the phase `failed`, twice. So
every case here drives `usher.cli.main` -- or the interpreter running `python -m usher`
-- with only the network replaced, and reads the checkpoint back from the database
the command wrote it to.
"""

import asyncio
import http.server
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete

import usher.composition
import usher.services.bootstrap
from usher import cli as usher_cli
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.services.bootstrap import RetryPolicy

_CROSSWALK = "wikidata.crosswalk"
#: Everything `--phase all` can fail against a transport that refuses every request
#: and an empty catalog (the three catalog-joining phases refuse before any request).
_ALL_FAILING = (
    "imdb.title.basics",
    "imdb.title.ratings",
    "tmdb.ids.movie",
    "tmdb.ids.series",
    _CROSSWALK,
)


def _stored(url: str, dataset: str) -> ImportRun | None:
    async def read() -> ImportRun | None:
        engine = build_engine(url)
        try:
            async with build_session_factory(engine)() as session:
                return await PostgresImportRunRepository(session).get(dataset)
        finally:
            await engine.dispose()

    return asyncio.run(read())


def _forget(url: str, datasets: tuple[str, ...]) -> None:
    """Delete this module's checkpoints, which the command committed for real."""

    async def run() -> None:
        engine = build_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(ImportRunRow).where(ImportRunRow.dataset.in_(datasets)))
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.fixture
def database(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The migrated test database, configured the way an operator's `.env` would be.

    Checkpoints are committed by the command itself, so they are removed afterwards
    rather than rolled back -- and asserted absent beforehand, since a leftover one
    would turn a fresh run into a resume.
    """
    for dataset in _ALL_FAILING:
        assert _stored(postgres_url, dataset) is None, f"the premise: no {dataset} checkpoint"
    monkeypatch.setenv("USHER_DATABASE_URL", postgres_url)
    monkeypatch.setenv("USHER_SECRET_KEY", "b" * 32)
    try:
        yield postgres_url
    finally:
        _forget(postgres_url, _ALL_FAILING)


def _wdqs(answer: Callable[[int], httpx.Response]) -> Callable[[Settings], httpx.AsyncClient]:
    """A `bulk_client` whose every request is answered by `answer(request_number)`."""
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return answer(count)

    return lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped attempt bound with the waits taken out."""
    monkeypatch.setattr(
        usher.services.bootstrap,
        "DEFAULT_RETRY",
        RetryPolicy(first_delay=0.0, max_delay=0.0),
    )


def _timeout_504(_: int) -> httpx.Response:
    return httpx.Response(504, text="upstream request timeout")


def _printed(out: str) -> list[str]:
    """What the command printed, without the log records sharing its stdout.

    `configure_logging` sends loguru to stdout as one JSON object per line
    (`USHER_LOG_JSON` defaults on), so a line opening with `{` is the log's, and every
    other line is the command's own report.
    """
    return [line for line in out.splitlines() if not line.startswith("{")]


def test_a_crosswalk_that_never_recovers_exits_non_zero_after_its_retries(
    database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Five attempts at the first page, each announced, then a failure that says what next."""
    monkeypatch.setattr(usher.composition, "bulk_client", _wdqs(_timeout_504))
    _instant_retries(monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap", "--phase", "crosswalk"])

    # `SystemExit(<str>)` is how the CLI exits 1 with a message on stderr.
    assert exit_info.value.code == (
        "usher bootstrap: 1 import failed: wikidata.crosswalk; each line above ends with "
        "the command that resumes it, and `usher bootstrap-status` shows the checkpoints"
    )
    assert _printed(capsys.readouterr().out) == [
        *(
            f"wikidata.crosswalk: attempt {n} of 5 failed at position 0: "
            "WDQS returned HTTP 504 for P4947 page 0; retrying in 0s"
            for n in range(1, 5)
        ),
        "wikidata.crosswalk failed at position 0: WDQS returned HTTP 504 for P4947 page 0 "
        "(gave up after 5 attempts over 0s); resume with: usher bootstrap --phase crosswalk",
    ]
    stored = _stored(database, _CROSSWALK)
    assert stored is not None and stored.status is ImportRunStatus.FAILED
    assert stored.position == 0


def test_a_crosswalk_that_recovers_exits_zero_and_says_it_retried(
    database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One timeout, then empty pages: the retry is printed and the command succeeds."""

    def once_then_empty(number: int) -> httpx.Response:
        if number == 1:
            return _timeout_504(number)
        return httpx.Response(200, json={"results": {"bindings": []}})

    monkeypatch.setattr(usher.composition, "bulk_client", _wdqs(once_then_empty))
    _instant_retries(monkeypatch)

    usher_cli.main(["bootstrap", "--phase", "crosswalk"])  # returns: exit status 0

    assert _printed(capsys.readouterr().out) == [
        "wikidata.crosswalk: attempt 1 of 5 failed at position 0: "
        "WDQS returned HTTP 504 for P4947 page 0; retrying in 0s"
    ]
    stored = _stored(database, _CROSSWALK)
    assert stored is not None and stored.status is ImportRunStatus.COMPLETED


def test_a_full_bootstrap_exits_non_zero_when_any_dataset_failed(
    database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--phase all` shares the fix: every failed dataset is named, and the exit is 1."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network in this case")

    monkeypatch.setattr(
        usher.composition,
        "bulk_client",
        lambda _: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    _instant_retries(monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap", "--phase", "all"])

    assert exit_info.value.code == (
        f"usher bootstrap: {len(_ALL_FAILING)} imports failed: {', '.join(_ALL_FAILING)}; "
        "each line above ends with the command that resumes it, and "
        "`usher bootstrap-status` shows the checkpoints"
    )
    resumes = [
        line.rsplit("resume with: ", 1)[1]
        for line in _printed(capsys.readouterr().out)
        if "resume with: " in line
    ]
    assert resumes == [
        "usher bootstrap --phase imdb",
        "usher bootstrap --phase imdb",
        "usher bootstrap --phase tmdb-ids",
        "usher bootstrap --phase tmdb-ids",
        "usher bootstrap --phase crosswalk",
    ]
    for dataset in _ALL_FAILING:
        stored = _stored(database, dataset)
        assert stored is not None and stored.status is ImportRunStatus.FAILED, dataset


class _Refuses400(http.server.BaseHTTPRequestHandler):
    """WDQS's answer to a query it cannot parse: `400`, which nothing retries."""

    def do_GET(self) -> None:
        body = b"MalformedQueryException"
        self.send_response(400)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@contextmanager
def _local_wdqs() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Refuses400)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/sparql"
    finally:
        server.shutdown()
        server.server_close()


def test_the_interpreter_exits_1_when_the_crosswalk_fails(database: str, tmp_path: Path) -> None:
    """The literal exit status, from `python -m usher` -- the console script's code path.

    A separate process, so nothing is patched: the endpoint is a local server
    answering `400`, which is malformed rather than transient, so the first attempt is
    the last and the case costs no retry waits. `cwd` is a scratch directory so a
    developer's own `.env` cannot configure the child.
    """
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("USHER_", "OTEL_"))
    }
    with _local_wdqs() as endpoint:
        result = subprocess.run(
            [sys.executable, "-m", "usher", "bootstrap", "--phase", "crosswalk"],
            cwd=tmp_path,
            env={
                **env,
                "USHER_DATABASE_URL": database,
                "USHER_SECRET_KEY": "c" * 32,
                "USHER_WIKIDATA_ENDPOINT": endpoint,
            },
            capture_output=True,
            text=True,
            timeout=120,
        )

    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "wikidata.crosswalk failed at position 0: WDQS rejected the query with HTTP 400 "
        "(P4947 page 0); resume with: usher bootstrap --phase crosswalk"
    ) in result.stdout.splitlines()
    assert "usher bootstrap: 1 import failed: wikidata.crosswalk;" in result.stderr
    stored = _stored(database, _CROSSWALK)
    assert stored is not None and stored.status is ImportRunStatus.FAILED
