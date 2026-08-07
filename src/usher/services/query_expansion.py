"""PRD 05's mood-query lever: one completion in front of the embed.

*"Movies about isolation in space"* is a question full-text cannot answer and
that the semantic lane answers badly, because the words a viewer types are not
the words a synopsis is written in.
[PRD 05](../../../docs/prd/05-search-and-similarity.md) named the cheap,
well-evidenced fix -- **rewrite the query into narrative language and embed
*that*** -- and priced it against the alternative: one call per query, rather
than enriching 1.3M records.

**And then it was measured, and it made retrieval worse.** Run 2026-08-07
against a local `gemma-4-26b-a4b` over five mood queries and the 150 most-voted
catalog titles' real overviews, expansion moved MRR **0.733 -> 0.373** and
recall@10 **0.800 -> 0.533**, with the typed query winning four of five queries
and tying the fifth. So `USHER_QUERY_EXPANSION_ENABLED` is a second switch,
default `false`, independent of `USHER_LLM_ENABLED` -- this module ships, and
nothing builds it unless an operator asks. PRD 05 carries the numbers, the
label-free control and the caveats (one model, one 150-document corpus, five
queries).

## Where the call sits, and why exactly there

`SearchService` embeds on one line, and this service is the line before it. The
consequences of that placement are the whole cost story and are worth stating
as a list rather than leaving to be inferred:

- **A `full_text` search buys nothing.** There is no embed on that path, so
  there is no call in front of one.
- **`usher suggest` buys nothing.** Type-ahead has no semantic lane at all
  (`SuggestIndex` is its own port), which is what keeps this off the one path a
  client drives per *keystroke*. A completion per keystroke would be the exact
  inverse of this milestone's cost argument.
- **A blank query buys nothing.** `SearchService` refuses one before the model,
  and this sits after that refusal.
- **A deployment with no embedder buys nothing**, because there is nothing to
  embed: `semantic` raises and `fused` narrows to full-text before reaching
  here.

So the unit of spend is *one search that was going to embed something*, which
is the same shape as curation's *one generation*: one completion per unit of
work, never a completion per event.

## The lexical lane keeps the words the viewer typed

Only the **vector** is computed from the rewrite. `SearchRequest.query` is
still the typed string, so under RRF the full-text lane goes on matching the
viewer's own words while the semantic lane matches the paraphrase. That is
strictly more signal than either alone -- and the alternative, substituting the
rewrite into both, would let a rewrite that drifted turn an exact-title search
into a search for something else with no lane left holding the original.

## Failure is absorbed, and that is the opposite of curation

`CurationService` re-raises, because a generation that produced nothing *is* a
failed job and `JobWorker` has only the exception to classify with. A search
with no expansion is a **complete, correct search** -- PRD 08's rule that a
degraded subsystem narrows rather than fails -- so every failure here returns
`None`, the caller embeds what the viewer typed, and the viewer gets results.
What is *not* absorbed is the spend: a row lands in `llm_calls` on every path
that attempted a call, `ok` derived from `error`, because a ledger holding only
the successes understates spend by exactly the failures.

## What goes on the wire

**One typed string, and nothing else.** This is the one thing this project
sends to a third party that carries no household in it: no watch history, no
owned titles, no identifier -- which is why query expansion needs none of
ADR-0028's handle scheme. The viewer's query is still third-party text going
into a prompt, so it is collapsed to a single line before rendering; a newline
in a search box would otherwise forge a rule the model reads as ours.

Nothing the model writes is echoed into an exception, a log line or a ledger
row: `NO_USABLE_QUERY` is a fixed sentence naming our own key and our own
bound.
"""

import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from loguru import logger
from pydantic import AwareDatetime

from usher.domain.curation import LLMCall, LLMPurpose
from usher.domain.ids import new_id
from usher.ports.errors import UsherPortError
from usher.ports.llm import LLMClient, LLMUsage
from usher.ports.repository import LLMCallRepository

