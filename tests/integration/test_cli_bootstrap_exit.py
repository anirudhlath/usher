"""`usher bootstrap`'s exit status, through `usher.cli.main` or `python -m usher`.

Only the network is replaced; each checkpoint is read back from the real database.
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
from sqlalchemy import delete, func, select

import usher.composition
import usher.services.bootstrap
from usher import cli as usher_cli
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.models.bootstrap import ImportRunRow
from usher.db.models.title import TitleRow
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.services.bootstrap import RetryPolicy

_CROSSWALK = "wikidata.crosswalk"
_BASICS = "imdb.title.basics"
_RATINGS = "imdb.title.ratings"
#: Every checkpoint a case here can leave behind, the command's or its own seed.
_WRITTEN = (
    _BASICS,
    _RATINGS,
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


def _titles(url: str) -> int:
    async def read() -> int:
        engine = build_engine(url)
        try:
            async with engine.connect() as conn:
                count = await conn.execute(select(func.count()).select_from(TitleRow))
                return int(count.scalar_one())
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
    for dataset in _WRITTEN:
        assert _stored(postgres_url, dataset) is None, f"the premise: no {dataset} checkpoint"
    monkeypatch.setenv("USHER_DATABASE_URL", postgres_url)
    monkeypatch.setenv("USHER_SECRET_KEY", "b" * 32)
    try:
        yield postgres_url
    finally:
        _forget(postgres_url, _WRITTEN)


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


def test_a_full_bootstrap_exits_non_zero_and_skips_what_a_failure_would_poison(
    database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--phase all` over a network that never answers.

    Every `HEAD` is retried to the bound first, as a fetch would be. Then the failed
    IMDb import skips every phase that reads the catalog it was loading, the two TMDb
    exports fail on their own, and the crosswalk waits on all three. The exit names
    both lists.
    """

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
        "usher bootstrap: 3 imports failed: imdb.title.basics, tmdb.ids.movie, "
        "tmdb.ids.series; 5 phases skipped: ratings, credit-names, aliases, crosswalk, "
        "movielens; each line above ends with the command that resumes it, and "
        "`usher bootstrap-status` shows the checkpoints"
    )
    printed = _printed(capsys.readouterr().out)
    head = "HEAD https://datasets.imdbws.com/title.basics.tsv.gz failed: ConnectError"
    assert [line for line in printed if line.startswith(f"{_BASICS}: attempt")] == [
        f"{_BASICS}: attempt {n} of 5 failed resolving its revision: {head}; retrying in 0s"
        for n in range(1, 5)
    ]
    resumes = [line.rsplit("resume with: ", 1)[1] for line in printed if "resume with: " in line]
    assert resumes == [
        "usher bootstrap --phase imdb",
        "usher bootstrap --phase imdb",
        "usher bootstrap --phase imdb, then usher bootstrap --phase credit-names",
        "usher bootstrap --phase imdb, then usher bootstrap --phase aliases",
        "usher bootstrap --phase tmdb-ids",
        "usher bootstrap --phase tmdb-ids",
        "usher bootstrap --phase imdb, then usher bootstrap --phase tmdb-ids, "
        "then usher bootstrap --phase crosswalk",
        "usher bootstrap --phase imdb, then usher bootstrap --phase movielens",
    ]
    for dataset in (_BASICS, "tmdb.ids.movie", "tmdb.ids.series"):
        stored = _stored(database, dataset)
        assert stored is not None and stored.status is ImportRunStatus.FAILED, dataset
    for dataset in ("imdb.title.ratings", _CROSSWALK):
        assert _stored(database, dataset) is None, f"{dataset} was skipped, never started"


def _seed_failed_basics(url: str) -> ImportRun:
    """An earlier `--phase imdb` that stopped part-way, as its own run would record it."""

    async def seed() -> ImportRun:
        engine = build_engine(url)
        try:
            async with build_session_factory(engine)() as session:
                repository = PostgresImportRunRepository(session)
                started = await repository.start(_BASICS, "an-earlier-revision")
                failed = started.evolve(
                    status=ImportRunStatus.FAILED,
                    position=700_000,
                    error="an earlier run's failure",
                )
                await repository.save(failed)
                await session.commit()
                await repository.release(_BASICS)
                return failed
        finally:
            await engine.dispose()

    return asyncio.run(seed())


def _seed_completed(url: str, dataset: str, position: int) -> None:
    """A finished import of `dataset`, as its own run would have left it."""

    async def seed() -> None:
        engine = build_engine(url)
        try:
            async with build_session_factory(engine)() as session:
                repository = PostgresImportRunRepository(session)
                started = await repository.start(dataset, "an-earlier-revision")
                await repository.save(
                    started.evolve(status=ImportRunStatus.COMPLETED, position=position)
                )
                await session.commit()
                await repository.release(dataset)
        finally:
            await engine.dispose()

    asyncio.run(seed())


