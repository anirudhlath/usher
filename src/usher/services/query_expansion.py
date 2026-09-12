"""PRD 05's mood-query lever: one completion in front of the embed."""

import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from pydantic import AwareDatetime

from usher.domain.curation import LLMPurpose
from usher.ports.errors import UsherPortError
from usher.ports.llm import LLMClient, LLMUsage
from usher.ports.repository import LLMCallRepository
from usher.services.curation_prompt import one_line
from usher.services.llm_ledger import LLMLedger

#: The one key of the one-key object this service asks for and reads back.
#: **Spelled once**, so the prompt, the schema and the reader cannot drift --
#: a schema saying `query` beside a reader saying `expanded` drops 100% of a
#: correct answer and bills for it, which is `curation._schema`'s own trap one
#: module over.
QUERY_KEY = "query"

# : The longest rewrite this service will hand to an embedder.
MAX_QUERY_CHARS = 400

# : What `llm_calls.error` says for a call that answered and carried nothing : this
# service could use.
NO_USABLE_QUERY = (
    f"the completion carried no usable {QUERY_KEY!r} string of 1 to {MAX_QUERY_CHARS} characters"
)

# : What the model is told to do.
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
    """The body of the one completion, rendered from one string."""
    return "\n".join(
        (
            _ROLE,
            "",
            *(f"- {rule}" for rule in EXPANSION_RULES),
            "",
            f"{_QUERY_HEADER}{one_line(query)}",
        )
    )


def read_expansion(payload: Mapping[str, Any]) -> str | None:
    """The rewrite this service will embed, or `None` if there is not one."""
    raw = payload.get(QUERY_KEY)
    if not isinstance(raw, str):
        return None
    collapsed = one_line(raw)
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
        # **The ledger rule is `services/llm_ledger.py`'s, not this module's.** This
        # class used to carry a verbatim copy of `CurationService`'s `_settle` /
        # `_ledger_row` / `_record` -- which put the count of spellings back at two, one
        # milestone after a sweep measured what a second spelling costs (a deleted
        # commit surviving 42 cases).
        self._spend = LLMLedger(
            ledger=ledger,
            commit=commit,
            model=model,
            purpose=LLMPurpose.QUERY_EXPANSION,
            now=now,
            clock=clock,
        )
        # The *same* callable the ledger holds, kept because `expand` stamps
        # `started` before the call and `settle` reads the other end of that
        # window. One clock, two readings -- handing the ledger a second
        # callable would make `elapsed_ms` a delta between two different
        # clocks, which is the shape `_T0` exists to make visible.
        self._clock = clock

    async def expand(self, query: str) -> str | None:
        """The rewrite to embed, or `None` to embed what the viewer typed."""
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
            # **`UsherPortError` and never `Exception`**, `_record`'s rule on the path
            # one method up.
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
            # **The only immediate signal that money bought nothing.** The failure is
            # absorbed, so the viewer gets results and `_print_search_answer` prints no
            # `expanded:` line -- an absence, which says nothing on its own.
            logger.warning(
                "query expansion produced nothing; the query was embedded as typed: {error}",
                error=error,
            )
        return expanded

    # -------------------------------------------------------------- ledger

    async def _settle(self, started: float, *, usage: LLMUsage | None, error: str | None) -> None:
        """Close out one attempted completion, through the one ledger.

        **`generation_id` stays `None` and is the ledger's default rather than
        this method's argument.** This purpose produces no `curated_rows` at
        all, so an id minted here would be a join key pointing at nothing, and
        PRD 10's dashboard 5 is `llm_calls JOIN curated_rows USING
        (generation_id)`.
        """
        await self._spend.settle(started, usage=usage, error=error)


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


__all__ = [
    "EXPANSION_RULES",
    "MAX_QUERY_CHARS",
    "NO_USABLE_QUERY",
    "QUERY_KEY",
    "QueryExpansionService",
    "build_expansion_prompt",
    "read_expansion",
]