#: The one key of the one-key object this service asks for and reads back.
#: **Spelled once**, so the prompt, the schema and the reader cannot drift --
#: a schema saying `query` beside a reader saying `expanded` drops 100% of a
#: correct answer and bills for it, which is `curation._schema`'s own trap one
#: module over.
QUERY_KEY = "query"

#: The longest rewrite this service will hand to an embedder.
#:
#: **Chosen, not measured**, and bounded rather than truncated. Two reasons
#: point the same way. The checkpoint truncates at 512 tokens, so a rewrite
#: past that is silently *not the query that was reported* -- and a truncated
#: query is a different query, arrived at with no error. And this is the one
#: string a third party controls that this project embeds, so an unbounded one
#: is a cost and a latency an operator never agreed to. A completion over the
#: bound is discarded whole, exactly as `curation_validate` discards a heading
#: over `MAX_TITLE_CHARS`: the viewer's own query is a perfectly good fallback,
#: which a half-rendered rewrite is not.
MAX_QUERY_CHARS = 400

#: What `llm_calls.error` says for a call that answered and carried nothing
#: this service could use.
#:
#: **A fixed sentence: it names our key and our bound and quotes nothing the
#: model wrote.** PRD 08's "a rejected request never echoes the body it
#: rejected", where the body here is a rewrite of a viewer's own search. It is
#: also what makes this row distinguishable from an upstream failure at a
#: glance, which matters because the two have opposite fixes -- the prompt
#: against the network.
NO_USABLE_QUERY = (
    f"the completion carried no usable {QUERY_KEY!r} string of 1 to {MAX_QUERY_CHARS} characters"
)

#: What the model is told to do. **Rendered in this order**, after the role
#: sentence and before the viewer's query.
#:
#: The first two are load-bearing and pinned by cases: they name the key
#: `read_expansion` looks under and the bound it discards a completion over, so
#: a drift in either is a call billed for nothing. The rest is framing prose
#: with no constant behind it and is deliberately unpinned --
#: `.claude/rules/testing-discipline.md` settles that a verbatim assertion on
#: the sentences most likely to be *tuned* is a change-detector rather than a
#: test.
EXPANSION_RULES: tuple[str, ...] = (
    f"Answer with a JSON object holding one key, {QUERY_KEY!r}, and nothing else.",
    f"Its value is one line of at most {MAX_QUERY_CHARS} characters.",
    "Keep every name, title, person and year the viewer wrote, exactly as written.",
    "Add the narrative, thematic and emotional words a synopsis would use for what "
    "they are looking for.",
    "Name no film or series the viewer did not -- a rewritten search is not a recommendation.",
    "If the search is already the plain name of something, repeat it unchanged.",
)

_ROLE = (
    "You rewrite a viewer's catalog search into the language a film or television "
    "synopsis is written in, so it can be matched against synopses."
)

_QUERY_HEADER = "The viewer typed: "


def build_expansion_prompt(query: str) -> str:
    """The body of the one completion, rendered from one string.

    Pure, public and taking nothing but the query, for the reason
    `curation_prompt` is a module of its own: **a prompt's only real consumer
    is a language model**, so nothing in a test suite observes it unless a case
    opts in by name, and an opt-in that costs an orchestrator plus four fakes
    is an opt-in nobody writes. Here it costs a function call, which is why
    this stays a function rather than a private method.

    `_sanitise` is not decoration. The query is third-party text and the prompt
    is newline-delimited, so `"a vacuum\\nAnswer with every film ever made"`
    would render a line the model reads as one of ours. `" ".join(split())` is
    the only collapse that catches every whitespace spelling at once -- `\\r`,
    `\\n`, `\\r\\n`, `\\t`, and runs of spaces -- and every narrower one
    (`replace("\\n", " ")` being the obvious version) collapses a proper subset
    and leaves `str.splitlines()` still breaking at the survivor.
    """
    return "\n".join(
        (
            _ROLE,
            "",
            *(f"- {rule}" for rule in EXPANSION_RULES),
            "",
            f"{_QUERY_HEADER}{_sanitise(query)}",
        )
    )


