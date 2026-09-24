"""Every outbound HTTP call in `src/usher/adapters/` is enumerated here.

Each one carries a recorded decision about its rate limiter.
"""

import ast
import pathlib
import re
from dataclasses import dataclass

import usher
import usher.adapters

#: The httpx client methods that put bytes on a wire: `httpx.AsyncClient`'s own
#: request-issuing surface, not a shortlist of the ones this tree uses today.
_OUTBOUND_METHODS = frozenset(
    {
        "build_request",
        "delete",
        "get",
        "head",
        "options",
        "patch",
        "post",
        "put",
        "request",
        "send",
        "stream",
    }
)

#: The anchor this repository requires of every scan: a name the walk must
#: find, so "the scan matched nothing" cannot read like "the scan found nothing
#: to report".
_ANCHOR = "usher.adapters.emby.session"

#: The two tokens the receiver test is written in terms of. `_CLIENT` alone is
#: what a receiver has to say to be one; both together are what an *annotation*
#: or a constructor has to say, which is what keeps `websockets`' own
#: `ClientConnection` and `httpx.Response` out of `_client_spellings`.
_CLIENT = "client"
_LIBRARY = "httpx"

#: The complement guard's exemption list, and the reason each one is on it. The
#: call-site scan asks *what expression is called*; this asks *what module imports the
#: library*, and the second question is the one a rename cannot dodge -- you cannot make
#: an httpx call without importing httpx. These are the modules under `adapters/` that
#: import it and hold no row in `_DECISIONS`.
_NO_CALL_OF_ITS_OWN: dict[str, str] = {
    "usher.adapters.bulk.imdb": (
        "takes `client: httpx.AsyncClient` and hands it to `CachedDatasetFile` "
        "(`bulk/download.py`, which holds the row); the dumps are read off disk"
    ),
    "usher.adapters.bulk.movielens": (
        "the same shape as `bulk/imdb.py` -- one `CachedDatasetFile` per archive member, "
        "and the archive itself is a `zipfile` read after the download"
    ),
    "usher.adapters.bulk.tmdb_ids": (
        "the same shape again; the daily id export is a `CachedDatasetFile` and this "
        "module parses the gzip it leaves behind"
    ),
    "usher.adapters.emby.adapter": (
        "**owns** the client -- `self._client = client or httpx.AsyncClient(...)` -- and "
        "hands it to `EmbySession`, which is where the gate is taken and which holds the "
        "row. An outbound call spelled here would be one that skipped `_send`"
    ),
    "usher.adapters.http": (
        "the shared helpers themselves: `httpx.Response` in three signatures and "
        "`httpx.HTTPError` in `UNTRANSLATED_FAILURES`. It holds no client and dials "
        "nothing -- it is what the modules that do are written against"
    ),
}

#: The other side of the same complement: a module with a row in `_DECISIONS`
#: that imports no httpx at all. Exactly one, and it is why the equality below
#: is stated in both directions rather than as a subset.
_PACED_THROUGH_ANOTHER_MODULE: dict[str, str] = {
    "usher.adapters.tmdb.provider": (
        "its `self._client` is a `TmdbClient`, not an `httpx.AsyncClient` -- six calls "
        "through Usher's own client, which is where the bucket and the httpx import both "
        "live. This is the row that makes the module census and the httpx-import census "
        "different sets rather than one set counted twice"
    ),
}


@dataclass(frozen=True)
class _Decision:
    """What one call site dials, what paces it, and where the code says so.

    `recorded_in` is where the *code* says so -- a module docstring for five of the six
    declines, and `composition.image_proxy`'s own docstring for the sixth. `paced` is a
    field rather than a string match on `limiter`, because every decline's prose
    contains the word "limiter" too.
    """

    upstream: str
    limiter: str
    recorded_in: str
    paced: bool


