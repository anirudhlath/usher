# ADR-0047 — The first release is `v0.1.0`, not `v1.0.0`

**Status:** Accepted (2026-09-07, M10's R3)

## Context

[PRD 09](../09-roadmap.md) heads the M1–M10 table **"v1 — the abstraction works
end to end"**, and gives the success condition as *"a client can be built
against Usher that fully replaces direct Emby access"* — which M9 met. A
reasonable reader gets from there to `1.0.0` in one step.

**The step is wrong, and the reason is that the two "v1"s are different
things.** "v1" in that heading names a **scope milestone**. "1.0.0" in semver
names a **compatibility promise**: that the public surface is stable and that
breaking it costs a major version. The first is true. The second is not, and
this project's recurring failure is exactly this shape — a bounded claim
restated as an absolute one hop up the document chain
(`.claude/rules/milestone-boundary-calls.md`).

## Decision

**The first tagged release is `v0.1.0`.** `0.x` says "the wire contract may
still move" in three characters, and `1.0.0` remains available the day it is
meant.

## Consequences

- A client author reads `0.x` and knows to pin. That is the whole point.
- `[project].version` stays on `0.` until someone overturns this record.
  `tests/unit/test_release_metadata.py::test_the_declared_version_is_pre_one_point_zero`
  fails naming this file, so the bump is a decision rather than an edit.
- `README`'s Status line reads **Beta** rather than "Pre-release": what is
  shipped is more than a preview and less than a promise.
- Nothing here defers a `1.0.0`. It is not scheduled, because the three
  conditions below are not scheduled.

## Evidence

Re-measured 2026-09-07 at `2f9dd63`, not inherited from the plan.

**There is no authentication anywhere.**
`grep -rn "fastapi.security\|HTTPBearer\|APIKeyHeader\|OAuth2\|HTTPBasic" src/ tests/`
returns **exactly one** hit, and it is prose in a docstring
(`adapters/emby/session.py:15`, *"Emby has no OAuth2"*). `get_default_user_id`
returns the singleton default user and there is no `current_user` symbol in the
tree. PRD 09 lists authentication as a **post-v1 candidate**, not as a deferral
with a date — so the surface a client would build against is one an auth model
must later change.

**There is one source adapter.** `usher/adapters/` holds `emby` plus the
generic `bulk`, `embedding`, `images`, `llm`, `search` and `tmdb` — no second
media server. PRD 09 names Jellyfin and Plex as *"the genuine test of the
abstraction"*, and that test has not been run. A port whose only implementation
is its first one is a port whose shape is unmeasured.

**Six open feature issues each move a wire contract** — #11 (the IMDb credits
parsers and their writer), #12 (a `(title_id, source)` index awaiting its
reader), #14 (a covering index for `GET /admin/unmatched`), #15 (query
expansion re-measured), #18 (`POST /admin/sources` accepting more than a
username and password), #21 (RRF's absent-lane `COALESCE` costing an exact-name
match). 44 issues are open in total.

⚠️ **The plan's own evidence for this record was stale in one place and it is
corrected here rather than repeated: it named *seven* such issues, including
#16, which is CLOSED.** The count is six. It does not change the decision, and
recording it is the point — a stale "verified" fact is worse than none.