def test_a_revision_that_fails_over_completed_imports_leaves_them_completed_and_shown(
    database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--phase imdb` that cannot reach IMDb: exit 1, and the imports it had stand.

    `bootstrap-status` then shows each `completed`, with the error beside it.
    """
    for dataset, position in ((_BASICS, 12345), (_RATINGS, 99)):
        _seed_completed(database, dataset, position)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network in this case")

    monkeypatch.setattr(
        usher.composition,
        "bulk_client",
        lambda _: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    _instant_retries(monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap", "--phase", "imdb"])

    assert exit_info.value.code == (
        "usher bootstrap: 2 imports failed: imdb.title.basics, imdb.title.ratings; each line "
        "above ends with the command that resumes it, and `usher bootstrap-status` shows the "
        "checkpoints"
    )
    error = {
        name: f"HEAD https://datasets.imdbws.com/{name.removeprefix('imdb.')}.tsv.gz failed: "
        "ConnectError (gave up after 5 attempts over 0s)"
        for name in (_BASICS, _RATINGS)
    }
    printed = _printed(capsys.readouterr().out)
    assert [line for line in printed if "resume with: " in line] == [
        f"{_BASICS} failed before its first batch landed, and its completed import stands: "
        f"{error[_BASICS]}; resume with: usher bootstrap --phase imdb",
        f"{_RATINGS} failed before its first batch landed, and its completed import stands: "
        f"{error[_RATINGS]}; resume with: usher bootstrap --phase ratings",
    ]

    usher_cli.main(["bootstrap-status"])

    shown = _printed(capsys.readouterr().out)
    assert sorted(line for line in shown if line.startswith("imdb.")) == [
        f"{_BASICS:<24} completed  position=12345 seen=0 written=0 error={error[_BASICS]}",
        f"{_RATINGS:<24} completed  position=99 seen=0 written=0 error={error[_RATINGS]}",
    ]


def test_a_crosswalk_after_a_failed_imdb_import_is_refused_with_exit_1(
    database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The single-phase half: an earlier run's failure stops the phase before any query.

    Without it the crosswalk walks WDQS for eight minutes and links only the titles the
    failed import landed, and nothing ever links the rest.
    """
    _seed_failed_basics(database)

    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a refused phase reached the network: {request.url}")

    monkeypatch.setattr(
        usher.composition,
        "bulk_client",
        lambda _: httpx.AsyncClient(transport=httpx.MockTransport(no_network)),
    )

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap", "--phase", "crosswalk"])

    assert exit_info.value.code == (
        "usher bootstrap: 1 phase skipped: crosswalk; each line above ends with the command "
        "that resumes it, and `usher bootstrap-status` shows the checkpoints"
    )
    assert _printed(capsys.readouterr().out) == [
        "crosswalk skipped: imdb.title.basics is failed at position 700000, and crosswalk "
        "reads what that import writes; resume with: usher bootstrap --phase imdb, then "
        "usher bootstrap --phase crosswalk"
    ]
    assert _stored(database, _CROSSWALK) is None


def test_a_phase_refusing_an_empty_catalog_exits_1_and_says_what_to_run_first(
    database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--phase credit-names` before any `--phase imdb`: nothing imported, and not a success."""
    assert _titles(database) == 0, "the premise: the catalog is empty"

    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a refused phase reached the network: {request.url}")

    monkeypatch.setattr(
        usher.composition,
        "bulk_client",
        lambda _: httpx.AsyncClient(transport=httpx.MockTransport(no_network)),
    )

    with pytest.raises(SystemExit) as exit_info:
        usher_cli.main(["bootstrap", "--phase", "credit-names"])

    assert exit_info.value.code == (
        "usher bootstrap: 1 phase refused: credit-names; each line above ends with the "
        "command that resumes it, and `usher bootstrap-status` shows the checkpoints"
    )
    assert _printed(capsys.readouterr().out) == [
        "credit-names needs a catalog to join against: title.principals is keyed on imdb_id "
        "and titles is empty. Run --phase imdb first.",
        "credit-names refused: titles is empty, and credit-names joins against it; resume "
        "with: usher bootstrap --phase imdb, then usher bootstrap --phase credit-names",
    ]
    assert _stored(database, "imdb.credit_names") is None


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
    assert result.stderr.splitlines()[-1] == (
        "usher bootstrap: 1 import failed: wikidata.crosswalk; each line above ends with "
        "the command that resumes it, and `usher bootstrap-status` shows the checkpoints"
    )
    assert "Traceback" not in result.stderr
    stored = _stored(database, _CROSSWALK)
    assert stored is not None and stored.status is ImportRunStatus.FAILED