_SOURCE = _Decision(
    upstream="the configured media source (Emby), i.e. a machine in a household",
    limiter=(
        "the per-source `_MinInterval` gate, taken in `EmbySession._send` immediately "
        "above `build_request`. Owned by `SourceGateRegistry` at the composition root "
        "and keyed by `source.id`, so one source has one gate per process"
    ),
    recorded_in="src/usher/adapters/emby/session.py",
    paced=True,
)
_TMDB = _Decision(
    upstream="api.themoviedb.org",
    limiter=(
        "`_TokenBucket` at `USHER_TMDB_REQUESTS_PER_SECOND` (30, under TMDb's ~40 rps ceiling)"
    ),
    recorded_in="src/usher/adapters/tmdb/client.py",
    paced=True,
)
_DATASETS = _Decision(
    upstream="the IMDb, TMDb and MovieLens dataset hosts (datasets.imdbws.com et al.)",
    limiter=(
        "none: this is **one streamed file per dataset plus one `HEAD` for its "
        "revision**, not a request stream. A requests-per-second ceiling over a handful "
        "of multi-hundred-megabyte downloads paces nothing an operator would notice and "
        "expresses no policy anybody asked for -- the transfer is bounded by the wire, "
        "and the `HEAD` beside it is one conditional request per dataset per bootstrap"
    ),
    recorded_in="src/usher/adapters/bulk/download.py",
    paced=False,
)

#: The closed table.
_DECISIONS: dict[tuple[str, str], _Decision] = {
    ("usher.adapters.emby.session", "self._client.build_request"): _SOURCE,
    ("usher.adapters.emby.session", "self._client.send"): _SOURCE,
    ("usher.adapters.tmdb.client", "self._client.build_request"): _TMDB,
    ("usher.adapters.tmdb.client", "self._client.send"): _TMDB,
    ("usher.adapters.tmdb.provider", "self._client.get"): _Decision(
        upstream="api.themoviedb.org",
        limiter="through `TmdbClient` above -- this module holds no client of its own",
        recorded_in="src/usher/adapters/tmdb/client.py",
        paced=True,
    ),
    # -- five of the six declines; `emby/push.py`'s is `_PUSH` below ----------
    ("usher.adapters.images.provider", "self._client.stream"): _Decision(
        upstream="image.tmdb.org (the provider's image CDN, unauthenticated)",
        limiter=(
            "none, deliberately: the CDN publishes no rate limit and is not the API "
            "the ~40 rps ceiling is about, so a limiter here would be invented against "
            "a number that does not exist. The real bound is the cache -- after the "
            "first request per (image, rung) there is no outbound traffic at all"
        ),
        recorded_in="src/usher/composition.py",
        paced=False,
    ),
    ("usher.adapters.bulk.download", "self._client.stream"): _DATASETS,
    # The `HEAD` half of the same decision.
    ("usher.adapters.bulk.download", "self._client.head"): _DATASETS,
    ("usher.adapters.bulk.wikidata", "self._client.get"): _Decision(
        upstream="query.wikidata.org (WDQS)",
        limiter=(
            "none, and named rather than omitted: this is a **bootstrap phase an "
            "operator runs by hand**, not a lane. It is 30 chunked SPARQL queries "
            "totalling a few minutes, run once per install, and WDQS's own ~65 s "
            "timeout plus the chunking is what bounds it. A courtesy gate here would "
            "pace a job nobody is waiting behind"
        ),
        recorded_in="src/usher/adapters/bulk/wikidata.py",
        paced=False,
    ),
    ("usher.adapters.llm.openai_compatible", "self._client.post"): _Decision(
        upstream="USHER_LLM_BASE_URL (any OpenAI-compatible endpoint; this deployment's "
        "is a local vLLM)",
        limiter=(
            "none: `curate` is capped at **1 in flight** by `KIND_CONCURRENCY` and PRD "
            "06 budgets one completion per household per day, so the concurrency "
            "ceiling already bounds this to a rate no gate would ever reach"
        ),
        recorded_in="src/usher/adapters/llm/openai_compatible.py",
        paced=False,
    ),
    ("usher.adapters.embedding.openai_compat", "self._client.post"): _Decision(
        upstream="the endpoint named by USHER_EMBEDDING_MODEL's `openai:` runtime prefix",
        limiter=(
            "none, on `openai_compatible.py`'s reasoning exactly: `index` is capped at "
            "**1 in flight** by `KIND_CONCURRENCY`, so the concurrency ceiling is the "
            "bound. Unlike `curate` this one is a backfill that can run for hours, "
            "which is why the cap is named here rather than assumed"
        ),
        recorded_in="src/usher/adapters/embedding/openai_compat.py",
        paced=False,
    ),
}

