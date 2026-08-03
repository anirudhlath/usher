"""PRD 03 stage 4, application side: what gets embedded, and its fingerprint.

`services/` may import only `domain/` and `ports/`
([ADR-0009](../../../docs/prd/decisions/0009-repositories-are-ports.md)), which
is exactly right here: composing a document out of a `Title` is a decision about
*meaning* and must not be able to reach a `tsvector`, a `halfvec`, or a model.

**The fingerprint is over the assembled string and nothing else.** It is what
turns "is this vector stale?" into one SQL predicate, and that predicate has
three consumers -- the backfill's cursor, the `usher.search.embeddings.stale`
gauge, and the test that proves the enqueue-on-enrichment path closes. Hash
anything but the exact bytes handed to `Embedder.embed` and all three go on
answering, wrongly.

**This assembly is a second implementation of
`usher.db.repositories.search._FINGERPRINT_SQL`, and it is permitted only
because a test pins the two together.** The predicate cannot call this function
-- the assembly is per-title, so it cannot be a bound parameter, and `db/` may
not import `services/` anyway -- so the fingerprint is spelled once in Python
and once in SQL. `tests/integration/test_search_repository.py` runs both over
the same seeded rows and compares. **Three shapes of the obvious Python
composer are unreproducible in SQL and all three are refused here**, each with
its own case in `tests/unit/test_services_search_document.py`:

1. *Appending a section only when the field is populated.* `_FINGERPRINT_SQL`
   is `coalesce(..., '')` on every nullable column with no conditionals, so it
   emits six segments for every title. The assembly below is positional for
   that reason: a missing overview is an empty line, never an absent one.
2. *Joining array elements on `", "`.* The predicate uses `usher_array_text`,
   which is `array_to_string($1, ' ')` -- the same `IMMUTABLE` wrapper the
   generated column uses, so this schema has one definition of "an array as
   text" rather than two.
3. *Including `year`.* It is genuinely useful text and the predicate has no
   `year` column, so it is left out. Adding it means adding it to both sides in
   one commit; adding it to one is failure mode (a) above.

**Changing the assembly invalidates every stored vector, on purpose.** Add a
field, change a separator, reorder a section -- every fingerprint moves, every
row matches the stale predicate, and the backfill re-embeds the enriched tier in
the 25 s to 2 min it costs (~8,000-10,700 tokens/s on CPU, ~100-130 tokens a
document). That is the scheme working, not a migration to write. The same
mechanism covers a model swap, which is why `model_name` records the runtime as
well as the checkpoint (`fastembed:BAAI/bge-small-en-v1.5`): the measured
ST-vs-fastembed difference is 6x the halfvec quantisation error, so the two are
not interchangeable without a re-embed.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from usher.domain.title import Title

# The two separators, named because they are load-bearing rather than
# cosmetic. `_SECTION` is `_FINGERPRINT_SQL`'s `CHR(10)` and `_ITEM` is
# `usher_array_text`'s `array_to_string($1, ' ')`. A change to either is a
# change to every fingerprint in the catalog.
_SECTION = "\n"
_ITEM = " "


@dataclass(frozen=True, slots=True)
class EmbeddingDocument:
    """One title as an embedder sees it, plus the hash of exactly that.

    **Deliberately not `ports.search.SearchDocument`**, which is a retrieval
    document with weight classes aimed at `index_many`. Sharing a type would
    invite the fingerprint being computed over the weighted form, which is
    the one way to get this wrong that nothing downstream can detect.

    `is_degenerate` is a flag on a fully-formed document, **never an
    absence**. A refused title still gets a `title_embeddings` row carrying
    this `fingerprint` and a `NULL` embedding, so it stops matching the stale
    predicate and starts matching the `embedding IS NULL` one a diagnostic
    counts. Returning `None` here would leave the caller nothing to write and
    the title re-claimed by every backfill pass forever -- the failure this
    repository has already shipped once, one lane over, when the
    watch-history repair carried the walk's instant and was refused by the
    very row it existed to repair.
    """

    text: str
    fingerprint: str
    is_degenerate: bool


def compose_document(title: Title, *, credits: Sequence[str] = ()) -> EmbeddingDocument:
    """The text this title embeds as, and the `md5` of that text.

    Pure: same `Title` in, same bytes out, in any process. Determinism is not
    a nicety -- a non-deterministic assembly makes `source_fingerprint`
    meaningless and the backfill non-terminating. Everything below iterates a
    tuple in the order a provider supplied it; nothing iterates a `set`.

    **The six segments, their order and their separators are
    `_FINGERPRINT_SQL`'s**, not a choice made here, for the reason the module
    docstring gives. Read that before editing this function.

    `credits` is always `()` in M6 and is a parameter anyway (boundary call
    2): no `Person`/`Credit`/`Collection`/`Image` table, model or port exists
    in `src/` -- `ports/metadata.py` defers all four by name -- and the only
    place credits physically exist is `raw_payloads.payload`, so assembling
    them here would put a *provider's* JSON shape in `services/`.

    **A non-empty `credits` breaks the agreement with `_FINGERPRINT_SQL`**,
    which has no credits column, and the failure is the silent one: every
    title with a credit matches the stale predicate forever. M7 moves both
    sides in one commit or neither. Pinned by
    `test_credits_are_accepted_and_are_empty_in_m6`, whose second assertion
    exists to make that visible rather than to check a string.
    """
    text = _SECTION.join(
        (
            title.name,
            title.original_name or "",
            title.overview or "",
            title.tagline or "",
            _ITEM.join(title.genres),
            _ITEM.join(title.keywords),
        )
    )
    if credits:
        text = text + _SECTION + _ITEM.join(credits)
    return EmbeddingDocument(
        text=text,
        # `usedforsecurity=False` is required, not decorative: ruff's `S`
        # rules flag `hashlib.md5` as S324, and the flag is the honest
        # statement -- this is a content hash for change detection and nothing
        # about it is a security boundary. `md5` because the predicate spells
        # `md5(...)` in SQL, the column is sized for 32 hex characters, and a
        # collision costs one un-refreshed vector.
        fingerprint=hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest(),
        # `not text.strip()`, and nothing more elaborate. Every whitespace-only
        # input embeds to the identical vector (cos = 1.0000 exactly).
        # Measured just as directly: a name-only skeleton is fine -- 0.5867
        # pairwise, 0.7638 self-retrieval against a 0.4751 cross-title mean.
        # The rule is about *empty*, not *thin*; a minimum word count would
        # exclude the majority tier from semantic results while every gauge
        # read zero. `strip()` and not `== ""`: the positional assembly means
        # an empty title is five newlines rather than the empty string.
        is_degenerate=not text.strip(),
    )


__all__ = ["EmbeddingDocument", "compose_document"]
