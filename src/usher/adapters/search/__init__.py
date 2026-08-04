"""The `SearchIndex` and `SuggestIndex` implementations.

A capability-named directory, which PRD 01 settles as correct even with one
implementation in it: "postgres" is not a nameable upstream the way `emby`
or `tmdb` is, and `adapters/postgres/` -- a directory named for a *storage
engine* rather than for a capability -- does not exist and must not be
created. `adapters/embedding/` is the sibling that made the same call.
"""