#: The sixth decline, and the one upstream in `adapters/` that this scan
#: **cannot** see -- which is why it is a row here rather than in `_DECISIONS`
#: above, and why it needs its own case below.
_PUSH = _Decision(
    upstream=(
        "/embywebsocket on the configured media source -- the same machine `_SOURCE` "
        "dials, over a different protocol, which is why a host count and a module "
        "count differ here"
    ),
    limiter=(
        "none: `usher.adapters.emby.push` dials through `websockets`, not httpx, and "
        "holds the connection open. **A socket held open is not a request**, so a "
        "requests-per-second gate has nothing to space. What limits it is the "
        "reconnect *backoff* (`PushSupervisor._backoff`, `src/usher/services/push.py`), "
        "which is the right shape for the failure a limiter would be for here -- a "
        "lane reconnecting in a loop against a server that is refusing"
    ),
    recorded_in="src/usher/adapters/emby/push.py",
    paced=False,
)

#: The module `_PUSH` is about, as the scan would name it if it could see it.
_NOT_A_REQUEST = "usher.adapters.emby.push"

#: Every record this file keeps, declines included. `_DECISIONS` is keyed by
#: call site and `_PUSH` has no call site to be keyed by, so the census and the
#: back-pointer check both walk this instead.
_RECORDS: tuple[_Decision, ...] = (*_DECISIONS.values(), _PUSH)


def _census() -> set[str]:
    """The nine modules under `adapters/` that dial an upstream."""
    return {module for module, _ in _DECISIONS} | {_NOT_A_REQUEST}


