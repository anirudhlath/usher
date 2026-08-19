"""Every outbound HTTP call in `src/usher/adapters/` is enumerated, and each
one has a recorded decision about its rate limiter.

**The deliverable here is the six declines, not the two limiters.** M10's S3
went through `adapters/` by grep rather than by memory.

**The unit counted is the module, and that is stated because three different
counts were in circulation.** Nine modules under `src/usher/adapters/` dial an
upstream: **eight over httpx**, between them **fifteen call sites** (which is
what `_call_sites` below resolves), and a ninth, `usher.adapters.emby.push`,
over `websockets`. *Upstreams* is a smaller number than nine however you count
it -- `tmdb/client.py` and `tmdb/provider.py` are one host, and
`/embywebsocket` is the same machine as the media source -- so "nine" is not a
host count and this file does not use one. **Three of the nine modules are
paced** (`emby/session.py` behind `_MinInterval`, `tmdb/client.py` behind
`_TokenBucket`, and `tmdb/provider.py` through that one client, which its six
call sites share) and **six deliberately are not**.
`test_the_module_census_is_the_one_the_records_quote` asserts those four
numbers off the table itself, so `docs/prd/01-architecture.md` and this
docstring cannot drift apart from it silently.

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
from dataclasses import dataclass

import usher.adapters

#: The httpx client methods that put bytes on a wire. `build_request` is in
#: here because `EmbySession._send` and `TmdbClient._send` both build a request
#: and then send the reference on its own line (each says why in its own
#: comment), so a scan that looked only for `send` would find one site per
#: adapter where the source shows two.
_OUTBOUND_METHODS = frozenset({"send", "stream", "get", "post", "request", "build_request"})

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
    ("usher.adapters.bulk.download", "self._client.stream"): _Decision(
        upstream="the IMDb, TMDb and MovieLens dataset hosts (datasets.imdbws.com et al.)",
        limiter=(
            "none: this is **one streamed file per dataset**, not a request stream. A "
            "requests-per-second ceiling over a handful of multi-hundred-megabyte "
            "downloads paces nothing an operator would notice and expresses no policy "
            "anybody asked for -- the transfer is bounded by the wire"
        ),
        recorded_in="src/usher/adapters/bulk/download.py",
        paced=False,
    ),
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


def _call_sites() -> list[tuple[str, str, int]]:
    """Every `<something with "client" in it>.<outbound method>(...)` under
    `src/usher/adapters/`, as `(module, expression, line)`.

    Resolved from the AST rather than by grep so a call spelled across a line
    break is found -- `.claude/rules/api-telemetry-and-lanes.md` records a
    line-oriented search that was structurally blind to exactly that -- and so
    the *receiver* can be read rather than guessed at from the text before the
    dot. The receiver filter is what keeps `dict.get`, `Mapping.get` and
    `httpx.Response.request` out: the client attribute on every adapter here is
    named `_client`, and an adapter that named it something else would need a
    row in this docstring rather than a silent pass.
    """
    found: list[tuple[str, str, int]] = []
    for module, path in _adapter_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = node.func
            if not isinstance(call, ast.Attribute) or call.attr not in _OUTBOUND_METHODS:
                continue
            receiver = ast.unparse(call.value)
            if "client" not in receiver.lower():
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
    modules = {module for module, _ in _DECISIONS} | {_NOT_A_REQUEST}
    paced = {module for (module, _), record in _DECISIONS.items() if record.paced}

    assert len(modules) == 9, (
        "the module census moved, so `docs/prd/01-architecture.md`'s table, this file's "
        f"docstring and ADR-0039 are all now quoting a different tree: {sorted(modules)}"
    )
    assert len(_call_sites()) == 15, (
        "the httpx call-site count moved -- PRD 01 prints it, so it is corrected there "
        "in the same commit as the adapter that changed it"
    )
    assert len(paced) == 3, f"the paced modules are no longer three: {sorted(paced)}"
    assert len(modules - paced) == 6, (
        "the declines are no longer six, and each one has to be written beside its own "
        f"code as well as here: {sorted(modules - paced)}"
    )
    assert not paced - modules, "the premise: every paced module is in the census"