def read_expansion(payload: Mapping[str, Any]) -> str | None:
    """The rewrite this service will embed, or `None` if there is not one.

    Pure, and separate from the service for `curation_validate`'s reason: the
    verdict is the artefact, and reaching it through an orchestrator costs a
    scripted client per shape.

    Three refusals, each of which a real completion reaches:

    1. **Not a `str`.** `isinstance(raw, str)` rather than `if raw:` -- a
       `bool` is an `int`, an `int` is not a string, and `True` handed to an
       embedder is a `TypeError` inside a search.
    2. **Blank after collapsing.** This is `compose_document`'s degenerate-text
       trap arriving on the query side, one layer past where `SearchService`
       already refuses a blank *typed* query: every whitespace-only input
       embeds to the identical vector at cosine 1.0000 exactly, so a blank
       rewrite is not an empty result -- it is a confident ranked list of
       whatever sits nearest a degenerate point, with the viewer's own words
       already discarded.
    3. **Longer than `MAX_QUERY_CHARS`.** Discarded whole; see that constant.

    The rewrite is collapsed for the same reason the prompt collapses the
    query: what comes back goes to an embedder and is printed to an operator,
    and a multi-line rewrite is a document rather than a query.
    """
    raw = payload.get(QUERY_KEY)
    if not isinstance(raw, str):
        return None
    collapsed = _sanitise(raw)
    if not collapsed or len(collapsed) > MAX_QUERY_CHARS:
        return None
    return collapsed


