"""Every outbound HTTP call in `src/usher/adapters/` is enumerated, and each
one has a recorded decision about its rate limiter.

**The deliverable here is the five declines, not the two limiters.** M10's S3
went through `adapters/` by grep rather than by memory and found nine upstreams
this project dials. Four of them are paced -- the media source behind
`_MinInterval` and TMDb's API behind `_TokenBucket`, the latter through one
client that six provider call sites share -- and **five deliberately are not**.
`.claude/rules/ports-and-error-taxonomy.md` records what happens when a
decision about an upstream is left implicit: the next reader cannot tell a
considered "no" from an oversight, and re-litigates it. So each of the five
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


@dataclass(frozen=True)
class _Decision:
    """What one call site dials, and what paces it.

    `recorded_in` is where the *code* says so -- a module docstring for four of
    the five declines, and `composition.image_proxy`'s own docstring for the
    fifth, which is where that decision was already written before S3 and which
    S3 confirms rather than reverses.
    """

    upstream: str
    limiter: str
    recorded_in: str


_SOURCE = _Decision(
    upstream="the configured media source (Emby), i.e. a machine in a household",
    limiter=(
        "the per-source `_MinInterval` gate, taken in `EmbySession._send` immediately "
        "above `build_request`. Owned by `SourceGateRegistry` at the composition root "
        "and keyed by `source.id`, so one source has one gate per process (ADR-0039)"
    ),
    recorded_in="src/usher/adapters/emby/session.py",
)
_TMDB = _Decision(
    upstream="api.themoviedb.org",
    limiter=(
        "`_TokenBucket` at `USHER_TMDB_REQUESTS_PER_SECOND` (30, under TMDb's ~40 rps "
        "ceiling -- ADR-0005, measured over 130,334 live requests)"
    ),
    recorded_in="src/usher/adapters/tmdb/client.py",
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
    ),
    # -- the five declines ---------------------------------------------------
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
    ),
}

#: The one upstream in `adapters/` that this scan **cannot** see, listed because
#: its absence is a decision too. `usher.adapters.emby.push` dials
#: `/embywebsocket` through `websockets`, not httpx, and holds the connection
#: open: **a socket held open is not a request**, so a requests-per-second gate
#: has nothing to space. What limits it is the reconnect *backoff*
#: (`PushSupervisor._backoff`, `src/usher/services/push.py`), which is the right
#: shape for the failure a limiter would be for here -- a lane reconnecting in a
#: loop against a server that is refusing.
_NOT_A_REQUEST = "usher.adapters.emby.push"


def _module_name(path: pathlib.Path, root: pathlib.Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return ".".join(("usher", "adapters", *parts))


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
    root = pathlib.Path(usher.adapters.__file__).parent
    found: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*.py")):
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
            found.append((_module_name(path, root), f"{receiver}.{call.attr}", node.lineno))
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
    """
    found = {module for module, _, _ in _call_sites()}
    assert _NOT_A_REQUEST not in found, (
        f"{_NOT_A_REQUEST} now makes an httpx call, so `a socket held open is not a "
        "request` no longer covers it and it needs a row in `_DECISIONS`"
    )


def test_every_recorded_decision_points_at_a_file_that_exists() -> None:
    """The declines are written where the code is, not only in this table.

    A pointer is checked for existence rather than for wording: a case
    asserting on the sentences would be a change-detector on prose that is
    meant to be improved (`.claude/rules/testing-discipline.md` records both
    halves of that trade). What a moved or renamed module *cannot* do is leave
    the pointer resolving.
    """
    repository = pathlib.Path(usher.adapters.__file__).parents[3]
    missing = sorted(
        {
            decision.recorded_in
            for decision in _DECISIONS.values()
            if not (repository / decision.recorded_in).is_file()
        }
    )
    assert not missing, f"a decision points at a file that is not there: {missing}"
    assert (repository / "src" / "usher" / "adapters").is_dir(), (
        "the premise: the repository root was resolved, so the check above ran "
        "against real paths rather than reporting every pointer as present"
    )
