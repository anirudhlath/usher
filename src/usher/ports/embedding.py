"""Port for computing text embeddings."""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class Embedder(ABC):
    """Turns text into vectors. Implementations are expected to batch.

    **Settled in M6, by measurement, and both halves resolved against this
    docstring's previous wording.** Evidence in ADR-0022.

    **There is no query/document asymmetry, and no caller should invent
    one.** This checkpoint requires no instruction prefix. Measured over 210
    paired observations (24 gold documents plus 1,200 distractors per draw,
    5 disjoint draws, 42 queries): the documented BGE query prefix moves MRR
    by **-0.0028**, 95% CI `[-0.0259, +0.0203]` -- an interval that excludes
    any benefit larger than +0.02. Applying it to **both** sides is
    significantly harmful: **-0.0663**, CI `[-0.1013, -0.0330]`. The
    experiment carries a power control, so this is a measured null and not a
    blind one: a deliberately wrong prefix moves MRR **-0.2497**, detected
    at P(>0) = 0.000.

    This clause used to say that **callers were responsible** for any
    query-side instruction prefix their chosen model needs -- reported in
    the past tense on purpose, because
    `tests/unit/test_ports_embedding.py` guards the present-tense
    instruction as a literal substring and a verbatim quotation of it here
    would be indistinguishable from its return. That was the hazard, not
    the guidance: a caller with one `embed` and two kinds of text obeys it
    most cheaply with a single symmetric loop, which *is* the -0.0663
    condition, and nothing in this codebase could detect it -- no error, no
    log line, just 6.6 MRR points. Corroborated twice at the library level:
    sentence-transformers 5.6.1's `encode_query()`/`encode_document()` and
    fastembed 0.8.0's `query_embed()`/`passage_embed()` are each
    **bit-identical to plain `embed()`** for this checkpoint, which declares
    empty prompts and whose `config_sentence_transformers.json` has no
    `prompts` key at all. Both libraries already offer the asymmetric API;
    both make it a no-op here. A future model that genuinely needs asymmetry
    arrives with a new `model_name`, a new measurement, and a port change
    made on evidence -- which is cheaper than shipping an unused asymmetry
    today and having every caller guess which side to use.

    **Vectors are L2-normalised, verified, and the mechanism matters.**
    Norms are 1.0 to within **5.96e-08**, and `normalize_embeddings=False`
    returns **bit-identical** vectors -- the flag cannot turn it off,
    because normalisation is baked into the *checkpoint* as a third module
    (`Transformer -> Pooling -> Normalize`) rather than applied by the
    library. The control confirms it: the same backbone with `2_Normalize`
    removed returns norms **8.99-9.46**. Three consequences:

    1. **It is a property of this checkpoint, not of embedders.** A model
       swap that drops the normalise module silently returns norm-9 vectors
       and every dot-product score is then ~85x too large -- a
       plausible-looking ranking that is wrong everywhere. An implementation
       therefore **asserts the norm on its first batch** rather than
       trusting a model card.
    2. **It stops holding after the `halfvec` cast.** Norm drift goes from
       1.19e-07 to **1.21e-04**, a 1000x change. Anything relying on
       "cosine == dot" must do so *before* the cast.
    3. **It is load-bearing only under the inner-product operator.**
       Verified against real pgvector: `<=>` is normalisation-*invariant*
       (a vector of norm 5 in the same direction gives the identical cosine
       distance), `<#>` is not. With `halfvec_cosine_ops`/`<=>` -- what PRD
       05 specifies -- normalisation buys **speed, not correctness**. Stated
       here because a contract that omits the operator reads as a
       correctness requirement the shipped index does not have, and a
       requirement that makes no observable difference is one somebody
       eventually deletes along with the `<#>` case it was really for.
    """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Stored alongside vectors, so a model change is one SQL predicate.

        **Records the runtime *and* the checkpoint**, e.g.
        `fastembed:BAAI/bge-small-en-v1.5`, never the checkpoint alone. The
        same checkpoint served by sentence-transformers and by fastembed
        produces vectors whose max pairwise-similarity delta is **1.41e-03**
        -- **6x the halfvec quantisation error** (1.21e-04) -- so the two are
        not interchangeable without a re-embed.

        Spelling the runtime into this string is what makes swapping it
        invalidate every stored vector through
        `e.model_name IS DISTINCT FROM :model_name`: the backfill re-claims
        them, the stale gauge climbs and then drains, and nobody has to
        remember to write a migration. The fingerprint scheme doing its job
        instead of a human doing it.

        **And that promise is scoped to a swap at one width, which nothing
        said until `m09e`.** `EMBEDDING_DIMENSIONS` is a `halfvec` typmod and
        a typmod is DDL, so a swap that changes the width needs a migration
        after all -- `m09e` is the first one, 384 -> 1024 for `BAAI/bge-m3`.
        Read this paragraph as narrowing the one above rather than
        contradicting it: within a width the mechanism is exactly as described
        and has been exercised; across widths there was never a claim, only
        the absence of a counterexample.

        **One thing the fingerprint still does not reach, recorded because
        `m09e` had to work around it:** `title_neighbors` rows are derived
        from these vectors and their `blend_fingerprint` hashes the blend's
        constants only, so a model swap leaves every neighbour row reading as
        current. See that revision's docstring.
        """

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector width, must match the database column (`halfvec(1024)`).

        **Report the model's own width, never this schema's.** The temptation
        is to return `EMBEDDING_DIMENSIONS`, which makes every implementation
        agree with the column by construction -- and turns
        `composition.embedder`'s startup comparison into `x == x`. The whole
        value of this property is that it can disagree.
        """

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, returning one vector per input **in order**.

        Order is the contract, not a convenience: an implementation that
        deduplicates or reorders internally lands title *n*'s vector on
        title *m*, which is the most damaging bug available in this
        milestone and is completely invisible to any per-vector assertion.

        An empty batch is an empty result and **not a call** -- on a
        GPU-resident model that is the difference between a no-op and a
        stall.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources (e.g. a GPU-resident model)."""