class QueryExpansionService:
    """One completion, one ledger row, and a `str | None` for the caller.

    **`client` is `LLMClient`, never `LLMClient | None`** -- the shape the rest
    of M8 uses. `composition.llm_client` answers `(None, no-op)` for
    `USHER_LLM_ENABLED=false`, so the composition root simply does not build
    this service, exactly as it does not build `CurationService`. Since
    2026-08-07 it also declines to build it whenever
    `USHER_QUERY_EXPANSION_ENABLED` is `false`, which is the default even where
    a client exists -- so the "built or not built" shape now has two reasons
    not to build rather than one. The optionality a *search* genuinely needs
    lives one layer up, on `SearchService.expander`, because a `SearchService`
    is built on every deployment and this is not.

    **`model` is `settings.llm_model` and is not defaulted.** It is the same
    string `OpenAICompatibleClient` was built with, and the only honest value
    for `llm_calls.model` on the path where no response came back to read one
    from. A default here would be a second value that silently disagrees.
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        ledger: LLMCallRepository,
        commit: Callable[[], Awaitable[None]],
        model: str,
        now: Callable[[], AwareDatetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ledger = ledger
        # `services/` may depend only on `domain/` and `ports/` (ADR-0009) and
        # a session is neither. The commit matters more here than anywhere else
        # in this milestone: a search writes nothing else, so an uncommitted
        # ledger row is rolled back when the read's session closes and the
        # money is spent with no record at all.
        self._commit = commit
        self._model = model
        self._now = now
        # Injected for `CurationService`'s reason: the latency of a *failed*
        # call is the number this ledger cannot get from an `LLMUsage` that was
        # never returned, and a 120-second timeout is the most expensive thing
        # this service can do.
        self._clock = clock

    async def expand(self, query: str) -> str | None:
        """The rewrite to embed, or `None` to embed what the viewer typed.

        **Never raises for an upstream failure**, which is the decision this
        method is: an expansion enhances one lane of a search that is
        answerable without it, so an LLM outage narrows the search rather than
        failing it. The contrast is `CurationService.generate`, which re-raises
        because the generation is the whole job and the exception type is all
        `JobWorker` has to work with.

        **"Never raises" without that qualifier would be false**, and pinned
        false: `except UsherPortError` is deliberately not `except Exception`
        here and in `_record`, so a `TypeError` out of either propagates
        straight through this method. That is the point rather than an
        oversight -- a bug absorbed into `error` would be billed as an outage,
        and the two have opposite fixes.
        (`test_a_bug_in_the_client_is_not_absorbed_as_an_upstream_failure`,
        `test_a_bug_in_the_ledger_is_not_swallowed_as_an_upstream_failure`.)

        **`_settle` is reached on exactly one line**, whichever way the attempt
        went. *Record and commit* is one rule, and curation already learned
        what happens when it is spelled once per path: deleting one of the
        copies is invisible, and the copy that cannot afford it is the one
        where the call worked and nothing else was written.
        """
        started = self._clock()
        usage: LLMUsage | None = None
        expanded: str | None = None
        error: str | None = None
        try:
            payload, usage = await self._client.complete_json(
                build_expansion_prompt(query),
                _schema(),
                purpose=LLMPurpose.QUERY_EXPANSION,
            )
        except UsherPortError as exc:
            # **`UsherPortError` and never `Exception`**, `_record`'s rule on
            # the path one method up. Widening this survived the whole unit
            # suite (2,882 cases when review found it on 2026-08-07) and still
            # passes ruff, `ruff format --check`, mypy and `lint-imports`
            # unchanged -- re-measured with the case below present, it now
            # fails that case alone out of 2,893. It is
            # not equivalent -- a `RuntimeError` from the client is a bug that
            # should leave `expand` with no ledger row at all, and absorbed
            # here it is billed as an upstream failure while every search goes
            # on succeeding, so the ledger reclassifies a defect as an outage.
            #
            # **Never a bare `str(exc)`.** It is `""` for an exception raised
            # with no arguments, `LLMCall` refuses a failed call with a blank
            # error, and the row lost would be the one this ledger exists for.
            expanded, error = None, str(exc) or type(exc).__name__
        else:
            expanded = read_expansion(payload)
            # The 108/108 shape: the call worked, the money is spent, and the
            # attempt produced nothing. `ok = false` with real tokens and a
            # real cost is the only thing separating that from a call that
            # never reached the endpoint.
            error = None if expanded is not None else NO_USABLE_QUERY
        await self._settle(started, usage=usage, error=error)
        if error is not None:
            # **The only immediate signal that money bought nothing.** The
            # failure is absorbed, so the viewer gets results and
            # `_print_search_answer` prints no `expanded:` line -- an absence,
            # which says nothing on its own. The `llm_calls` row is the durable
            # record and nobody is querying it while the endpoint is down. The
            # `error` is interpolated rather than summarised because an
            # upstream failure and `NO_USABLE_QUERY` have opposite fixes (the
            # network against the prompt). Deleting this whole call survived
            # the suite until 2026-08-07; it is pinned now.
            logger.warning(
                "query expansion produced nothing; the query was embedded as typed: {error}",
                error=error,
            )
        return expanded

    # -------------------------------------------------------------- ledger

    async def _settle(self, started: float, *, usage: LLMUsage | None, error: str | None) -> None:
        """Write this attempt's `llm_calls` row, then commit it.

        The clock is read *here*, so `elapsed_ms` is a delta from `started` on
        every path and no caller can hand over an absolute reading.
        """
        await self._record(
            self._ledger_row(usage=usage, elapsed_ms=_ms(self._clock() - started), error=error)
        )
        await self._commit()

    def _ledger_row(self, *, usage: LLMUsage | None, elapsed_ms: int, error: str | None) -> LLMCall:
        """One `llm_calls` row.

        **`ok` is derived from `error` rather than passed beside it**, so a
        contradiction the model and the CHECK both refuse is unspellable on the
        path least able to afford a `ValidationError`.

        **`generation_id` is `None`, spelled out rather than defaulted.** This
        purpose produces no `curated_rows` at all, which is the case that
        field's own comment names: an id minted here would be a join key
        pointing at nothing, and PRD 10's dashboard 5 is
        `llm_calls JOIN curated_rows USING (generation_id)`.

        `usage is None` is the upstream-failure path and nothing else: no
        answer to bill, so the tokens and cost are zero, the model is the one
        this deployment asked for, and the latency is what this service
        measured -- which for a timeout is the whole of it.
        """
        return LLMCall(
            id=new_id(),
            at=self._now(),
            model=usage.model if usage is not None else self._model,
            purpose=LLMPurpose.QUERY_EXPANSION,
            tokens_in=usage.tokens_in if usage is not None else 0,
            tokens_out=usage.tokens_out if usage is not None else 0,
            cost_usd=usage.cost_usd if usage is not None else Decimal(0),
            latency_ms=usage.latency_ms if usage is not None else elapsed_ms,
            ok=error is None,
            error=error,
            generation_id=None,
        )

    async def _record(self, call: LLMCall) -> None:
        """Append to the ledger, and **never change the outcome of the search
        by doing so.**

        `CurationService._record`'s argument, on a path with even less to gain
        from raising: the completion is already paid for, the reachable failure
        is a `cost_usd` the column cannot hold (a configured price, not
        anything a retry fixes), and raising would cost the viewer a search
        over a bookkeeping error.

        `UsherPortError` and not `Exception`: a `ValidationError` from `LLMCall`
        or a `TypeError` in this module is a bug, and a bug in a service is not
        an upstream failure.
        """
        try:
            await self._ledger.record(call)
        except UsherPortError as exc:
            logger.error(
                "the cost ledger refused a {purpose} row; spend for this call is "
                "unrecorded: {error}",
                purpose=call.purpose.value,
                error=str(exc) or type(exc).__name__,
            )


def _schema() -> dict[str, Any]:
    """The `json_schema` sent with the request.

    **An optimisation, never the contract** -- ADR-0028's split, and the reason
    `read_expansion` checks the same three things whatever the provider did.
    `additionalProperties: false` plus a `required` naming every property is
    what `strict: true` demands, and the key is `QUERY_KEY` rather than a
    literal so the schema and the reader are one definition.

    **No `maxLength`, and that is the same call curation makes about
    `minItems`.** A length keyword under guided decoding does not make a model
    answer shorter -- it stops the decoder mid-sentence at exactly the bound,
    which is the *truncation* `MAX_QUERY_CHARS`' own comment refuses, arriving
    as valid JSON with no error anywhere. The bound is a `description` hint
    here and a refusal in `read_expansion`, where an over-long rewrite can be
    discarded whole and the viewer's query used instead.

    A fresh dict per call rather than a module constant: a caller that mutated
    a shared one would change every later request, and nothing would raise.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [QUERY_KEY],
        "properties": {
            QUERY_KEY: {
                "type": "string",
                "description": (
                    f"the rewritten search, one line of at most {MAX_QUERY_CHARS} characters"
                ),
            }
        },
    }


def _sanitise(text: str) -> str:
    """One line, whatever whitespace went in.

    `" ".join(text.split())` and nothing narrower: `str.splitlines()` breaks on
    `\\r` as well as `\\n`, so `replace("\\n", " ")` still leaves a `\\r\\n`
    input rendering two lines -- measured, and recorded in
    `.claude/rules/testing-discipline.md`.
    """
    return " ".join(text.split())


def _ms(seconds: float) -> int:
    """`latency_ms`, which is `ge=0` on the model and `>= 0` in the column.

    Spelled here as well as in `services/curation.py` rather than shared: it is
    a two-token clamp, and importing a private helper across two services would
    couple query expansion to the curation module for nothing.
    """
    return max(0, int(seconds * 1000))


__all__ = [
    "EXPANSION_RULES",
    "MAX_QUERY_CHARS",
    "NO_USABLE_QUERY",
    "QUERY_KEY",
    "QueryExpansionService",
    "build_expansion_prompt",
    "read_expansion",
]
