"""What every `Embedder` implementation owes the index path."""

import math

from usher.ports.embedding import Embedder

# 1e-6 admits both the real checkpoint and the fake with room to spare, and
# still fails a norm-9 vector -- the model-swap-that-dropped-`2_Normalize`
# failure -- by seven orders of magnitude.
_NORM_TOLERANCE = 1e-6

_TEXTS = (
    "The Quiet Vacuum",
    "Harbour Lights, a study in salt and sodium",
    "Vane",
)


class EmbedderContract:
    def model_calls(self, embedder: Embedder) -> int | None:
        """How many times the underlying model was invoked, or `None` if unknowable.

        Only the empty-batch case reads it, and only the half that needs it is
        conditional -- the "empty in, empty out" half runs everywhere.
        """
        return None

    async def test_every_vector_is_unit_normalised(self, embedder: Embedder) -> None:
        """Rules out an embedder whose vectors are not unit-normalised.

        Brute-force exact cosine equals a dot product only when this holds, and the
        failure is silent: a checkpoint missing its `2_Normalize` module returns
        norms near 9, which makes every score ~85x too large and every ranking
        plausible and wrong.
        """
        vectors = await embedder.embed(list(_TEXTS))
        for text, vector in zip(_TEXTS, vectors, strict=True):
            norm = math.sqrt(sum(component * component for component in vector))
            assert abs(norm - 1.0) <= _NORM_TOLERANCE, f"{text!r} embedded to norm {norm}"

    async def test_dimension_matches_the_declared_dimension(self, embedder: Embedder) -> None:
        """Rules out a declared dimension that has drifted from the real vector width.

        A model swap that silently changes width writes vectors the `halfvec(384)`
        column rejects -- or worse, accepts, because only the declared side moved.
        """
        assert embedder.dimension > 0
        vectors = await embedder.embed(list(_TEXTS))
        assert [len(vector) for vector in vectors] == [embedder.dimension] * len(_TEXTS)

    async def test_a_batch_returns_one_vector_per_input_in_order(self, embedder: Embedder) -> None:
        """Rules out an implementation that deduplicates or reorders a batch internally.

        Landing title *n*'s vector on title *m* is invisible to any per-vector
        assertion: each is a valid unit vector of the right width, and the
        similarity graph is quietly wired to the wrong titles. The duplicate at
        positions 0 and 2 is what a deduplicating implementation collapses, and the
        single-text calls pin "in order" to the order a caller gets one at a time.
        """
        batch = await embedder.embed(["The Quiet Vacuum", "Vane", "The Quiet Vacuum"])
        assert len(batch) == 3
        assert batch[0] == batch[2]
        assert batch[0] != batch[1]
        alone = [(await embedder.embed([text]))[0] for text in ("The Quiet Vacuum", "Vane")]
        assert batch[0] == alone[0]
        assert batch[1] == alone[1]

    async def test_the_same_text_embeds_identically_twice(self, embedder: Embedder) -> None:
        """Rules out a non-deterministic embedder, which no backfill could ever drain.

        `source_fingerprint` would be useless: the predicate re-claims the row, the
        `usher.search.embeddings.stale` gauge never reaches zero, and the queue
        churns forever on work that cannot succeed. Exact equality, not `approx` --
        the fingerprint scheme rests on reproducibility, so an implementation that
        cannot offer it has to fail rather than have a tolerance written around it.
        """
        first = await embedder.embed(["The Quiet Vacuum"])
        second = await embedder.embed(["The Quiet Vacuum"])
        assert first == second

    async def test_an_empty_batch_is_an_empty_result_and_not_a_call(
        self, embedder: Embedder
    ) -> None:
        """Rules out an implementation that round-trips a model for zero inputs.

        On a GPU-resident model that is the difference between a no-op and a stall,
        and a backfill draining the tail of a predicate hits it on every last pass.
        """
        before = self.model_calls(embedder)
        assert await embedder.embed([]) == []
        after = self.model_calls(embedder)
        if before is not None and after is not None:
            assert after == before, "an empty batch reached the model"
