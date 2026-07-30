# ADR-0013 — The source contract suite drives a harness, not a cassette

**Status:** Accepted

## Context

PRD 08 calls the adapter contract suite "the load-bearing one": it is what
makes "Emby is replaceable" a testable claim rather than a slogan. The
question is what the suite talks to. Four options were live:

1. **Recorded HTTP cassettes** (VCR-style) replayed per adapter.
2. **A real Emby in a container**, driven for real.
3. **A hand-written suite per adapter**, sharing nothing.
4. **A harness ABC** the suite arranges state through, implemented once per
   adapter.

## Decision

Option 4. `tests/contract/source_adapter_contract.py` speaks only
`usher.ports.source`'s own DTOs and arranges every precondition through
`tests/contract/source_harness.py`'s `SourceHarness`. Each adapter supplies
a harness that translates those DTOs into its own upstream's shape.

## Consequences

**Gained:** a Jellyfin or Plex adapter passes the *same file*, unmodified.
The suite cannot accidentally encode Emby's field names, because it has no
way to mention them. Two implementations run it in M3 — a pure in-memory
adapter and the real Emby one — and the pair is what makes the claim
checkable: if only the Emby run existed, "source-agnostic" would be
untested.

**Accepted cost:** every adapter writes a harness, which is real work
(`FakeEmbyServer` is the largest single file in M3's test tree). Some of a
harness's own behaviour is untested — nothing verifies that `EmbyHarness`
renders a `SourceItem` into JSON the *real* Emby would also produce.

**Mitigation for that cost:** the fixtures the fake server renders are
shape-recorded from a real response, and a separate test parses each
committed fixture through the adapter's own mapper with no fake server
involved. A wrong *shape* therefore fails a test that does not depend on
the harness at all. A wrong *endpoint path* is the residual gap, and only a
live run closes it — which is why M3's definition of done requires one.

## Why not the others

**Cassettes** pin one server's bytes. The suite would become "Emby's
recorded responses replayed", and a second adapter could not run it at all
without recording its own — at which point the two suites are the
hand-written duplicates option 3 already loses on, with extra machinery.

**A real containerised Emby** is not redistributable, needs a licence key
for some features, and would make the *load-bearing* suite Docker-gated —
`tests/unit` exists precisely so the fast lane needs nothing. It is
valuable as a manual verification step, and M3's definition of done keeps
it as exactly that.

**A suite per adapter** is what `TitleRepositoryContract`'s own docstring
already argues against: "two hand-maintained copies of these assertions
would drift the moment someone updated one and not the other."
