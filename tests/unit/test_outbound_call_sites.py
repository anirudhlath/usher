"""Every outbound HTTP call in `src/usher/adapters/` is enumerated, and each
one has a recorded decision about its rate limiter.

**The deliverable here is the six declines, not the two limiters.** M10's S3
went through `adapters/` by grep rather than by memory.

**The unit counted is the module, and that is stated because three different
counts were in circulation.** Nine modules under `src/usher/adapters/` dial an
upstream: **eight over httpx**, between them **sixteen call sites** (which is
what `_call_sites` below resolves), and a ninth, `usher.adapters.emby.push`,
over `websockets`. *Upstreams* is a smaller number than nine however you count
it -- `tmdb/client.py` and `tmdb/provider.py` are one host, and
`/embywebsocket` is the same machine as the media source -- so "nine" is not a
host count and this file does not use one. **Three of the nine modules are
paced** (`emby/session.py` behind `SourceGate`, `tmdb/client.py` behind
`_TokenBucket`, and `tmdb/provider.py` through that one client, which its six
call sites share) and **six deliberately are not**.
`test_the_module_census_is_the_one_the_records_quote` asserts those four
numbers off the table itself, and
`test_prd_01_prints_the_census_this_table_computes` **opens
`docs/prd/01-architecture.md` and reads them back out of it**, so the document
and this docstring cannot drift apart from the tree silently.

🔴 **That last clause was a claim and not a case until 2026-08-19, and the
count it protected was wrong.** This docstring said the census assertion kept
PRD 01 from drifting; nothing here opened PRD 01, whose filename appeared only
in prose and in an assertion *message*, so editing its "nine modules" to "ten"
was green everywhere -- the same one-way-pointer finding
`_BACK_POINTER` records for the `src/` files, applied to `src/` and not to the
document. And the call-site count really had drifted, in the other direction:
`_OUTBOUND_METHODS` omitted **five** of `httpx.AsyncClient`'s eleven
request-issuing methods, and one of the five was live --
`CachedDatasetFile.revision`'s `self._client.head(...)`
(`bulk/download.py`), a real `HEAD` per dataset per bootstrap that no row of
this table named and that every number here and in PRD 01 was short by. The
figure was fifteen and is sixteen.

`.claude/rules/ports-and-error-taxonomy.md` records what happens when a
decision about an upstream is left implicit: the next reader cannot tell a
considered "no" from an oversight, and re-litigates it. So each of the six
carries its reason here *and* beside the code, and this case is what makes the
table closed rather than illustrative.

**A new adapter with a new outbound call is a red, not a discovery.** The
assertion is set *equality* against `_DECISIONS`, so an unlisted call site
fails and so does a listed one that no longer exists -- the second half is what
keeps the table from rotting into a description of an older tree.

**What this case is not.** It does not check that a limiter is *wired* -- that
is `test_adapters_factory.py`'s and `test_composition.py`'s job, and
`test_adapters_emby_session.py::test_every_send_passes_the_gate_including_the_authenticating_one`
counts the acquisitions. It checks that no outbound call exists whose limiter
nobody has decided about.
"""

import ast
import pathlib
import re
from dataclasses import dataclass

import usher
import usher.adapters

#: The httpx client methods that put bytes on a wire -- **all eleven of them**,
#: which is `httpx.AsyncClient`'s own request-issuing surface and not a
#: shortlist of the ones this tree happens to use today. `build_request` is in
#: here because `EmbySession._send` and `TmdbClient._send` both build a request
#: and then send the reference on its own line (each says why in its own
#: comment), so a scan that looked only for `send` would find one site per
#: adapter where the source shows two.
#:
#: 🔴 **`put`, `delete`, `patch`, `head` and `options` were missing, and the
#: omission was not theoretical.** `bulk/download.py`'s
#: `CachedDatasetFile.revision` has issued `self._client.head(...)` since M2 and
#: no row of `_DECISIONS` named it, because nothing looked. The four that were
#: still unused matter for the same reason the enum is closed at
#: `ConfiguredSourceAdapterFactory.build`: `factory.py` anticipates a Jellyfin
#: adapter at the `SourceKind` seam, and a write-back adapter -- which is
#: already what Usher does to Emby, currently routed through `_send` -- is
#: spelled `put`/`delete`. A scan blind to the verbs a *new* adapter would use
#: is a scan that reports "no new outbound calls" about exactly the adapter it
#: was written for.
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

