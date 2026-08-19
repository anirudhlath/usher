"""The write-time half of the genre vocabulary — ADR-0039's deferred point 2.

`usher.domain.genres` holds Usher's own 31-concept vocabulary and, until this
service, applied it only at *read* time: `_browse_filters` expands a label into
every spelling of the concepts it names and `browse_facets` collapses the counts
back. That fixed `/browse` and left `search_document`'s weight class D, the
embedded documents, and every row provider reading the raw column split across
two importers' alphabets.

**This is not an Alembic migration, and that is the central decision.** The
vocabulary is *data*: `GENRE_ALIASES` grew by five members the day it was
written and will grow again when a third importer or a new TMDb genre arrives.
A one-shot migration normalises the catalog as of the day it ran and cannot be
re-run when the map changes — and it would run inside `alembic upgrade head`,
which every integration test and every container start executes, holding one
transaction over 1.27M rows. `canonicalise_genres` is idempotent, so the shape
that fits is the one `usher index --backfill` already has: sweep, write, report,
run it again whenever the vocabulary moves.

**Nothing here knows about embeddings except how to count them.** `titles.genres`
is segment 6 of 7 in `compose_document`, so a row this sweep moves stops
reproducing its stored `source_fingerprint` and `usher index` claims it on its
own. The service reads `count_stale` on both sides of its own writes purely so
the report can say what it cost; it never marks anything stale, because a second
definition of stale beside `_FINGERPRINT_SQL` is the failure
`db/repositories/search.py` records as a dashboard reading zero while a worker
still claims rows. `tests/integration/test_genre_backfill.py` is what proves the
fingerprint really does the work.

`commit` is injected because `services/` may depend only on `domain/` and
`ports/` ([ADR-0009](../../../docs/prd/decisions/0009-repositories-are-ports.md)),
and a session is neither.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from usher.domain.genres import canonicalise_genres
from usher.ports.repository import (
    TitleEmbeddingRepository,
    TitleGenres,
    TitleRepository,
)

__all__ = ["GenreNormalisationReport", "GenreNormalisationService"]


@dataclass(frozen=True, slots=True)
class GenreNormalisationReport:
    """What one sweep did, in numbers an operator can check against the next
    run.

    **Four counts and a cursor, no percentage**, for the reason
    `DerivationReport` gives: a ratio is `0/0` on the empty database PRD 08
    requires every command to work against.

    `rows_scanned == rows_rewritten + rows_unchanged` always, and that identity
    is worth stating because it is what makes a partial run legible — a sweep
    stopped by `--limit` reports a scan smaller than the catalog rather than a
    rewrite count that looks like completion.

    `embeddings_staled` is the *difference* in what the stale predicate claims,
    not a count of rewritten rows carrying a vector. The two disagree by
    exactly the rows that were already stale, which on a catalog whose embedded
    population is the enriched tier is most of what a genre sweep touches: on
    the live catalog 79,913 rows move and 304 embeddings go stale.

    `last_id` is the cursor to hand back as `--after`. `None` means the sweep
    read nothing at all.
    """

    rows_scanned: int = 0
    rows_rewritten: int = 0
    rows_unchanged: int = 0
    embeddings_staled: int = 0
    last_id: uuid.UUID | None = None


class GenreNormalisationService:
    def __init__(
        self,
        *,
        titles: TitleRepository,
        embeddings: TitleEmbeddingRepository,
        commit: Callable[[], Awaitable[None]],
        model_name: str,
    ) -> None:
        self._titles = titles
        self._embeddings = embeddings
        self._commit = commit
        self._model_name = model_name

    async def normalise(
        self,
        *,
        batch_size: int = 1000,
        limit: int = 0,
        after: uuid.UUID | None = None,
        write: bool = True,
    ) -> GenreNormalisationReport:
        """Sweep the catalog through `canonicalise_genres`, one batch at a
        time.

        **The cursor advances on the last id of the page, always** — never on
        "how many rows were still unnormalised afterwards". A loop that
        re-asked its own predicate would not terminate against a row the
        predicate cannot clear, and this repository has shipped exactly that
        non-convergence once, in the watch-history repair. It is also the
        mutation `usher index --backfill` records as *hanging* the suite rather
        than failing a case, which is why the sweep here is shaped identically.

        **Batched, and the batch is the transaction.** One `UPDATE` over 1.27M
        rows in a single transaction holds every row lock it takes until the
        end and loses the whole run to an interrupt; a page of `batch_size`
        commits on its own, so an interrupted sweep loses at most one batch and
        the catalog it leaves behind is a *prefix* that is normalised and a
        tail that is not. Neither half is wrong, because the vocabulary is the
        same one the readers already expand.

        `limit` bounds rows **scanned**, not rows written. Compared against
        writes it would never fire on a re-run — where the honest answer is
        zero writes — so the brake an operator reached for would silently sweep
        the whole catalog. `usher index --backfill`'s own second-run defect,
        avoided by having seen it.

        `write=False` is the bare `usher genres` form: it counts what it would
        rewrite and issues no `UPDATE` and no commit, which is the same bargain
        `usher index` and `usher derive` take so a report is safe to run on a
        production box.
        """
        report = GenreNormalisationReport(last_id=None)
        stale_before = await self._embeddings.count_stale(self._model_name)
        cursor = after
        while True:
            page = await self._titles.list_genres_page(limit=batch_size, after=cursor)
            if not page:
                break
            changed = [
                TitleGenres(id=row.id, genres=canonical)
                for row in page
                if (canonical := canonicalise_genres(row.genres)) != row.genres
            ]
            written = len(changed)
            if write and changed:
                # The repository's own `IS DISTINCT FROM` guard means this
                # number is what Postgres moved, not what the filter above
                # proposed. They agree today; the two are kept separate
                # because only one of them is a fact about the database.
                written = await self._titles.replace_genres(changed)
            if write:
                await self._commit()
            report = replace(
                report,
                rows_scanned=report.rows_scanned + len(page),
                rows_rewritten=report.rows_rewritten + written,
                rows_unchanged=report.rows_unchanged + len(page) - written,
                last_id=page[-1].id,
            )
            cursor = page[-1].id
            if limit and report.rows_scanned >= limit:
                break
        # After the sweep rather than per page: the predicate is a join over
        # `title_embeddings` with `md5` evaluated per row, and the number an
        # operator wants is what the whole run cost. Same placement, same
        # reason, as `usher index`'s post-sweep gauge refresh.
        stale_after = await self._embeddings.count_stale(self._model_name)
        return replace(report, embeddings_staled=stale_after - stale_before)
