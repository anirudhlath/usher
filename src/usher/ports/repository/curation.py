"""Curated rows -- the port an LLM generation is persisted through.

Implemented by
`usher.db.repositories.curation.PostgresCuratedRowRepository`.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from usher.domain.curation import CuratedRow

__all__ = [
    "CuratedRowRepository",
]


class CuratedRowRepository(ABC):
    """`curated_rows` -- what one generation proposed, per household.

    **The only table in this project whose contents no re-run reproduces**
    (`domain/curation.py`), which decides both methods below. There is no
    oracle to diff a curated row against and no fixed-temperature re-run that
    reproduces one, so a write here is either the whole of a generation or
    none of it, and a read is either a whole generation or nothing.

    **The write is a scoped replace, and the scope is `user_id` -- never the
    rows being written.** `TitleNeighborRepository.replace` and
    `CreditRepository.replace_for_titles` make the identical argument for
    their own scopes and it arrives here at a third table: a generation that
    validated to *zero* rows contributes nothing to any scope derived from the
    rows, so such a delete deletes nothing and last night's screen stays up
    forever, with no future generation able to repair it. Here the argument is
    sharper than at either of those two, because the artefact is a *screen*: a
    stale shelf is not a stale number, it is a heading a household reads and
    believes.

    **Same session ownership as every other repository here: flushes, never
    commits.** `CurationService` writes the rows and the `llm_calls` ledger
    entry for the same generation in one transaction (PRD 10's dashboard 5 is
    that join), so the commit boundary is the caller's.
    """

    @abstractmethod
    async def replace_for_user(self, user_id: uuid.UUID, rows: Sequence[CuratedRow]) -> int:
        """Replace this user's whole screen with `rows`, atomically.

        Delete-then-insert in one transaction, so a generation that fails
        part-way leaves the *previous* screen intact rather than half of a new
        one. An implementation that cannot roll the delete back with the
        insert has not implemented this method: the failure it would produce
        is an empty home screen for a household whose last generation was
        fine, and nothing distinguishes that from a household the LLM has
        never run for.

        **There is no `generation_id` parameter, and M8's plan named one.**
        The departure is deliberate and this is the record of it. A separate
        scope argument exists on the two sibling ports because the scope
        genuinely cannot be recovered from the rows -- `seed_ids` and
        `title_ids` name things that may contribute *no* rows at all. That
        argument does not transfer: the scope here is `user_id`, which is
        already a parameter for exactly that reason, and `generation_id` is
        not a scope but a *stamp* that every `CuratedRow` already carries as a
        required field. `TitleNeighborRepository.replace`'s keyword-only
        `blend_fingerprint` is the near-miss to check this against, and it
        differs on the one point that matters: `ScoredNeighbor` has no
        fingerprint field, so passing it is the only way to make "write the
        rows and stamp them in a second statement" unspellable. Here the stamp
        is inside the row before the call is made. A third argument could
        therefore only restate a fact the rows hold -- and a signature that
        can be handed a `generation_id` disagreeing with its rows is one that
        eventually will be, which is a defect this shape cannot express.

        What the argument *would* have bought is bought instead by refusing
        the two disagreements that remain reachable, and both raise
        `ValueError` **before anything is written**:

        - a row whose `user_id` is not this call's, which would put a shelf on
          another household's screen and outside this delete's scope; and
        - rows carrying more than one `generation_id`, which is a half-built
          generation. Nothing raises on it later: `list_for_user` returns the
          newest generation, so the screen would simply come back short, which
          is the one failure this table is least able to make visible.

        `ValueError` rather than `RepositoryConflict`, following
        `SearchRequest`'s refusal of a fused request with no vector: neither
        is the backing store rejecting a write, both are a caller assembling a
        call that cannot mean anything. **The trade-off is that a `ValueError`
        is not a `UsherPortError`**, so a service catching this project's port
        taxonomy broadly does not catch these two -- and this is the first
        repository method here to raise a builtin across the port boundary
        (`SearchRequest` is a DTO, and `postgres.py`'s three are configuration
        bounds). That is deliberate rather than an oversight: `usher.ports.
        errors` exists to keep *storage-specific* exception types away from
        callers, which a builtin does not violate, and every member of it
        describes something that happened to a request -- an upstream refused,
        a row conflicted, a payload was malformed. Neither of these is a
        failure a caller could degrade around or retry; both are the call
        itself being wrong, and a service that catches them is a service
        papering over its own bug.

        **An empty `rows` is a legitimate and meaningful call, not a no-op.**
        It says "this generation produced nothing", and it must clear the
        household's screen -- see ADR-0028: a validator that ate the whole
        completion and a model that had nothing to say produce the same empty
        result, and both are honestly rendered as no curated shelves rather
        than as last night's.

        Idempotent by construction (PRD 08's redelivery rule, and
        `JobWorker.recover()` requeues an abandoned claim): the same
        rows twice leave the same screen and report the same count. That is
        also why the order is delete-then-insert -- the reverse meets this
        table's primary key on the very rows it is about to remove.

        Returns the number of rows stored, which is what makes `usher
        curate`'s report a number rather than a reassurance.

        **Anything the backing store refuses about a row raises
        `RepositoryConflict`**, and the enumeration is deliberately by
        outcome rather than by constraint kind, because the first version of
        it said "a CHECK or a foreign key" and was wrong twice over. It
        covers a `user_id` naming no household, a row the table's own CHECKs
        refuse, **a batch naming one row id twice** -- a primary key, which is
        neither of those, and a reachable caller-assembly mistake this port
        does not otherwise refuse -- and **a value a column cannot hold at
        all**, which is not a constraint: `position` is `ge=0` here and
        `integer` there, so a large enough one is refused by the driver before
        a statement is sent. An implementation that translates only integrity
        violations lets that last one cross the boundary raw.

        The session stays usable for the caller's other pending work either
        way -- the service commits the rows together with the ledger entry
        that paid for them, and a refused generation must not take the ledger
        entry with it.
        """

    @abstractmethod
    async def list_for_user(self, user_id: uuid.UUID) -> list[CuratedRow]:
        """This user's newest generation, in the model's own order.

        **Ordered by `position`, and that ordering is the product.**
        `CuratedRow`'s docstring: a curated row *is* an ordering, it is the
        only judgement the completion was bought for, and nothing downstream
        may re-sort it. `position` indexes the list the model returned, so it
        is the whole of the order; `id` breaks a tie only so that two reads of
        one generation agree, and no generation should ever produce one.
        Neither `slug` nor `id` is the key, and **the reason is no longer that
        the slug sorts wrong.** This paragraph read *"the slugs are minted
        `curated-1`, `curated-2`, … and sort `curated-1 < curated-10 <
        curated-2`"*, which was true when it was written and is not true now:
        M8 Task 13 made `services.curation_validate` -- the only thing that
        mints a curated slug -- zero-pad it to the width of the generation, so
        ten rows are `curated-01` … `curated-10` and the lexicographic order
        *is* the model's order.

        The conclusion is unchanged and the argument is now the stronger one:
        `position` is the field that **means** the ordering, and a slug that
        happens to sort correctly is a rendering that agrees with it. Ordering
        on the rendering would silently become wrong again the day anything
        else mints one, or the day a slug carries something other than a
        count. A UUIDv7 primary key is the same mistake without the reprieve:
        it agrees with insertion order, so it is *right on a small fixture and
        wrong in production*.

        **Only the newest generation, and the filter defends a state the write
        path here does not by itself reach.** `replace_for_user` is
        delete-then-insert in one transaction, so one writer leaves exactly
        one generation and a failure leaves the previous one whole. The filter
        is kept anyway, for three reasons in descending order of how much they
        cost:

        1. **Two writers can reach it.** M8 gives this table two call sites --
           the nightly `JobKind.CURATE` job and `POST
           /admin/rows/regenerate` -- and under PostgreSQL's default READ
           COMMITTED two concurrent generations for one household can both
           commit: the second transaction's `DELETE` cannot see rows the first
           has not committed yet, so it removes nothing of them. (Reasoned
           from the isolation level's own rules rather than measured here; the
           contract suite constructs the two-generation state through a seeder
           instead of racing two connections.) The write is atomic per
           generation, which is not the same promise as one generation
           existing.
        2. **The schema was chosen on the strength of this read.** `m08a`
           declares `ix_curated_rows_user_newest (user_id, generated_at DESC)`
           for it, and refuses `UNIQUE (user_id, slug)` precisely because
           that constraint would turn a second generation into a *failed
           write* where this read turns it into a stale screen stepped over. A
           read without the filter makes the refused constraint the wrong
           call in hindsight.
        3. **Retention.** Keeping the last N generations -- what PRD 10's
           dashboard 5 wants the day "cost per curated row" is asked over a
           window -- is a retention policy plus this read, or a retention
           policy plus a breaking change to it.

        What it costs is one correlated subquery per read, served by the same
        index as the outer predicate, on a table holding tens of rows per
        household.

        **Newest is decided by `generated_at` and then resolved to one
        `generation_id`**, rather than by taking every row sharing the newest
        timestamp. The two agree whenever the writer stamped one instant onto
        a whole generation -- which is what `curated_rows.generated_at`
        carrying no server default exists to guarantee -- and they diverge
        exactly when it did not, where returning a whole generation is the
        answer that keeps a screen coherent.

        An empty list for a household with no generation, never `None`: there
        is no third state to distinguish, and a nullable answer would make
        every caller branch on the difference between "nothing yet" and
        "nothing tonight", which this table cannot tell apart either.
        """
