# ADR-0022 — The embedder is optional, and its contract is measured rather than asserted

**Status:** Accepted. Implemented in M6 — settles a provisional marker in
[PRD 05](../05-search-and-similarity.md) and corrects two of its statements.
**Date:** 2026-08-02

## Context

Three things arrived at M6 unverified, and all three are load-bearing.

1. **[PRD 05](../05-search-and-similarity.md) and
   [01](../01-architecture.md) name `sentence-transformers`** as the
   embedding runtime. Nothing had ever installed it in this project.
2. **`usher.ports.embedding.Embedder` carried a 🔶** asking whether the port
   needs `embed_query`/`embed_documents` instead of one `embed`, because
   BGE-family models document a query-side instruction prefix. Its docstring
   meanwhile told callers they were "responsible for any query-side
   instruction prefix their chosen model needs".
3. **The port *asserted* that vectors are unit-normalised, and nothing had
   ever checked it.** PRD 05's "brute-force exact cosine is sub-millisecond"
   is only equal to a dot product if that holds.

All three were settled by measurement on 2026-08-02, and **all three
resolved against the previous wording.**

## Decision

### Part 1 — `fastembed`, and the embedder is **optional**

**`sentence-transformers` cannot be a dependency of this project.** 59
packages, **2.62 GiB downloaded, 4.8 GiB installed**, 104 s cold install —
against a current `usher` image of **332 MB**. It resolves
`torch==2.13.0+cu130`, and **~4.5 GiB of the 4.8 GiB is GPU runtime**
(`nvidia/` 2.7 G, `torch/` 1.1 G, `triton/` 689 M), pulled unconditionally
onto a host that may never have a GPU.

`fastembed` ships instead, and it is better on every axis measured: 28
packages, **167 MiB installed** (29× smaller), **1.2 s** cold install, **no
torch**, a 65 MB model artefact, **252.9 texts/s against 229.5** on identical
input (+10%), peak RSS 1,067 MiB against 1,381 MiB. The checkpoint is
unchanged (`BAAI/bge-small-en-v1.5`); only the runtime moved.

**And the dependency is behind an extra.** `uv sync --extra embedding`
installs it; `USHER_EMBEDDING_ENABLED` is **off by default**;
`worker.register(JobKind.INDEX, …)` is guarded on the embedder being present
exactly as `ENRICH` is guarded on `provider is not None`, so a worker never
claims work it cannot run. A deployment with no model still has full-text and
trigram — [PRD 05](../05-search-and-similarity.md)'s "catalog lookup" tier,
the one serving all 1,271,138 titles. That is a **narrowed** deployment, not
a broken one, which is [PRD 08](../08-operations.md)'s degradation rule.

### Part 2 — one `embed`, and the docstring's instruction is deleted

**Measured over 210 paired observations** (24 gold documents + 1,200
distractors per draw, 5 disjoint draws, 42 queries): the documented BGE query
prefix moves MRR by **−0.0028**, 95% CI `[−0.0259, +0.0203]` — an interval
that excludes any benefit larger than +0.02. Applying it to **both** sides is
significantly *harmful*: **−0.0663**, CI `[−0.1013, −0.0330]`.

**The experiment carries a power control, so this is a measured null and not
a blind one**: a deliberately wrong prefix moves MRR **−0.2497**, detected at
P(>0) = 0.000. An experiment that cannot detect a real effect reports the
same "no difference" as one that measured a genuine absence, and the two are
worth different amounts.

Corroborated twice at the library level: sentence-transformers 5.6.1's
`encode_query()`/`encode_document()` and fastembed 0.8.0's
`query_embed()`/`passage_embed()` are each **bit-identical to plain
`embed()`** for this checkpoint, which declares empty prompts and whose
`config_sentence_transformers.json` has no `prompts` key at all. Both
libraries already offer the asymmetric API; both make it a no-op here.

**So `Embedder` keeps one `embed` — and the docstring's previous clause is
deleted, because that clause is the actual hazard.** Telling callers they are
responsible for a query-side prefix invites a caller holding one `embed` and
two kinds of text to satisfy it the cheapest way available: a single
symmetric loop. That *is* the −0.0663 condition, and nothing in this codebase
could catch it — no error, no log line, 6.6 MRR points. It is replaced by the
measured fact and an instruction not to add one.

