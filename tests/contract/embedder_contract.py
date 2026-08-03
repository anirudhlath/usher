"""What every `Embedder` implementation owes the index path.

Runs against `FakeEmbedder` (no model, no network) and -- marked, opt-in --
against the real `FastEmbedEmbedder`, which downloads 129-134 MB and is
therefore not part of any default selection.

**Nothing here asserts relevance**, and that is deliberate rather than an
omission: `FakeEmbedder` is a hash, so a relevance assertion against it
would pass for a reason unrelated to the code under test, which is the
vacuous-pass failure this repository has shipped once already. These five
cases are the plumbing: order, width, normalisation, determinism, and not
calling a model for nothing.

Subclass and provide an `embedder` fixture.
"""

import math

from usher.ports.embedding import Embedder

# The real checkpoint's measured norm error is 5.96e-08 and the fake's is
# 1.11e-16, so 1e-6 admits both with room and still fails a norm-9 vector --
# the model-swap-that-dropped-`2_Normalize` failure -- by seven orders of
# magnitude. A tolerance chosen to pass is not a tolerance.
_NORM_TOLERANCE = 1e-6

_TEXTS = (
    "The Quiet Vacuum",
    "Harbour Lights, a study in salt and sodium",
    "Vane",
)


class EmbedderContract:
    def model_calls(self, embedder: Embedder) -> int | None:
        """How many times the underlying model was invoked, or `None` if
        this implementation cannot see inside itself.

        Only the empty-batch case reads it, and only the half that needs it
        is conditional -- the "empty in, empty out" half runs everywhere.
        """
        return None

    async def test_every_vector_is_unit_normalised(self, embedder: Embedder) -> None:
        """**The port's own stated contract, which nothing had ever
        checked.** PRD 05's "brute-force exact cosine" equals a dot product
        only when this holds, and the failure is silent: a checkpoint whose
        `2_Normalize` module is missing returns norms 8.99-9.46 (measured),
        which makes every dot-product score ~85x too large and every ranking
        plausible and wrong.

        This is why the shipped implementation asserts the norm on its first
        batch rather than trusting a model card.
        """
        vectors = await embedder.embed(list(_TEXTS))
        for text, vector in zip(_TEXTS, vectors, strict=True):
            norm = math.sqrt(sum(component * component for component in vector))
            assert abs(norm - 1.0) <= _NORM_TOLERANCE, f"{text!r} embedded to norm {norm}"

    async def test_dimension_matches_the_declared_dimension(self, embedder: Embedder) -> None:
        """A model swap that silently changes width and writes vectors a
        `halfvec(384)` column rejects -- or worse, accepts, because the
        declared dimension and the real one drifted in the same commit and
        only one of them is what the migration created."""
        assert embedder.dimension > 0
        vectors = await embedder.embed(list(_TEXTS))
        assert [len(vector) for vector in vectors] == [embedder.dimension] * len(_TEXTS)

    async def test_a_batch_returns_one_vector_per_input_in_order(self, embedder: Embedder) -> None:
        """**The most damaging possible bug in this milestone, and it is
        completely invisible to any per-vector assertion.** An implementation
        that deduplicates or reorders internally lands title *n*'s vector on
        title *m*: every vector is a valid unit vector of the right width,
        every norm assertion passes, and the catalog's similarity graph is
        quietly wired to the wrong titles.

        The batch carries a **duplicate at positions 0 and 2**, which is what
        a deduplicating implementation collapses -- returning two vectors for
        three inputs, or three in the wrong slots. Cross-checked against
        single-text calls so "in order" means the same order a caller would
        get one at a time, not merely a self-consistent one.
        """
        batch = await embedder.embed(["The Quiet Vacuum", "Vane", "The Quiet Vacuum"])
        assert len(batch) == 3
        assert batch[0] == batch[2]
        assert batch[0] != batch[1]
        alone = [(await embedder.embed([text]))[0] for text in ("The Quiet Vacuum", "Vane")]
        assert batch[0] == alone[0]
        assert batch[1] == alone[1]

    async def test_the_same_text_embeds_identically_twice(self, embedder: Embedder) -> None:
        """Non-determinism that would make `source_fingerprint` useless and
        the backfill never drain: the predicate re-claims the row, the
        `usher.search.embeddings.stale` gauge never reaches zero, and the
        queue churns forever on work that cannot succeed. This project has
        shipped exactly that bug once, in the watch-history repair.

        Exact equality, not `approx`. A real implementation whose kernels
        are non-deterministic *fails this*, and that is the intended
        outcome -- the fingerprint scheme rests on reproducibility, so an
        implementation that cannot offer it has to say so rather than have a
        tolerance written around it.
        """
        first = await embedder.embed(["The Quiet Vacuum"])
        second = await embedder.embed(["The Quiet Vacuum"])
        assert first == second

    async def test_an_empty_batch_is_an_empty_result_and_not_a_call(
        self, embedder: Embedder
    ) -> None:
        """An implementation that round-trips a model for zero inputs, which
        on a GPU-resident model is the difference between a no-op and a
        stall -- and which a backfill draining the tail of a predicate hits
        on its last pass, every pass, forever."""
        before = self.model_calls(embedder)
        assert await embedder.embed([]) == []
        after = self.model_calls(embedder)
        if before is not None and after is not None:
            assert after == before, "an empty batch reached the model"