#: **The back-pointer every record must carry**, and what makes
#: `test_every_recorded_decision_points_at_a_file_that_exists` able to fail.
#: A pointer that only runs table -> file is satisfied by any `src/` module
#: that exists for other reasons, so deleting a whole decline paragraph left
#: that case green. This is the other direction: the file the table names has
#: to name the table back.
#:
#: **Chosen over the upstream's host name, and the reason is measured rather
#: than stylistic.** A host recurs in these files for unrelated reasons --
#: `datasets.imdbws.com` appears three times in `bulk/download.py` and
#: `query.wikidata.org` three times in `bulk/wikidata.py`, only one of each
#: inside the decline -- so a host token is satisfied by a file whose decline
#: has been deleted. This path appears **exactly once** in each of the seven
#: files below and nowhere else under `src/`, which is why the assertion is on
#: a count of one rather than on membership.
_BACK_POINTER = "tests/unit/test_outbound_call_sites.py"

#: The two tokens the receiver test is written in terms of. `_CLIENT` alone is
#: what a receiver has to say to be one; both together are what an *annotation*
#: or a constructor has to say, which is what keeps `websockets`' own
#: `ClientConnection` and `httpx.Response` out of `_client_spellings`.
_CLIENT = "client"
_LIBRARY = "httpx"