### Part 3 — normalisation is verified, and the port says which operator it is for

Norms are 1.0 to within **5.96e-08**, and `normalize_embeddings=False`
returns **bit-identical** vectors. **The flag cannot turn it off**, because
normalisation is baked into the *checkpoint* as a third module
(`Transformer → Pooling → Normalize`), not applied by the library. Control:
the same backbone with `2_Normalize` removed returns norms **8.99–9.46**.

Three consequences the port and the schema carry, none of which the old
docstring stated:

1. **It is a property of *this checkpoint*, not of embedders.** A model swap
   that drops the normalise module silently returns norm-9 vectors, and every
   dot-product score is then ~85× too large. `FastEmbedEmbedder` therefore
   **asserts the norm on its first batch** rather than trusting a model card;
   the tolerance (1e-4) sits comfortably above the measured 5.96e-08 and
   comfortably below 8.99, so it can neither be tripped by float noise nor
   passed by the failure it exists to catch.
2. **After the `halfvec` cast the vectors are no longer unit.** Norm drift
   goes from 1.19e-07 to **1.21e-04**, a 1000× change. Anything relying on
   "cosine == dot" must do so *before* the cast.
3. **The contract is only load-bearing under the inner-product operator.**
   Verified against real pgvector: `<=>` is normalisation-*invariant* — a
   vector of norm 5 in the same direction gives the identical cosine distance
   — while `<#>` is not. The shipped index is
   `hnsw (embedding halfvec_cosine_ops)` and the shipped query is `<=>`,
   which is what PRD 05 specifies, so **normalisation buys speed here, not
   correctness**. A docstring that does not say which operator reads as a
   correctness requirement the shipped index does not actually have.

## Consequences

**Gained.** 167 MiB against 4.8 GiB; no torch; 1.2 s cold install against
104 s; +10% throughput; lower peak RSS; and a container that stays in the
same order of magnitude as its current 332 MB. Semantic search is a
capability a household can decline without losing search.

✅ **The argument was reused in M8 and it held, which is worth recording here
because a precedent nobody cites is one nobody checked.**
[ADR-0027](0027-the-llm-client-is-one-http-call.md) declined `litellm` on this
ADR's shape — *"a runtime pulled unconditionally for a capability the
deployment may never use"* — and the measurement came out the same way: **+146
MB and 29 distributions against +0 and 0**, and among the 29 are
`huggingface-hub`, `hf-xet`, `filelock`, `fsspec`, `tokenizers` and `tiktoken`,
which is a model-download client and two tokenizer runtimes. That is
*literally* the middle group this ADR refused `sentence-transformers` for,
arriving through a different door for a feature that only needs a `POST`.
**Two milestones, two dependencies, the same test.** The one place M8's answer
differs is that it took **zero** rather than a smaller alternative: this ADR's
capability genuinely needed a runtime and M8's needed an HTTP call.

**Given up / Also — two supply-chain facts that belong in the open rather
than in a comment.**

1. **fastembed serves an optimised ONNX conversion from a *third-party*
   repository** (`qdrant/bge-small-en-v1.5-onnx-q`), not BAAI's own weights.
   BAAI's repo does ship its own `onnx/model.onnx` as the fallback path, so
   there is somewhere to go; the point is that the default artefact is not
   the model author's.
2. **The two runtimes are not interchangeable without a re-embed.** The
   ST↔fastembed vector difference is a max pairwise-similarity delta of
   **1.41e-03**, which is **6× larger than the halfvec quantisation error**
   (1.21e-04). **This is why `model_name` records the runtime as well as the
   checkpoint** — `fastembed:BAAI/bge-small-en-v1.5`. Swapping the
   implementation then invalidates every stored vector through
   [ADR-0020](0020-derived-state-carries-its-fingerprint.md)'s stale
   predicate automatically, with no migration to write.

