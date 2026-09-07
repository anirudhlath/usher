"""The LLM cost ledger: a write on every attempted completion, and one
windowed read for the statement that judges what they cost.

Implemented by
`usher.db.repositories.llm_call.PostgresLLMCallRepository`.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from pydantic import AwareDatetime

from usher.domain.curation import LLMCall

__all__ = [
    "LLMCallRepository",
]


class LLMCallRepository(ABC):
    """`llm_calls` -- PRD 10's cost ledger, one row per *attempted*
    completion.

    **Two methods: an append and one windowed read, and the read arrived last
    on purpose.** This port carried no read at all from M8 until M10, and the
    deferral was a decision with a date on it rather than an oversight. `m08a`
    shipped this table with its primary key and no other index *on the
    strength of this port having no read*, and wrote the two indexes it was
    deferring out as copy-pasteable `CREATE INDEX` statements beside the query
    each serves; `m10c` then shipped both verbatim, ahead of any reader,
    because M10 gets one migration and a reader task authoring its own DDL
    would be a second head. `list_since` is what discharges the rest of it.
    Until it landed, `ix_llm_calls_at` was an index nothing read -- which is
    the state `ffc` dropped `ix_titles_popularity` out of, and the state
    `PushHealth.record_reconnect` left PRD 10's reconnect metric in for a
    milestone, a dashboard reporting a healthy number about a thing that was
    never measured.

    🔴 **The caller that justifies the read is in `src/`, and it is not a
    dashboard.** PRD 10's own first principle
    (`10-telemetry-and-dashboards.md`, the datasource table: *"What is in the
    library, what do I watch, what did it cost -- **Postgres**, queried
    directly by Grafana"*) puts every spend panel on SQL living in the
    dashboard JSON, so dashboard 5 never calls this method and never will. A
    read whose only consumer were a Grafana panel would be
    `ix_titles_popularity` with extra steps: a surface maintained by this
    codebase for something that does not import it. What this method exists
    for is M10's **cost-anomaly evaluation**, which is application code that
    has to answer *"is today's spend more than three times the trailing
    seven-day median"* against the same rows, and which is the only consumer
    that makes the index defensible from inside `src/`. Anything that deletes
    that caller should delete this method with it.

    **`record()` is called on both paths and `ok` is the discriminator.** A
    ledger holding only the successes understates spend by exactly the
    failures, which are the rows an operator most wants to see -- and `ok` is
    not "the HTTP call returned 200" but "this generation produced something",
    the two being allowed to disagree in exactly one direction (ADR-0028: a
    call that answered perfectly and validated to zero rows is `ok = false`
    with a reason).

    **There is no `user_id` anywhere on this port**, because there is none on
    the table. Spend is attributed to an outcome by joining `curated_rows` on
    `generation_id`, which is what PRD 10's dashboard 5 *is*, rather than by
    denormalising a household onto a cost row.

    **Same session ownership as every other repository here: flushes, never
    commits.** `CurationService` writes the rows and the ledger entry for one
    generation in one transaction -- that join is dashboard 5 -- so the commit
    boundary is the caller's.
    """

    @abstractmethod
    async def record(self, call: LLMCall) -> None:
        """Append one *attempted* completion to the ledger, whether or not it
        worked -- and "worked" is not "got an answer".

        **One row per attempt**, which is narrower than one per call and wider
        than one per answer. A call that never reached the endpoint is a row,
        with zeroed tokens and the model this deployment asked for; a call that
        answered perfectly and validated to nothing is a row too, `ok = false`
        with the real tokens and the real cost. The single path that writes
        none is the one that attempted nothing at all -- an empty candidate
        pool raises before the client is touched, and an empty catalog is an
        operator's problem rather than an event of the LLM subsystem.
        PRD 06's record rule and
        [ADR-0028](../../../docs/prd/decisions/0028-the-pool-is-the-contract.md)'s
        rule 3 are the two halves of that.

        **Takes the domain model, not its eleven parts**, and the reason is
        not brevity. `LLMCall` already enforces the one invariant this row has
        -- `ok` and `error` must agree -- so a parts-shaped signature would
        have to build the identical model and raise the identical
        `ValidationError` one stack frame deeper, inside a repository, where a
        caller reading its own failure path cannot see it. The only version
        that would *not* raise is one that coerced a blank error into a
        placeholder, and inventing the operator-facing string that says what
        went wrong is a decision only the layer that knows what went wrong can
        make. Eleven adjacent parameters -- three of them integers, two of
        them UUIDs -- is also eleven chances to fill the wrong slot and still
        store a well-formed row.

        **What that leaves for the caller, stated because the failure path is
        the one this ledger exists for.** The model is constructed *inside*
        the `except` handler, and a call that raises there loses precisely the
        row it was about to write and replaces the original failure with a
        pydantic error. There is one reachable way to do that: `error` must be
        non-empty when `ok` is false, and `str(exc)` is `""` for an exception
        raised with no arguments. So the failure path spells it
        `error=str(exc) or type(exc).__name__`, never a bare `str(exc)`. Tasks
        11-13 own that call site; it is recorded here because this is where a
        reader looks for what `record()` will not do for them.

        **No scope parameter, therefore nothing to refuse.**
        `CuratedRowRepository.replace_for_user` raises `ValueError` for two
        caller-assembly mistakes, and both exist because it takes a `user_id`
        *and* rows that each carry one -- two spellings of one fact, which can
        disagree. This signature has one argument and no second source for any
        column, so the analogous refusal has nothing to compare. Nothing here
        raises `ValueError`.

        **One call, never a batch, and the shape is not provisional.** Both
        call sites record exactly once: `CurationService` makes one completion
        per generation, and `QueryExpansionService` makes one per search that
        embeds -- one per *request*, not a set assembled and flushed later. A
        batch would also be wrong in kind for the failure path, where the
        whole value of the row is that it is written at the moment of failure
        rather than accumulated into something a crash loses. This flushes and
        does not commit, so a caller that genuinely wants several rows in one
        transaction already has that; what it does not have is one round trip
        for all of them, and nothing here is inside a walk.

        Returns nothing. There is exactly one row and the caller already holds
        its id, so a count would be the constant `1` dressed as a measurement
        -- unlike `replace_for_user`, whose count is how many shelves a
        generation actually kept.

        **Raises `RepositoryConflict`, and the reason is not the primary
        key.** A fresh UUIDv7 makes a duplicate id nearly unreachable (a
        redelivered job re-runs the generation and mints a new one, which is
        the honest ledger: the money was spent twice), though it is translated
        too, since re-recording one object is a caller bug rather than
        something a retry clears. What makes the translation *load-bearing* is
        `cost_usd`: the column is `NUMERIC(12, 8)`, so a single call above
        `$9,999.99999999` raises `numeric field overflow` -- and `LLMCall`
        bounds that field with `ge=0` and no ceiling, so this is reachable
        from a **validly constructed** domain model. It is the one
        misconfiguration that precision was chosen to catch -- a price scaled
        *up* by a million on the way in, `$36,000` on one 12,000-token call.
        `usher.db.models.curation`'s module docstring is the one copy of that
        mechanism and of the two limitations it does not cover; this names it
        and points there.

        **Without translation it arrives at a service as a bare
        `sqlalchemy.exc.DBAPIError`** -- measured, and neither of the two
        exceptions an implementer reaches for: not `sqlalchemy.exc.
        IntegrityError`, and not `sqlalchemy.exc.DataError` either, so an
        `except` naming either catches nothing and a raw SQLAlchemy type
        reaches a service, which ADR-0009 forbids.
        `usher.db.repositories._errors.ROW_REFUSED_SQLSTATE_CLASSES` holds the
        one copy of that measurement, the identical shape on
        `curated_rows."position"`, and the bound on the claim; `is_row_refusal`
        is the shared filter both repositories use.

        A conflict leaves the session usable for the caller's other pending
        work, which matters more here than on any sibling port: the caller is
        typically already inside an exception handler with curated rows it
        still has to commit, and a poisoned session turns a failed ledger
        write into a lost generation.
        """

    @abstractmethod
    async def list_since(
        self, since: AwareDatetime, *, until: AwareDatetime | None = None
    ) -> Sequence[LLMCall]:
        """Every *attempted* completion whose `at` falls in `[since, until)`,
        oldest first.

        **Returns the domain model, symmetrically with `record()`, and the
        symmetry is the decision.** The parts-shaped alternative here is not a
        wider signature but a narrower *return*: a method answering
        `(day, model, purpose, sum(cost_usd), sum(tokens_in), sum(tokens_out))`
        instead of rows. That spelling looks cheaper and puts the aggregation
        inside a repository, which is the one place a panel author cannot read
        it -- the `GROUP BY` that decides whether a retried generation is one
        night's spend or two would live in SQL nobody reviews alongside the
        number it produces. `record()`'s own docstring makes the mirrored
        argument for the write: the parts version raises the identical
        `ValidationError` one stack frame deeper, inside a repository, where
        the caller reading its own failure path cannot see it. Rows out, model
        in, and the arithmetic stays where somebody can check it.

        **Half-open, `[since, until)`, and both halves are load-bearing.** A
        closed upper bound puts a call landing exactly on midnight into two
        adjacent days, so a spend-per-day series billing consecutive windows
        double-counts precisely the boundary rows -- and those are the rows a
        nightly curation run at a fixed hour produces most of. `since` is
        inclusive for the mirrored reason: the window that starts where the
        last one ended must not drop the instant between them.

        **`until=None` is unbounded, never `now()`.** `at` is when the
        completion happened, written by the caller -- `llm_calls.at` carries no
        `server_default` for exactly that reason -- so a row timestamped ahead
        of the server's clock is a reachable state rather than a corrupt one,
        and a default upper bound would silently discard it.

        **Every attempt, and never a `WHERE ok`.** The read is as wide as the
        write, or the ledger it reads back is not the ledger `record()` wrote:
        a call that answered perfectly and validated to zero rows
        ([ADR-0028](../../../docs/prd/decisions/0028-the-pool-is-the-contract.md))
        really did burn its tokens and really was billed for them. Filtering to
        the successes understates spend by exactly the failures, which are the
        calls an operator most wants to see, and it does it invisibly on every
        deployment whose calls happen to succeed.

        **Ordered by `at`, declared rather than inherited.** Postgres returns
        whatever the chosen plan produces, and `llm_calls.id` being a UUIDv7
        makes id order agree with `at` order on every fixture that mints its
        rows in the order it wants them back -- so an implementation with no
        `ORDER BY` looks correct until the day a redelivered job records an
        older call after a newer one.

        **No `purpose` or `model` filter, and no `limit`.** Neither column is
        indexed and both are refused deliberately (`m08a`: a deployment holds
        one or two values of each, so a btree over either is a structure with
        two entries). The caller groups in Python over a window it already
        bounded; a predicate here would be a sequential scan wearing a
        parameter. The bound on the answer is the window, which is the same
        bound `ix_llm_calls_at` serves.

        Raises nothing. There is no scope to refuse, no second source for any
        column to disagree with, and a window matching no rows is an empty
        sequence rather than an error -- a night on which nothing was
        generated is a real answer and the one a cost alert must be able to
        read.
        """