#: **The complement guard's exemption list, and the reason each one is on it.**
#:
#: The call-site scan asks *what expression is called*; this asks *what module
#: imports the library*, and the second question is the one a rename cannot
#: dodge -- **you cannot make an httpx call without importing httpx.** Twelve
#: modules under `adapters/` import it, seven of them hold a row in
#: `_DECISIONS`, and these five are the difference. Each holds or hands out a
#: client rather than calling one, so each is a *decision* too and gets a
#: sentence here rather than being subtracted silently.
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

    `recorded_in` is where the *code* says so -- a module docstring for five of
    the six declines, and `composition.image_proxy`'s own docstring for the
    sixth, which is where that decision was already written before S3 and which
    S3 confirms rather than reverses.

    `paced` is what `test_the_module_census_is_the_one_the_records_quote`
    counts. It is a field rather than a string match on `limiter`, because
    every decline's prose contains the word "limiter" too.
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
        "and keyed by `source.id`, so one source has one gate per process (ADR-0039)"
    ),
    recorded_in="src/usher/adapters/emby/session.py",
    paced=True,
)
_TMDB = _Decision(
    upstream="api.themoviedb.org",
    limiter=(
        "`_TokenBucket` at `USHER_TMDB_REQUESTS_PER_SECOND` (30, under TMDb's ~40 rps "
        "ceiling -- ADR-0005, measured over 130,334 live requests)"
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

#: The closed table. **Keyed by module and call expression rather than by line**
#: -- a line number drifts on every edit above it, and a key that drifts is a
#: table that has to be rewritten rather than read. Two sites in one module
#: spelled the same way (`tmdb/provider.py`'s six `self._client.get` calls) are
#: one row, because they are one decision; a *new* spelling in that module
#: (`self._client.post`) would be a new key and a red.
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
        upstream="image.tmdb.org (the provider's image CDN, unauthenticated -- ADR-0032)",
        limiter=(
            "none, deliberately and already recorded before S3: the CDN publishes no "
            "rate limit and is not the API ADR-0005's ~40 rps is about, so a limiter "
            "here would be invented against a number nobody has measured. The real "
            "bound is the cache -- after the first request per (image, rung) there is "
            "no outbound traffic at all. S3 confirms this rather than reversing it"
        ),
        recorded_in="src/usher/composition.py",
        paced=False,
    ),
    ("usher.adapters.bulk.download", "self._client.stream"): _DATASETS,
    # The `HEAD` half of the same decision, and **the row this table was
    # missing entirely** until `_OUTBOUND_METHODS` grew the five verbs it had
    # omitted. `CachedDatasetFile.revision` has issued it since M2. One
    # `_Decision` object for both keys, because they are one decision: the
    # census counts modules, and a second reason here would read as a second
    # upstream.
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
    """Every spelling in one module that refers to an httpx client, beyond the
    ones whose own text says so.

    🔴 **A one-line alias defeated the receiver filter, and both spellings of
    that were measured passing.** `c = self._client` followed by `await
    c.get(...)` reads as `c.get` at the call site, and `self._http =
    self._client` reads as `self._http.post` -- neither contains the token, so
    the scan found nothing and all four cases here stayed green. The second was
    half-acknowledged in this docstring as *"would need a row in this docstring
    rather than a silent pass"*, and it **was** the silent pass.

    So a receiver also counts when it is **bound to** something that is a
    client. Three seeds, and each is narrow on purpose:

    - a parameter annotated with a type naming both `httpx` and a client
      (`client: httpx.AsyncClient`, and `transport: httpx.AsyncClient` for a
      constructor that renames it);
    - an assignment whose value is a bare name or attribute that is already
      one (`c = self._client`, `self._http = self._client`);
    - an assignment whose value calls something naming an httpx client
      (`self._conn = httpx.AsyncClient(...)`).

    **Narrow on purpose, and the wide version was written first and thrown
    away.** Seeding from *any* expression mentioning a client made
    `payload = await self._client.get(...)` a client (the callee names one),
    `self._session = EmbySession(client=...)` a client, and -- through the
    `websockets.asyncio.client.ClientConnection` annotation -- `emby/push.py`'s
    socket a client, turning the one module whose whole decline is *"a socket
    held open is not a request"* into four bogus call sites. Anchoring the
    seeds to `httpx` keeps every one of those out and still catches both
    aliases above, because `self._client` is a client by its own name.

    Iterated to a fixed point so an alias of an alias resolves. Module-wide
    rather than scope-aware: a name is a client anywhere in the file once it is
    one anywhere, which over-matches rather than under-matches, and
    over-matching is a red.
    """
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
    """Every `<an httpx client>.<outbound method>(...)` under
    `src/usher/adapters/`, as `(module, expression, line)`.

    Resolved from the AST rather than by grep so a call spelled across a line
    break is found -- `.claude/rules/api-telemetry-and-lanes.md` records a
    line-oriented search that was structurally blind to exactly that -- and so
    the *receiver* can be read rather than guessed at from the text before the
    dot. The receiver test is what keeps `dict.get`, `Mapping.get` and
    `httpx.Response.request` out: the client attribute on every adapter here is
    named `_client`, and one that is named something else is reached through
    `_client_spellings` above rather than through a silent pass.

    **It over-matches, and that is the safe direction, kept deliberately.**
    `self._client_config.get("x")` would resolve as a call site here. The
    failure that causes is a **red** with the expression printed, which somebody
    fixes in a minute; the failure a tighter test causes is an unlisted outbound
    call passing in silence, which is what this whole file exists to prevent.
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
    """The acceptance: every outbound call site is in the table above, and
    every row of the table is a call site.

    The three guards before the assertion are the ones this repository requires
    of a scan, and each fails for a different reason: a walk that found nothing
    (`>= 9`), a walk that found *something else* (the anchor), and a walk whose
    receiver filter has started matching the wrong shape (the anchor again,
    which is the module the filter was written against).
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

    `emby/push.py` is the one module in `adapters/` that dials out and gets
    nothing from this scan -- because it is a websocket rather than an httpx
    call. That is a *reason* rather than an omission, and it is only a reason if
    the scan really does find no httpx call there: a push channel that had
    quietly grown an HTTP poll would be an unlimited request stream against a
    household's server, hidden behind the very sentence that excuses the socket.

    🔴 **Its two premises, because an absence assertion is exactly the shape a
    broken scan satisfies.** This case shipped in S3 with neither, and measured:
    with `_call_sites`' receiver filter broken so the walk returns `[]`, the
    sibling case above failed on `assert 0 >= 9` and **this one passed** -- an
    absence proved by a scan that found nothing, in a file whose own docstring
    is about that failure mode. So the same `>= 9` guard runs here (a scan that
    globs nothing cannot satisfy it), and so does the one this case needs and
    its sibling does not: that the walk **parsed `emby/push.py` at all**. The
    second is not implied by the first -- a module renamed or moved out of
    `adapters/` produces a healthy scan and a vacuous absence.
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
    """The declines are written where the code is, not only in this table --
    and the file says so itself rather than merely existing.

    🔴 **Existence alone could not fail, which is what this case was for.**
    Every `recorded_in` names an ordinary `src/` module that exists for its own
    reasons, so as S3 shipped it, deleting an entire decline paragraph from any
    of the docstrings left this green: the acceptance was met and nothing
    checked that it stayed met. The repair is the **other direction of the
    pointer** -- the file the table names has to name the table back
    (`_BACK_POINTER`), so a record removed from the code removes the token and
    this case goes red.

    **Still not a change-detector on prose**, which is the trade
    `.claude/rules/testing-discipline.md` records both halves of: what is
    asserted is one module *path*, not a sentence, so every word of the
    reasoning around it can be rewritten freely. **Exactly once**, not merely
    present, for the reason `_BACK_POINTER`'s own comment gives -- a token that
    recurs in a file for unrelated reasons is a token a deletion cannot remove.
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
    assert not missing, f"a decision points at a file that is not there: {missing}"

    unrecorded = sorted(
        {
            f"{record.recorded_in} ({count}x)"
            for record in _RECORDS
            for count in [
                (repository / record.recorded_in).read_text(encoding="utf-8").count(_BACK_POINTER)
            ]
            if count != 1
        }
    )
    assert not unrecorded, (
        f"a file this table points at does not name `{_BACK_POINTER}` exactly once, so the "
        "record beside the code has been deleted, duplicated or never written -- and this "
        f"table would go on describing a decision the code no longer states: {unrecorded}"
    )


def test_the_module_census_is_the_one_the_records_quote() -> None:
    """The four numbers this file's docstring and PRD 01 both print, asserted
    off the table rather than counted by hand twice.

    🔴 **Three countings were in circulation and none of them reconciled.**
    PRD 01 said *"nine upstreams, fifteen call sites across eight modules"* over
    a table of **eight** rows; this file said *"four are paced and five are
    not"*, which only sums to nine if `emby/push.py` is counted as paced -- and
    both the plan's row and PRD 01's own cell say its limiter is **none**. The
    unit is the **module** (the docstring says why a host count is a different,
    smaller number), and these are the four figures every other site must
    agree with.

    A count is asserted rather than a bound: `>= 9` is satisfied by a table
    that grew a row nobody wrote a decline for, which is the drift the whole
    file exists to make loud.
    """
    modules = _census()
    paced = {module for (module, _), record in _DECISIONS.items() if record.paced}

    assert len(modules) == 9, (
        "the module census moved, so `docs/prd/01-architecture.md`'s table, this file's "
        f"docstring and ADR-0039 are all now quoting a different tree: {sorted(modules)}"
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
    """The complement of the scan above, and it closes structurally what the
    receiver test can only close by spelling.

    🔴 **The scan asks what expression is called, and that question has a
    rename-shaped hole in it.** `_client_spellings` now resolves an alias and a
    renamed attribute, and it will never resolve *every* spelling -- a client
    reached through a factory function, a `getattr`, a list. This asks the other
    question, which has no spelling to dodge: **a module cannot make an httpx
    call without importing httpx.** So the set of importers is closed, in both
    directions, against the table:

    - an importer with no row and no exemption is a new outbound call, whatever
      it named its client -- which is the Jellyfin-adapter case `factory.py`
      anticipates at the `SourceKind` seam;
    - a row whose module no longer imports httpx is a decision describing an
      older tree, the same second half `_DECISIONS`' own equality carries;
    - an exemption that has stopped importing httpx is a paragraph explaining
      an absence.

    **The exemptions are five and each carries a reason** (`_NO_CALL_OF_ITS_OWN`),
    for `.claude/rules/ports-and-error-taxonomy.md`'s argument in the same shape
    the six declines take: a considered "this one holds a client rather than
    calling one" and an oversight look identical to the next reader unless the
    first is written down.
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
#: **second** table -- the port/implementation one -- whose rows name
#: `adapters/bulk/movielens.py` and `services/rows/base.py`. A section-wide
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
    """🔴 **The docstring above claimed this and no case did it.**

    `test_the_module_census_is_the_one_the_records_quote` asserts four numbers
    off `_DECISIONS`; it never opens `docs/prd/01-architecture.md`, whose name
    appeared in this module only in prose and in an assertion *message*, and
    whose own `recorded_in` pointers are eight `src/` files and no document.
    `tests/unit/test_docs_currency.py` is the only PRD-consistency case in the
    repository and it covers two status tables, not this one. So editing PRD
    01's "nine modules" to "ten" was green everywhere, and what the census case
    actually bought was a *prompt* -- which is the one-way-pointer finding
    `_BACK_POINTER` records, applied to the `src/` files and not to the
    document that prints the same numbers.

    **Two halves, because either alone is satisfiable by the other's defect.**
    The table's rows are compared as a **set of modules** against the census, so
    a row added, dropped or renamed is red and every word of prose around them
    stays free to be rewritten -- the trade
    `.claude/rules/testing-discipline.md` records both sides of. And the four
    figures in the paragraph above it are matched as spelled numerals, because
    a table with nine rows under a sentence saying "ten modules" is exactly the
    drift that was reachable.

    Scoped to the table rather than to the document (`_PRD_TABLE`), for M9 H2's
    finding: a check that reads a whole document can be satisfied by the prose
    written to explain its own repair.
    """
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