**Also — offline behaviour is a trap, and it is configured rather than
assumed.** A warm cache is **not** sufficient. With the cache populated, no
network, and `HF_HUB_OFFLINE` unset, the load **fails** with
`RuntimeError: Cannot send a request, as the client has been closed` —
huggingface_hub 1.26.0 reuses a closed client on its retry path instead of
falling back to the cache, and the message names neither the network nor the
cache. Reproduced two independent ways. `HF_HUB_OFFLINE=1` is also the only
setting under which a genuine cache miss produces a comprehensible `OSError`.

`usher.composition` therefore sets it **before** the library is imported
(huggingface_hub reads it while constructing its client) via
`os.environ.setdefault`, driven by `USHER_EMBEDDING_OFFLINE`, which defaults
on. `setdefault`, so an operator warming the cache once — or a container that
set its own — wins. And **do not use `snapshot_download`**: it fetches
401 MB / 14 files, three redundant copies of the same weights (`.bin`,
`.safetensors`, `.onnx`), where the normal load path pulls ~129–134 MB / 12
blobs.

**Rejected: making the embedder mandatory and shipping the model in the
image.** It triples the image for a capability the "catalog lookup" tier does
not need, and it makes a first `docker compose up` depend on a 65 MB
third-party artefact download for a search box that would have worked without
it.

## Evidence

All measurements 2026-08-02 on this host (Python 3.13.14, `uv` 0.11.31,
Ryzen 7 5800X3D, RTX 4090); corpora synthetic.

**Agreement between the two runtimes**, which is what makes the substitution
a runtime change rather than a model change: over 205 documents, **minimum
cosine 0.99999619**, **top-1 identical 205/205**, top-10 mean overlap
1.0000.

**The throughput invariant, which is the number most easily misquoted:
throughput is linear in *tokens*, not texts.** CPU holds **~8,000–10,700
tokens/s** across the whole range — 412.7 texts/s at 19 tokens, 83.5 at 100,
18.7 at 516. A realistic `name + overview + genres + keywords` document is
**~100–130 tokens**, i.e. **~83 texts/s**. Best CPU batch size is **16** —
which is `USHER_EMBEDDING_BATCH_SIZE`'s default — with the curve flat from
16–64 and degrading at 128. Quoting "252.9 texts/s" as a planning rate is
quoting a 38-token benchmark at a 130-token workload.

The sizing that follows is what makes
[ADR-0020](0020-derived-state-carries-its-fingerprint.md)'s population choice
(boundary call 4) pay: the enriched tier at 2k–10k titles is **~25 seconds to
2 minutes**; all 1,271,138 titles would be **4–6 hours**.

**The port's contract, pinned rather than asserted.**
`tests/contract/embedder_contract.py` runs the same five cases against
`FakeEmbedder` and — marked, opt-in — the real model: unit norm, declared
dimension, one vector per input **in order**, determinism across calls, and
an empty batch that is an empty result and not a model call. The port had
none of these before M6, and `test_a_batch_returns_one_vector_per_input_in_order`
is the one that matters most: an implementation that dedupes or reorders
internally lands title *n*'s vector on title *m*, which is the most damaging
bug available in this milestone and is completely invisible to any per-vector
assertion.

## Uncertainty

**GPU throughput is not measured, deliberately.** The 4090 had **210 MiB free
of 24,564** — a live `vllm` container held 21,764 MiB — and creating a CUDA
context alone needs more than was free, so the probe declined to disturb a
running service. **No decision in this ADR rests on a GPU number**; the CPU
figures are what the backfill is sized against. Re-run when the GPU is free.

**Whether a different BGE-family checkpoint needs the prefix is unknown.**
This result is about *this* checkpoint, whose `config_sentence_transformers.json`
has no `prompts` key. A model that genuinely needs asymmetry arrives with a
new `model_name`, a new measurement, and a port change made on evidence —
which is cheaper than shipping an unused asymmetry today and having every
caller guess which side to use.

**Relevance is not measured at all.** Everything above is about vectors,
throughput and agreement; nothing in M6 measures whether semantic search
returns *better results*. `FakeEmbedder` is a hash, so no unit test can
distinguish a working semantic search from one returning noise, and any unit
test asserting semantic relevance against it is a defect in the test. The
only relevance evidence M6 has is
[ADR-0002](0002-postgres-first-search.md)'s gate on the *trigram* path, which
measures a different lane.