def _module_name(path: pathlib.Path, root: pathlib.Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(("usher", "adapters", *parts))


def _adapter_modules() -> list[tuple[str, pathlib.Path]]:
    """Every module the walk below parses, as `(module, path)`.

    Separate from `_call_sites` so a case can assert the walk *visited* a
    module that produces no call sites -- which is the whole of
    `test_the_push_channel_is_not_a_request_and_the_scan_confirms_it`, whose
    subject contributes nothing to the scan's output by design.
    """
    root = pathlib.Path(usher.adapters.__file__).parent
    return [(_module_name(path, root), path) for path in sorted(root.rglob("*.py"))]


def _names_a_client(spelling: str) -> bool:
    return _CLIENT in spelling.lower()


def _names_an_httpx_client(spelling: str) -> bool:
    lowered = spelling.lower()
    return _CLIENT in lowered and _LIBRARY in lowered


def _client_spellings(tree: ast.Module) -> set[str]:
    """Every name in one module that refers to an httpx client, however it is spelled."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        arguments = node.args
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            arguments.vararg,
            arguments.kwarg,
        ):
            if argument is None or argument.annotation is None:
                continue
            if _names_an_httpx_client(ast.unparse(argument.annotation)):
                names.add(argument.arg)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if isinstance(value, ast.Name | ast.Attribute):
                spelling = ast.unparse(value)
                bound = _names_a_client(spelling) or spelling in names
            elif isinstance(value, ast.Call):
                bound = _names_an_httpx_client(ast.unparse(value.func))
            else:
                bound = False
            if not bound:
                continue
            for target in targets:
                spelling = ast.unparse(target)
                if spelling not in names:
                    names.add(spelling)
                    changed = True
    return names


def _call_sites() -> list[tuple[str, str, int]]:
    """Every `<an httpx client>.<outbound method>(...)`, as `(module, expression, line)`.

    Resolved from the AST rather than by grep, so a call spelled across a line break is
    found and the *receiver* can be read rather than guessed at from the text before the
    dot -- which is what keeps `dict.get` and `httpx.Response.request` out. It
    over-matches deliberately: a red with the expression printed is cheaper than an
    unlisted outbound call passing in silence.
    """
    found: list[tuple[str, str, int]] = []
    for module, path in _adapter_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        clients = _client_spellings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = node.func
            if not isinstance(call, ast.Attribute) or call.attr not in _OUTBOUND_METHODS:
                continue
            receiver = ast.unparse(call.value)
            if not _names_a_client(receiver) and receiver not in clients:
                continue
            found.append((module, f"{receiver}.{call.attr}", node.lineno))
    return found


def test_no_outbound_http_call_escapes_a_recorded_decision() -> None:
    """Every outbound call site is in the table above, and every row of it is a call site.

    The three guards before the assertion each fail for a different reason: a walk that
    found nothing (`>= 9`), a walk that found something else (the anchor), and a walk
    whose receiver filter has started matching the wrong shape (the anchor again).
    """
    found = _call_sites()

    assert len(found) >= 9, (
        "the scan found nothing and a scan that globs nothing passes exactly like "
        f"one that passes -- {found}"
    )
    modules = {module for module, _, _ in found}
    assert _ANCHOR in modules, (
        f"the anchor is missing, so this walk is not looking at `adapters/` -- {sorted(modules)}"
    )

    keyed = {(module, expression) for module, expression, _ in found}
    unrecorded = keyed - set(_DECISIONS)
    stale = set(_DECISIONS) - keyed
    assert not unrecorded, (
        "an adapter dials an upstream that no row of `_DECISIONS` names, so nothing "
        f"records whether it should be paced: {sorted(unrecorded)}"
    )
    assert not stale, (
        "a row of `_DECISIONS` names a call site that no longer exists, so the table "
        f"describes an older tree: {sorted(stale)}"
    )


def test_the_push_channel_is_not_a_request_and_the_scan_confirms_it() -> None:
    """The ninth upstream, and the control on the table's completeness.

    `emby/push.py` dials out and gets nothing from this scan because it is a websocket
    rather than an httpx call -- a reason only if the scan really does find no httpx
    call there, since a push channel that had quietly grown an HTTP poll would be an
    unlimited request stream hidden behind the sentence that excuses the socket. An
    absence assertion is the shape a broken scan satisfies, so both premises are
    asserted: the `>= 9` guard, and that the walk parsed `emby/push.py` at all.
    """
    walked = {module for module, _ in _adapter_modules()}
    assert _NOT_A_REQUEST in walked, (
        f"the premise: the walk never parsed {_NOT_A_REQUEST}, so its absence from the "
        f"scan says nothing about what it dials -- {sorted(walked)}"
    )
    found = _call_sites()
    assert len(found) >= 9, (
        "the premise: the scan found nothing, and an absence assertion against a scan "
        f"that globs nothing passes exactly like one against a scan that works -- {found}"
    )

    assert _NOT_A_REQUEST not in {module for module, _, _ in found}, (
        f"{_NOT_A_REQUEST} now makes an httpx call, so `a socket held open is not a "
        "request` no longer covers it and it needs a row in `_DECISIONS`"
    )


def test_every_recorded_decision_points_at_a_file_that_exists() -> None:
    """A `recorded_in` naming nothing is a table describing an older tree.

    What fails here is a module renamed or moved out from under its row. A row naming a
    call site that no longer exists is caught instead by
    `test_no_outbound_http_call_escapes_a_recorded_decision`'s set equality.
    """
    repository = pathlib.Path(usher.adapters.__file__).parents[3]
    assert (repository / "src" / "usher" / "adapters").is_dir(), (
        "the premise: the repository root was resolved, so the checks below ran "
        "against real paths rather than reporting every pointer as present"
    )

    missing = sorted(
        {
            record.recorded_in
            for record in _RECORDS
            if not (repository / record.recorded_in).is_file()
        }
    )
    assert _RECORDS, "the premise: the record table is empty, so nothing was checked"
    assert not missing, f"a decision points at a file that is not there: {missing}"


def test_the_module_census_is_the_one_the_records_quote() -> None:
    """The four numbers this file and PRD 01 both print, taken off the table itself.

    The unit is the module, not the host. Counts rather than bounds: `>= 9` is satisfied
    by a table that grew a row nobody wrote a decline for, which is the drift this file
    exists to make loud.
    """
    modules = _census()
    paced = {module for (module, _), record in _DECISIONS.items() if record.paced}

    assert len(modules) == 9, (
        "the module census moved, so `docs/prd/01-architecture.md`'s table and this file's "
        f"docstring are both now quoting a different tree: {sorted(modules)}"
    )
    assert len(_call_sites()) == 16, (
        "the httpx call-site count moved -- PRD 01 prints it, so it is corrected there "
        "in the same commit as the adapter that changed it"
    )
    assert len(paced) == 3, f"the paced modules are no longer three: {sorted(paced)}"
    assert len(modules - paced) == 6, (
        "the declines are no longer six, and each one has to be written beside its own "
        f"code as well as here: {sorted(modules - paced)}"
    )
    assert not paced - modules, "the premise: every paced module is in the census"


def _imports_httpx(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".")[0] == _LIBRARY for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == _LIBRARY:
            return True
    return False


def test_every_module_that_imports_httpx_is_recorded_or_exempt() -> None:
    """The complement of the scan above, closing structurally what spelling cannot.

    You cannot make an httpx call without importing httpx.
    """
    importers = {
        module
        for module, path in _adapter_modules()
        if _imports_httpx(ast.parse(path.read_text(encoding="utf-8"), str(path)))
    }
    assert len(importers) >= 8, (
        "the premise: the import walk found almost nothing, and a complement guard over "
        f"an empty set is satisfied by every exemption list there is -- {sorted(importers)}"
    )

    recorded = _census() - {_NOT_A_REQUEST}
    unrecorded = importers - recorded - set(_NO_CALL_OF_ITS_OWN)
    assert not unrecorded, (
        "a module under `adapters/` imports httpx, holds no row in `_DECISIONS` and is not "
        "named as holding a client rather than calling one -- so an outbound call may exist "
        f"there under any spelling the scan does not know: {sorted(unrecorded)}"
    )

    stale = set(_NO_CALL_OF_ITS_OWN) - importers
    assert not stale, f"an exemption names a module that no longer imports httpx: {sorted(stale)}"

    without = recorded - importers
    assert without == set(_PACED_THROUGH_ANOTHER_MODULE), (
        "a recorded module stopped importing httpx (or started), so the two censuses no "
        f"longer differ by exactly the modules that pace through another one: {sorted(without)}"
    )


#: The number words PRD 01 spells its census in. A document written in prose
#: says "nine", not "9", so an assertion that reads it has to say so too.
_WORDS: dict[int, str] = {
    3: "three",
    6: "six",
    8: "eight",
    9: "nine",
    16: "sixteen",
}

#: The header row of the census table in `docs/prd/01-architecture.md`. Scoped
#: to this table rather than to its `##` heading, because that section carries a
#: **second** table -- the port/implementation one -- whose rows name source
#: paths such as `services/rows/base.py`. A section-wide
#: harvest would collect those and read as a census that had grown.
_PRD_TABLE = "| module → upstream | limiter |"

#: `emby/session.py` -> `usher.adapters.emby.session`, as PRD 01's rows spell
#: it: relative to `src/usher/adapters/`, in backticks.
_PRD_MODULE = re.compile(r"`([a-z_]+/[a-z_]+)\.py`")


def _prd_01() -> str:
    return (
        pathlib.Path(usher.__file__).parents[2] / "docs" / "prd" / "01-architecture.md"
    ).read_text(encoding="utf-8")


def _census_table(document: str) -> list[str]:
    """The contiguous rows of PRD 01's outbound table, header excluded."""
    lines = document.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == _PRD_TABLE]
    assert len(starts) == 1, (
        f"the premise: PRD 01 has {len(starts)} rows spelled {_PRD_TABLE!r}, so this walk is "
        "reading either nothing or two tables"
    )
    rows: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if not line.startswith("|"):
            break
        if set(line) <= set("|- "):
            continue
        rows.append(line)
    return rows


