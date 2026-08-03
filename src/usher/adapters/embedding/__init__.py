"""The `Embedder` implementations.

A capability-named directory with one implementation in it, which PRD 01
settles as correct: neither `fastembed` nor a future `litellm`-backed
embedder is a nameable single upstream the way `emby` or `tmdb` is, so the
directory is named for what it does. `adapters/postgres/` does not exist for
the same reason and must not be created.
"""
