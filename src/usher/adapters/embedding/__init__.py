"""The `Embedder` implementations.

A capability-named directory, which PRD 01 settles as correct: neither
`fastembed` nor an OpenAI-compatible endpoint is a nameable single
upstream the way `emby` or `tmdb` is, so the directory is named for what
it does.

**Two implementations since `m09e`, and the naming call is what made that
cheap.** `fastembed.py` loads a model in-process; `openai_compat.py` calls
`POST /v1/embeddings` on whatever serves one -- this deployment's is a
second local vLLM. The directory needed no rename, because it was never
named after the thing that turned out to be replaceable. The runtime
prefix in `Settings.embedding_model` picks between them and
`composition._load_embedder` is the only place that reads it.

`adapters/postgres/` does not exist for the same reason and must not be
created.
"""