def test_prd_01_prints_the_census_this_table_computes() -> None:
    """PRD 01's table and prose name the same modules and counts as `_DECISIONS`."""
    document = _prd_01()
    rows = _census_table(document)
    assert len(rows) >= 5, f"the premise: the table walk found {len(rows)} rows"

    named = {
        f"usher.adapters.{path.replace('/', '.')}"
        for row in rows
        for path in _PRD_MODULE.findall(row)
    }
    census = _census()
    assert named == census, (
        "`docs/prd/01-architecture.md`'s outbound table and this file's `_DECISIONS` no "
        f"longer describe the same tree -- only in the document: {sorted(named - census)}; "
        f"only here: {sorted(census - named)}"
    )

    paced = {module for (module, _), record in _DECISIONS.items() if record.paced}
    prose = " ".join(document.split()).lower()
    for figure in (
        f"**{_WORDS[len(census)]} modules**",
        f"**{_WORDS[len(census) - 1]} over httpx**",
        f"**{_WORDS[len(_call_sites())]} call sites**",
        f"{_WORDS[len(paced)]} of the {_WORDS[len(census)]} are paced; "
        f"{_WORDS[len(census - paced)]} deliberately are not",
    ):
        assert figure in prose, (
            f"`docs/prd/01-architecture.md` does not print {figure!r}, so the document and "
            "this table are quoting different counts -- PRD 01 is corrected in the same "
            "commit as the adapter that moved the number (`CLAUDE.md`, 'Keep the PRD "
            "current')"
        )
