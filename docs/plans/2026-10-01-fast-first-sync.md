# Fast, Resumable First Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A first sync of a million-item Emby library is reachable within two minutes, complete within about an hour, and resumes where it stopped.

**Architecture:** Phase 1 (PR 1) tunes the adapter's one offset walk — 1,000-item pages, the count asked for once, pages that overlap and are deduplicated, one page of read-ahead, a 120 s listing read budget — and turns the watch lane's first walk into two filtered listings (`IsPlayed`, then `IsResumable`). Phase 2 (PR 2) splits a whole-library walk into a persisted plan of units (a seed, per-library titles, per-library episode chunks) that several walkers fetch concurrently while one writer commits, with a stage barrier, an `after_seed` hook that runs the watch lane early, a heartbeat, resume in place, and backoff.

**Tech Stack:** Python 3.13, asyncio, httpx (`MockTransport` fakes), SQLAlchemy 2 async + asyncpg, Alembic, pydantic domain models, OpenTelemetry metrics, pytest (+ testcontainers `pgvector/pgvector:pg17`), the React console's settings catalogue (`web/`).

**Spec:** `docs/specs/2026-10-01-fast-first-sync-design.md`. Read it beside this plan; this plan argues from it and departs from its letter in eighteen places, each called out where it happens. Six are in Phase 1: a short page judged against the longest page served in Task 2; the overlap clamp, the drained-tail rule and a shift judged against the whole previous page in Task 3; a listing read that keeps a client budget longer than 120 s in Task 5; and an already-`FAILED` first walk keeping its own error in Task 7. Twelve are in Phase 2, listed at its head.

## Global Constraints

- `USHER_SOURCE_PAGE_SIZE` defaults to **1,000**, up from 200; the existing cap of 1,000 stays.
- `USHER_SOURCE_REQUESTS_PER_SECOND` keeps its **0.4** default. Nothing in this plan moves it.
- `PAGE_OVERLAP = 50`. A listing page's read timeout is **120 s**, or the client's when that is longer (Task 5's departure); every other request keeps the adapter's `USHER_SOURCE_TIMEOUT_SECONDS` (30 s).
- A superseded first watch walk's `error` is exactly `superseded: a first watch walk restarts`.
- Phase 2 settings: `USHER_SYNC_WALKERS` default **4** (1 restores one request at a time); `USHER_SYNC_UNIT_MAX_ITEMS` default **100,000**.
- Phase 2 timings: a run whose `heartbeat_at` is older than **10 minutes** is stale; the writer heartbeats on every commit and at least **once a minute** while it waits on the queue.
- Phase 2 backoff: a retryable listing failure drops the listing limit to **1**; each run of **10** successful pages raises it by 1, back up to the walker count.
- Phase 2's queue is `asyncio.Queue(maxsize=walkers)`. The spec's `(unit_key, items, cursor)` entries are `_Fetched(unit_key, page)`, where the page carries the items and where the unit resumes (departure 1).
- Unit keys are opaque, adapter-owned strings, persisted like `MediaItem.external_id`, and never in an API response.
- The Phase 2 migration (`m10g`) is purely additive: table `sync_run_units` and column `sync_runs.heartbeat_at`.
- Live runs are read-only against the source and write no credential, token, user id or host into the repository. Anything beyond the shipped CLI is a throwaway script **outside the repository**. Results go on the PR as raw output, not summaries.
- Never deploy Phase 2 while an old-image full walk is running: `docker ps --filter name=usher-prod-sync` is the check.
- Merging to `main` deploys to production. Nothing in this plan merges without the owner's go-ahead.

## Review Focus

The five inputs most likely to hurt a person running this, none of which the spec's own test list exercises. Each has its test in the owning task.

1. **Items deleted from the source mid-walk shrink the library below the first page's total.** The walk ends a page or two later instead of paging out `MAX_PAGES` (~7 h of requests) and failing. Task 3: `test_a_walk_that_deletions_left_short_of_its_total_ends_on_its_tail`.
2. **A server that caps `Limit` at or below `PAGE_OVERLAP`.** The walk still advances and reads everything, rather than re-reading one page until `MAX_PAGES`. Task 3: `test_a_limit_capped_below_the_overlap_still_advances`.
3. **A server that ignores `Filters`.** The first watch walk still yields only played or in-progress states, each once. When most of the played listing reads unwatched, as most of a library is, it takes the server to ignore the filter: it walks the library once rather than twice and says so in a WARNING. A played listing whose unwatched entries only tie or trail its watched ones is not taken for one. Task 6: `test_a_server_that_ignores_the_filter_still_yields_only_watched_states_once`, `test_a_played_listing_is_judged_unfiltered_only_when_mostly_unwatched` and `test_a_series_reading_unwatched_in_the_played_listing_keeps_the_in_progress_one`.
4. **The consumer stops after a read-ahead has already failed.** Stopping raises nothing, and asyncio never reports the failure as "never retrieved". Task 4: `test_a_read_ahead_that_failed_is_retrieved_when_the_walk_stops`.
5. **The consumer is cancelled while a read-ahead is in flight.** The cancellation propagates out of the walk, and the request is cancelled with it. Task 4: `test_cancelling_the_consumer_cancels_the_walk_rather_than_being_swallowed`.

Phase 2 has five more of its own, listed at the head of Phase 2.

## How to work this plan

- Worktree: `~/code/.worktrees/usher/fast-first-sync`, branch `feat/fast-first-sync` (Phase 1). Phase 2 gets its own branch from `main` after PR 1 merges (Task 10).
- `uv sync --extra eval` once per worktree, or five test modules abort at collection.
- During a task, run only the named test files: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly <files>` (never add `-q`; `addopts` already has it). The full gate runs once per phase (Tasks 8 and 20):
  `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly`.
- A commit touching `web/` also needs, from `web/`: `npm run verify`, `npm run e2e`, `npm run e2e:visual`.
- **Every new guard is planted and seen to fail on its own line.** The cycle (from `.claude/rules/testing-discipline.md`): `cp <file> /var/tmp/plant.bak`; apply the plant with Edit; `grep -n` that it landed; run the case and `grep -E '^E .*<message>'`; `cp /var/tmp/plant.bak <file>`; `diff /var/tmp/plant.bak <file>` to read the restore back. Never undo with `git checkout`, `restore`, `stash` or `reset`.
- Python prose caps (`.claude/hooks/guard_prose.py`): module docstring ≤ 3 lines, function docstring ≤ 20 lines, comment block ≤ 5 lines, and no dates, commit shas or the word "measured" in Python prose.
- The PRD and `CHANGELOG.md` change in the same commit as the behaviour they describe.
- Commit subjects follow `git log`: a lowercase area and a sentence (`emby: …`, `watch: …`, `sync: …`). No trailers, no model names.
- IMDb ids written into tests sit in the `tt99` band.

## File map

**Phase 1**

| File | Change |
|---|---|
| `src/usher/adapters/emby/paging.py` | **new** — `OffsetWindow` and `Page`: where each page starts, what in it is new, when the walk ends |
| `src/usher/adapters/emby/adapter.py` | `_walk` rebuilt on `OffsetWindow` with read-ahead; `_listing_query`; the filtered first watch walk; `LISTING_READ_SECONDS`; page-size default |
| `src/usher/adapters/emby/session.py` | `read_timeout=` through `json_body`/`ok`/`request`/`_send` |
| `src/usher/ports/source.py` | `watch_state`'s docstring states the first-walk contract |
| `src/usher/services/watch_sync.py` | an unfinished cursorless run is superseded, not resumed |
| `src/usher/config.py`, `.env.example`, `web/src/features/operator/Config.settings.ts` | page size 1,000; the timeout's description |
| `tests/fakes/emby_server.py` | uncounted listings report 0; `max_limit`; `Filters=IsPlayed`/`IsResumable` |
| `tests/fakes/source_adapter.py` | a first walk yields non-default states only |
| `tests/fixtures/emby/README.md` | the listing facts the fake now models |
| `tests/contract/source_adapter_contract.py` | the first-walk case; the zero-state case becomes a delta case |
| `tests/unit/test_adapters_emby_paging.py` | **new** |
| `tests/unit/test_adapters_emby_adapter.py`, `tests/unit/test_fakes_emby_server.py`, `tests/unit/test_services_watch_sync.py`, `tests/integration/test_services_watch_sync.py`, `tests/integration/test_services_reconcile.py` | as each task says |
| `docs/prd/03-sources-and-sync.md`, `CHANGELOG.md`, `.claude/rules/emby-push-and-ingest.md` | per task |
| `docs/plans/progress.md`, `docs/prd/README.md` | this plan's status cells (Task 8) |

**Phase 2** — listed at the head of Phase 2.

---

## Phase 1 — tune the single walk (PR 1)

Everything here is inside the Emby adapter, its fakes and the watch-lane service. It speeds up the next sync of every deployment and needs no migration.

### Task 1: Pages of 1,000

**Files:**
- Modify: `src/usher/config.py` (`source_page_size`)
- Modify: `src/usher/adapters/emby/adapter.py` (`EmbyAdapter.__init__`'s `page_size` default)
- Modify: `src/usher/adapters/factory.py` (`ConfiguredSourceAdapterFactory.__init__`'s `page_size` default)
- Modify: `.env.example` (`USHER_SOURCE_PAGE_SIZE=`)
- Modify: `web/src/features/operator/Config.settings.ts` (the `USHER_SOURCE_PAGE_SIZE` entry's `def`)
- Modify: `tests/unit/test_adapters_emby_adapter.py` (`test_the_default_page_size_is_what_goes_out_as_the_limit`)
- Modify: `tests/integration/test_services_reconcile.py` (the comment above `CEILING`)
- Modify: `docs/prd/03-sources-and-sync.md` ("Walking the library"), `CHANGELOG.md`

**Interfaces:**
- Produces: `Settings.source_page_size` defaults to `1000`; `EmbyAdapter(..., page_size=1000)` and `ConfiguredSourceAdapterFactory(..., page_size=1000)` are the constructor defaults.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_adapters_emby_adapter.py`, `test_the_default_page_size_is_what_goes_out_as_the_limit`, change the last line:

```python
    assert captured[0].url.params["Limit"] == "1000"
```

In `web/src/features/operator/Config.settings.ts`, the `USHER_SOURCE_PAGE_SIZE` entry:

```ts
  {
    key: 'USHER_SOURCE_PAGE_SIZE',
    group: 'sources',
    def: '1000',
    about: 'Items per page when walking a source’s library.',
    secret: false,
    measured: false,
  },
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py::test_the_default_page_size_is_what_goes_out_as_the_limit tests/unit/test_console_settings_catalogue.py`
Expected: FAIL — `assert '200' == '1000'`, and `test_every_catalogued_default_is_the_default_usher_actually_ships` naming `USHER_SOURCE_PAGE_SIZE`.

- [ ] **Step 3: Change the defaults**

`src/usher/config.py`:

```python
    source_page_size: int = Field(default=1000, ge=1, le=1000)
```

`src/usher/adapters/emby/adapter.py`, in `EmbyAdapter.__init__`'s signature, and `src/usher/adapters/factory.py`, in `ConfiguredSourceAdapterFactory.__init__`'s — a factory nobody configured must build what the setting's default builds:

```python
        page_size: int = 1000,
```

`.env.example`:

```
USHER_SOURCE_PAGE_SIZE=1000
```

`tests/integration/test_services_reconcile.py` — the comment above `CEILING` claims a coincidence that is no longer true. Replace the four comment lines with:

```python
# The ceiling case's two numbers, named so the arithmetic below reads. The
# ceiling is counted in *items*, deliberately, because `MAX_PAGES` already
# means something else.
```

`docs/prd/03-sources-and-sync.md`, "Walking the library" — replace the lead-in sentence:

```markdown
`list_items` and `watch_state` page over the source's own listing, in pages of
`USHER_SOURCE_PAGE_SIZE` items (default **1,000**, at most 1,000), one page in
flight at a time:
```

`CHANGELOG.md`, first bullet under `## [Unreleased]` → `### Changed`:

```markdown
- **`USHER_SOURCE_PAGE_SIZE` defaults to 1,000, up from 200.** A page of 1,000
  costs a source little more than a page of 200, so a walk makes a fifth of the
  requests.
```

- [ ] **Step 4: Run the tests and the console gate**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py tests/unit/test_console_settings_catalogue.py tests/unit/test_config.py`
Expected: PASS.
Run: `cd web && npm run verify`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/usher/config.py src/usher/adapters/emby/adapter.py src/usher/adapters/factory.py \
  .env.example web/src/features/operator/Config.settings.ts tests/unit/test_adapters_emby_adapter.py \
  tests/integration/test_services_reconcile.py docs/prd/03-sources-and-sync.md CHANGELOG.md
git commit -m "sync: walk a source in pages of 1,000 by default"
```

---

### Task 2: The total, once — and a walk that ends on a short page past it

Spec §1.2. `EnableTotalRecordCount=true` goes on a walk's first page only. The walk ends on an empty page, or on a page shorter than requested once the cursor has reached the first page's total. That second rule keeps a server that caps `Limit` from ending a walk early, and keeps reading a library that grew past its stale total.

**Departure from the spec's letter** (made in review): a page is short when it is shorter than the longest page served before it, or than `limit` before any. Judged against `limit`, as §1.2 words it, every page a capped server serves is short, so the walk ends at the first page's stale total and loses what a growing library added.

The live server still sends `TotalRecordCount` on an uncounted page, as **0** (recorded 2026-10-01, at the head and deep in a 1.16M-item listing). Omitting the flag counts. The fake must say the same before the adapter relies on it.

**Files:**
- Create: `src/usher/adapters/emby/paging.py`
- Create: `tests/unit/test_adapters_emby_paging.py`
- Modify: `src/usher/adapters/emby/adapter.py` (`_walk`, a new `_listing_query`, `_list_items`, `_watch_state`)
- Modify: `tests/fakes/emby_server.py` (`__init__`, `_list`)
- Modify: `tests/fixtures/emby/README.md` (a new section)
- Test: `tests/unit/test_adapters_emby_adapter.py`, `tests/unit/test_fakes_emby_server.py`

**Interfaces:**
- Produces: `usher.adapters.emby.paging.Page(fresh: list[dict[str, Any]], ended: bool)`; `OffsetWindow(*, limit: int, start: int)` with `.start: int`, `.total: int | None`, `.receive(entries: list[Any], total: object) -> Page`, `.advance() -> int`. Task 3 extends both.
- Produces: `_listing_query(since_param: str, since: AwareDatetime | None) -> dict[str, str]` in `adapter.py`; `EmbyAdapter._walk(query: Mapping[str, str], *, start_index: int) -> AsyncGenerator[dict[str, Any]]`.
- Produces: `FakeEmbyServer.max_limit: int | None`.

- [ ] **Step 1: Record the facts the fake is about to model**

Append to `tests/fixtures/emby/README.md`, before `## Regenerating`:

```markdown
## Listing protocol (fast first sync)

Recorded 2026-10-01 against a 1.16M-item library with a read-only script
outside the repository, which printed counts only. No payload was kept,
because these rows are behaviour rather than shape: every item a listing
returns has the shape of the item fixtures above.

| Behaviour | What the server does | What depends on it |
|---|---|---|
| `EnableTotalRecordCount=false` | `TotalRecordCount` is still present, as **0**, at the head of the listing and a million items deep | `OffsetWindow` reads the total from a walk's first page only; `FakeEmbyServer` renders 0 |
| `EnableTotalRecordCount` omitted | counted, exactly as `true` | the fake counts when the flag is absent |
```

- [ ] **Step 2: Write the failing tests for the fake**

Append to `tests/unit/test_fakes_emby_server.py`:

```python
# --- the listing's count and its ceiling ----------------------------------


async def test_an_uncounted_listing_still_reports_a_total_of_zero(driver: _Driver) -> None:
    """`EnableTotalRecordCount=false` sends the key, as 0, as the live server does."""
    driver.server.add_item(MOVIE, T0)
    body = await driver.session.json_body(
        "GET",
        f"/Users/{USER_ID}/Items",
        params={"Recursive": "true", "EnableTotalRecordCount": "false"},
        op="list",
    )
    assert body["TotalRecordCount"] == 0
    assert len(body["Items"]) == 1, "the premise: the page itself was served"


async def test_a_listing_that_does_not_say_is_counted(driver: _Driver) -> None:
    driver.server.add_item(MOVIE, T0)
    body = await driver.session.json_body(
        "GET", f"/Users/{USER_ID}/Items", params={"Recursive": "true"}, op="list"
    )
    assert body["TotalRecordCount"] == 1


async def test_a_capped_limit_serves_fewer_than_were_asked_for(driver: _Driver) -> None:
    for index in range(3):
        driver.server.add_item(replace(MOVIE, external_id=f"movie-{index}"), T0)
    driver.server.max_limit = 2
    body = await driver.session.json_body(
        "GET", f"/Users/{USER_ID}/Items", params={"Recursive": "true", "Limit": "10"}, op="list"
    )
    assert len(body["Items"]) == 2
    assert body["TotalRecordCount"] == 3
```

- [ ] **Step 3: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_fakes_emby_server.py -k "uncounted or does_not_say or capped"`
Expected: FAIL — `assert 1 == 0` on the first; `AttributeError` or `assert 3 == 2` on the third. The second passes already; that is its job (it pins the default the first case must not break).

- [ ] **Step 4: Teach the fake**

`tests/fakes/emby_server.py`, in `__init__` after `self.fail_after`:

```python
        # A listing's `Limit` ceiling, or `None` for none. A server may serve fewer
        # items than asked for on every page, which a walk must not read as its end.
        self.max_limit: int | None = None
```

In `_list`, replace the `limit` line and the response:

```python
        limit = int(params.get("Limit", str(self.page_size)))
        if self.max_limit is not None:
            limit = min(limit, self.max_limit)
        counted = (params.get("EnableTotalRecordCount") or "true").lower() != "false"
        ordered = self._ordered(params)
        if self.fail_after is not None and start >= self.fail_after:
            raise httpx.ReadTimeout("upstream stopped responding")
        page = ordered[start : start + limit]
        return httpx.Response(
            200,
            json={
                "Items": [self._payload(external_id, for_listing=True) for external_id in page],
                # Present and 0 when uncounted, as the live server sends it.
                "TotalRecordCount": len(ordered) if counted else 0,
            },
        )
```

Run the Step 3 command again. Expected: PASS.

- [ ] **Step 5: Write the failing tests for the window**

Create `tests/unit/test_adapters_emby_paging.py`:

```python
"""`OffsetWindow`: where a walk's next page starts, and when the walk ends."""

from typing import Any

import pytest

from usher.adapters.emby.paging import OffsetWindow


def _entries(*ids: str) -> list[Any]:
    return [{"Id": external_id} for external_id in ids]


def test_the_total_comes_from_the_first_page_only() -> None:
    window = OffsetWindow(limit=2, start=0)
    window.receive(_entries("a", "b"), 5)
    window.advance()
    window.receive(_entries("c", "d"), 9)
    assert window.total == 5


@pytest.mark.parametrize("reported", [0, -1, None, "5", 5.5])
def test_a_total_that_is_not_a_positive_integer_is_no_total(reported: object) -> None:
    window = OffsetWindow(limit=2, start=0)
    window.receive(_entries("a"), reported)
    assert window.total is None


def test_an_empty_page_ends_the_walk() -> None:
    assert OffsetWindow(limit=2, start=0).receive([], 5).ended


def test_a_short_page_at_the_total_ends_the_walk() -> None:
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a", "b", "c", "d"), 6).ended
    window.advance()
    assert window.receive(_entries("e", "f"), 0).ended


def test_a_short_page_below_the_total_does_not_end_the_walk() -> None:
    """A server that caps `Limit` serves nothing but short pages until the end."""
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a", "b"), 6).ended
    window.advance()
    assert not window.receive(_entries("c", "d"), 0).ended
    window.advance()
    assert window.receive(_entries("e", "f"), 0).ended


def test_a_full_page_past_the_total_does_not_end_the_walk() -> None:
    """A library that grew during the walk keeps serving full pages past its old total."""
    window = OffsetWindow(limit=2, start=0)
    assert not window.receive(_entries("a", "b"), 2).ended
    window.advance()
    assert window.receive(_entries("c"), 0).ended


def test_a_walk_with_no_total_ends_only_on_an_empty_page() -> None:
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a"), None).ended
    window.advance()
    assert window.receive([], None).ended


def test_the_cursor_advances_by_what_was_served_not_by_the_limit() -> None:
    window = OffsetWindow(limit=4, start=10)
    window.receive(_entries("a"), 20)
    assert window.advance() == 11


def test_a_resumed_walk_starts_where_it_was_told() -> None:
    assert OffsetWindow(limit=4, start=300).start == 300


def test_an_entry_that_is_not_an_object_is_not_yielded() -> None:
    page = OffsetWindow(limit=4, start=0).receive(["junk", {"Id": "a"}], 2)
    assert page.fresh == [{"Id": "a"}]
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_paging.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.emby.paging'`.

- [ ] **Step 6: Write the window**

Create `src/usher/adapters/emby/paging.py`:

```python
"""Where each page of a `StartIndex` walk starts, and when the walk ends."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Page:
    """One listing response, read against the walk so far."""

    fresh: list[dict[str, Any]]
    ended: bool


class OffsetWindow:
    """The arithmetic of one walk, kept apart from the requests it plans.

    The total is read from the first page only, the one asked to count. The walk
    ends on an empty page, or on a page shorter than `limit` once the cursor has
    reached that total: a server that caps `Limit` serves short pages while the
    cursor is still below it, and a library that grows during the walk keeps
    serving full pages past it.
    """

    def __init__(self, *, limit: int, start: int) -> None:
        self.limit = limit
        self.start = start
        self.total: int | None = None
        self._cursor = start
        self._first = True

    def receive(self, entries: list[Any], total: object) -> Page:
        """Account for the page requested at `start`."""
        if self._first and isinstance(total, int) and total > 0:
            self.total = total
        self._first = False
        self._cursor = self.start + len(entries)
        reached = self.total is not None and self._cursor >= self.total
        ended = not entries or (len(entries) < self.limit and reached)
        return Page(fresh=[entry for entry in entries if isinstance(entry, dict)], ended=ended)

    def advance(self) -> int:
        """The next request's `StartIndex`."""
        self.start = self._cursor
        return self.start
```

Run the Step 5 command. Expected: PASS.

- [ ] **Step 7: Write the failing adapter tests**

In `tests/unit/test_adapters_emby_adapter.py`, give the two helpers the knobs the new cases need. `_adapter` gains `max_pages`; `_on` gains `page_size`, passed only when given, because `test_the_default_page_size_is_what_goes_out_as_the_limit` must keep exercising the constructor's own default:

```python
def _adapter(
    server: FakeEmbyServer, *, page_size: int = 2, max_pages: int = MAX_PAGES
) -> EmbyAdapter:
    return EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(transport=server.transport(), base_url=SOURCE.base_url),
        page_size=page_size,
        max_pages=max_pages,
    )
```

```python
def _on(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_pages: int = MAX_PAGES,
    waits: _Waits | None = None,
    page_size: int | None = None,
) -> EmbyAdapter:
    """An adapter over a hand-written handler, for shapes `FakeEmbyServer` will not produce.

    `page_size` reaches the constructor only when a case names one.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SOURCE.base_url)
    options: dict[str, Any] = {"max_pages": max_pages}
    if page_size is not None:
        options["page_size"] = page_size
    if waits is not None:
        options |= {"sleep": waits.sleep, "clock": waits.clock}
    return EmbyAdapter(SOURCE, CREDENTIALS, client=client, **options)
```

Add `from typing import Any` to the imports. Then add, after `test_the_walk_pages_until_the_library_is_exhausted`:

```python
async def test_only_the_first_page_asks_for_the_total() -> None:
    """A count costs Emby a pass over the whole filtered set, so a walk asks for it once.

    Every later page sends `false`, and the walk reads its end from the first page's
    total.
    """
    server = FakeEmbyServer()
    for index in range(5):
        server.add_item(_movie(index), T0)
    captured: list[httpx.Request] = []

    def spy(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return server.handle(request)

    adapter = _on(spy, page_size=2)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    flags = [
        request.url.params["EnableTotalRecordCount"]
        for request in captured
        if request.url.path.endswith("/Items")
    ]
    assert sorted(seen) == [f"movie-{index}" for index in range(5)]
    assert len(flags) > 1, "the premise: the walk paged"
    assert flags[0] == "true"
    assert set(flags[1:]) == {"false"}


async def test_a_server_that_caps_the_limit_is_still_walked_to_its_end() -> None:
    """Short pages below the first page's total are the server's cap, not the library's end."""
    server = FakeEmbyServer()
    server.max_limit = 2
    for index in range(7):
        server.add_item(_movie(index), T0)
    sizes: list[int] = []

    def spy(request: httpx.Request) -> httpx.Response:
        response = server.handle(request)
        if request.url.path.endswith("/Items"):
            sizes.append(len(response.json()["Items"]))
        return response

    adapter = _on(spy, page_size=4)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert sizes and max(sizes) < 4, "the premise: every page came back shorter than asked for"
    assert sorted(set(seen)) == [f"movie-{index}" for index in range(7)]


async def test_a_library_that_grows_during_the_walk_is_read_to_its_end() -> None:
    """Items added mid-walk sort last, past the stale total its first page reported."""
    server = FakeEmbyServer()
    for index in range(4):
        server.add_item(_movie(index), T0)

    def grow(request: httpx.Request) -> httpx.Response:
        response = server.handle(request)
        if request.url.path.endswith("/Items") and server.listings == 1:
            for index in range(4, 7):
                server.add_item(replace(_movie(index), added_at=T1), T1)
        return response

    adapter = _on(grow, page_size=2)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert sorted(set(seen)) == [f"movie-{index}" for index in range(7)]
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py -k "first_page_asks or caps_the_limit or grows_during"`
Expected: FAIL — `assert {'true'} == {'false'}` on the first, and the second ending after the first short page (`seen` holds two items). The third passes already against today's `start >= total` rule only because 4 is not a multiple of the growth; it is here to stop Step 8 from breaking it.

- [ ] **Step 8: Rebuild `_walk` on the window**

In `src/usher/adapters/emby/adapter.py`: import `AsyncGenerator` beside the other `collections.abc` names and `from usher.adapters.emby.paging import OffsetWindow`. Add, above `class EmbyAdapter`:

```python
def _listing_query(since_param: str, since: AwareDatetime | None) -> dict[str, str]:
    """A listing's parameters, less the paging `_walk` adds to each request."""
    query = {
        "Recursive": "true",
        "IncludeItemTypes": ITEM_TYPES,
        "Fields": ITEM_FIELDS,
        "SortBy": SORT_BY,
        "SortOrder": "Ascending",
    }
    if since is not None:
        query[since_param] = emby_datetime(since)
    return query
```

Replace `_walk` with:

```python
    async def _walk(
        self, query: Mapping[str, str], *, start_index: int
    ) -> AsyncGenerator[dict[str, Any]]:
        """Page one listing to its end; `start_index` is the resume point (#41).

        Deliberately no default: every caller states its own, so `list_items`
        passing 0 is written down rather than inferred.
        """
        path = f"/Users/{_segment(await self._session.user_id())}/Items"
        window = OffsetWindow(limit=self._page_size, start=start_index)
        # `for`, not `while True`: the bound is part of the loop, and the raise
        # below is reachable only by a walk that never ended.
        for number in range(self._max_pages):
            params = {
                **query,
                "StartIndex": str(window.start),
                "Limit": str(self._page_size),
                "EnableTotalRecordCount": "true" if number == 0 else "false",
            }
            body = await self._page(path, params, window.start)
            entries = body.get("Items")
            if not isinstance(entries, list):
                # Not a truncation: a caller must be able to tell "the library
                # ended" from "that was not a listing at all".
                raise PortDataMalformed(
                    "Emby's item listing carried no Items array",
                    detail=f"StartIndex={window.start}",
                )
            page = window.receive(entries, body.get("TotalRecordCount"))
            for payload in page.fresh:
                yield payload
            if page.ended:
                return
            window.advance()
        raise PortDataMalformed(
            "Emby's item listing never ended; the server appears to ignore StartIndex",
            detail=f"gave up after {self._max_pages} pages at StartIndex={window.start}",
        )
```

Point the two callers at it:

```python
    async def _list_items(self, since: AwareDatetime | None) -> AsyncIterator[SourceItem]:
        # `start_index=0` always: the item lanes have a working `since`
        # cursor, so a failed walk restarts from it rather than resuming.
        query = _listing_query(LIBRARY_SINCE_PARAM, since)
        async for payload in self._walk(query, start_index=0):
            item = to_source_item(payload)
            if item is not None:
                yield item
```

```python
    async def _watch_state(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[SourceWatchState]:
        user_id = await self._session.user_id()
        query = _listing_query(USER_DATA_SINCE_PARAM, since)
        async for payload in self._walk(query, start_index=start_index):
            # play_history_is_trustworthy=False: this is the listing route.
            state = to_watch_state(
                payload, source_user_id=user_id, play_history_is_trustworthy=False
            )
            if state is not None:
                yield state
```

- [ ] **Step 9: Run the adapter, contract and fake suites**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_paging.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_contract.py tests/unit/test_fakes_emby_server.py tests/unit/test_adapters_emby_session.py`
Expected: PASS. `test_the_walk_pages_until_the_library_is_exhausted` still makes three requests; `test_a_naive_since_cursor_never_reaches_the_wire` still sees no listing, because `_listing_query` raises before `_walk` runs.

- [ ] **Step 10: Plant and verify the two new rules**

Each plant must fail its own case on its own `E ` line, then be restored and read back.

1. In `OffsetWindow.receive`, make every short page end the walk: `ended = not entries or len(entries) < self.limit`. Expect `test_a_server_that_caps_the_limit_is_still_walked_to_its_end` and `test_a_short_page_below_the_total_does_not_end_the_walk` to fail.
2. Make the total end the walk whatever the page: `ended = not entries or reached`. Expect `test_a_library_that_grows_during_the_walk_is_read_to_its_end` and `test_a_full_page_past_the_total_does_not_end_the_walk` to fail.
3. In `_walk`, send `"true"` on every page. Expect `test_only_the_first_page_asks_for_the_total` to fail.
4. In `receive`, drop `self._first and` from the total's condition. Expect `test_the_total_comes_from_the_first_page_only` to fail (the second page's 9 overwrites the 5).

- [ ] **Step 11: Commit**

```bash
git add src/usher/adapters/emby/paging.py src/usher/adapters/emby/adapter.py \
  tests/unit/test_adapters_emby_paging.py tests/unit/test_adapters_emby_adapter.py \
  tests/fakes/emby_server.py tests/unit/test_fakes_emby_server.py tests/fixtures/emby/README.md
git commit -m "emby: ask for a listing's total once, and end a walk on a short page past it"
```

---

### Task 3: Overlapping pages, deduplicated, and a walk that ends on its drained tail

Spec §1.3. After the first page, a page is requested at `StartIndex = max(0, cursor − overlap)`. Entries the previous page carried are dropped by `Id`. When a page holds none of the previous page's ids although its request reached back for one, the walk logs a WARNING naming the `StartIndex` and carries on.

**Three departures from the spec's letter**; each has a case below that fails without it:

- **The overlap is clamped to half the page before it**: `overlap = min(PAGE_OVERLAP, previous_page_len // 2)`. A fixed 50 re-reads a whole page whenever a page is 50 items or fewer (a server capping `Limit`, or the small pages this repository's fakes run). The cursor then stands still or moves backwards, and the walk pages out `MAX_PAGES` instead of the library.
- **A page shorter than the longest served that brought nothing new ends the walk.** Deletions mid-walk leave the cursor short of the first page's total for good, so §1.2's rule can never fire. Without this rule the clamped overlap halves its way down to an empty page, costing several more deep requests; with a fixed overlap the walk would loop until `MAX_PAGES`. "Longest served" rather than `limit`, so a capped server's full pages never read as a tail.
- **A page has shifted when it holds none of the previous page's ids**, provided its request reached back for one, and not when it lacks the previous page's last id (made in review). The last item can itself leave the listing between two requests, and items listed ahead of the reach-back can push it out of view; neither skips anything, and a WARNING that cries wolf is one an operator learns to ignore. With no id to reach back for — no reach-back, or one over entries without an id — nothing says the page shifted.

The WARNING says "at least": a shift of exactly the overlap also leaves none of the previous page's ids in view, though it skips nothing, and nothing inside the walk can tell the two apart.

**Files:**
- Modify: `src/usher/adapters/emby/paging.py`
- Modify: `src/usher/adapters/emby/adapter.py` (`_walk` logs the shift)
- Test: `tests/unit/test_adapters_emby_paging.py`, `tests/unit/test_adapters_emby_adapter.py`
- Modify: `docs/prd/03-sources-and-sync.md`, `CHANGELOG.md`, `.claude/rules/emby-push-and-ingest.md`

**Interfaces:**
- Consumes: Task 2's `OffsetWindow`, `Page`.
- Produces: `PAGE_OVERLAP = 50` in `paging.py`; `Page.shifted: bool`; `OffsetWindow.overlap: int`, the reach-back of the request in flight. Phase 2's chunked units (Task 13) build on this window.

- [ ] **Step 1: Write the failing window tests**

Append to `tests/unit/test_adapters_emby_paging.py`, and add `PAGE_OVERLAP` to its import:

```python
def _numbered(count: int) -> list[Any]:
    return _entries(*(f"m{index:04d}" for index in range(count)))


def test_a_page_after_the_first_reaches_back_by_the_overlap() -> None:
    window = OffsetWindow(limit=200, start=0)
    window.receive(_numbered(200), 1000)
    assert window.advance() == 200 - PAGE_OVERLAP


def test_the_overlap_is_clamped_to_half_the_page_just_served() -> None:
    """A fixed reach-back of `PAGE_OVERLAP` re-reads a whole page of 50 or fewer."""
    window = OffsetWindow(limit=200, start=0)
    window.receive(_numbered(40), 1000)
    assert window.advance() == 20


def test_a_one_entry_page_reaches_back_nothing() -> None:
    window = OffsetWindow(limit=200, start=0)
    window.receive(_entries("a"), 3)
    assert window.advance() == 1


def test_an_entry_the_previous_page_carried_is_not_yielded_again() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    assert window.advance() == 2
    page = window.receive(_entries("c", "d", "e", "f"), 0)
    assert [entry["Id"] for entry in page.fresh] == ["e", "f"]


def test_an_entry_with_no_id_is_always_yielded() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    window.advance()
    page = window.receive([{"Name": "no id"}, *_entries("d", "e", "f")], 0)
    assert page.fresh[0] == {"Name": "no id"}


def test_an_overlapping_page_holding_none_of_the_last_page_has_shifted() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    window.advance()
    assert window.receive(_entries("f", "g", "h", "i"), 0).shifted


def test_an_overlapping_page_holding_the_last_id_has_not_shifted() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    window.advance()
    assert not window.receive(_entries("d", "e", "f", "g"), 0).shifted


def test_an_overlapping_page_missing_only_the_last_id_has_not_shifted() -> None:
    """The last item may simply have left; holding `c`, the page moved past nothing."""
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    window.advance()
    assert not window.receive(_entries("c", "e", "f", "g"), 0).shifted


def test_a_request_that_reached_back_nothing_cannot_have_shifted() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a"), 10)
    window.advance()
    assert window.overlap == 0, "the premise: a one-entry page reaches back nothing"
    assert not window.receive(_entries("x"), 0).shifted


def test_a_page_after_one_carrying_no_ids_has_not_shifted() -> None:
    """With no id from the page before to look for, nothing says the listing moved."""
    window = OffsetWindow(limit=4, start=0)
    window.receive([{"Name": "w"}, {"Name": "x"}, {"Name": "y"}, {"Name": "z"}], 10)
    window.advance()
    assert window.overlap == 2, "the premise: the request reached back"
    assert not window.receive(_entries("e", "f", "g", "h"), 0).shifted


def test_a_reach_back_over_entries_without_ids_cannot_have_shifted() -> None:
    """The ids that judge a shift are the ones the request reached back for, not the page's."""
    window = OffsetWindow(limit=4, start=0)
    window.receive([*_entries("a", "b"), {"Name": "y"}, {"Name": "z"}], 10)
    window.advance()
    assert window.overlap == 2, "the premise: the request reached back over the two without ids"
    assert not window.receive([{"Name": "y"}, {"Name": "z"}, *_entries("e", "f")], 0).shifted


def test_one_id_in_a_reach_back_is_enough_to_judge_a_shift() -> None:
    """`d` is the one id the request reached back for; deleting `a`, `b` and `d` skips `e`."""
    window = OffsetWindow(limit=4, start=0)
    window.receive([*_entries("a", "b", "d"), {"Name": "y"}], 10)
    assert window.advance() == 2, "the premise: it reached back over d and one without an id"
    assert window.receive(_entries("f", "g", "h", "i"), 0).shifted


def test_items_inserted_ahead_of_the_reach_back_are_not_a_shift() -> None:
    """Four items listed before `a` push `c` and `d` out of view, and the page re-reads.

    Holding `a` and `b`, the page skipped nothing: every item not yet read lists after them.
    """
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    assert window.advance() == 2, "the premise: the request reached back for c and d"
    assert not window.receive(_entries("y", "z", "a", "b"), 0).shifted


def test_a_short_page_that_brought_nothing_new_ends_the_walk() -> None:
    """Two deletions leave the cursor short of the first page's total for good."""
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 6)
    window.advance()
    page = window.receive(_entries("c", "d"), 0)
    assert window.total == 6 and page.fresh == [], "the premise: a drained tail, below the total"
    assert page.ended


def test_a_full_page_that_brought_nothing_new_does_not_end_the_walk() -> None:
    """Items added behind the cursor can fill a page with what was already read."""
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 12)
    window.advance()
    page = window.receive(_entries("a", "b", "c", "d"), 0)
    assert page.fresh == [] and not page.ended


def test_a_capped_page_that_brought_nothing_new_does_not_end_the_walk() -> None:
    """A capped server's full pages are short of `limit` and must not read as a tail."""
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b"), 12)
    window.advance()
    page = window.receive(_entries("a", "b"), 0)
    assert page.fresh == [] and not page.ended
```

Three of Task 2's window cases feed pages only a walk without a reach-back is served, and fail under this task's window or no longer describe it. Replace them in place, intents kept; the fourth, whose name stopped being true, is renamed and its body kept:

```python
def test_a_full_page_past_the_total_does_not_end_the_walk() -> None:
    """A library that grew during the walk keeps serving full pages past its old total."""
    window = OffsetWindow(limit=2, start=0)
    assert not window.receive(_entries("a", "b"), 2).ended
    assert window.advance() == 1
    assert not window.receive(_entries("b", "c"), 0).ended
    assert window.advance() == 2
    assert window.receive(_entries("c"), 0).ended


def test_a_walk_with_no_total_does_not_end_on_a_short_page_alone() -> None:
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a"), None).ended
    window.advance()
    assert window.receive([], None).ended


def test_a_short_page_at_the_total_ends_the_walk() -> None:
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a", "b", "c", "d"), 5).ended
    assert window.advance() == 2
    assert window.receive(_entries("c", "d", "e"), 0).ended


def test_a_short_page_below_the_total_does_not_end_the_walk() -> None:
    """A server that caps `Limit` serves nothing but short pages until the end."""
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a", "b", "c"), 6).ended
    assert window.advance() == 2
    assert not window.receive(_entries("c", "d", "e"), 0).ended
    assert window.advance() == 4
    assert window.receive(_entries("e", "f"), 0).ended
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_paging.py`
Expected: FAIL — `ImportError: cannot import name 'PAGE_OVERLAP'`.

- [ ] **Step 2: Extend the window**

Replace `src/usher/adapters/emby/paging.py` with:

```python
"""Where each page of a `StartIndex` walk starts, what in it is new, and when it ends."""

from dataclasses import dataclass
from typing import Any

# How far a page after the first reaches back into the one before. A deletion
# behind the cursor shifts every later item left by one, so without this the
# next page skips one item per deletion, and the sweep retracts a file that is
# still there.
PAGE_OVERLAP = 50


@dataclass(frozen=True, slots=True)
class Page:
    """One listing response, read against the walk so far."""

    fresh: list[dict[str, Any]]
    ended: bool
    shifted: bool


class OffsetWindow:
    """The arithmetic of one walk, kept apart from the requests it plans.

    The total is read from the first page only, the one asked to count. A page is
    short when it is shorter than the longest page served before it, or than
    `limit` before any. A walk ends on an empty page; on a short page once the
    cursor has reached that total; or on a short page that brought nothing new.
    The second rule keeps a server that caps `Limit` from ending a walk early,
    and keeps reading a library that grows past its old total, capped or not.
    The third ends a walk that deletions left short of its total, which would
    otherwise re-read its tail until the end.

    Each page after the first reaches back `PAGE_OVERLAP` items, clamped to half
    the page before it so that a short page still advances. An entry the
    previous page carried is dropped by its `Id`. A page whose request reached
    back for an id, yet which holds none of the previous page's, has shifted by
    at least the reach-back. The previous page's last id alone would not say so:
    that item may have left.
    """

    def __init__(self, *, limit: int, start: int) -> None:
        self.limit = limit
        self.start = start
        self.overlap = 0
        self.total: int | None = None
        self._cursor = start
        self._served = 0
        self._longest = 0
        self._previous: frozenset[str] = frozenset()
        self._served_ids: list[str | None] = []
        self._first = True

    def receive(self, entries: list[Any], total: object) -> Page:
        """Account for the page requested at `start`."""
        ids = [_id_of(entry) for entry in entries]
        fresh = [
            entry
            for entry, external_id in zip(entries, ids, strict=True)
            if isinstance(entry, dict)
            and (external_id is None or external_id not in self._previous)
        ]
        # With no id to reach back for, because the request reached back nothing or
        # over entries without one, nothing says the page moved. Judged against the
        # whole previous page, not the reach-back alone: items listed ahead of it can
        # push the reach-back out of a page that skipped nothing.
        reach = self._served_ids[len(self._served_ids) - self.overlap :]
        reached_for_an_id = any(external_id is not None for external_id in reach)
        shifted = reached_for_an_id and self._previous.isdisjoint(ids)
        # `> 0`, not `>= 0`: 0 is what a listing not asked to count reports. Not a
        # `bool` either, because JSON `true` is an `int` to Python.
        if self._first and isinstance(total, int) and not isinstance(total, bool) and total > 0:
            self.total = total
        self._cursor = self.start + len(entries)
        self._served = len(entries)
        reached = self.total is not None and self._cursor >= self.total
        # Against the longest page served, or `limit` before the first: a capped
        # server serves nothing longer, and its full pages past a stale total are a
        # library that grew.
        short = len(entries) < (self._longest or self.limit)
        drained = not self._first and short and not fresh
        self._longest = max(self._longest, len(entries))
        named = [external_id for external_id in ids if external_id is not None]
        self._previous = frozenset(named)
        self._served_ids = ids
        self._first = False
        ended = not entries or (short and reached) or drained
        return Page(fresh=fresh, ended=ended, shifted=shifted)

    def advance(self) -> int:
        """The next request's `StartIndex`, reaching back into the page just served."""
        self.overlap = min(PAGE_OVERLAP, self._served // 2)
        self.start = max(0, self._cursor - self.overlap)
        return self.start


def _id_of(entry: object) -> str | None:
    if isinstance(entry, dict):
        value = entry.get("Id")
        if isinstance(value, str) and value:
            return value
    return None
```

Run the Step 1 command. Expected: PASS.

- [ ] **Step 3: Write the failing adapter tests**

In `tests/unit/test_adapters_emby_adapter.py`, add `from collections.abc import Sequence` beside `Callable`, and these helpers after `_movie`:

```python
def _numbered(count: int) -> list[SourceItem]:
    """`count` movies whose listing order is their number: the names are zero-padded."""
    return [
        replace(
            _movie(0), external_id=f"movie-{index:03d}", name=f"Movie {index:03d}", provider_ids={}
        )
        for index in range(count)
    ]


def _deleting(
    server: FakeEmbyServer, external_ids: Sequence[str]
) -> Callable[[httpx.Request], httpx.Response]:
    """`server`, with `external_ids` deleted once its first listing has been served."""

    def handle(request: httpx.Request) -> httpx.Response:
        response = server.handle(request)
        if request.url.path.endswith("/Items") and server.listings == 1:
            for external_id in external_ids:
                server.remove_item(external_id)
        return response

    return handle
```

Replace `test_the_walk_pages_until_the_library_is_exhausted` — its three requests over pages of two become five once each page reaches back one item:

```python
async def test_the_walk_pages_until_the_library_is_exhausted() -> None:
    """5 items over pages of 4 is two requests.

    The second reaches back two items, comes back short and at the total, and ends
    the walk rather than paying a third request for an empty page.
    """
    server = FakeEmbyServer()
    for index in range(5):
        server.add_item(_movie(index), T0)
    adapter = _adapter(server, page_size=4)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert sorted(seen) == [f"movie-{index}" for index in range(5)]
    listings = [entry for entry in server.requests if entry.endswith("/Items")]
    assert len(listings) == 2
```

`test_only_the_first_page_asks_for_the_total` (Task 2) pins three requests over pages of two. Under the reach-back a final page of pure overlap ends a walk by the drained rule whether or not the total was read, so the case moves to pages of four, where the second page (`movie-2`–`movie-4`) is short, brings `movie-4` and ends the walk at the total. A window that never takes the total asks a third time:

```python
    adapter = _on(spy, page_size=4)
```

```python
    assert len(flags) == 2, "the walk did not end on the page that reached the first page's total"
```

Then add, after it:

```python
async def test_a_deletion_behind_the_cursor_skips_nothing() -> None:
    """Deleting three of the first page's items shifts every later one left by three.

    The next page reaches back `PAGE_OVERLAP` items, so the three it would have
    skipped are still in it; the entries it re-reads are dropped by id, and a shift
    inside the overlap is not reported.
    """
    server = FakeEmbyServer()
    library = _numbered(250)
    for item in library:
        server.add_item(item, T0)
    gone = ["movie-010", "movie-020", "movie-030"]
    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter = _on(_deleting(server, gone), page_size=100)
        try:
            seen = [item.external_id async for item in adapter.list_items()]
        finally:
            await adapter.aclose()
    finally:
        logger.remove(handle)
    survivors = {item.external_id for item in library} - set(gone)
    assert survivors - set(seen) == set(), "an item shifted behind the cursor was skipped"
    assert len(seen) == len(set(seen)), "an entry re-read by the overlap was yielded twice"
    assert lines == []


async def test_a_shift_past_the_overlap_is_logged_with_the_page_it_hit() -> None:
    """Sixty deletions behind the cursor are more than the overlap can absorb.

    Ten items are skipped, which nothing inside the walk can repair; it says so,
    names the page, and carries on.
    """
    server = FakeEmbyServer()
    for item in _numbered(250):
        server.add_item(item, T0)
    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter = _on(
            _deleting(server, [f"movie-{index:03d}" for index in range(60)]), page_size=100
        )
        try:
            _ = [item async for item in adapter.list_items()]
        finally:
            await adapter.aclose()
    finally:
        logger.remove(handle)
    assert [line.rstrip("\n") for line in lines] == [
        "Living Room Emby's listing shifted by at least 50 items before StartIndex=50; "
        "an item shifted further was not read, and the next full walk reads it"
    ]


async def test_the_last_item_of_a_page_leaving_the_listing_is_not_a_shift() -> None:
    """The first page's last item is deleted, so the next page lacks its id.

    That page still re-reads the rest of its reach-back, so nothing moved past it:
    nothing is skipped and nothing is reported.
    """
    server = FakeEmbyServer()
    library = _numbered(250)
    for item in library:
        server.add_item(item, T0)
    gone = ["movie-099"]
    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter = _on(_deleting(server, gone), page_size=100)
        try:
            seen = [item.external_id async for item in adapter.list_items()]
        finally:
            await adapter.aclose()
    finally:
        logger.remove(handle)
    survivors = {item.external_id for item in library} - set(gone)
    assert survivors - set(seen) == set(), "an item behind the cursor was skipped"
    assert len(seen) == len(set(seen)), "an entry re-read by the overlap was yielded twice"
    assert lines == []


async def test_a_limit_capped_below_the_overlap_still_advances() -> None:
    """A server serving 40 a page, against an overlap of 50, is still read to its end.

    An unclamped reach-back re-reads all 40 and moves the cursor backwards, so the
    walk runs out its page bound instead of the library.
    """
    server = FakeEmbyServer()
    server.max_limit = 40
    library = _numbered(200)
    for item in library:
        server.add_item(item, T0)
    adapter = _adapter(server, page_size=100, max_pages=30)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert sorted(set(seen)) == [item.external_id for item in library]


async def test_a_walk_that_deletions_left_short_of_its_total_ends_on_its_tail() -> None:
    """Two deletions make the first page's total of six unreachable.

    The page that re-reads the tail brings nothing new and ends the walk. Without
    that rule the reach-back halves its way down to an empty page, one deep request
    at a time.
    """
    server = FakeEmbyServer()
    library = _numbered(6)
    for item in library:
        server.add_item(item, T0)
    adapter = _on(_deleting(server, ["movie-000", "movie-001"]), page_size=4)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    listings = [entry for entry in server.requests if entry.endswith("/Items")]
    assert set(seen) == {item.external_id for item in library}
    assert len(listings) == 3
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py -k "exhausted or deletion or shift_past or last_item or capped_below or short_of_its_total"`
Expected: FAIL — `_walk` does not log yet, so `test_a_shift_past_the_overlap_is_logged_with_the_page_it_hit` fails on `[] == [...]`. The others already pass against Step 2's window; Step 6 plants prove each one can fail.

- [ ] **Step 4: Log the shift**

In `_walk`, right after `page = window.receive(...)`:

```python
            if page.shifted:
                logger.warning(
                    "{source}'s listing shifted by at least {overlap} items before "
                    "StartIndex={start}; an item shifted further was not read, and the next "
                    "full walk reads it",
                    source=self._source.name,
                    overlap=window.overlap,
                    start=window.start,
                )
```

Run the Step 3 command. Expected: PASS.

- [ ] **Step 5: Run the adapter, contract and paging suites**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_paging.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_contract.py tests/unit/test_fakes_emby_server.py`
Expected: PASS. The contract runs at a page size of two, where every page reaches back one item; that is slower and still exact.

- [ ] **Step 6: Plant and verify**

1. `self.overlap = 0` in `advance` (no reach-back). Expect `test_a_deletion_behind_the_cursor_skips_nothing` ("skipped") and `test_a_page_after_the_first_reaches_back_by_the_overlap` to fail.
2. `self.overlap = PAGE_OVERLAP` (unclamped). Expect `test_a_limit_capped_below_the_overlap_still_advances` (`PortDataMalformed` after 30 pages) and `test_the_overlap_is_clamped_to_half_the_page_just_served` to fail.
3. `drained = False`. Expect `test_a_walk_that_deletions_left_short_of_its_total_ends_on_its_tail` (`4 == 3`) and `test_a_short_page_that_brought_nothing_new_ends_the_walk` to fail.
4. `short = len(entries) < self.limit`. Expect `test_a_capped_page_that_brought_nothing_new_does_not_end_the_walk`, `test_a_capped_walk_past_a_stale_total_reads_what_was_added` and `test_a_capped_server_on_a_growing_library_is_read_to_its_end` to fail.
5. `fresh` keeping every dict (no `not in self._previous` test). Expect `test_a_deletion_behind_the_cursor_skips_nothing` ("yielded twice") and `test_an_entry_the_previous_page_carried_is_not_yielded_again` to fail.
6. `shifted = reached_for_an_id and reach[-1] not in ids` (the last id only, the spec's letter). Expect `test_an_overlapping_page_missing_only_the_last_id_has_not_shifted`, `test_items_inserted_ahead_of_the_reach_back_are_not_a_shift` and `test_the_last_item_of_a_page_leaving_the_listing_is_not_a_shift` (`lines == []`) to fail.
7. `shifted = reached_for_an_id and {e for e in reach if e is not None}.isdisjoint(ids)` (the reach-back alone). Expect `test_items_inserted_ahead_of_the_reach_back_are_not_a_shift` to fail.
8. `shifted = self._previous.isdisjoint(ids)` (no `reached_for_an_id`). Expect `test_a_request_that_reached_back_nothing_cannot_have_shifted`, `test_a_page_after_one_carrying_no_ids_has_not_shifted` and `test_a_reach_back_over_entries_without_ids_cannot_have_shifted` to fail, among others.
9. `reach = self._served_ids` (the whole previous page, reach-back or not). Expect `test_a_request_that_reached_back_nothing_cannot_have_shifted` and `test_a_reach_back_over_entries_without_ids_cannot_have_shifted` to fail.
10. `reach = self._served_ids if self.overlap else []` (every entry of the previous page once it reached back). Expect `test_a_reach_back_over_entries_without_ids_cannot_have_shifted` to fail.
11. `reached_for_an_id = bool(reach) and all(external_id is not None for external_id in reach)`, and separately `reached_for_an_id = bool(reach) and reach[-1] is not None`. Expect `test_one_id_in_a_reach_back_is_enough_to_judge_a_shift` to fail under each.
12. Every new or rewritten case that no plant above names is seen to fail for its own reason: show its RED against Task 2's window, or plant for it.

- [ ] **Step 7: Say it in the PRD, the changelog and the rules**

`docs/prd/03-sources-and-sync.md`, "Walking the library", the first bullet becomes:

```markdown
- **Items are walked in ascending creation order**, so items added during a
  walk land at the end. Each page after the first re-reads the last 50 items of
  the page before (half the page, if it held fewer than 100), so a deletion
  mid-walk shifts nothing out of view unless more items than that vanish between
  two pages; the walk then logs a WARNING naming the page, and the next full
  reconcile covers what it missed. Duplicates are permitted; silent truncation
  is not.
```

`CHANGELOG.md`, under `### Fixed`:

```markdown
- **Items deleted from the source mid-walk no longer hide others from the
  walk**, unless more of them vanish between two pages than a page re-reads.
  Each page re-reads the end of the page before, so a full walk no longer marks
  a file that is still there unavailable.
```

`.claude/rules/emby-push-and-ingest.md`, under "Rules the pipeline enforces", after the "Nor may a blip" bullet:

```markdown
- **A page's reach-back is clamped to half the page before it**, and a walk ends
  on a short page that brought nothing new (`paging.OffsetWindow`): a fixed
  `PAGE_OVERLAP` stalls on pages of 50 or fewer, and deletions strand the total.
```

Then, to stay near the file's 200-line target, cut the last sentence of the "Gap-closing walks" paragraph (`That walk resumes from sync_runs.position; the guard still reads one cursor.`) — Task 6 rewrites that paragraph anyway.

- [ ] **Step 8: Commit**

```bash
git add src/usher/adapters/emby/paging.py src/usher/adapters/emby/adapter.py \
  tests/unit/test_adapters_emby_paging.py tests/unit/test_adapters_emby_adapter.py \
  docs/prd/03-sources-and-sync.md CHANGELOG.md .claude/rules/emby-push-and-ingest.md
git commit -m "emby: overlap each page with the last, so a deletion mid-walk skips nothing"
```

---

### Task 4: One page of read-ahead

Spec §1.4. As soon as page N arrives, the walk requests page N+1, then yields page N's items while that request is in flight. The outstanding request is cancelled when the consumer stops. The gate still spaces it.

**Files:**
- Modify: `src/usher/adapters/emby/adapter.py` (`_walk`, a new `_read`, `_settle` and `_retrieve`, `aclosing` in `_list_items` and `_watch_state`)
- Test: `tests/unit/test_adapters_emby_adapter.py`
- Modify: `docs/prd/03-sources-and-sync.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 3's window.
- Produces: `EmbyAdapter._read(path: str, query: Mapping[str, str], start: int, *, count: bool) -> asyncio.Task[dict[str, Any]]`; module-level `async def _settle(task: asyncio.Task[Any]) -> None`, and `def _retrieve(task: asyncio.Task[Any]) -> None`, the done-callback `_settle` attaches. Phase 2's listing limiter (Task 15) wraps `_page`, which `_read` schedules.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_adapters_emby_adapter.py`, add imports — `contextlib`, `gc`, `time`, `weakref`; `from collections.abc import AsyncGenerator, AsyncIterator, Coroutine`; `from typing import cast` — and widen `_on`'s handler, since an async handler is what lets a case hold a request open:

```python
_Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)
```

with `handler: _Handler` in `_on`'s signature. Then add a section after the walk cases:

```python
# --- read-ahead ---------------------------------------------------------------


def _parking_second_listing(
    server: FakeEmbyServer, parked: asyncio.Event, cancelled: asyncio.Event
) -> _Handler:
    """`server`, holding its second listing open until the request is cancelled."""
    listings = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal listings
        if request.url.path.endswith("/Items"):
            listings += 1
            if listings == 2:
                parked.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
        return server.handle(request)

    return handle


def _the_read_ahead() -> weakref.ref[asyncio.Task[Any]]:
    """A weak reference to the walk's read-ahead: the one other task still running.

    asyncio reports an exception nobody retrieved only when the task is destroyed, so
    a case asserting that nothing was reported first asserts that the read-ahead was
    collected, and holds nothing that would keep it alive.
    """
    others = asyncio.all_tasks() - {asyncio.current_task()}
    assert len(others) == 1, f"the premise: one read-ahead in flight, found {len(others)} tasks"
    return weakref.ref(others.pop())


def _recording(
    lost: list[dict[str, Any]],
) -> Callable[[asyncio.AbstractEventLoop, dict[str, Any]], None]:
    """A loop exception handler appending each report to `lost`, minus the task it names.

    asyncio reports a lost exception from the task's finalizer, with the task in the
    report. Keeping it would bring the task back to life, and the premise that the
    read-ahead was collected would then fail where `lost` should.
    """

    def record(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        lost.append({key: value for key, value in context.items() if key != "future"})

    return record


async def test_the_next_page_is_requested_while_the_current_one_is_handled() -> None:
    """Read-ahead, shown by observed overlap rather than by counting requests.

    The consumer holds the first item until the second page's request reaches the
    server, with a deadline that gives up. A walk that asks only when the consumer
    comes back for more sends nothing in that window, so the deadline expires and
    the two recorded intervals come out disjoint.
    """
    server = FakeEmbyServer()
    for index in range(4):
        server.add_item(_movie(index), T0)
    arrivals: list[float] = []
    second = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Items"):
            arrivals.append(time.monotonic())
            if len(arrivals) == 2:
                second.set()
        return server.handle(request)

    adapter = _on(handle, page_size=2)
    handling: tuple[float, float] | None = None
    try:
        async for _item in adapter.list_items():
            if handling is None:
                began = time.monotonic()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(second.wait(), timeout=2.0)
                handling = (began, time.monotonic())
    finally:
        await adapter.aclose()
    assert handling is not None and len(arrivals) >= 2, "the premise: the walk paged"
    began, ended = handling
    assert began <= arrivals[1] <= ended, (
        f"the second page was requested at {arrivals[1]:.3f}, outside the first page's "
        f"handling [{began:.3f}, {ended:.3f}]: nothing was read ahead"
    )


@pytest.mark.parametrize(
    "walk",
    [
        pytest.param(lambda adapter: adapter.list_items(), id="list_items"),
        # A delta from `T0`, which every item here carries: one listing, walked from
        # its start, as `_parking_second_listing` assumes and a first walk need not be.
        pytest.param(lambda adapter: adapter.watch_state(since=T0), id="watch_state"),
    ],
)
async def test_stopping_the_walk_cancels_the_request_it_read_ahead(
    walk: Callable[[EmbyAdapter], AsyncIterator[SourceItem | SourceWatchState]],
) -> None:
    server = FakeEmbyServer()
    for index in range(4):
        server.add_item(_movie(index), T0)
    parked, cancelled = asyncio.Event(), asyncio.Event()
    reported: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    adapter = _on(_parking_second_listing(server, parked, cancelled), page_size=2)
    items = cast(AsyncGenerator[SourceItem | SourceWatchState], walk(adapter))
    try:
        first = await asyncio.wait_for(anext(items), timeout=2.0)
        await asyncio.wait_for(parked.wait(), timeout=2.0)
        await asyncio.wait_for(items.aclose(), timeout=2.0)
    finally:
        loop.set_exception_handler(previous)
        await adapter.aclose()
    assert first.external_id == "movie-0"
    assert cancelled.is_set(), "the request read ahead outlived the walk that asked for it"
    assert reported == [], f"stopping the walk left asyncio something to report: {reported}"


async def test_a_read_ahead_that_failed_is_retrieved_when_the_walk_stops() -> None:
    """A page refused while the consumer is still on the one before.

    The consumer then stops. Stopping raises nothing, and asyncio is never left
    holding an exception nobody read, which it would log as lost.
    """
    server = FakeEmbyServer()
    for index in range(4):
        server.add_item(_movie(index), T0)
    refused = asyncio.Event()
    listings = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal listings
        if request.url.path.endswith("/Items"):
            listings += 1
            if listings == 2:
                refused.set()
                return httpx.Response(404, json={"Error": "gone"})
        return server.handle(request)

    lost: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(_recording(lost))
    adapter = _on(handle, page_size=2)
    try:
        items = cast(AsyncGenerator[SourceItem], adapter.list_items())
        await asyncio.wait_for(anext(items), timeout=2.0)
        ahead = _the_read_ahead()
        await asyncio.wait_for(refused.wait(), timeout=2.0)
        # Long enough for the refused request's task to finish raising.
        await asyncio.sleep(0.05)
        t = ahead()
        assert t is not None and t.done() and not t.cancelled(), (
            "the premise: the refused read had finished failing before the walk stopped"
        )
        del t
        await items.aclose()
        del items
        gc.collect()
    finally:
        loop.set_exception_handler(previous)
        await adapter.aclose()
    assert listings == 2, "the premise: the second page was asked for and refused"
    assert ahead() is None, "the premise: the read-ahead was collected"
    assert lost == [], f"asyncio reported a read-ahead failure nobody retrieved: {lost}"


async def test_a_read_ahead_that_fails_once_cancelled_is_retrieved_too() -> None:
    """Cancelling asks a read to stop, and a read can answer with a failure instead.

    `cancel()` alone silences a failure that is already in, so a read-ahead that failed
    before the walk stopped is quiet whether or not the walk reads what it raised. Here
    the second listing is held open until it is cancelled and then refused: the task
    ends raising, after the cancel.
    """
    server = FakeEmbyServer()
    for index in range(4):
        server.add_item(_movie(index), T0)
    parked, refused = asyncio.Event(), asyncio.Event()
    listings = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal listings
        if request.url.path.endswith("/Items"):
            listings += 1
            if listings == 2:
                parked.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    refused.set()
                return httpx.Response(404, json={"Error": "gone"})
        return server.handle(request)

    lost: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(_recording(lost))
    adapter = _on(handle, page_size=2)
    try:
        items = cast(AsyncGenerator[SourceItem], adapter.list_items())
        await asyncio.wait_for(anext(items), timeout=2.0)
        ahead = _the_read_ahead()
        await asyncio.wait_for(parked.wait(), timeout=2.0)
        await asyncio.wait_for(items.aclose(), timeout=2.0)
        del items
        gc.collect()
    finally:
        loop.set_exception_handler(previous)
        await adapter.aclose()
    assert refused.is_set(), "the premise: the read-ahead was cancelled, then refused"
    assert ahead() is None, "the premise: the read-ahead was collected"
    assert lost == [], f"asyncio reported a read-ahead failure nobody retrieved: {lost}"


async def test_cancelling_the_consumer_cancels_the_walk_rather_than_being_swallowed() -> None:
    server = FakeEmbyServer()
    for index in range(4):
        server.add_item(_movie(index), T0)
    parked, cancelled = asyncio.Event(), asyncio.Event()
    adapter = _on(_parking_second_listing(server, parked, cancelled), page_size=2)

    async def consume() -> list[str]:
        return [item.external_id async for item in adapter.list_items()]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(parked.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
    finally:
        await adapter.aclose()
    assert cancelled.is_set(), "the request in flight survived its consumer's cancellation"


async def test_a_read_ahead_that_fails_after_its_walk_was_cancelled_is_retrieved() -> None:
    """The walk is cancelled while it waits on its read-ahead, which fails afterwards.

    A lane shutting down: the task closing the walk is cancelled mid-wait and the
    adapter closed, and only then does the read-ahead, slow to stop, fail. That
    cancellation leaves the walk rather than ending there, and asyncio must still not
    report the read-ahead's failure as lost, though nothing waits on it any more.
    """
    server = FakeEmbyServer()
    for index in range(4):
        server.add_item(_movie(index), T0)
    parked, stopping = asyncio.Event(), asyncio.Event()
    release, failing = asyncio.Event(), asyncio.Event()
    listings = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal listings
        if request.url.path.endswith("/Items"):
            listings += 1
            if listings == 2:
                parked.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    stopping.set()
                    await release.wait()
                failing.set()
                raise httpx.ReadError("the connection dropped while the read was stopping")
        return server.handle(request)

    lost: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(_recording(lost))
    adapter = _on(handle, page_size=2)
    items = cast(AsyncGenerator[SourceItem], adapter.list_items())

    async def stop(walk: AsyncGenerator[SourceItem]) -> None:
        await walk.aclose()

    try:
        await asyncio.wait_for(anext(items), timeout=2.0)
        ahead = _the_read_ahead()
        await asyncio.wait_for(parked.wait(), timeout=2.0)
        closing = asyncio.create_task(stop(items))
        await asyncio.wait_for(stopping.wait(), timeout=2.0)
        assert not closing.done(), "the premise: the walk is still waiting on its read-ahead"
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=2.0)
        # Closed first, as a lane shutting down closes it, so `_page` passes the
        # failure on rather than asking again.
        await adapter.aclose()
        release.set()
        await asyncio.wait_for(failing.wait(), timeout=2.0)
        # Long enough for the read-ahead's task to finish raising.
        await asyncio.sleep(0.05)
        del items, closing
        gc.collect()
    finally:
        loop.set_exception_handler(previous)
        await adapter.aclose()
        # Released on every path, or a read-ahead first cancelled at teardown, by a
        # walk that never stopped it, waits on this forever and hangs the session.
        release.set()
    assert ahead() is None, "the premise: the read-ahead was collected"
    assert lost == [], f"asyncio reported a read-ahead failure nobody retrieved: {lost}"


async def test_the_page_that_ends_the_walk_is_the_last_one_asked_for() -> None:
    """Nothing is read ahead past the end, however long the consumer takes.

    The consumer waits on every item, which is when a page read ahead reaches the
    server: the second arrives during the first item, so one asked for past the end
    would arrive during the last.
    """
    server = FakeEmbyServer()
    for index in range(5):
        server.add_item(_movie(index), T0)
    seen: list[str] = []
    arrivals: list[int] = []

    def spy(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Items"):
            arrivals.append(len(seen))
        return server.handle(request)

    adapter = _on(spy, page_size=4)
    try:
        async for item in adapter.list_items():
            seen.append(item.external_id)
            await asyncio.sleep(0.01)
    finally:
        await adapter.aclose()
    assert sorted(seen) == [f"movie-{index}" for index in range(5)]
    assert arrivals[:2] == [0, 1], "the premise: a page read ahead arrives while the consumer waits"
    assert len(arrivals) == 2, f"a page was asked for after the one that ended the walk: {arrivals}"


async def test_the_last_page_the_bound_allows_is_the_last_one_asked_for() -> None:
    """A walk at its page bound reads nothing ahead, however long the consumer takes.

    Every page here is new, so the listing never ends and each page has an item for
    the consumer to wait on, which is when a page read past the bound would arrive.
    """
    seen: list[str] = []
    arrivals: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        arrivals.append(len(seen))
        # The escape hatch, so a walk with no bound fails this case instead of
        # hanging it.
        if len(arrivals) > 20:
            return httpx.Response(200, json={"Items": []})
        index = len(arrivals) - 1
        entry = {"Id": f"movie-{index}", "Type": "Movie", "Name": f"M{index}"}
        return httpx.Response(200, json={"Items": [entry]})

    adapter = _on(handler, max_pages=3)
    try:
        with pytest.raises(PortDataMalformed, match="never ended"):
            async for item in adapter.list_items():
                seen.append(item.external_id)
                await asyncio.sleep(0.01)
    finally:
        await adapter.aclose()
    assert seen == ["movie-0", "movie-1", "movie-2"]
    assert arrivals[:3] == [0, 1, 2], "the premise: each page read ahead arrives mid-item"
    assert len(arrivals) == 3, f"a page was asked for past the bound: {arrivals}"
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py -k "read_ahead or requested_while or stopping_the_walk or cancelling_the_consumer or last_one_asked_for"`
Expected: FAIL — `test_the_next_page_is_requested_while_the_current_one_is_handled` on its interval message after the 2 s deadline; the stopping case with `TimeoutError` at `parked.wait()`, because nothing is read ahead to park; and the three read-ahead failure cases on a `TimeoutError` at a premise wait or on `_the_read_ahead`'s premise, since no read-ahead is in flight. The consumer-cancellation case and the two last-page cases may pass today; Step 4's plants prove they can fail.

- [ ] **Step 2: Read ahead**

In `src/usher/adapters/emby/adapter.py`, import `aclosing` from `contextlib` beside `AbstractAsyncContextManager`. Add, beside `_segment`, `_settle` and the callback it attaches:

```python
async def _settle(task: asyncio.Task[Any]) -> None:
    """Cancel a read-ahead the walk no longer wants, and retrieve what it raised.

    Waited on rather than awaited, so its failure is never raised over whatever is
    ending the walk, and the caller's own cancellation still propagates. Retrieved
    by a callback, because asyncio reports an exception nobody read as lost, and
    the caller may be cancelled before the read-ahead finishes.
    """
    task.add_done_callback(_retrieve)
    task.cancel()
    await asyncio.wait({task})


def _retrieve(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()
```

Replace `_walk`'s body with the read-ahead form, and add `_read`:

```python
    async def _walk(
        self, query: Mapping[str, str], *, start_index: int
    ) -> AsyncGenerator[dict[str, Any]]:
        """Page one listing to its end, one request ahead of the consumer.

        The next page is asked for as soon as a page arrives and before its items
        are yielded, so the source answers while the caller writes; the request
        outstanding when the consumer stops is cancelled. `start_index` is the
        resume point (#41), never defaulted: every caller states its own.
        """
        path = f"/Users/{_segment(await self._session.user_id())}/Items"
        window = OffsetWindow(limit=self._page_size, start=start_index)
        pending = self._read(path, query, window.start, count=True)
        try:
            # `for`, not `while True`: the bound is part of the loop, and the raise
            # below is reachable only by a walk that never ended.
            for number in range(1, self._max_pages + 1):
                body = await pending
                entries = body.get("Items")
                if not isinstance(entries, list):
                    # Not a truncation: a caller must be able to tell "the library
                    # ended" from "that was not a listing at all".
                    raise PortDataMalformed(
                        "Emby's item listing carried no Items array",
                        detail=f"StartIndex={window.start}",
                    )
                page = window.receive(entries, body.get("TotalRecordCount"))
                if page.shifted:
                    logger.warning(
                        "{source}'s listing shifted by at least {overlap} items before "
                        "StartIndex={start}; an item shifted further was not read, and the "
                        "next full walk reads it",
                        source=self._source.name,
                        overlap=window.overlap,
                        start=window.start,
                    )
                if not page.ended and number < self._max_pages:
                    pending = self._read(path, query, window.advance(), count=False)
                for payload in page.fresh:
                    yield payload
                if page.ended:
                    return
        finally:
            await _settle(pending)
        raise PortDataMalformed(
            "Emby's item listing never ended; the server appears to ignore StartIndex "
            "or to cap Limit far below the page size",
            detail=f"gave up after {self._max_pages} pages at StartIndex={window.start}",
        )

    def _read(
        self, path: str, query: Mapping[str, str], start: int, *, count: bool
    ) -> asyncio.Task[dict[str, Any]]:
        """One page's request, started now and awaited when the walk reaches it."""
        params = {
            **query,
            "StartIndex": str(start),
            "Limit": str(self._page_size),
            "EnableTotalRecordCount": "true" if count else "false",
        }
        return asyncio.create_task(self._page(path, params, start))
```

The last page under `max_pages` requests nothing further, so `test_a_server_that_ignores_start_index_ends_the_walk_rather_than_running_forever` still sees exactly three requests.

Close the walk with its consumer. In `_list_items`:

```python
        query = _listing_query(LIBRARY_SINCE_PARAM, since)
        # `aclosing`, so a consumer that stops closes the walk now, read-ahead
        # included, rather than whenever the generator is collected.
        async with aclosing(self._walk(query, start_index=0)) as payloads:
            async for payload in payloads:
                item = to_source_item(payload)
                if item is not None:
                    yield item
```

and the same `async with aclosing(self._walk(query, start_index=start_index)) as payloads:` shape in `_watch_state`.

- [ ] **Step 3: Run the adapter and contract suites**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_contract.py tests/unit/test_adapters_emby_session.py`
Expected: PASS. `test_a_walk_whose_adapter_is_closed_under_it_is_not_retried` still sees one request: the read-ahead task has not started when the consumer closes the adapter, so its first step finds the session closed.

- [ ] **Step 4: Plant and verify**

Each plant names the cases it fails. Every case fails under at least one plant of its own.

1. `_settle` reduced to `pass`. Expect both arms of `test_stopping_the_walk_cancels_the_request_it_read_ahead`, `test_a_read_ahead_that_failed_is_retrieved_when_the_walk_stops`, `test_a_read_ahead_that_fails_once_cancelled_is_retrieved_too` and `test_a_read_ahead_that_fails_after_its_walk_was_cancelled_is_retrieved` to fail.
2. `task.cancel()` removed from `_settle`. Expect both stopping arms, `test_a_read_ahead_that_fails_once_cancelled_is_retrieved_too` and `test_a_read_ahead_that_fails_after_its_walk_was_cancelled_is_retrieved` to fail, each on a `TimeoutError` at a close or premise deadline rather than a hang.
3. `_retrieve` attached only in an `except asyncio.CancelledError`. Expect `test_a_read_ahead_that_fails_once_cancelled_is_retrieved_too` alone to fail ("nobody retrieved").
4. The `add_done_callback(_retrieve)` dropped. Expect `test_a_read_ahead_that_fails_once_cancelled_is_retrieved_too` and `test_a_read_ahead_that_fails_after_its_walk_was_cancelled_is_retrieved` to fail ("nobody retrieved").
5. A trailing `if not task.cancelled(): task.exception()` in place of the callback. Expect `test_a_read_ahead_that_fails_after_its_walk_was_cancelled_is_retrieved` alone to fail: its walk is cancelled before the read-ahead finishes, so nothing after the wait runs.
6. A bare `task.exception()` in `_retrieve`, without the `cancelled()` check. Expect both stopping arms to fail ("stopping the walk left asyncio something to report").
7. `_settle` as `task.cancel()` / `try: await task` / `except asyncio.CancelledError: pass`. Expect `test_a_read_ahead_that_failed_is_retrieved_when_the_walk_stops`, `test_a_read_ahead_that_fails_once_cancelled_is_retrieved_too` and `test_a_read_ahead_that_fails_after_its_walk_was_cancelled_is_retrieved` to fail: the read-ahead's refusal is raised out of `aclose()`, or on "DID NOT RAISE CancelledError".
8. The same with `except BaseException: pass`. Expect `test_a_read_ahead_that_fails_once_cancelled_is_retrieved_too` (on its premise that the read-ahead was collected) and `test_a_read_ahead_that_fails_after_its_walk_was_cancelled_is_retrieved` ("DID NOT RAISE CancelledError") to fail.
9. In `_walk`, `body = await pending` wrapped as `try: body = await pending` / `except BaseException as exc: raise PortUnavailable("page failed") from exc`, or the same with `except asyncio.CancelledError`. Expect `test_cancelling_the_consumer_cancels_the_walk_rather_than_being_swallowed` to fail with `PortUnavailable` where `CancelledError` was expected.
10. The request for the next page moved to after the `for payload in page.fresh` loop (no read-ahead). Expect `test_the_next_page_is_requested_while_the_current_one_is_handled` to fail on its interval message, both stopping arms, the three read-ahead failure cases on "the premise: one read-ahead in flight, found 0 tasks", and `test_the_page_that_ends_the_walk_is_the_last_one_asked_for`.
11. `aclosing` removed from `_list_items` (a bare `async for` over `self._walk(...)`). Expect the `list_items` arm of the stopping case to fail — the inner walk is closed only when collected, after the assertion — and the three read-ahead failure cases on their premises.
12. `aclosing` removed from `_watch_state`. Expect the `watch_state` arm of the stopping case alone to fail.
13. `if number < self._max_pages:`, dropping `not page.ended`. Expect `test_the_page_that_ends_the_walk_is_the_last_one_asked_for` alone to fail.
14. `if not page.ended:`, dropping the page bound. Expect `test_the_last_page_the_bound_allows_is_the_last_one_asked_for` alone to fail.
15. `range(1, self._max_pages)`, one page fewer. Expect `test_a_server_that_ignores_start_index_ends_the_walk_rather_than_running_forever` and `test_the_last_page_the_bound_allows_is_the_last_one_asked_for` to fail.
16. In each read-ahead failure case, hold a strong reference to the read-ahead (`_kept = ahead()`). Expect that case alone to fail, on "the premise: the read-ahead was collected".
17. `_parking_second_listing` parking the first listing instead. Expect both stopping arms to fail with `TimeoutError`.
18. In `test_a_read_ahead_that_failed_is_retrieved_when_the_walk_stops`'s handler, `await asyncio.sleep(1)` before refusing, so the refused read is still pending at `aclose`. Expect its premise to fail on its own line: "the refused read had finished failing before the walk stopped". A shorter settle sleep is not that plant: the refused read finishes failing in the event-loop step that sets `refused`.

- [ ] **Step 5: Say it in the PRD and the changelog**

`docs/prd/03-sources-and-sync.md`, "Walking the library", the lead-in becomes:

```markdown
`list_items` and `watch_state` page over the source's own listing, in pages of
`USHER_SOURCE_PAGE_SIZE` items (default **1,000**, at most 1,000), asking for
the next page while the current one is written — one page read ahead, cancelled
when the walk stops:
```

`CHANGELOG.md`, `### Changed`:

```markdown
- **A walk asks for its next page while it writes the current one**, so the
  source and the database work at once. One page is read ahead, and it is
  cancelled when the walk stops.
```

- [ ] **Step 6: Commit**

```bash
git add src/usher/adapters/emby/adapter.py tests/unit/test_adapters_emby_adapter.py \
  docs/prd/03-sources-and-sync.md CHANGELOG.md
git commit -m "emby: read one page ahead while the walk writes the current one"
```

---

### Task 5: A 120 s read budget for listing pages

Spec §1.5. Listing pages get a 120 s read timeout through httpx's per-request `timeout=`; every other call keeps the adapter's 30 s. `failure_detail` already reports the budget a request ran under, so a listing timeout names 120 s.

**One departure from the spec's letter: 120 s is a floor.** A client whose own read budget is longer — an operator who set `USHER_SOURCE_TIMEOUT_SECONDS` above 120 — keeps it for listing pages too, because the case for the budget, that a deep page outlasts it and asking again early only queues a second copy of the slowest query, is stronger there, not weaker. At every default a listing reads for exactly 120 s, and a client with no read limit keeps none. Two session cases below fail without it.

**Files:**
- Modify: `src/usher/adapters/emby/session.py` (`_send`, `request`, `ok`, `json_body`, a new `_timeout`)
- Modify: `src/usher/adapters/emby/adapter.py` (`LISTING_READ_SECONDS`, `_page`, `PAGE_RETRY_WAITS`'s comment)
- Modify: `.env.example`, `web/src/features/operator/Config.settings.ts` (the timeout's description)
- Test: `tests/unit/test_adapters_emby_adapter.py`, `tests/unit/test_adapters_emby_session.py`, `tests/unit/test_cli_errors.py` (a sample error)
- Modify: `docs/prd/03-sources-and-sync.md`, `docs/prd/08-operations.md`, `CHANGELOG.md`, `.claude/rules/emby-push-and-ingest.md`

**Interfaces:**
- Produces: `EmbySession.json_body(method, path, *, params=None, payload=None, op, read_timeout: float | None = None)`, and the same keyword on `ok` and `request`. `None` keeps the client's budget, and a number only ever lengthens its read phase: the request reads for the longer of the two.
- Produces: `LISTING_READ_SECONDS = 120.0` in `adapter.py`. Phase 2's views, counts and seed requests (Tasks 13–14) use it too.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_adapters_emby_adapter.py`, in the walk section:

```python
async def test_a_listing_page_may_take_two_minutes_to_read_and_nothing_else_may() -> None:
    """A deep page of a big library outlasts the 30 s every other request gets.

    Read off each request's `timeout` extension, which is what httpx enforces and
    what `failure_detail` reports. Only the read phase moves. Every call site in the
    adapter that sends a request is reached -- `verify`'s three, the walks, `_fetch`
    through `get_item`, and both of `push_watch_state`'s writes -- so a listing's
    budget reaching any of them reads 120 here.
    """
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    captured: list[httpx.Request] = []

    def spy(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return server.handle(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(spy), base_url=SOURCE.base_url, timeout=30.0
    )
    adapter = EmbyAdapter(SOURCE, CREDENTIALS, client=client)
    try:
        await adapter.verify()
        _ = [item async for item in adapter.list_items()]
        _ = [state async for state in adapter.watch_state(since=T0)]
        await adapter.get_item("movie-0")
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=600, played=True)
        )
    finally:
        await adapter.aclose()
    budgets = {
        (request.method, redact_path(request.url.path), request.extensions["timeout"]["read"])
        for request in captured
    }
    assert budgets == {
        ("POST", "/Users/AuthenticateByName", 30.0),
        ("GET", "/System/Info/Public", 30.0),
        ("GET", "/System/Info", 30.0),
        ("GET", "/Users/{user_id}", 30.0),
        ("GET", "/Users/{user_id}/Items", 120.0),
        ("GET", "/Users/{user_id}/Items/{item_id}", 30.0),
        ("POST", "/Users/{user_id}/Items/{item_id}/UserData", 30.0),
        ("POST", "/Users/{user_id}/PlayedItems/{item_id}", 30.0),
    }
    listing = next(request for request in captured if request.url.path.endswith("/Items"))
    assert listing.extensions["timeout"] == {
        "connect": 30.0,
        "read": 120.0,
        "write": 30.0,
        "pool": 30.0,
    }


async def test_a_listing_that_times_out_says_it_had_two_minutes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        raise httpx.ReadTimeout("", request=request)

    adapter = _on(handler, waits=_Waits())
    try:
        with pytest.raises(PortUnavailable) as caught:
            _ = [item async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert str(caught.value) == (
        "GET /Users/{user_id}/Items failed: ReadTimeout after 120.0s (read budget) "
        "(gave up after 6 attempts over 465s)"
    )
```

In `tests/unit/test_adapters_emby_session.py`, let `_session` take a client, so a case can watch what goes out — its first statement becomes `client = client or httpx.AsyncClient(transport=server.transport(), base_url="https://emby.invalid")`, with `client: httpx.AsyncClient | None = None` added after `clock` in its keyword-only parameters. Add a spying client builder after it:

```python
def _spied(
    server: FakeEmbyServer,
    captured: list[httpx.Request],
    *,
    timeout: httpx.Timeout | float = 30.0,
) -> httpx.AsyncClient:
    """A client over `server` that records every request, with a 30 s budget throughout.

    `timeout` replaces that budget, for a case about a client configured otherwise.
    """

    def spy(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return server.handle(request)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(spy), base_url="https://emby.invalid", timeout=timeout
    )
```

Then append:

```python
async def test_a_read_budget_lengthens_only_the_read_phase() -> None:
    server = FakeEmbyServer()
    captured: list[httpx.Request] = []
    session, client = _session(server, client=_spied(server, captured))
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info", read_timeout=120.0)
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    budgets = [r.extensions["timeout"] for r in captured if r.url.path == SYSTEM_INFO_PATH]
    assert budgets == [
        {"connect": 30.0, "read": 120.0, "write": 30.0, "pool": 30.0},
        {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0},
    ]


async def test_a_read_budget_never_shortens_a_client_read_that_is_longer() -> None:
    """A named read budget is a floor: an operator who set 300 s keeps 300 s."""
    server = FakeEmbyServer()
    captured: list[httpx.Request] = []
    session, client = _session(server, client=_spied(server, captured, timeout=300.0))
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info", read_timeout=120.0)
    finally:
        await client.aclose()
    budgets = [r.extensions["timeout"] for r in captured if r.url.path == SYSTEM_INFO_PATH]
    assert budgets == [{"connect": 300.0, "read": 300.0, "write": 300.0, "pool": 300.0}]


async def test_a_client_with_no_read_limit_keeps_none_under_a_read_budget() -> None:
    """No read limit is longer than any budget a caller can name."""
    server = FakeEmbyServer()
    captured: list[httpx.Request] = []
    unlimited = httpx.Timeout(30.0, read=None)
    session, client = _session(server, client=_spied(server, captured, timeout=unlimited))
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info", read_timeout=120.0)
    finally:
        await client.aclose()
    budgets = [r.extensions["timeout"] for r in captured if r.url.path == SYSTEM_INFO_PATH]
    assert budgets == [{"connect": 30.0, "read": None, "write": 30.0, "pool": 30.0}]
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_session.py -k "two_minutes or read_budget"`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'read_timeout'`, and the listing's read budget reading `30.0`.

- [ ] **Step 2: Thread the budget through the session**

In `src/usher/adapters/emby/session.py`, add to `EmbySession`:

```python
    def _timeout(self, read: float | None) -> httpx.Timeout:
        """The client's budget, whose read phase a named read budget only ever lengthens.

        A client with no read limit keeps none, and the other three phases are always
        the client's.
        """
        base = self._client.timeout
        if read is None:
            return base
        if base.read is None:
            return base
        return httpx.Timeout(
            connect=base.connect, read=max(read, base.read), write=base.write, pool=base.pool
        )
```

Give `_send` a `read_timeout: float | None = None` keyword and build the request with it:

```python
            request = self._client.build_request(
                method,
                path,
                params=params,
                json=payload,
                headers=dict(headers),
                timeout=self._timeout(read_timeout),
            )
```

Give `request`, `ok` and `json_body` the same keyword (`read_timeout: float | None = None`, after `op`), passing it down: `request` hands it to **both** of its `_send` calls (the first attempt and the one after re-authenticating), `ok` to `request`, `json_body` to `ok`. `request`'s docstring says `read_timeout` can lengthen the client's read budget for the request, never shorten it, since the name alone reads like an exact budget.

In `src/usher/adapters/emby/adapter.py`, beside `PAGE_RETRY_WAITS`:

```python
# A listing page may read for this long, or for the client's budget if that is longer;
# every other request keeps the client's. A timed-out request does not stop the
# server's query, so asking again early only adds a second copy of the slowest query
# there is.
LISTING_READ_SECONDS = 120.0
```

and in `_page`:

```python
                return await self._session.json_body(
                    "GET", path, params=params, op="list", read_timeout=LISTING_READ_SECONDS
                )
```

`PAGE_RETRY_WAITS`'s comment says "about eight minutes of waits across six attempts": the waits are eight minutes, and the attempts between them now take up to 120 s each.

Run the Step 1 command. Expected: PASS.

- [ ] **Step 3: Plant and verify**

1. In `_timeout`, return `base` unconditionally. Expect both new adapter cases and `test_a_read_budget_lengthens_only_the_read_phase` to fail.
2. In `request`, pass `read_timeout` to the first `_send` only. Add this case to `test_adapters_emby_session.py` first, then plant:

```python
async def test_a_re_authenticated_request_keeps_its_read_budget() -> None:
    server = FakeEmbyServer()
    captured: list[httpx.Request] = []
    session, client = _session(server, client=_spied(server, captured))
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        server.expire_session()
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info", read_timeout=120.0)
    finally:
        await client.aclose()
    reads = [r.extensions["timeout"]["read"] for r in captured if r.url.path == SYSTEM_INFO_PATH]
    assert reads == [30.0, 120.0, 120.0], "the request after re-authenticating lost its budget"
```

Expect it to fail under the plant on its message.

3. In `_timeout`, `read=read` in place of the `max`. Expect `test_a_read_budget_never_shortens_a_client_read_that_is_longer` to fail.
4. In `_timeout`, drop the no-limit guard and take `max(read, base.read or 0.0)`. Expect `test_a_client_with_no_read_limit_keeps_none_under_a_read_budget` to fail.
5. In `_timeout`, `httpx.Timeout(read)` in place of the four-phase build. Expect `test_a_read_budget_lengthens_only_the_read_phase` to fail.
6. Pass `read_timeout=LISTING_READ_SECONDS` on `verify`'s `/System/Info` request, then on `push_watch_state`'s PlayedItems POST. Expect `test_a_listing_page_may_take_two_minutes_to_read_and_nothing_else_may` to fail under each.

- [ ] **Step 4: Say it where the timeout is described**

At the defaults, a page whose every attempt connects and then stalls now holds a walk for six reads of 120 s and 465 s of waits, about 20 minutes, where 30 s reads held it about 11. So every sentence that said a timed-out page holds a walk "about eight minutes" says this instead. A 5xx or a refused connection still fails fast, so the older CHANGELOG bullet about outages stays as it is. `composition.py` builds the TMDb client from the same setting, so both descriptions of it name TMDb.

`.env.example`, above `USHER_SOURCE_TIMEOUT_SECONDS=30`:

```
# Seconds one request to a source, or to TMDb, may take before it is a failure.
# A library listing page gets 120 s to read, or this setting if it is longer.
```

`web/src/features/operator/Config.settings.ts`, the `USHER_SOURCE_TIMEOUT_SECONDS` entry's `about`:

```ts
    about:
      'How long one request to a media server, or to TMDb, may take before it is a failure. A library listing page gets 120 s to read, or this setting if it is longer.',
```

`docs/prd/03-sources-and-sync.md`, "Walking the library", the retry bullet becomes:

```markdown
- **A page that fails as unreachable is asked for again** — a 5xx, a 408, a
  refused or dropped connection, a timeout — after 15, 30, 60, 120 and 240 s,
  about eight minutes of waiting. A listing page has 120 s to answer once
  connected, or `USHER_SOURCE_TIMEOUT_SECONDS` (default 30) if that is longer,
  so at the defaults a page whose every attempt connects and then stalls holds
  the walk about 20 minutes. Connecting, and every other request, has
  `USHER_SOURCE_TIMEOUT_SECONDS`. A 429 is asked for again on the same schedule,
  waiting out its `Retry-After` when that is longer, up to 240 s a time. The
  sixth failure ends the walk, and its error says how many attempts it made over
  how long; each page gets its own six. Any other 4xx, an answer that is not a
  listing, a rejected credential and a closed adapter fail at once.
```

`docs/prd/08-operations.md`, the "Source unreachable" row becomes:

```markdown
| Source unreachable | Catalog fully browsable. Playback → 503 `source_unavailable`. Availability goes stale, not wrong. A walk in progress waits out about eight minutes of it on the page it hit before failing, or, at the defaults, about 20 when the source accepts connections and then stalls ([03](03-sources-and-sync.md)) |
```

`.claude/rules/emby-push-and-ingest.md`, the "Nor may a blip" bullet becomes:

```markdown
- **Nor may a blip: `EmbyAdapter._page` retries an outage or a 429** through
  eight minutes of waits, as a failed item walk restarts from the top — never a
  `RequestRefused`, the 4xx `EmbySession.ok` still reports as `PortUnavailable`.
  A test that fails a walk on purpose injects `sleep=instant_sleep`
  (`tests/fakes/emby_harness.py`), or it sits through those minutes as a hang.
```

`CHANGELOG.md`, `### Changed`:

```markdown
- **A library listing page may take 120 s**, where every other request still
  gets `USHER_SOURCE_TIMEOUT_SECONDS`; a timeout set longer than 120 s applies
  to listing pages too. Deep pages of a large library outlast 30 s, and asking
  again early only queued a second copy of the slowest query. At the defaults,
  a source that accepts connections and then stalls now holds a walk about 20
  minutes before it fails, where it was about 11.
```

`tests/unit/test_cli_errors.py`, the sample `sync_runs.error` in `test_a_failed_watch_lane_is_a_non_zero_exit_without_the_retraction_hint` reads as a listing timeout now does. 1065 s is five more reads of 120 s and the 465 s of waits, timed from the first failure:

```python
error=(
    "GET /Users/{user_id}/Items failed: ReadTimeout after 120.0s (read budget) "
    "(gave up after 6 attempts over 1065s)"
),
```

- [ ] **Step 5: Run the suites and the console gate**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_session.py tests/unit/test_adapters_emby_contract.py tests/unit/test_console_settings_catalogue.py tests/unit/test_cli_errors.py`
Expected: PASS.
Run, from `web/` in one command: `npm run verify && npm run e2e && npm run e2e:visual`
Expected: PASS, with no snapshot written.

- [ ] **Step 6: Commit**

```bash
git add src/usher/adapters/emby/session.py src/usher/adapters/emby/adapter.py .env.example \
  web/src/features/operator/Config.settings.ts tests/unit/test_adapters_emby_adapter.py \
  tests/unit/test_adapters_emby_session.py tests/unit/test_cli_errors.py \
  docs/prd/03-sources-and-sync.md docs/prd/08-operations.md CHANGELOG.md \
  .claude/rules/emby-push-and-ingest.md
git commit -m "emby: give a listing page 120 s to read, and every other request 30"
```

---

### Task 6: The first watch walk asks only for what was watched

Spec §1.6. With `since=None`, `watch_state` makes two walks — `Filters=IsPlayed`, then `Filters=IsResumable`, skipping ids the first yielded — each paged as in Tasks 2–4. With a `since`, nothing changes. The port contract changes to match: with no `since`, every **non-default** state (played, or holding a resume position) and nothing else.

Live facts, recorded 2026-10-01 on the account behind Shared Emby: `IsPlayed` listed 765 items, all played, 21 of them also holding a position; `IsResumable` listed 208, all holding a position, 21 of them played. The two overlap by exactly those 21. A filtered listing pages normally: the page at `StartIndex` 700 of the 765 held 65.

**Review Focus 3** lives here: a server that ignores `Filters` lists the whole library on the first walk. The adapter still yields only non-default states, and skips the second walk, because the first has already seen every resume position. It judges the played listing unfiltered only when its unwatched entries strictly outnumber its watched ones, and logs a WARNING when it does. So a few entries reading unwatched among watched ones are not enough: a Series marked played that has since gained an episode may match `IsPlayed` and still read unwatched, and taking it for an unfiltered server would cost the walk every resume position. A played listing of nothing but such entries is still judged unfiltered. A tie or an empty listing goes on to the second listing, whose repeats the `yielded` set drops.

The fake adapter changes shape with the port, which is what breaks eight watch-sync cases and one integration case below: each walked a first walk over items with no state and expected zeros. They become deltas — a completed run first — which is the shape whose behaviour they were written about.

**Files:**
- Modify: `src/usher/adapters/emby/adapter.py` (`FIRST_WALK_FILTERS`, `_listing_query`'s `filters`, `_watch_state`, `_listed_state`)
- Modify: `src/usher/ports/source.py` (`watch_state`'s docstring)
- Modify: `tests/fakes/emby_server.py` (`_ordered`, a new `_passes`, a new `set_unplayed_episodes`)
- Modify: `tests/fakes/source_adapter.py` (`_walk_states`, a new `_watched`)
- Modify: `tests/fixtures/emby/README.md` (three rows, and a paragraph on `set_unplayed_episodes`)
- Modify: `tests/contract/source_adapter_contract.py`
- Modify: `tests/unit/test_adapters_emby_contract.py` (the contract's case count)
- Modify: `tests/unit/test_adapters_emby_adapter.py`, `tests/unit/test_fakes_emby_server.py`
- Modify: `tests/unit/test_services_watch_sync.py` (`_Fixture.given_completed_walk`, eight cases, and the four resume cases that seed a run)
- Modify: `tests/integration/test_services_watch_sync.py` (one case)
- Modify: `docs/prd/03-sources-and-sync.md`, `CHANGELOG.md`, `.claude/rules/emby-push-and-ingest.md`
- Modify: wherever this makes prose false. A watch walk no longer yields a record per library item, and dropping a zero state is a bug only in a delta; fifteen more files said otherwise: `src/usher/ports/repository/{media_item,sync,taste}.py`, `src/usher/services/{push,watch_sync}.py`, `tests/contract/{episode,media_item,sync_run,watch_state}_repository_contract.py`, `tests/integration/{test_ingest_end_to_end,test_watch_state_repository}.py`, `tests/unit/{test_api_lanes,test_ports_source,test_services_push}.py` and `docs/prd/09-roadmap.md`

**Interfaces:**
- Consumes: Task 2's `_listing_query`, Task 4's `aclosing` shape.
- Produces: `FIRST_WALK_FILTERS = ("IsPlayed", "IsResumable")`; `_listing_query(since_param, since, *, filters: str | None = None)`. Phase 2's SEED unit (Task 14) reuses both.
- Produces: `_Fixture.given_completed_walk(*, at: AwareDatetime = T0) -> None` in the watch-sync unit tests. Task 7 uses it.

- [ ] **Step 1: Record the filter facts**

Add three rows to the `## Listing protocol (fast first sync)` table in `tests/fixtures/emby/README.md`:

```markdown
| `Filters=IsPlayed` | only items whose `UserData.Played` is true; some also hold a resume position | the first watch walk's first listing |
| `Filters=IsResumable` | only items with a non-zero `PlaybackPositionTicks`, **played or not**, so it overlaps `IsPlayed` (21 of 208 on the recorded account) | the first watch walk's second listing, deduplicated by id |
| A filtered listing's paging | the filter applies before `StartIndex`/`Limit`: the page at 700 of a 765-item filtered set held 65 | a filtered walk pages like any other |
```

- [ ] **Step 2: Write the failing fake-server tests**

Append to `tests/unit/test_fakes_emby_server.py`:

```python
# --- Filters ---------------------------------------------------------------


def _given_watched(driver: _Driver) -> None:
    """Four items: played, in progress, both, and untouched."""
    for name, state in (
        ("played", (0, True)),
        ("resuming", (640, False)),
        ("both", (90, True)),
        ("untouched", None),
    ):
        driver.server.add_item(replace(MOVIE, external_id=name, name=name), T0)
        if state is not None:
            position, played = state
            driver.server.set_watch_state(
                SourceWatchState(external_id=name, position_seconds=position, played=played)
            )


async def _filtered(driver: _Driver, filters: str, **paging: str) -> dict[str, Any]:
    return await driver.session.json_body(
        "GET",
        f"/Users/{USER_ID}/Items",
        params={"Recursive": "true", "Filters": filters, "SortBy": "SortName", **paging},
        op="list",
    )


async def test_a_played_filter_lists_only_played_items(driver: _Driver) -> None:
    _given_watched(driver)
    body = await _filtered(driver, "IsPlayed")
    assert [entry["Id"] for entry in body["Items"]] == ["both", "played"]


async def test_a_resumable_filter_lists_every_item_with_a_position_played_or_not(
    driver: _Driver,
) -> None:
    _given_watched(driver)
    body = await _filtered(driver, "IsResumable")
    assert [entry["Id"] for entry in body["Items"]] == ["both", "resuming"]


async def test_a_filter_applies_before_the_page_is_cut(driver: _Driver) -> None:
    _given_watched(driver)
    body = await _filtered(driver, "IsPlayed", StartIndex="1", Limit="1")
    assert [entry["Id"] for entry in body["Items"]] == ["played"]
    assert body["TotalRecordCount"] == 2


async def test_a_played_series_with_an_unplayed_episode_is_listed_as_played_and_reads_unwatched(
    driver: _Driver,
) -> None:
    """Both routes derive its `Played` from its episodes; `IsPlayed` reads the stored flag."""
    series = SourceItem(external_id="series", name="series", kind=SourceItemKind.SERIES, year=2004)
    driver.server.add_item(series, T0)
    driver.server.set_watch_state(
        SourceWatchState(external_id="series", position_seconds=0, played=True)
    )
    driver.server.set_unplayed_episodes("series", 1)
    body = await _filtered(driver, "IsPlayed")
    single = await driver.payload("series")
    keys = ("Played", "UnplayedItemCount", "PlaybackPositionTicks")
    assert [entry["Id"] for entry in body["Items"]] == ["series"]
    assert [body["Items"][0]["UserData"].get(key) for key in keys] == [False, 1, 0]
    assert [single["UserData"].get(key) for key in keys] == [False, 1, 0]
    assert driver.server.recorded_watch_state("series") == (0, True)


def test_only_a_series_takes_unplayed_episodes() -> None:
    server = FakeEmbyServer()
    server.add_item(MOVIE, T0)
    with pytest.raises(ValueError, match="only a Series has episodes"):
        server.set_unplayed_episodes(MOVIE.external_id, 1)
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_fakes_emby_server.py -k "filter or unplayed"`
Expected: FAIL — every item listed, `untouched` included; and the two Series cases on `AttributeError: 'FakeEmbyServer' object has no attribute 'set_unplayed_episodes'`.

- [ ] **Step 3: Teach the fake `Filters`**

`tests/fakes/emby_server.py`, in `_ordered`, read the filters beside `since` and skip what they exclude:

```python
        wanted = {name for name in (params.get("Filters") or "").split(",") if name}
```

```python
            if since is not None and _stamp(changed_at) < since:
                continue
            if not self._passes(external_id, wanted):
                continue
```

and add beside `_state_of`:

```python
    def _passes(self, external_id: str, filters: set[str]) -> bool:
        """`Filters`, applied before the page is cut, as the live server does.

        `IsPlayed` is the played flag; `IsResumable` is a non-zero resume position,
        played or not. A filter this fake does not model filters nothing.
        """
        state = self._state_of(external_id)
        if "IsPlayed" in filters and not state.played:
            return False
        return not ("IsResumable" in filters and state.position_seconds <= 0)
```

Then the control for a Series whose listed `Played` disagrees with the filter. In `__init__`, beside `self._states`:

```python
        self._unplayed_episodes: dict[str, int] = {}
```

in `remove_item`, beside the other pops:

```python
        self._unplayed_episodes.pop(external_id, None)
```

beside `set_watch_state`:

```python
    def set_unplayed_episodes(self, external_id: str, count: int) -> None:
        """Give a seeded Series `count` unplayed episodes, which its `Played` is rendered from.

        Emby derives a Series' `Played` from its episodes -- `series_item.json` says
        `Played: false` beside `UnplayedItemCount: 12` -- while `Filters` here still reads
        the flag `set_watch_state` stored. So a Series marked played that has since gained
        an episode matches `IsPlayed` and is listed as unwatched. No live listing has shown
        one; it is the entry a first watch walk must not take for a server that ignored
        `Filters`.
        """
        item, _ = self._items[external_id]
        if item.kind is not SourceItemKind.SERIES:
            raise ValueError(f"{external_id} is a {item.kind.value}; only a Series has episodes")
        self._unplayed_episodes[external_id] = count
```

and in `_user_data`, after the dict is built and before the `for_listing` branch:

```python
        episodes = self._unplayed_episodes.get(external_id)
        if episodes is not None:
            # Derived on both routes; `_passes` keeps reading the stored flag.
            user_data["Played"] = episodes == 0
            user_data["UnplayedItemCount"] = episodes
```

Run the Step 2 command. Expected: PASS.

- [ ] **Step 4: Write the failing contract and adapter tests**

In `tests/contract/source_adapter_contract.py`, replace `test_watch_state_emits_a_zero_state_rather_than_skipping_it` with:

```python
    async def test_a_delta_walk_emits_a_zero_state_rather_than_skipping_it(
        self, harness: SourceHarness
    ) -> None:
        """Filtering empty states out of a delta looks like a saving and is a correctness bug.

        Un-marking something played *is* an all-zero state, so an adapter that skipped
        them could never propagate a reset -- the delta walk would find the changed item
        and then discard exactly the record describing the change.
        """
        await harness.given_item(MOVIE, changed_at=T1)
        states = {state.external_id async for state in harness.adapter.watch_state(since=T1)}
        assert "movie-1" in states

    async def test_a_first_walk_yields_every_watched_state_once_and_nothing_else(
        self, harness: SourceHarness
    ) -> None:
        """With no `since`, only played or in-progress states, each exactly once.

        A first walk over a million items is a few hundred states. An item it does not
        yield is not asserted unplayed, so a default state has no place in it, and an
        item both played and in progress is still one record.
        """
        await self._seed_library(harness)
        for external_id, position, played in (
            ("filler-1", 0, True),
            ("filler-3", 640, False),
            ("filler-5", 90, True),
        ):
            await harness.given_watch_state(
                SourceWatchState(external_id=external_id, position_seconds=position, played=played)
            )
        everything = {state.external_id async for state in harness.adapter.watch_state(since=T0)}
        assert len(everything) == 7, "the premise: four of the seven hold a default state"
        walked = [state.external_id async for state in harness.adapter.watch_state()]
        assert sorted(walked) == ["filler-1", "filler-3", "filler-5"]
```

and in `test_watch_state_raises_rather_than_truncating`, give the seven fillers something to walk — after `await self._seed_library(harness)`:

```python
        for index in range(7):
            await harness.given_watch_state(
                SourceWatchState(external_id=f"filler-{index}", position_seconds=0, played=True)
            )
```

The contract now has one case more. In `tests/unit/test_adapters_emby_contract.py`, `test_both_implementations_run_the_same_assertions`: `assert len(cases) == 51` becomes `== 52`, and both `51`s in its docstring become `52`.

In `tests/unit/test_adapters_emby_adapter.py`, add after `test_watch_state_is_attributed_to_the_authenticated_user`:

```python
async def test_a_first_watch_walk_lists_played_then_in_progress_items() -> None:
    server = FakeEmbyServer()
    for index in range(3):
        server.add_item(_movie(index), T0)
    server.set_watch_state(SourceWatchState(external_id="movie-0", position_seconds=0, played=True))
    server.set_watch_state(
        SourceWatchState(external_id="movie-2", position_seconds=640, played=True)
    )
    captured: list[httpx.Request] = []

    def spy(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return server.handle(request)

    adapter = _on(spy)
    try:
        walked = [state.external_id async for state in adapter.watch_state()]
    finally:
        await adapter.aclose()
    listings = [request.url.params for request in captured if request.url.path.endswith("/Items")]
    assert [params.get("Filters") for params in listings] == ["IsPlayed", "IsResumable"]
    assert not any("MinDateLastSavedForUser" in params for params in listings)
    assert walked == ["movie-0", "movie-2"], "a state both played and in progress came twice"


async def test_a_server_that_ignores_the_filter_still_yields_only_watched_states_once() -> None:
    """A server that ignored `Filters` lists the whole library on the first listing.

    Most of a library is unwatched, which is how the walk tells. It still yields only
    played or in-progress states, and stops there: the second listing would repeat the
    first, item for item.
    """
    entries: list[dict[str, Any]] = [
        {
            "Id": "movie-0",
            "Type": "Movie",
            "Name": "A",
            "UserData": {"PlaybackPositionTicks": 0, "Played": True},
        },
        {
            "Id": "movie-1",
            "Type": "Movie",
            "Name": "B",
            "UserData": {"PlaybackPositionTicks": 0, "Played": False},
        },
        {
            "Id": "movie-2",
            "Type": "Movie",
            "Name": "C",
            "UserData": {"PlaybackPositionTicks": 6_400_000_000, "Played": False},
        },
        {
            "Id": "movie-3",
            "Type": "Movie",
            "Name": "D",
            "UserData": {"PlaybackPositionTicks": 0, "Played": False},
        },
        {
            "Id": "movie-4",
            "Type": "Movie",
            "Name": "E",
            "UserData": {"PlaybackPositionTicks": 0, "Played": False},
        },
    ]
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        asked.append(request.url.params.get("Filters", ""))
        return httpx.Response(200, json={"Items": entries, "TotalRecordCount": len(entries)})

    adapter = _on(handler)
    try:
        walked = [
            (state.external_id, state.played, state.position_seconds)
            async for state in adapter.watch_state()
        ]
    finally:
        await adapter.aclose()
    data = [entry["UserData"] for entry in entries]
    unseen = sum(1 for each in data if not each["Played"] and not each["PlaybackPositionTicks"])
    assert unseen > len(data) - unseen, "the premise: the library listed is mostly unwatched"
    assert walked == [("movie-0", True, 0), ("movie-2", False, 640)]
    assert asked == ["IsPlayed"], "the second listing repeated a whole-library walk"


async def test_a_series_reading_unwatched_in_the_played_listing_keeps_the_in_progress_one() -> None:
    """A Series marked played that has since gained an episode may match `IsPlayed`.

    Its entry reads unwatched, with no position. One such entry beside one played
    movie is a tie, not a server that ignored `Filters`, so the walk still asks for
    the in-progress listing and yields the resume position it holds.
    """
    server = FakeEmbyServer()
    series = SourceItem(
        external_id="series-0", name="Series 0", kind=SourceItemKind.SERIES, year=2004, added_at=T0
    )
    for item in (_movie(0), _movie(1), series):
        server.add_item(item, T0)
    server.set_watch_state(SourceWatchState(external_id="movie-0", position_seconds=0, played=True))
    server.set_watch_state(
        SourceWatchState(external_id="movie-1", position_seconds=640, played=False)
    )
    server.set_watch_state(
        SourceWatchState(external_id="series-0", position_seconds=0, played=True)
    )
    server.set_unplayed_episodes("series-0", 1)
    asked: list[str] = []
    served: dict[str, list[dict[str, Any]]] = {}

    def spy(request: httpx.Request) -> httpx.Response:
        response = server.handle(request)
        if request.url.path.endswith("/Items"):
            asked.append(request.url.params["Filters"])
            served[asked[-1]] = response.json()["Items"]
        return response

    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter = _on(spy)
        try:
            walked = [
                (state.external_id, state.played, state.position_seconds)
                async for state in adapter.watch_state()
            ]
        finally:
            await adapter.aclose()
    finally:
        logger.remove(handle)
    played_listing = sorted(
        (
            entry["Id"],
            entry["UserData"]["Played"],
            entry["UserData"]["PlaybackPositionTicks"],
            entry["UserData"].get("UnplayedItemCount"),
        )
        for entry in served["IsPlayed"]
    )
    assert played_listing == [("movie-0", True, 0, None), ("series-0", False, 0, 1)], (
        "the premise: the played listing holds a played movie and a Series reading unwatched"
    )
    assert server.recorded_watch_state("movie-1") == (640, False), (
        "the premise: the in-progress listing holds one unplayed movie at 640 s"
    )
    assert asked == ["IsPlayed", "IsResumable"], (
        "one Series reading unwatched cost the walk its in-progress listing"
    )
    assert walked == [("movie-0", True, 0), ("movie-1", False, 640)]
    assert lines == [], "a server that honours Filters was logged as ignoring them"


@pytest.mark.parametrize(
    ("unwatched", "watched", "listings", "warnings"),
    [
        pytest.param(
            2,
            1,
            ["IsPlayed"],
            [
                "Living Room Emby appears to ignore Filters: its first watch walk's played "
                "listing was mostly unwatched (2 unwatched skipped, 1 watched yielded), so "
                "the walk did not ask for the in-progress listing"
            ],
            id="unwatched-outnumber-watched",
        ),
        pytest.param(1, 1, ["IsPlayed", "IsResumable"], [], id="a-tie"),
    ],
)
async def test_a_played_listing_is_judged_unfiltered_only_when_mostly_unwatched(
    unwatched: int, watched: int, listings: list[str], warnings: list[str]
) -> None:
    """A server ignoring `Filters` lists the whole library, and a library is mostly unwatched.

    So the walk lists once, and says so, only when the played listing's unwatched entries
    strictly outnumber its watched ones. On a tie it asks for the second listing as well,
    dropping by id what the first already yielded.
    """
    entries: list[dict[str, Any]] = [
        {
            "Id": f"movie-{index}",
            "Type": "Movie",
            "Name": f"Movie {index}",
            "UserData": {"PlaybackPositionTicks": 0, "Played": index >= unwatched},
        }
        for index in range(unwatched + watched)
    ]
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authenticated = _authenticated(request)
        if authenticated is not None:
            return authenticated
        asked.append(request.url.params.get("Filters", ""))
        return httpx.Response(200, json={"Items": entries, "TotalRecordCount": len(entries)})

    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter = _on(handler)
        try:
            walked = [(state.external_id, state.played) async for state in adapter.watch_state()]
        finally:
            await adapter.aclose()
    finally:
        logger.remove(handle)
    stored = {entry["Id"]: entry["UserData"] for entry in entries}
    unseen = [
        key for key, data in stored.items() if not (data["Played"] or data["PlaybackPositionTicks"])
    ]
    seen = [key for key in stored if key not in unseen]
    assert (len(unseen), len(seen)) == (unwatched, watched), (
        f"the premise: the entries stored are {unwatched} unwatched and {watched} watched"
    )
    assert asked == listings, "the played listing was misjudged as filtered or as unfiltered"
    assert len(lines) == len(warnings), "the walk listed once without a WARNING, or warned anyway"
    assert [line.rstrip("\n") for line in lines] == warnings, "the WARNING miscounted the entries"
    assert walked == [(key, True) for key in seen], "a watched state was lost or yielded twice"
```

A library is mostly unwatched, which is how the walk tells a server that ignored `Filters`: the first case lists three unwatched entries against two watched, and asserts that premise before its expectations.

In the same file, `test_a_library_walk_and_a_watch_state_walk_filter_on_different_stamps` gains a last line:

```python
    assert "Filters" not in listings[1].url.params, "a delta is not filtered on watched-ness"
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_contract.py tests/unit/test_source_adapter_contract.py tests/unit/test_adapters_emby_adapter.py -k "first_walk or first_watch or ignores_the_filter or mostly_unwatched or reading_unwatched or zero_state or truncating"`
Expected: FAIL — `test_a_first_walk_yields_every_watched_state_once_and_nothing_else` on both arms (all seven walked), the four adapter cases (no `Filters` sent).

- [ ] **Step 5: Implement the filtered first walk**

`src/usher/adapters/emby/adapter.py`, beside `USER_DATA_SINCE_PARAM`:

```python
# A first watch walk asks only for what the account has watched: played items,
# then items holding a resume position. The two overlap.
FIRST_WALK_FILTERS = ("IsPlayed", "IsResumable")
```

`_listing_query` takes the filter:

```python
def _listing_query(
    since_param: str, since: AwareDatetime | None, *, filters: str | None = None
) -> dict[str, str]:
    """A listing's parameters, less the paging `_walk` adds to each request."""
    query = {
        "Recursive": "true",
        "IncludeItemTypes": ITEM_TYPES,
        "Fields": ITEM_FIELDS,
        "SortBy": SORT_BY,
        "SortOrder": "Ascending",
    }
    if since is not None:
        query[since_param] = emby_datetime(since)
    if filters is not None:
        query["Filters"] = filters
    return query
```

Add beside `_version_of`:

```python
def _listed_state(payload: dict[str, Any], user_id: str) -> SourceWatchState | None:
    # play_history_is_trustworthy=False: this is the listing route.
    return to_watch_state(payload, source_user_id=user_id, play_history_is_trustworthy=False)
```

Replace `watch_state` and `_watch_state`:

```python
    def watch_state(
        self, since: AwareDatetime | None = None, *, start_index: int = 0
    ) -> AsyncIterator[SourceWatchState]:
        """A delta since `since`; with none, the account's played and in-progress items."""
        return self._watch_state(since, start_index)

    async def _watch_state(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[SourceWatchState]:
        user_id = await self._session.user_id()
        if since is not None:
            query = _listing_query(USER_DATA_SINCE_PARAM, since)
            async with aclosing(self._walk(query, start_index=start_index)) as payloads:
                async for payload in payloads:
                    state = _listed_state(payload, user_id)
                    if state is not None:
                        yield state
            return
        # A resumed first walk starts its first listing at `start_index` and its
        # second at 0, which keeps the port's promise.
        yielded: set[str] = set()
        for number, filters in enumerate(FIRST_WALK_FILTERS):
            query = _listing_query(USER_DATA_SINCE_PARAM, None, filters=filters)
            unwatched = watched = 0
            walk = self._walk(query, start_index=start_index if number == 0 else 0)
            async with aclosing(walk) as payloads:
                async for payload in payloads:
                    state = _listed_state(payload, user_id)
                    if state is None or state.external_id in yielded:
                        continue
                    if not state.played and state.position_seconds <= 0:
                        unwatched += 1
                        continue
                    yielded.add(state.external_id)
                    watched += 1
                    yield state
            if number == 0 and unwatched > watched:
                # Unwatched entries strictly outnumbering watched ones mean the server
                # ignored `Filters` and listed everything, resume positions included, so
                # a second walk would only repeat it. A tie or an empty listing goes on:
                # a Series marked played that has since gained an episode may match
                # `IsPlayed` and still read unwatched.
                logger.warning(
                    "{source} appears to ignore Filters: its first watch walk's played "
                    "listing was mostly unwatched ({unwatched} unwatched skipped, {watched} "
                    "watched yielded), so the walk did not ask for the in-progress listing",
                    source=self._source.name,
                    unwatched=unwatched,
                    watched=watched,
                )
                return
```

`src/usher/ports/source.py`, `watch_state`'s docstring:

```python
        """Watch state: a delta since `since`, or with none the account's watched items.

        With `since`, every item whose watch state changed since then, an all-zero
        state included -- un-marking something played *is* one. With no `since`,
        every **non-default** state, meaning played or holding a resume position,
        and nothing else; an item it does not yield is not asserted unplayed.

        `start_index` counts what this walk yields, never rows of the source's
        unfiltered set. A resumed walk may repeat a record. A record that leaves
        the set between attempts shifts later ones behind the resume point, and
        no walk yields those until their own state changes.
        """
```

`tests/fakes/source_adapter.py` — `_walk_states` takes its candidates from a first-walk order when there is no `since`:

```python
    async def _walk_states(
        self, since: AwareDatetime | None, start_index: int
    ) -> AsyncIterator[SourceWatchState]:
        await self._ready()
        yielded = 0
        skipped = 0
        # With no `since`, the first walk's shape: played items, then in-progress
        # ones, and nothing holding a default state.
        candidates = list(self._items) if since is not None else self._watched()
        for external_id in candidates:
            if since is not None and self._changed_at[external_id] < since:
                continue
            # The skip comes *after* the filter, because `start_index` is an offset into
            # the stream this walk yields rather than into the source's unfiltered set
            # -- which is what a server that filters before it pages hands back, and is
            # exactly what `FakeEmbyServer._list` does (`_ordered` filters, then the
            # slice).
            if skipped < start_index:
                skipped += 1
                continue
            if self._fail_after is not None and yielded >= self._fail_after:
                raise PortUnavailable("source went away mid-walk")
            # A delta yields an all-zero state for an item with none recorded -- see
            # the contract's test_a_delta_walk_emits_a_zero_state_rather_than_skipping_it.
            yield self._states.get(external_id) or SourceWatchState(
                external_id=external_id, position_seconds=0, played=False
            )
            yielded += 1

    def _watched(self) -> list[str]:
        """A first walk's order: played items, then in-progress ones not played."""
        states = [(external_id, self._states.get(external_id)) for external_id in self._items]
        played = [external_id for external_id, state in states if state and state.played]
        resuming = [
            external_id
            for external_id, state in states
            if state and not state.played and state.position_seconds > 0
        ]
        return played + resuming
```

- [ ] **Step 6: Run the contract and adapter suites**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_contract.py tests/unit/test_source_adapter_contract.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_fakes_emby_server.py`
Expected: FAIL in `tests/unit/test_adapters_emby_adapter.py` only, on cases that walk a first walk and mean a delta: `test_a_watch_state_walk_resumes_from_the_start_index_it_is_given`, `test_a_resumed_watch_state_walk_re_yields_what_it_dropped`, and the `watch_state` parametrisations of `test_a_page_that_fails_transiently_is_asked_for_again` and `test_a_page_that_keeps_failing_ends_the_walk_on_the_sixth_attempt`, and any of the further cases Step 7 converts.

- [ ] **Step 7: Make those four cases the deltas they describe**

- `test_a_watch_state_walk_resumes_from_the_start_index_it_is_given`: `adapter.watch_state(T0, start_index=50_000)`, and its docstring's first line becomes `A resumed delta asks Emby for the page it stopped at rather than for page one.`
- `test_a_resumed_watch_state_walk_re_yields_what_it_dropped`: `adapter.watch_state(T0)` and `adapter.watch_state(T0, start_index=len(first))`.
- `test_a_page_that_fails_transiently_is_asked_for_again` and `test_a_page_that_keeps_failing_ends_the_walk_on_the_sixth_attempt`: `getattr(adapter, walk)(T0)`.
- `test_watch_state_is_attributed_to_the_authenticated_user`, `test_the_walk_reports_absent_play_history`, `test_reporting_a_position_does_not_reach_the_played_route` and `test_every_path_this_adapter_issues_redacts_to_a_route_with_no_identifier`: each walk passes `T0`, since each walks items the first walk would drop.
- `tests/contract/source_adapter_contract.py`: `test_watch_state_reports_position_and_played`, `test_a_walk_never_reports_play_history_it_cannot_know` and `test_watch_state_reports_a_played_item` are parametrised over `since` in `(None, T0)`, ids `first-walk` and `delta`, so both walks must report a state alike. The method count stays 52.
- `_FlakyLibrary`'s docstring, first paragraph: ``Every entry carries a default `UserData`, so a watch-state delta yields all three; a first walk would yield none.``

Run the Step 6 command. Expected: PASS.

- [ ] **Step 8: Repair the watch-sync cases that meant a delta**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_watch_sync.py`
Expected: FAIL on exactly `test_a_source_that_reports_a_zero_has_its_zero_written`, `test_a_walk_still_reports_the_same_counters_after_the_split`, `test_a_failed_walk_is_resumed_from_the_position_it_committed`, `test_the_position_advances_per_committed_batch`, `test_a_failed_walk_keeps_the_position_it_reached` and `test_each_failed_attempt_resumes_further_in_than_the_last`, each a first walk over items with no state; and `test_the_resume_point_is_the_position_and_not_the_counter`, `test_a_running_run_left_by_a_killed_process_is_reclaimed_not_orphaned` and `test_a_resumed_attempt_merges_at_its_own_start_not_the_reclaimed_runs`, each a reclaimed cursorless run that now resumes as a first walk over items with no state. If the failing set differs, stop and find out why before editing.

Add to `_Fixture`, after `given_matched`:

```python
    async def given_completed_walk(self, *, at: AwareDatetime = T0) -> None:
        """A finished first walk, so the next run is a delta that yields every state."""
        await self.runs.add(
            SyncRun(
                source_id=self.source.id,
                kind=SyncRunKind.WATCH_STATE,
                status=SyncRunStatus.COMPLETED,
                started_at=at,
                finished_at=at,
            )
        )
```

Call `await fixture.given_completed_walk()` (or `fixture_batched.`, matching the case's fixture) as the first line of each of the six cases, and of `test_an_unplayed_item_is_not_enqueued_for_backfill`, which otherwise passes with nothing walked. In `test_a_source_that_reports_a_zero_has_its_zero_written`'s docstring, `filtering zero states out of a walk` becomes `filtering zero states out of a delta walk`.

The four cases that seed an unfinished run mean a resumed delta, so they become one, which is also what Task 7's supersede needs of them: add `cursor_at=T0,` to the `SyncRun(...)` seed of `test_the_resume_point_is_the_position_and_not_the_counter`, `test_a_running_run_left_by_a_killed_process_is_reclaimed_not_orphaned`, `test_a_resumed_attempt_merges_at_its_own_start_not_the_reclaimed_runs` and `test_the_span_records_the_page_the_walk_resumed_from`. Each walks items seeded at `T0`, which a delta since `T0` includes. Rename the section comment above `test_a_failed_walk_is_resumed_from_the_position_it_committed` to:

```python
# -- the resume, which keeps a long delta from starting over ------------------
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_watch_sync.py`
Expected: PASS.

In `tests/integration/test_services_watch_sync.py`, `test_one_batch_keeps_an_absent_count_and_writes_a_reported_zero` walks a reset, which only a delta reports. Add `SyncRun, SyncRunKind` to its `usher.domain.sync` import, and make the case's first statement:

```python
    await PostgresSyncRunRepository(session).add(
        SyncRun(
            source_id=source.id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.COMPLETED,
            started_at=RUN_AT,
            finished_at=RUN_AT,
        )
    )
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/integration/test_services_watch_sync.py tests/integration/test_ingest_end_to_end.py tests/integration/test_admin_sources.py`
Expected: PASS.

- [ ] **Step 9: Plant and verify**

1. In the adapter's first walk, drop the `if not state.played and state.position_seconds <= 0` branch. Expect `test_a_server_that_ignores_the_filter_still_yields_only_watched_states_once` to fail (`movie-1` walked).
2. Drop the early return. Expect the same case to fail on `asked == ["IsPlayed"]`, and the `unwatched-outnumber-watched` arm too.
3. Drop `or state.external_id in yielded`. Expect `test_a_first_watch_walk_lists_played_then_in_progress_items` and the contract's first-walk case on the Emby arm to fail ("came twice").
4. Iterate `FIRST_WALK_FILTERS[:1]` only. Expect the contract's first-walk case to fail on both arms' Emby side (`filler-3` missing) — and on the fake arm, plant `_watched` returning `played` only, expecting the same.
5. In `FakeEmbyServer._passes`, make `IsResumable` require `not state.played`. Expect `test_a_resumable_filter_lists_every_item_with_a_position_played_or_not` to fail.
6. Judge the listing unfiltered on any unwatched entry (`if unwatched:`). Expect `test_a_series_reading_unwatched_in_the_played_listing_keeps_the_in_progress_one` to fail on "one Series reading unwatched cost the walk its in-progress listing".
7. `>=` for `>`. Expect the `a-tie` arm to fail on "the played listing was misjudged as filtered or as unfiltered".
8. Drop the WARNING. Expect the `unwatched-outnumber-watched` arm alone to fail, on "the walk listed once without a WARNING, or warned anyway".
9. Log `watched=unwatched`. Expect the same arm alone to fail, on "the WARNING miscounted the entries".
10. In the fake's rendered user data, drop the derived `Played`. Expect the fake's Series case to fail, and the Series case's premise about the played listing.
11. Flip the fourth entry (`movie-3`) of the ignored-filter case to `Played: True`. Expect its premise to fail: "the library listed is mostly unwatched".

- [ ] **Step 10: Say it in the PRD, the changelog and the rules**

`docs/prd/03-sources-and-sync.md`, the paragraph beginning `**The watch lane is resumable.**` keeps its first sentence and replaces everything from `🔶` to the paragraph's end with:

```markdown
**Until a source has completed one `watch_state` run, its watch lane has no
cursor**, and its next run asks the source only for what the account has played
or holds a resume position in — two filtered listings, a few requests on most
libraries — and merges nothing else. Nothing schedules that first walk.
```

`CHANGELOG.md`, `### Changed`:

```markdown
- **A source's first watch-state walk asks only for what was watched.** It lists
  played items, then in-progress ones: a few requests, where it used to walk the
  whole library, which on a million-item library took most of a day. If most of
  the played listing reads unwatched, the server is taken to ignore the filter,
  and the walk lists once and logs a WARNING.
```

`.claude/rules/emby-push-and-ingest.md`, "Gap-closing walks are unasked-for work" — replace the `⚠️ **And that guard reads the item lane's cursor only…` sentence with:

```markdown
⚠️ **And that guard reads the item lane's cursor only, while `_close_gap` also
runs `watch.sync(...)`** (#41): a source with completed delta runs and no
completed `watch_state` run passes it and runs the watch lane's first walk —
two filtered listings (`IsPlayed`, then `IsResumable`, which overlap) — and
**neither log line names it**.
```

- [ ] **Step 11: Commit**

```bash
git add src/usher/adapters/emby/adapter.py src/usher/ports/source.py tests/fakes/emby_server.py \
  tests/fakes/source_adapter.py tests/fixtures/emby/README.md tests/contract/source_adapter_contract.py \
  tests/unit/test_adapters_emby_contract.py \
  tests/unit/test_adapters_emby_adapter.py tests/unit/test_fakes_emby_server.py \
  tests/unit/test_services_watch_sync.py tests/integration/test_services_watch_sync.py \
  docs/prd/03-sources-and-sync.md CHANGELOG.md .claude/rules/emby-push-and-ingest.md
git commit -m "watch: a first walk lists only played and in-progress items"
```

---

### Task 7: An unfinished first walk is superseded, not resumed

Spec §1.6, last part. If `latest_incomplete_run` returns a run whose `cursor_at` is `None`, the service closes it `failed` with `error = "superseded: a first watch walk restarts"` and starts a fresh run at position 0. A run left part-way through an old-style first walk carries a position such as 300,000, which would skip the whole filtered walk; and `save` only ever raises `position`, so resetting the row is impossible. A delta still resumes (#41).

**Departure from the spec's letter:** a cursorless run that is already `FAILED` is closed already, so it keeps its own `error` — the diagnostic of why that walk failed — and a fresh run starts beside it. Only a `RUNNING` one (a killed process, the case Task 9 sets up on a clone) is relabelled.

**Files:**
- Modify: `src/usher/services/watch_sync.py` (`SUPERSEDED_ERROR`, `sync`, a new `_supersede`)
- Modify: `src/usher/adapters/emby/adapter.py` (the first walk's comment), `README.md` (an interrupted first sync)
- Modify: `tests/contract/sync_run_repository_contract.py` (the overtaken-walk case, now a delta)
- Modify: wherever this makes prose false — the `sync` comment, the docstrings in `src/usher/ports/repository/sync.py` and `src/usher/db/models/sync.py`, and `docs/prd/09-roadmap.md`, which called the lane resumable without qualification
- Test: `tests/unit/test_services_watch_sync.py`, `tests/integration/test_services_watch_sync.py`
- Modify: `docs/prd/03-sources-and-sync.md`, `CHANGELOG.md`, `.claude/rules/emby-push-and-ingest.md`

**Interfaces:**
- Consumes: Task 6's `given_completed_walk`.
- Produces: `usher.services.watch_sync.SUPERSEDED_ERROR = "superseded: a first watch walk restarts"`.

- [ ] **Step 1: Write the failing tests**

Append to the resume section of `tests/unit/test_services_watch_sync.py`:

```python
async def test_an_unfinished_first_walk_is_superseded_rather_than_resumed(
    fixture: _Fixture,
) -> None:
    """A first walk is a few hundred states; an old position would skip its played listing.

    `save` only raises `position`, so the old row cannot be reset: it is closed
    `FAILED`, and a fresh run walks from the start.
    """
    await fixture.given_matched("movie-0")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-0", position_seconds=0, played=True)
    )
    abandoned = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.RUNNING,
        position=300_000,
        items_seen=300_000,
        started_at=T0,
    )
    await fixture.runs.add(abandoned)

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert run.id != abandoned.id, "the abandoned first walk was resumed"
    assert fixture.adapter.resumed_from == [0]
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_matched == 1, "the fresh walk merged nothing: it kept the old position"
    closed = await fixture.runs.get(abandoned.id)
    assert closed is not None
    assert (closed.status, closed.error) == (
        SyncRunStatus.FAILED,
        "superseded: a first watch walk restarts",
    )
    assert closed.finished_at is not None


async def test_a_first_walk_that_already_failed_keeps_its_own_error(fixture: _Fixture) -> None:
    """Closed already, so it is not relabelled: its error says why that walk failed."""
    failed = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.FAILED,
        position=40,
        error="GET /Users/{user_id}/Items returned HTTP 502",
        started_at=T0,
        finished_at=T0,
    )
    await fixture.runs.add(failed)

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert run.id != failed.id, "the failed first walk was resumed"
    assert fixture.adapter.resumed_from == [0]
    stored = await fixture.runs.get(failed.id)
    assert stored is not None
    assert stored.error == "GET /Users/{user_id}/Items returned HTTP 502"
```

Append to `tests/integration/test_services_watch_sync.py`:

```python
async def test_an_unfinished_first_walk_is_closed_and_a_fresh_one_runs(
    session: AsyncSession,
    service: WatchStateSyncService,
    media_items: PostgresMediaItemRepository,
    watch_states: PostgresWatchStateRepository,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """Against Postgres, whose `save` keeps the greater `position`.

    Resetting the old row in place would leave 300,000 in it, so it is closed and
    the fresh run starts at 0.
    """
    runs = PostgresSyncRunRepository(session)
    abandoned = SyncRun(
        source_id=source.id,
        kind=SyncRunKind.WATCH_STATE,
        position=300_000,
        items_seen=300_000,
        started_at=RUN_AT,
    )
    await runs.add(abandoned)
    watched = await _given_matched_movie(session, media_items, source, "movie-1")
    adapter = FakeSourceAdapter(source)
    adapter.seed(_item("movie-1"), CHANGED_AT)
    adapter.seed_state(SourceWatchState(external_id="movie-1", position_seconds=0, played=True))

    run = await service.sync(source, adapter, user_id=user_id)

    assert adapter.resumed_from == [0]
    assert run.id != abandoned.id
    assert (run.status, run.position) == (SyncRunStatus.COMPLETED, 1)
    closed = await runs.get(abandoned.id)
    assert closed is not None
    assert (closed.status, closed.error) == (
        SyncRunStatus.FAILED,
        "superseded: a first watch walk restarts",
    )
    stored = await watch_states.get_for_title(user_id, watched)
    assert stored is not None and stored.played is True
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_watch_sync.py tests/integration/test_services_watch_sync.py -k "superseded or already_failed or closed_and_a_fresh"`
Expected: FAIL on each, because the cursorless row is resumed: the unit cases on `assert run.id != abandoned.id`, and the integration case a line earlier, on `adapter.resumed_from == [0]`.

- [ ] **Step 2: Supersede**

`src/usher/services/watch_sync.py`, below the instruments:

```python
# What a superseded first walk's row says. A constant, so a test and an operator
# grepping `sync_runs.error` read the same words.
SUPERSEDED_ERROR = "superseded: a first watch walk restarts"
```

In `sync`, right after `incomplete = await self._runs.latest_incomplete_run(...)`:

```python
            if incomplete is not None and incomplete.cursor_at is None:
                await self._supersede(incomplete, attempt_started)
                incomplete = None
```

and add to the class:

```python
    async def _supersede(self, run: SyncRun, now: AwareDatetime) -> None:
        """Close an unfinished first walk instead of resuming it.

        A first walk is a few hundred states, so its position means nothing to the
        next one, and an old-style walk's 300,000 would skip its whole played
        listing. `save` only ever raises `position`, so the row is closed rather
        than reset. One already `FAILED` is closed already, and keeps its own error.
        """
        if run.status is SyncRunStatus.RUNNING:
            await self._runs.save(
                run.evolve(status=SyncRunStatus.FAILED, error=SUPERSEDED_ERROR, finished_at=now)
            )
```

The supersede save shares the fresh run's commit, so the two rows change together.

- [ ] **Step 3: Run the watch-sync suites**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_watch_sync.py tests/integration/test_services_watch_sync.py`
Expected: PASS. The four resume cases that seed an unfinished run have been deltas since Task 6, so the supersede leaves them alone.

- [ ] **Step 4: Say what the supersede makes true**

In `src/usher/adapters/emby/adapter.py`, the first walk's comment in `_watch_state` gets back the clause Task 6 left out, which is true now:

```python
        # A resumed first walk starts its first listing at `start_index` and its
        # second at 0. `WatchStateSyncService` never resumes one -- it starts
        # again -- so this only keeps the port's promise.
```

`README.md`, quickstart step 4: the sentence on an interrupted sync becomes, rewrapped:

```markdown
If it's interrupted, run it again. Both walks start over from the beginning and
duplicate nothing; the watch-state walk asks only for what was watched, so it is
the short one.
```

`tests/contract/sync_run_repository_contract.py`: `test_an_overtaken_walk_cannot_un_complete_the_run_that_overtook_it` seeds its run with `cursor_at=EARLIER - timedelta(days=1)`. A cursorless `RUNNING` row is now superseded, not reclaimed, so the reclaim race the case pins exists only for a delta; its docstring says so, and its assertions stand.

Run the Step 3 command. Expected: PASS.

- [ ] **Step 5: Plant and verify**

1. In `sync`, supersede every incomplete run (drop `and incomplete.cursor_at is None`). Expect `test_a_failed_walk_is_resumed_from_the_position_it_committed` (a delta) to fail on `second.id == first.id`.
2. In `_supersede`, drop the `RUNNING` condition. Expect `test_a_first_walk_that_already_failed_keeps_its_own_error` to fail.
3. In `_supersede`, save `run.evolve(position=0, …)` instead of closing, and let `sync` resume that row (`incomplete` left as the evolved run). Expect the integration case to fail on `run.id != abandoned.id` — and, if the resume is forced through, on `adapter.resumed_from == [0]`, because Postgres kept 300,000.

- [ ] **Step 6: Say it in the PRD, the changelog and the rules**

`docs/prd/03-sources-and-sync.md`, the paragraph Task 6 rewrote now reads in full:

```markdown
**A watch-lane delta is resumable.** A run checkpoints its position on
`sync_runs.position`, and the next attempt reclaims that same row and resumes
there, so a failure that outlasts a page's retries costs the page in flight
rather than the whole walk. **Until a source has completed one `watch_state`
run, its watch lane has no cursor**, and its next run asks the source only for
what the account has played or holds a resume position in — two filtered
listings, a few requests on most libraries — and merges nothing else. That
first walk is never resumed: an unfinished one still `running` is closed
`failed` with `superseded: a first watch walk restarts`, and the next run starts
again. Nothing schedules it.
```

`CHANGELOG.md`, `### Changed`:

```markdown
- **An unfinished first watch-state walk starts again rather than resuming.**
  Its position counted the old whole-library walk, so resuming it would skip
  every played item that is not also in progress; its row is closed `failed`
  as superseded.
```

`.claude/rules/emby-push-and-ingest.md`, append to the sentence Task 6 wrote, cutting as many stale or duplicated lines elsewhere in the file so it does not grow:

```markdown
An unfinished first walk is superseded, never resumed (`SUPERSEDED_ERROR`):
`save` only raises `position`, so its row cannot be reset.
```

- [ ] **Step 7: Commit**

```bash
git add src/usher/services/watch_sync.py tests/unit/test_services_watch_sync.py \
  tests/integration/test_services_watch_sync.py docs/prd/03-sources-and-sync.md CHANGELOG.md \
  .claude/rules/emby-push-and-ingest.md src/usher/adapters/emby/adapter.py README.md \
  tests/contract/sync_run_repository_contract.py src/usher/ports/repository/sync.py \
  src/usher/db/models/sync.py docs/prd/09-roadmap.md
git commit -m "watch: supersede an unfinished first walk instead of resuming it"
```

---

### Task 8: Phase 1's gate, status rows and PR

**Files:**
- Modify: `docs/plans/progress.md`, `docs/prd/README.md` (this plan's status cells)

- [ ] **Step 1: The full Python gate**

Run: `uv sync --extra eval && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly`
Expected: every command exits 0; pytest reports no failures and no errors, with Docker up for `tests/integration/`. Record the pass count in the PR body.

- [ ] **Step 2: The console gate**

Run, from `web/`: `npm run verify && npm run e2e && npm run e2e:visual`
Expected: PASS. Tasks 1 and 5 changed `Config.settings.ts`.

- [ ] **Step 3: The network guard**

Write `/var/tmp/usher-netguard/sitecustomize.py` as `.claude/rules/fixtures-and-fakes.md` gives it if it is not there, then run both halves in one environment:

```bash
PYTHONPATH=/var/tmp/usher-netguard uv run python -c "import socket; socket.getaddrinfo('api.themoviedb.org', 443)"
PYTHONPATH=/var/tmp/usher-netguard PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit 2>&1 | head -1
```

Expected: the probe raises `RuntimeError: NETWORK BLOCKED`, and the suite's first line is `[netguard] installed`.

- [ ] **Step 4: Status cells**

In `docs/plans/progress.md`, this plan's table: Tasks 1–8 → `✅ landed (PR #<n>)` once the PR merges, `🔨 in review (PR #<n>)` until then. In `docs/prd/README.md`'s implementation-plan table, this plan's row says the same thing in the same words.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin feat/fast-first-sync
```

Open the PR against `main` titled `A first sync that pages faster and asks only for what was watched`. Follow the repository's PR template if one exists. The body states what Phase 1 changes for an operator, the gate results with counts, the six departures from the spec's letter, and that Phase 2 follows in a separate PR. Its operator section also says what an upgrade keeps: a `.env` that sets `USHER_SOURCE_PAGE_SIZE` or `USHER_PUSH_STALE_AFTER_SECONDS` keeps that value over the new default, 1,000 and 300. At 200, every page after the first re-reads 50 items, so a walk makes a third more requests than pages of 200 did, and the read-ahead overlaps little of it; at 90, an idle push channel reconnects, with a gap-closing delta, after 90 s of silence. The body names no deployment's values. It ends with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Merging deploys to production. Ask the owner; do not merge. The ask names production's two such lines, `USHER_SOURCE_PAGE_SIZE=200` and `USHER_PUSH_STALE_AFTER_SECONDS=90` in `~/code/usher-deploy/.env`, and recommends dropping both before the merge, so the deploy starts on the defaults. Editing that file is a production write, so it waits for the owner's yes.

- [ ] **Step 6: Commit the status cells**

```bash
git add docs/plans/progress.md docs/prd/README.md
git commit -m "docs(plan): fast first sync phase 1 is in review"
git push
```

---

### Task 9: Phase 1 live acceptance

Spec "Measurement and acceptance" 3 (Phase 1): a first watch walk under 1 minute, and an item walk of at most 6 h.

**The spec's production handover no longer applies.** The old image's sync finished its whole-library watch walk on its own, completing and exiting 0, so production has no unfinished watch run to stop or supersede. Its watch lane now runs cursored deltas of a few seconds. So the first watch walk is timed on a scratch clone of the dev catalog (`usher_catalog` in `usher-postgres-1`), left the way the handover would have left production: its newest watch run is an unfinished first walk. The item walk is timed on production.

Every step that loads the source or touches production says so and waits for the owner.

- [ ] **Step 1: Clone the dev catalog**

A template copy needs the catalog to itself, so check first:

```bash
docker exec usher-postgres-1 psql -U usher -d postgres -Atc \
  "SELECT count(*) FROM pg_stat_activity WHERE datname = 'usher_catalog'"
docker exec usher-postgres-1 createdb -U usher -T usher_catalog -O usher_dev ffs_first_watch
sed -n '/^_HAND_OVER_SQL = """$/,/^"""$/p' ~/code/usher-devdb/scripts/wt_db.py | sed '1d;$d' \
  | sed 's/{role}/usher_dev/g' \
  | docker exec -i usher-postgres-1 psql -U usher -d ffs_first_watch -v ON_ERROR_STOP=1
```

Expected: `0`, then `createdb` succeeds, then `DO`. The clone takes about 8 GB. If the count is not `0`, another worktree is using the catalog. Wait for it to finish rather than ending its session.

The dev stack's `.env` connects as `usher_dev`, which the catalog revokes, and a template copy keeps the catalog's grants. So the clone is handed to `usher_dev` the way `wt_db.py up` hands over a worktree's database: the schema, its tables and sequences, and its routines other than the extensions'. Without the hand-over, Step 2's `alembic` fails with `no schema has been selected to create in`.

- [ ] **Step 2: Bring the clone to PR 1's schema, as the handover would have left it**

Run from this worktree, which holds the code PR 1 merged. The dev stack's `.env` supplies `USHER_SECRET_KEY`, which decrypts the clone's stored source credentials. The URL keeps its credentials and changes only the database name, so no credential is typed:

```bash
cd ~/code/.worktrees/usher/fast-first-sync
set -a; . ~/code/usher-devdb/.env; set +a
export USHER_DATABASE_URL="${USHER_DATABASE_URL%/*}/ffs_first_watch"
unset OTEL_EXPORTER_OTLP_ENDPOINT
uv run alembic upgrade head
docker exec usher-postgres-1 psql -U usher -d ffs_first_watch -c \
  "DELETE FROM sync_runs WHERE kind = 'watch_state' AND status <> 'running'"
docker exec usher-postgres-1 psql -U usher -d ffs_first_watch -Atc \
  "SELECT status, count(*), bool_or(cursor_at IS NOT NULL) FROM sync_runs WHERE kind = 'watch_state' GROUP BY 1"
docker exec usher-postgres-1 psql -U usher -d ffs_first_watch -Atc \
  "WITH gone AS (DELETE FROM watch_states WHERE origin = 'source' AND position_seconds > 0 AND NOT played RETURNING 1) SELECT count(*) FROM gone"
```

Expected: `alembic` ends at the repository's head, and the `sync_runs` query prints only a `running|<n>|f` row. Those are unfinished first walks with no cursor. With every completed watch run gone, the next watch walk has no cursor to resume from, and it supersedes the newest of them. The last command prints how many in-progress states the clone held and deletes them. Only the walk's second listing, the in-progress one, can put them back, so Step 3 counts them.

- [ ] **Step 3: A first watch walk under a minute — with the owner's go-ahead**

The walk reads the shared server, so ask first. Then, in one shell (the first four lines are Step 2's):

```bash
cd ~/code/.worktrees/usher/fast-first-sync
set -a; . ~/code/usher-devdb/.env; set +a
export USHER_DATABASE_URL="${USHER_DATABASE_URL%/*}/ffs_first_watch"
unset OTEL_EXPORTER_OTLP_ENDPOINT
timeout 3600 uv run usher sync --source "Shared Emby" --kind delta 2>&1 \
  | tee /var/tmp/sync-speed/first-watch.raw | grep -v '^{"text"' \
  | tee /var/tmp/sync-speed/first-watch.out
docker exec usher-postgres-1 psql -U usher -d ffs_first_watch -Atc \
  "SELECT status, coalesce(error, ''), round(extract(epoch FROM finished_at - started_at)), items_seen, items_matched FROM sync_runs WHERE kind = 'watch_state' ORDER BY started_at DESC LIMIT 2" \
  | tee -a /var/tmp/sync-speed/first-watch.out
docker exec usher-postgres-1 psql -U usher -d ffs_first_watch -Atc \
  "SELECT count(*) FROM watch_states WHERE origin = 'source' AND position_seconds > 0 AND NOT played" \
  | tee -a /var/tmp/sync-speed/first-watch.out
grep -c 'appears to ignore Filters' /var/tmp/sync-speed/first-watch.raw
```

The item lane is a cursored delta from the clone's last completed walk. The watch lane then supersedes the newest unfinished run and runs the filtered first walk.

Pass:
- the first row is `completed`, and its seconds are under `60`. That is the watch run's own `finished_at − started_at`, whatever the item lane took;
- the second row reads `failed|superseded: a first watch walk restarts`;
- the `grep -c` prints `0`. The walk did not judge the source to ignore `Filters`, so it asked for both listings. The log lines are JSON, which the filter keeps out of `first-watch.out`, so the check reads `first-watch.raw`. That file can hold a host, so it is never posted;
- the in-progress count is at least `1` if Step 2's was above `0`, because the second listing merged. If Step 2 printed `0`, the clone held no in-progress state, so say the check saw nothing. If the count is `0` while Step 2's was not, check the account's Continue Watching before calling it a failure.

Expect far fewer merges than production would make. The clone holds only part of the source's items (52,560 when this plan was written), so most states find no item and count as unmatched. The listings walked are the same.

Before posting, check the file: `grep -c -E '[0-9a-f]{32}'` must print `0`, and `grep -n -E 'https?://|([0-9]{1,3}\.){3}[0-9]{1,3}'` must print nothing. Then post `first-watch.out` raw on PR 1.

- [ ] **Step 4: An item walk of at most 6 h, on production — with the owner's go-ahead**

A full walk loads a server the operator may not administer, so ask first. Production's `.env` must not set the page size (Task 8's merge ask): `grep -c -E '^USHER_SOURCE_PAGE_SIZE=' ~/code/usher-deploy/.env` prints `0`. If it prints `1`, ask the owner to drop the line, or to approve dropping it. `docker compose run` reads `.env` when it creates the walk's container, so no restart is needed. If they keep it, the walk runs at that page size: say so in the post, and do not call it the default. Production must be idle: `docker ps --filter name=usher-prod-sync --format '{{.Names}} {{.Status}}'` prints nothing. Then start the walk detached, the way the last production sync was started, with its kind stated. The old log is kept under a dated name:

```bash
cd ~/code/usher-deploy
[ -f data/bulk/sync.log ] && mv data/bulk/sync.log "data/bulk/sync-$(date -u +%Y%m%dT%H%M).log"
docker compose run -d --rm --no-deps --name usher-prod-sync usher \
  sh -c 'usher sync --source "Shared Emby" --kind full > /data/bulk/sync.log 2>&1; echo "exit=$?" >> /data/bulk/sync.log'
```

Wait for its end with one background wait, never by polling in the foreground:

```bash
timeout 28800 sh -c 'until grep -q "^exit=" ~/code/usher-deploy/data/bulk/sync.log; do sleep 300; done'
grep -v '^{"text"' ~/code/usher-deploy/data/bulk/sync.log | tail -4
docker exec usher-prod-postgres-1 psql -U usher -d usher -Atc \
  "SELECT kind, status, started_at, finished_at, round(extract(epoch FROM finished_at - started_at) / 3600, 2), items_seen FROM sync_runs WHERE kind = 'full' ORDER BY started_at DESC LIMIT 1"
```

Pass: `exit=0`, and the `full` row is `completed` in at most `6.00` hours at default settings. Post the log tail and the row on PR 1, raw.

- [ ] **Step 5: Post the results, drop the clone, close Phase 1**

If either target misses, post the numbers and stop: Phase 2's targets assume Phase 1's.

The clone is scratch. Once its numbers are posted, drop it:

```bash
docker exec usher-postgres-1 dropdb -U usher ffs_first_watch
```

Then go on to Phase 2. Task 10, Step 1 marks Phase 1 landed and Task 9's first watch walk passed in both status tables, as the Phase 2 branch's first commit; Task 20 marks Task 9 passed once its item walk's number is in.

---

## Phase 2 — the unit walk (PR 2)

Spec §2.1–2.8. A **whole-library walk** — a full walk, or a delta with no cursor yet — asks the adapter for a plan of units, fetches the units with several walkers, commits them with one writer, runs the watch lane as soon as the seed has landed, and resumes in place after a failure. A delta with a cursor and the gap-closer keep Phase 1's single walk.

Phase 2 starts once PR 1 has merged and Task 9's first watch walk has passed, on its own branch (Task 10, Step 1). Task 9's item walk on production waits on the owner; Task 20 does not open PR 2 until it has passed.

### Departures from the spec's letter

Each is repeated in the task where it happens.

1. **`list_unit` yields pages, not items: `UnitPage(items, resume_at)`.** Only the adapter knows where its overlap put the next request, so it states where a unit resumes; the spec's `AsyncIterator[SourceItem]` cannot say it. The queue holds `_Fetched(unit_key, page)` — the spec's `(unit_key, items, cursor)` under other names. (Task 11.)
2. **A resumed unit counts up to `PAGE_OVERLAP` items twice in `items_seen`.** Its first page re-reads the overlap its last committed page already counted. The catalog is unaffected, because ingest upserts on `(source_id, external_id)`; the spec's "nothing counted twice" holds exactly for an adapter without overlap, which is what Task 17's resume case asserts. The overlap is also all a resume reaches back: a resumed unit's first page has no page before it, so a resume after more than `PAGE_OVERLAP` deletions ahead of a unit's position misses items, exactly as an in-walk shift does, without the WARNING. A full walk's sweep then retracts the items it skipped, up to the sweep's ceiling, until a later full walk reads them again. (Task 17.)
3. **The writer commits a unit's pages once they add up to `USHER_SYNC_BATCH_SIZE` items, and when the unit ends — not after every page.** A batch can run past the size by less than a page. At the defaults (pages and batches of 1,000), a page after a unit's first adds at most 950 new items, because 50 overlap the page before. So most commits carry two pages. (Task 16.)
4. **The run and its first heartbeat are committed before the plan is made; the units ride the next commit.** A plan that fails still leaves a row, and a run with a heartbeat and no units is a walk that died while planning. (Tasks 16, 17.)
5. **A `failed` run with units resumes whatever its heartbeat says.** Only a `running` run whose heartbeat is younger than 10 minutes is refused. (Task 17.)
6. **A superseded unit-less run that is already `failed` keeps its own error**, as Task 7 decided for the watch lane. (Task 17.)
7. **On a failure, pages fetched but not yet committed are dropped**, and the failing unit is marked `failed` at its committed position, which is where the next attempt resumes it. (Task 16.)
8. **The seed streams.** Each page is led by the series its episodes need that neither it nor an earlier page holds, fetched by `Ids` there and then. The spec reads every listing first and fetches the series after. (Task 14.)
9. **A resumed seed starts again; every seed page resumes at 0.** A count into listings that may have changed could skip an item the watch lane is about to look for. (Task 14.)
10. **The seed is planned only when both watch filters narrow the library, and a fallback plan keeps its seed.** A server that ignored a filter would make the seed a whole-library walk ahead of the real one. (Task 14.)
11. **The watch lane after the walk reads back to the instant the walk began.** `WatchStateSyncService.sync` takes `since_at_most`. Without it, the run after the seed has moved the cursor past the walk's start, and a state saved meanwhile for an item not yet stored would be skipped. That run is the after-seed hook's, and the item walk does not beat while it runs, so a hook walk longer than `STALE_AFTER` leaves the item walk looking dead to a second walk: at the default gate, 0.4 requests/s with pages of 1,000 that each add 950 after the first, that is any hook walk of more than about 228,000 watch states (240 pages in 10 minutes), and fewer on a server slower than the gate. (Task 18.)
12. **A run whose sweep was refused is not resumed.** The next attempt reads the library again, because a resume would re-run the sweep over the same rows and refuse again. (Task 17.)
13. **A watch-state run carries a heartbeat too, and a live one is left alone.** The spec gives the heartbeat to whole-library walks; a watch-state run's is set when it is inserted or reclaimed and on every batch. A `running` watch run whose heartbeat is younger than 10 minutes is another walk's, alive: a second watch run neither supersedes nor resumes it, and runs beside it in a row of its own — a delta from the latest completed cursor, or a filtered first walk when there is none. Nothing is refused, so no caller changes. Both runs merge on one key under one conflict rule, so they converge, but for a state that changes between their two reads, which can keep the earlier read until it next changes. (Task 17.)

### Review Focus (Phase 2)

1. **A walk killed while planning.** The run row and its heartbeat are committed, the units never are. The next walk is refused while that heartbeat is fresh and supersedes the row once it is stale, instead of resuming a run that has no plan or refusing forever. Task 17: `test_a_walk_killed_while_planning_is_superseded_once_its_heartbeat_is_stale`.
2. **A movie library's EPISODES unit.** Every library gets both kinds of unit, so most EPISODES units hold nothing. Each must cost exactly one request and end, not page on or fail. Task 13: `test_a_movie_library_s_episodes_unit_ends_on_one_empty_page`.
3. **Items outside every library.** When the libraries hold fewer items than the source total, a full walk over the libraries alone would sweep the rest as unavailable. The plan falls back to the single walk and logs both numbers. Task 13: `test_libraries_that_hold_less_than_the_total_fall_back_to_one_walk`.
4. **Collection and playlist views.** They only point at items held in libraries. Walking them reads items twice, and counting them inflates the coverage sum enough to hide a missing library. Task 13: `test_collection_and_playlist_views_are_not_libraries`.
5. **A library removed between attempts.** A resumed unit whose view is gone ends empty without a request — the fake models the worst case, where an unknown `ParentId` answers the whole library — and the resumed unit completes. Task 13: `test_a_library_removed_between_attempts_ends_its_unit_empty`; Task 17: `test_a_resumed_unit_that_yields_nothing_completes`.

### File map (Phase 2)

| File | Change |
|---|---|
| `src/usher/domain/sync.py` | `WalkStage`, `STAGE_ORDER`, `SyncRunUnitStatus`, `SyncRunUnit`, `SyncRun.heartbeat_at`, `WalkProgress`, `walk_progress` |
| `src/usher/ports/source.py` | `WalkUnit`, `WalkPlan`, `WHOLE_LIBRARY`, `UnitPage`, `pages_of`; concrete `plan_walk`/`list_unit` on `SourceAdapter` |
| `src/usher/ports/repository/sync.py` | `add_units`, `save_unit`, `units_for`; `latest_run`, with `latest_incomplete_run` built on it and serving the whole-library walk too |
| `src/usher/db/models/sync.py`, `src/usher/db/models/__init__.py` | `SyncRunUnitRow`; `SyncRunRow.heartbeat_at` |
| `src/usher/db/migrations/versions/m10g_sync_run_units.py` | **new** — the table and the column |
| `src/usher/db/repositories/sync.py` | the three unit methods; `latest_run` over `_NEWEST` |
| `src/usher/db/backup_manifest.py`, `docs/runbooks/disaster-recovery.md` | `sync_run_units` is rebuildable |
| `scripts/audit_bounded_columns.py` | `sync_run_units` joins `_DOMAIN_FOR_TABLE`; the published census gains its five bounded columns |
| `src/usher/adapters/emby/planning.py` | **new** — unit keys and episode chunks |
| `src/usher/adapters/emby/limit.py` | **new** — `ListingLimit`, the shared backoff |
| `src/usher/adapters/emby/paging.py` | `OffsetWindow(stop=…)`, `cursor`, `request_limit` |
| `src/usher/adapters/emby/adapter.py` | `_pages`, `plan_walk`, `list_unit`, the seed, the limiter around `_page` |
| `src/usher/adapters/emby/session.py` | `_ROUTE_WORDS` gains `Views`, `NextUp` |
| `src/usher/adapters/factory.py`, `src/usher/composition.py`, `src/usher/api/deps.py`, `src/usher/config.py` | `unit_max_items`, `listing_concurrency`, `walkers` |
| `src/usher/services/reconcile.py` | the planned walk: walkers, one writer, the stage barrier, `after_seed`, the heartbeat, `_claim`, `WalkRefused`, the unit histogram |
| `src/usher/services/watch_sync.py` | the heartbeat; a run beside a live watch walk, never over it; `sync(…, since_at_most=…)` |
| `src/usher/services/handlers.py`, `src/usher/cli.py`, `src/usher/api/lanes.py` | `after_seed` and `since_at_most`; what each caller does with `WalkRefused`; `sync-status`'s plan line; the gap-closer's `plan=False` |
| `src/usher/api/dto/source.py`, `src/usher/api/routers/sources.py`, `web/src/api/schema.d.ts`, `web/src/test/fixtures/admin.ts` | `last_sync` on the status response |
| `dashboards/03-pipeline.json`, `dashboards/README.md` | panel 11, the listing limit and the mean unit duration, and its section |
| `tests/fakes/source_adapter.py` | libraries and plans; `stage`, `fail_unit_after`, `hold`, `journal`, `unit_starts`, `uncounted` |
| `tests/fakes/emby_server.py`, `tests/fakes/emby_harness.py`, `tests/fakes/sync_run_repository.py` | views, `ParentId`, `IncludeItemTypes`, `Ids`, NextUp; unit storage and `latest_run` |
| `tests/contract/source_harness.py`, `tests/contract/source_adapter_contract.py`, `tests/contract/sync_run_repository_contract.py` | `given_item_in_libraries`; the unit cases; the `latest_run` cases |
| `tests/fixtures/emby/view_item.json`, `tests/fixtures/emby/README.md` | **new** fixture; the Phase 2 listing facts |
| `tests/unit/test_adapters_emby_limit.py` | **new** |
| `tests/unit/test_domain_sync.py`, `test_ports_source.py`, `test_db_models_ingest.py`, `test_db_migration_status.py`, `test_backup_manifest.py`, `test_adapters_emby_adapter.py`, `test_adapters_emby_contract.py`, `test_adapters_emby_paging.py`, `test_adapters_emby_planning.py`, `test_adapters_factory.py`, `test_fakes_emby_server.py`, `test_config.py`, `test_composition.py`, `test_services_reconcile.py`, `test_services_watch_sync.py`, `test_services_handlers.py`, `test_api_lanes.py`, `test_cli.py`, `test_cli_errors.py`, `test_telemetry_metric_names.py`, `test_dashboards.py`, `test_alerts.py`, `test_no_third_party_data.py` (all under `tests/unit/`) | as each task says |
| `tests/integration/test_migrations.py`, `test_sync_run_repository.py`, `test_bulk_repository.py`, `test_services_reconcile.py`, `test_ingest_end_to_end.py`, `test_pipeline_deps.py`, `test_pipeline_spans.py`, `test_cli_pipeline.py`, `test_admin_sources.py` (all under `tests/integration/`) | as each task says |
| `docs/prd/02-…`, `03-…`, `07-…`, `08-…`, `10-…`, `CHANGELOG.md`, `.env.example`, `docs/guide/configuration.md`, `docs/guide/command-line.md`, `web/src/features/operator/Config.settings.ts`, `.claude/rules/emby-push-and-ingest.md`, `.claude/rules/db-and-sql.md` | per task |
| `docs/plans/progress.md`, `docs/prd/README.md` | this plan's status cells (Tasks 10, 20, 21) |

---

### Task 10: The facts the planner rests on — owner-gated

Spec "Measurement and acceptance" 1 and 2, and "Testing: the fake server". Nothing in Tasks 11–19 is written before this task has passed, because its numbers set two defaults and can change the planner's design. It touches production only by reading, through a throwaway script outside the repository that prints counts, booleans, seconds and type names — never an id, a name, a token, a user id or a host.

**Files:**
- Create: `tests/fixtures/emby/view_item.json`
- Modify: `tests/fixtures/emby/README.md` (the intro's file count, the file table, the listing-protocol table)
- Modify: `tests/unit/test_no_third_party_data.py` (`test_the_guard_reads_what_it_claims_to_read`'s list)
- Modify: `docs/plans/progress.md`, `docs/prd/README.md` (Phase 1's status cells)

**Interfaces:**
- Produces: `load_emby_fixture("view_item")` — the template Task 13's fake server renders each view from.
- Produces: the verdicts Task 13 and Task 15 depend on, recorded in the README: what an unknown `ParentId` answers, the walker default, the unit size, whether NextUp pages and whether `Ids` needs `Recursive`.

- [ ] **Step 1: Branch from `main` after PR 1 merges**

```bash
git -C ~/code/usher fetch origin main
git -C ~/code/usher worktree add ~/code/.worktrees/usher/fast-first-sync-units -b feat/fast-first-sync-units origin/main
cd ~/code/.worktrees/usher/fast-first-sync-units && uv sync --extra eval
git log --oneline -1   # the merge of PR 1
```

Every later Phase 2 command runs in this worktree.

Mark Phase 1 in both status tables, claiming only what has run. In `docs/plans/progress.md`, the Tasks 1–8 row becomes `✅ landed (PR #<n>)`, the Task 9 row becomes `🔨 the first watch walk passed in <s> s; the item walk on production waits on the owner`, with `<s>` the time Task 9 Step 3 printed, and the Tasks 10–20 row becomes `🔨 in progress — Task 10's writer measurement passed`, Step 8 having passed before this commit was made. `docs/prd/README.md` holds one row for this plan, and says the same in its one cell: `🔨 Phase 1 landed (PR #<n>); its first watch walk passed in <s> s, and its item walk on production waits on the owner. Phase 2 in progress — Task 10's writer measurement passed`. Task 20 Step 4 flips the Task 9 row to `✅ passed` once Task 9 Step 4's time is in. This is the Phase 2 branch's first commit, and it carries the plan's own amendments with it:

```bash
git add docs/plans/2026-10-01-fast-first-sync.md docs/plans/progress.md docs/prd/README.md
git commit -m "docs(plan): fast first sync phase 1 has landed and its first watch walk passed"
```

- [ ] **Step 2: Ask the owner, and check that production is idle**

The probe loads a server the operator may not administer. Ask the owner first, then:

```bash
docker ps --filter name=usher-prod-sync --format '{{.Names}} {{.Status}}'
```

Expected: no output. If a sync container is running, stop here and wait for it: a measurement taken under a walk measures the walk.

- [ ] **Step 3: Write the probe**

`/var/tmp/sync-speed/facts2.py` (outside the repository; never committed):

```python
# Throwaway, read-only: the facts Phase 2's planner rests on. Prints counts, booleans,
# seconds and type names only -- no ids, names, tokens, user ids or hosts.
import asyncio
import secrets
import time
from collections import Counter

from usher.adapters.emby.adapter import ITEM_FIELDS, ITEM_TYPES, SORT_BY
from usher.cli import _open_adapter, _session_for, build_pipeline, selected_sources
from usher.config import Settings

SKIPPED = {"boxsets", "playlists"}


async def main() -> None:
    settings = Settings()
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        [source] = await selected_sources(pipeline, "Shared Emby")
        adapter = await _open_adapter(pipeline, source)
        s = adapter._session
        try:
            uid = await s.user_id()
            items = f"/Users/{uid}/Items"
            base = {"Recursive": "true", "IncludeItemTypes": ITEM_TYPES, "Fields": ITEM_FIELDS,
                    "SortBy": SORT_BY, "SortOrder": "Ascending"}

            async def get(path, params, *, pause=True):
                if pause:
                    await asyncio.sleep(3.0)
                started = time.perf_counter()
                body = await s.json_body("GET", path, params=params, op="probe")
                return body, time.perf_counter() - started

            def page(start, *, parent, counted="false"):
                return {**base, "ParentId": parent, "StartIndex": str(start), "Limit": "1000",
                        "EnableTotalRecordCount": counted}

            # 1. Views: which kinds exist, and the shape of one.
            views, _ = await get(f"/Users/{uid}/Views", {})
            entries = views.get("Items", [])
            print(f"views={len(entries)} "
                  f"types={dict(Counter(v.get('CollectionType') for v in entries))}", flush=True)
            if entries:
                shape = {key: type(value).__name__ for key, value in sorted(entries[0].items())}
                print(f"view shape={shape}", flush=True)
            libraries = [v for v in entries if v.get("CollectionType") not in SKIPPED]

            # 2. Coverage: the whole source against the sum of its libraries.
            body, _ = await get(items, {**base, "Limit": "0", "EnableTotalRecordCount": "true"})
            total = body.get("TotalRecordCount")
            held = {}
            for library in libraries:
                body, _ = await get(items, {**base, "ParentId": library["Id"], "Limit": "0",
                                            "EnableTotalRecordCount": "true"})
                held[library["Id"]] = body.get("TotalRecordCount") or 0
                print(f"library type={library.get('CollectionType')} "
                      f"held={held[library['Id']]}", flush=True)
            print(f"total={total} libraries_sum={sum(held.values())} "
                  f"covered={sum(held.values()) >= (total or 0)}", flush=True)

            # 3. The largest library: titles against episodes.
            largest = max(held, key=held.__getitem__)
            depth = held[largest]
            for label, types in (("titles", "Movie,Series"), ("episodes", "Episode")):
                body, _ = await get(items, {**base, "IncludeItemTypes": types, "ParentId": largest,
                                            "Limit": "0", "EnableTotalRecordCount": "true"})
                print(f"largest {label}={body.get('TotalRecordCount')}", flush=True)

            # 4. A ParentId no view carries.
            try:
                body, _ = await get(items, {**base, "ParentId": secrets.token_hex(16),
                                            "Limit": "0", "EnableTotalRecordCount": "true"})
                answered = body.get("TotalRecordCount")
                print(f"unknown ParentId total={answered} "
                      f"equals_whole_source={answered == total}", flush=True)
            except Exception as exc:  # a probe reports; it does not handle
                print(f"unknown ParentId raised {type(exc).__name__}", flush=True)

            # 5. Page cost inside the largest library: head, middle, deep end.
            for label, start in (("head", 0), ("middle", depth // 2),
                                 ("deep", max(0, depth - 1_000))):
                body, seconds = await get(items, page(start, parent=largest))
                print(f"page {label:6} start={start} items={len(body.get('Items', []))} "
                      f"seconds={seconds:.1f}", flush=True)

            # 6. The count against no count, at the deep end.
            for counted in ("true", "false"):
                _, seconds = await get(items, page(max(0, depth - 1_000), parent=largest,
                                                   counted=counted))
                print(f"deep page counted={counted} seconds={seconds:.1f}", flush=True)

            # 7. Pages per second with 1, 2 and 4 walkers, through the deployment's gate.
            for walkers in (1, 2, 4):
                starts = [min(depth - 1_000, depth * index // 8 + walkers * 3_000)
                          for index in range(8)]

                async def walker():
                    while starts:
                        await get(items, page(starts.pop(), parent=largest), pause=False)

                began = time.perf_counter()
                await asyncio.gather(*(walker() for _ in range(walkers)))
                elapsed = time.perf_counter() - began
                print(f"walkers={walkers} pages=8 seconds={elapsed:.1f} "
                      f"pages_per_second={8 / elapsed:.2f}", flush=True)
                await asyncio.sleep(10.0)

            # 8. NextUp: shape, total and paging.
            def next_up(start):
                return {"UserId": uid, "Fields": ITEM_FIELDS, "StartIndex": str(start),
                        "Limit": "5", "EnableTotalRecordCount": "true"}

            first, _ = await get("/Shows/NextUp", next_up(0))
            second, _ = await get("/Shows/NextUp", next_up(5))
            a = [entry.get("Id") for entry in first.get("Items", [])]
            b = [entry.get("Id") for entry in second.get("Items", [])]
            print(f"nextup total={first.get('TotalRecordCount')} page0={len(a)} page5={len(b)} "
                  f"disjoint={not set(a) & set(b)} "
                  f"types={dict(Counter(e.get('Type') for e in first.get('Items', [])))} "
                  f"with_series={sum(1 for e in first.get('Items', []) if e.get('SeriesId'))}",
                  flush=True)

            # 9. Ids, with and without Recursive.
            series = sorted({e["SeriesId"] for e in first.get("Items", []) if e.get("SeriesId")})
            if series:
                asked = ",".join(series[:3])
                plain, _ = await get(items, {"Ids": asked, "Fields": ITEM_FIELDS})
                deep, _ = await get(items, {"Ids": asked, "Fields": ITEM_FIELDS,
                                            "Recursive": "true", "IncludeItemTypes": "Series"})
                print(f"ids asked={len(series[:3])} plain={len(plain.get('Items', []))} "
                      f"recursive={len(deep.get('Items', []))}", flush=True)
        finally:
            await adapter.aclose()


asyncio.run(main())
```

- [ ] **Step 4: Run it inside the production container and keep the raw output**

```bash
(cd ~/code/usher-deploy && docker compose cp /var/tmp/sync-speed/facts2.py usher:/tmp/facts2.py >/dev/null \
  && timeout 1200 docker compose exec -T usher python /tmp/facts2.py 2>&1 | grep -v '^{"text"') \
  | tee /var/tmp/sync-speed/facts2.out
sha256sum /var/tmp/sync-speed/facts2.out | tee /var/tmp/sync-speed/facts2.out.sha256
grep -c -E '[0-9a-f]{32}' /var/tmp/sync-speed/facts2.out
```

Expected: every section printed, and the last command prints `0` — no 32-hex identifier reached the output. If it prints anything else, delete the output file and fix the script before going on.

Section 9 takes its series from NextUp, so it prints nothing when the account's NextUp is empty. Then run this follow-up, `/var/tmp/sync-speed/facts2b.py` (outside the repository; never committed), which takes three series from a listing instead and makes three requests in all:

```python
# Throwaway, read-only: facts2.py's section 9 with series taken from a listing, since the
# account's NextUp is empty. Prints counts only -- no ids, names, tokens, user ids or hosts.
import asyncio

from usher.adapters.emby.adapter import ITEM_FIELDS
from usher.cli import _open_adapter, _session_for, build_pipeline, selected_sources
from usher.config import Settings


async def main() -> None:
    settings = Settings()
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        [source] = await selected_sources(pipeline, "Shared Emby")
        adapter = await _open_adapter(pipeline, source)
        s = adapter._session
        try:
            uid = await s.user_id()
            items = f"/Users/{uid}/Items"
            listed = await s.json_body("GET", items, params={
                "Recursive": "true", "IncludeItemTypes": "Series", "Limit": "3",
                "Fields": ITEM_FIELDS}, op="probe")
            series = [entry["Id"] for entry in listed.get("Items", [])]
            asked = ",".join(series)
            await asyncio.sleep(3.0)
            plain = await s.json_body("GET", items, params={"Ids": asked, "Fields": ITEM_FIELDS},
                                      op="probe")
            await asyncio.sleep(3.0)
            deep = await s.json_body("GET", items, params={
                "Ids": asked, "Fields": ITEM_FIELDS, "Recursive": "true",
                "IncludeItemTypes": "Series"}, op="probe")
            print(f"ids asked={len(series)} plain={len(plain.get('Items', []))} "
                  f"recursive={len(deep.get('Items', []))} "
                  f"plain_types={sorted({e.get('Type') for e in plain.get('Items', [])})}",
                  flush=True)
        finally:
            await adapter.aclose()


asyncio.run(main())
```

```bash
(cd ~/code/usher-deploy && docker compose cp /var/tmp/sync-speed/facts2b.py usher:/tmp/facts2b.py >/dev/null \
  && timeout 300 docker compose exec -T usher python /tmp/facts2b.py 2>&1 | grep -v '^{"text"') \
  | tee /var/tmp/sync-speed/facts2b.out
sha256sum /var/tmp/sync-speed/facts2b.out | tee /var/tmp/sync-speed/facts2b.out.sha256
grep -c -E '[0-9a-f]{32}' /var/tmp/sync-speed/facts2b.out
```

Expected: one `ids asked=3 …` line, and the last command prints `0`. Step 5 reads its `Ids` verdict from it.

- [ ] **Step 5: Read the verdicts, and stop where a gate says stop**

| Question | Expected | If the probe says otherwise |
|---|---|---|
| Deep page inside the largest library (section 5) | under ~10 s | **Stop and replan.** Offset chunks pay the library's depth; the split moves to name ranges (`NameStartsWithOrGreater` + `NameLessThan`), which this plan does not cover. Post the numbers and ask the owner. |
| Pages/s with 4 walkers against 2 (section 7) | 4 is faster | If 4 is no faster than 2, Task 15's `sync_walkers` default is **2**, and every `4` in Tasks 15–16 that names the default (setting, `.env.example`, catalogue entry, guide, PRD 08) becomes 2. |
| `covered` (section 2) | `True` | Expected either way: the planner's fallback exists for `False`. Record both numbers. |
| An unknown `ParentId` (section 4) | the whole source (`equals_whole_source=True`) | Record what it answered. The fake keeps the worst case either way; the adapter never sends a vanished view's id (Task 13). |
| NextUp paging (section 8) | `disjoint=True`, `types` all `Episode`, `with_series` equal to `page0` | Record what it printed. Task 14 needs no change either way: NextUp is small, so its first page is short and at its total, and ends the walk whatever its paging. |
| `Ids` (section 9) | `plain` equals `asked` | If only `recursive` finds them, Task 14 adds `Recursive=true` and `IncludeItemTypes=Series` to the series request. |

`USHER_SYNC_UNIT_MAX_ITEMS` stays 100,000 unless section 5's middle page costs more than twice its head page; then it is 50,000, and Task 13's default and every place that names it change with it.

- [ ] **Step 6: Record the facts**

Add rows to the `## Listing protocol (fast first sync)` table in `tests/fixtures/emby/README.md`, each stating what the probe printed (the expected answers are shown; write the observed ones):

```markdown
| `/Users/{id}/Views` | every view the account sees, with a `CollectionType`; `boxsets` and `playlists` point at items held elsewhere | the planner skips those two kinds; `view_item.json` is one view's shape |
| `ParentId={view}` with `Limit=0` | that library's count over `Movie,Series,Episode`; the libraries summed to <sum> against a source total of <total> | the plan's coverage check |
| `ParentId` no view carries | <what section 4 printed> | the fake answers the whole library, the worst case; the adapter never sends a vanished view's id |
| Pages inside one library | head <s> s, middle <s> s, deep <s> s at 1,000 items | offset chunks of `USHER_SYNC_UNIT_MAX_ITEMS` |
| Concurrent pages at the deployment gate | 1 walker <p>/s, 2 walkers <p>/s, 4 walkers <p>/s | the `USHER_SYNC_WALKERS` default |
| `/Shows/NextUp` | episodes with a `SeriesId`, paged by `StartIndex`/`Limit`, <total> in all on the recorded account | the seed's third source |
| `Ids=a,b,c` | the named items, without `Recursive` | the seed's series request |
```

Below the section's opening paragraph (`Recorded 2026-10-01 … the shape of the item fixtures above.`), add one more, so the provenance stays true for the new rows:

```markdown
The rows from `/Users/{id}/Views` on were recorded on <date> by Phase 2's
probe, a second read-only script outside the repository, which printed
counts, seconds, booleans and type names, never an id, a name or a token.
The one shape it kept is `view_item.json`, every value in it invented.
```

If Step 4's follow-up ran, the paragraph says so before its last sentence: ``The recorded account's NextUp was empty, so the `Ids` row comes from a three-request follow-up that took its series from a listing.``

Fill every `<…>` from `facts2.out` (the `Ids` row from `facts2b.out`, if the follow-up ran), and `<date>` with the day Step 4 ran, before committing — nothing in angle brackets survives this step.

In the intro paragraph, change `**Every value in these seven files is invented**` to `**Every value in these eight files is invented**`, and add a row to the file table:

```markdown
| `view_item.json` | one library view from `/Users/{id}/Views`: `CollectionType` says what the library holds, and `boxsets`/`playlists` views are not libraries |
```

- [ ] **Step 7: Write the view fixture**

`tests/fixtures/emby/view_item.json`, with the six keys below, then every other key the probe's `view shape` line printed, each with an invented value of the printed type (ids zero-filled, as in the item fixtures):

```json
{
  "Name": "Example Library",
  "ServerId": "0000000000000000000000000000feed",
  "Id": "0000000000000000000000000000d001",
  "IsFolder": true,
  "Type": "CollectionFolder",
  "CollectionType": "movies"
}
```

Add `"tests/fixtures/emby/view_item.json",` after `"tests/fixtures/emby/push_sessions.json",` in `test_the_guard_reads_what_it_claims_to_read`'s list in `tests/unit/test_no_third_party_data.py`.

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_no_third_party_data.py tests/unit/test_docs_pointers.py`
Expected: PASS.

- [ ] **Step 8: The writer keeps up with the gate**

Spec "Measurement and acceptance" 2: the writer must sustain 400 items/s, the most the default gate can deliver. `scripts/measure_ingest.py` TRUNCATEs the catalog it is pointed at, so it runs only against a throwaway container — never `usher_catalog`, the dev database or production:

```bash
docker run -d --rm --name ffs-measure -e POSTGRES_USER=usher -e POSTGRES_PASSWORD=usher \
  -e POSTGRES_DB=usher -p 127.0.0.1:55432:5432 pgvector/pgvector:pg17
export USHER_DATABASE_URL=postgresql+asyncpg://usher:usher@127.0.0.1:55432/usher
export USHER_SECRET_KEY=0123456789abcdef0123456789abcdef
timeout 90 sh -c 'until uv run alembic upgrade head; do sleep 2; done'
PYTHONPATH=. uv run python scripts/measure_ingest.py --items 50000 --batch-size 1000 \
  | tee /var/tmp/sync-speed/measure_ingest.out
sha256sum /var/tmp/sync-speed/measure_ingest.out | tee /var/tmp/sync-speed/measure_ingest.out.sha256
docker stop ffs-measure
```

Expected: pass 1's `items / second` is at least **400**. If it is not, stop: the writer is fixed before any Phase 2 target is claimed. Post the numbers and ask the owner.

- [ ] **Step 9: Commit, and post the raw output**

```bash
git add tests/fixtures/emby/view_item.json tests/fixtures/emby/README.md tests/unit/test_no_third_party_data.py
git commit -m "emby: record the library, seed and concurrency facts the unit walk rests on"
```

Post `facts2.out`, `facts2b.out` (when Step 4's follow-up ran) and `measure_ingest.out`, raw and with their sha256 lines, as the first comment on PR 2 when it opens (Task 20); until then keep them under `/var/tmp/sync-speed/`.

---

### Task 11: The walk plan on the port

Spec §2.1. `SourceAdapter` gains two concrete methods: `plan_walk`, whose default is one unit, `all`, and `list_unit`, whose default for `all` is `list_items(None)` in pages. An adapter that does not override them walks exactly as it does today. Nothing calls them yet.

**Departure from the spec's letter (1):** `list_unit` yields `UnitPage(items, resume_at)` rather than bare items. The adapter alone knows where its overlap put the next request, so it is the one that says where a unit resumes.

**Files:**
- Modify: `src/usher/domain/sync.py` (`WalkStage`, `STAGE_ORDER`)
- Modify: `src/usher/ports/source.py` (`DEFAULT_UNIT_KEY`, `WalkUnit`, `WalkPlan`, `WHOLE_LIBRARY`, `UnitPage`, `pages_of`, `SourceAdapter.plan_walk`, `SourceAdapter.list_unit`)
- Modify: `tests/contract/source_harness.py` (`given_item_in_libraries`), `tests/contract/source_adapter_contract.py` (two cases)
- Modify: `tests/fakes/source_adapter.py` (`page_size`, `place`, `plan_walk`, `list_unit`, `FakeSourceHarness.given_item_in_libraries`), `tests/fakes/emby_harness.py` (`given_item_in_libraries`)
- Test: `tests/unit/test_domain_sync.py`, `tests/unit/test_ports_source.py`, `tests/unit/test_adapters_emby_contract.py`

**Interfaces:**
- Produces: `usher.domain.sync.WalkStage` (`SEED = "seed"`, `TITLES = "titles"`, `EPISODES = "episodes"`) and `STAGE_ORDER: tuple[WalkStage, ...]`.
- Produces, in `usher.ports.source`: `DEFAULT_UNIT_KEY = "all"`; `WalkUnit(key: str, stage: WalkStage, label: str, expected_items: int | None = None)`; `WalkPlan(units: tuple[WalkUnit, ...], expected_total: int | None = None)`; `WHOLE_LIBRARY: WalkPlan`; `UnitPage(items: tuple[SourceItem, ...], resume_at: int)`; `pages_of(items: AsyncIterator[SourceItem], *, start_index: int = 0, size: int = 1_000) -> AsyncGenerator[UnitPage]`; `SourceAdapter.plan_walk() -> WalkPlan` (async); `SourceAdapter.list_unit(key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]`.
- Produces: `SourceHarness.given_item_in_libraries(item, libraries: Sequence[str], *, changed_at)`; `FakeSourceAdapter.page_size` (2), `FakeSourceAdapter.place(external_id, *libraries)`. A fake with libraries plans one `TITLES` unit per library, keyed `library:<name>`.

- [ ] **Step 1: Write the failing domain and port tests**

Append to `tests/unit/test_domain_sync.py` (add `STAGE_ORDER, WalkStage` to its `usher.domain.sync` import):

```python
def test_a_whole_library_walk_runs_its_stages_seed_then_titles_then_episodes() -> None:
    """The stage barrier's order, and every stage has a place in it.

    A stage missing from the order is a stage whose units are never walked.
    """
    assert STAGE_ORDER == (WalkStage.SEED, WalkStage.TITLES, WalkStage.EPISODES)
    assert set(STAGE_ORDER) == set(WalkStage)
```

Append to `tests/unit/test_ports_source.py` (add `from collections.abc import AsyncIterator`, `from contextlib import aclosing`, `from datetime import UTC, datetime` and `from typing import TYPE_CHECKING`; `from usher.domain.enums import SourceKind` beside `HdrFormat`; `from usher.domain.ids import new_id`, `from usher.domain.source import Source`, `from usher.domain.sync import WalkStage`, `from usher.ports.errors import PortDataMalformed, PortUnavailable`; and `DEFAULT_UNIT_KEY, WHOLE_LIBRARY, SourceItem, SourceItemKind, UnitPage, pages_of` to the `usher.ports.source` import):

```python
# --- the walk plan ------------------------------------------------------------


def _walked(*external_ids: str) -> list[SourceItem]:
    return [
        SourceItem(external_id=one, name=one, kind=SourceItemKind.MOVIE) for one in external_ids
    ]


async def _stream(items: list[SourceItem]) -> AsyncIterator[SourceItem]:
    for item in items:
        yield item


async def _read(pages: AsyncIterator[UnitPage]) -> list[tuple[list[str], int]]:
    return [([item.external_id for item in page.items], page.resume_at) async for page in pages]


async def test_a_walk_is_paged_and_ends_on_its_short_page() -> None:
    pages = await _read(pages_of(_stream(_walked("a", "b", "c", "d", "e")), size=2))
    assert pages == [(["a", "b"], 2), (["c", "d"], 4), (["e"], 5)]


async def test_a_resume_point_counts_the_items_it_skipped() -> None:
    """`resume_at` is a `start_index`, so it counts what `start_index` skipped too.

    Counted from the page's own items, a resumed unit would come back two pages
    early and walk them twice; counted from nothing, it would come back at 0.
    """
    pages = await _read(pages_of(_stream(_walked("a", "b", "c", "d", "e")), start_index=3, size=2))
    assert pages == [(["d", "e"], 5)]


async def test_a_walk_that_fails_mid_page_yields_what_it_had_and_then_raises() -> None:
    """The items received before the failure are still a page; the error still surfaces."""

    async def failing() -> AsyncIterator[SourceItem]:
        for item in _walked("a", "b", "c"):
            yield item
        raise PortUnavailable("source went away mid-walk")

    seen: list[tuple[list[str], int]] = []
    with pytest.raises(PortUnavailable):
        async for page in pages_of(failing(), size=2):
            seen.append(([item.external_id for item in page.items], page.resume_at))
    assert seen == [(["a", "b"], 2), (["c"], 3)]


async def test_a_walk_is_never_paged_into_an_empty_page() -> None:
    """An empty page would read to the writer as progress that held nothing."""
    assert await _read(pages_of(_stream([]), size=2)) == []
    assert await _read(pages_of(_stream(_walked("a", "b")), size=2)) == [(["a", "b"], 2)]
    assert await _read(pages_of(_stream(_walked("a", "b")), start_index=5, size=2)) == []


async def test_a_consumer_that_stops_early_closes_the_walk_it_was_paging() -> None:
    """The walk under the pages is closed at once, not whenever it is collected.

    Left to the collector, a walk's read-ahead would keep a request in flight.
    """
    closed: list[bool] = []

    async def walk() -> AsyncIterator[SourceItem]:
        try:
            for item in _walked("a", "b", "c", "d", "e"):
                yield item
        finally:
            closed.append(True)

    async with aclosing(pages_of(walk(), size=2)) as pages:
        async for _ in pages:
            break
    assert closed == [True]


def _fake() -> "FakeSourceAdapter":
    from tests.fakes.source_adapter import FakeSourceAdapter

    return FakeSourceAdapter(
        Source(
            kind=SourceKind.EMBY,
            name="Walked Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-walked",
            device_id=str(new_id()),
        )
    )


async def test_the_default_plan_is_one_unit_holding_the_whole_library() -> None:
    """Called on the port itself, past the fake's own override."""
    plan = await SourceAdapter.plan_walk(_fake())
    assert plan == WHOLE_LIBRARY
    assert [(unit.key, unit.stage) for unit in plan.units] == [(DEFAULT_UNIT_KEY, WalkStage.TITLES)]


async def test_the_default_unit_is_list_items_in_pages() -> None:
    """An adapter that does not plan walks exactly as it always has, from any offset."""
    adapter = _fake()
    for item in _walked("m0", "m1", "m2"):
        adapter.seed(item, datetime(2026, 7, 1, tzinfo=UTC))
    pages = await _read(SourceAdapter.list_unit(adapter, DEFAULT_UNIT_KEY, start_index=1))
    assert pages == [(["m1", "m2"], 3)]


def test_the_default_unit_refuses_a_key_it_never_planned() -> None:
    with pytest.raises(PortDataMalformed):
        SourceAdapter.list_unit(_fake(), "library:Films")
```

The `"FakeSourceAdapter"` return annotation is a string; below the imports, add `if TYPE_CHECKING: from tests.fakes.source_adapter import FakeSourceAdapter`, using the `TYPE_CHECKING` import from the list above, the way the module already imports the fake lazily inside its cases.

- [ ] **Step 2: Write the failing contract cases**

`tests/contract/source_harness.py` — add `from collections.abc import Sequence`, and after `given_item`:

```python
    @abstractmethod
    async def given_item_in_libraries(
        self, item: SourceItem, libraries: Sequence[str], *, changed_at: AwareDatetime
    ) -> None:
        """`given_item`, with the item placed in each named library.

        A library is created the first time it is named. Two names put one item in
        two libraries, which real servers allow.
        """
```

`tests/contract/source_adapter_contract.py` — add `from contextlib import aclosing`, `from usher.domain.sync import WalkStage`, and `PortDataMalformed` to the `usher.ports.errors` import. Append to `SourceAdapterContract`, after the listing section:

```python
    # --- the walk plan -------------------------------------------------

    async def test_the_plan_s_units_together_yield_what_a_whole_walk_yields(
        self, harness: SourceHarness
    ) -> None:
        """The units outside `SEED`, taken together, cover `list_items()` exactly.

        An item in two libraries is yielded by both units and is still one item, and a
        series shares its library with its episode. A plan that dropped a library, or a
        unit that stopped after its first page, leaves an item out.
        """
        await harness.given_item_in_libraries(MOVIE, ["Films", "Favourites"], changed_at=T0)
        await harness.given_item_in_libraries(SERIES, ["Shows"], changed_at=T0)
        await harness.given_item_in_libraries(EPISODE, ["Shows"], changed_at=T0)
        for index in range(5):
            await harness.given_item_in_libraries(_filler(index), ["Films"], changed_at=T0)
        whole = {item.external_id async for item in harness.adapter.list_items()}
        assert len(whole) == 8, "the premise: the source holds eight items"

        plan = await harness.adapter.plan_walk()
        walked: set[str] = set()
        for unit in plan.units:
            if unit.stage is WalkStage.SEED:
                continue
            async with aclosing(harness.adapter.list_unit(unit.key)) as pages:
                async for page in pages:
                    walked.update(item.external_id for item in page.items)
        assert walked == whole

    async def test_a_unit_the_plan_never_named_is_refused(self, harness: SourceHarness) -> None:
        """A key this adapter never planned raises rather than walking nothing.

        An empty unit reads to the writer as a unit that completed, and a full walk
        would then sweep everything that unit should have held.
        """
        await self._seed_library(harness)
        with pytest.raises(PortDataMalformed):
            async with aclosing(harness.adapter.list_unit("no-such-unit")) as pages:
                async for _ in pages:
                    pass
```

`tests/unit/test_adapters_emby_contract.py`, `test_both_implementations_run_the_same_assertions`: `assert len(cases) == 52` becomes `== 54`, and both `52`s in its docstring become `54`.

The resume case waits for Task 13. Until `EmbyAdapter` pages its own units, the port's default groups its whole walk into pages of 1,000, so seven items are one page and a resume case would have nothing to resume.

- [ ] **Step 3: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_sync.py tests/unit/test_ports_source.py tests/unit/test_source_adapter_contract.py tests/unit/test_adapters_emby_contract.py`
Expected: collection errors, which interrupt the run before any case: `ImportError: cannot import name 'STAGE_ORDER'` from `test_domain_sync.py`, and `cannot import name 'WalkStage'` from the other three, where `from usher.domain.sync import WalkStage` sorts ahead of the port's names.

- [ ] **Step 4: The domain's stages**

`src/usher/domain/sync.py`, after `SyncRunStatus`:

```python
class WalkStage(StrEnum):
    """Which part of a whole-library walk a unit belongs to.

    `SEED` is what the account is watching, so the watch lane can run early;
    `TITLES` is movies and series; `EPISODES` waits until every title has
    committed, so each episode finds its series.
    """

    SEED = "seed"
    TITLES = "titles"
    EPISODES = "episodes"


#: The stage barrier's order: no unit of a stage is fetched before every unit of
#: the stages ahead of it has committed complete.
STAGE_ORDER: tuple[WalkStage, ...] = (WalkStage.SEED, WalkStage.TITLES, WalkStage.EPISODES)
```

- [ ] **Step 5: The port**

`src/usher/ports/source.py` — `from collections.abc import AsyncGenerator, AsyncIterator`; `from usher.domain.sync import WalkStage`; `from usher.ports.errors import PortDataMalformed, UsherPortError`. After `SourceEvent`:

```python
#: The one unit a plan holds when its adapter does not split the library.
DEFAULT_UNIT_KEY = "all"


@dataclass(frozen=True, slots=True)
class WalkUnit:
    """One piece of a whole-library walk.

    `key` is opaque and the adapter's own: stored like `MediaItem.external_id`
    and never in an API response. `label` is for logs and the CLI only.
    `expected_items` is a count taken while planning, or `None`.
    """

    key: str
    stage: WalkStage
    label: str
    expected_items: int | None = None


@dataclass(frozen=True, slots=True)
class WalkPlan:
    """The units of one whole-library walk, and the source's own total if it gave one."""

    units: tuple[WalkUnit, ...]
    expected_total: int | None = None


WHOLE_LIBRARY = WalkPlan((WalkUnit(DEFAULT_UNIT_KEY, WalkStage.TITLES, "the whole library"),))


@dataclass(frozen=True, slots=True)
class UnitPage:
    """One page of a unit: its items, and the `start_index` that resumes after it."""

    items: tuple[SourceItem, ...]
    resume_at: int


async def pages_of(
    items: AsyncIterator[SourceItem], *, start_index: int = 0, size: int = 1_000
) -> AsyncGenerator[UnitPage]:
    """A walk in pages of `size`, from its `start_index`th item.

    `resume_at` counts every item the walk produced, skipped ones included, so it
    is the `start_index` that resumes after the page. A walk that fails mid-page
    yields the items it had, then raises. Never yields an empty page. A consumer
    that stops closes `items` at once.
    """
    received = 0
    held: list[SourceItem] = []
    try:
        async for item in items:
            received += 1
            if received <= start_index:
                continue
            held.append(item)
            if len(held) >= size:
                yield UnitPage(tuple(held), resume_at=received)
                held = []
    except UsherPortError:
        if held:
            yield UnitPage(tuple(held), resume_at=received)
        raise
    finally:
        # A consumer that stops closes the walk now, read-ahead included, rather
        # than leaving it to the garbage collector.
        aclose = getattr(items, "aclose", None)
        if aclose is not None:
            await aclose()
    if held:
        yield UnitPage(tuple(held), resume_at=received)
```

In `SourceAdapter`, between `push_messages_received` and `probe_push`:

```python
    async def plan_walk(self) -> WalkPlan:
        """How to walk the whole library, unit by unit.

        By default one unit, `all`, which is `list_items(None)`. The units outside
        `SEED`, taken together, yield exactly the items `list_items(None)` yields,
        possibly more than once: they may overlap, because ingest upserts. A `SEED`
        unit may repeat items other units yield.
        """
        return WHOLE_LIBRARY

    def list_unit(self, key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]:
        """One unit of this adapter's plan, in pages, from `start_index`.

        Each page's `resume_at` is the `start_index` that resumes after it. Never
        yields an empty page, and raises rather than truncating, as `list_items`
        does. A key this adapter never planned raises `PortDataMalformed`.
        """
        if key != DEFAULT_UNIT_KEY:
            raise PortDataMalformed(f"no walk unit {key!r} in this adapter's plan")
        return pages_of(self.list_items(None), start_index=start_index)
```

- [ ] **Step 6: The fake plans by library, and both harnesses place items**

`tests/fakes/source_adapter.py` — add `from collections.abc import AsyncGenerator, AsyncIterator`, `from usher.domain.sync import WalkStage`, `PortDataMalformed` to the errors import, and `DEFAULT_UNIT_KEY, WHOLE_LIBRARY, UnitPage, WalkPlan, WalkUnit, pages_of` to the `usher.ports.source` import. In `FakeSourceAdapter.__init__`, after `self._fail_after`:

```python
        # Library name -> the external ids placed in it, in placement order. Empty
        # means the fake plans the port's default single unit.
        self._libraries: dict[str, list[str]] = {}
        #: Items per `list_unit` page: small, so a walk of a few items pages.
        self.page_size = 2
```

After `seed_state`:

```python
    def place(self, external_id: str, *libraries: str) -> None:
        """Put a seeded item in each named library, creating any not yet named."""
        for library in libraries:
            placed = self._libraries.setdefault(library, [])
            if external_id not in placed:
                placed.append(external_id)
```

After `_walk_items`:

```python
    async def plan_walk(self) -> WalkPlan:
        await self._ready()
        if not self._libraries:
            return WHOLE_LIBRARY
        units = tuple(
            WalkUnit(f"library:{name}", WalkStage.TITLES, f"library {name}", len(placed))
            for name, placed in self._libraries.items()
        )
        return WalkPlan(units, expected_total=len(self._items))

    def list_unit(self, key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]:
        if key == DEFAULT_UNIT_KEY:
            return pages_of(self._walk_items(None), start_index=start_index, size=self.page_size)
        name = key.removeprefix("library:")
        if name == key or name not in self._libraries:
            raise PortDataMalformed(f"no walk unit {key!r} in this source's plan")
        return pages_of(self._walk_library(name), start_index=start_index, size=self.page_size)

    async def _walk_library(self, name: str) -> AsyncIterator[SourceItem]:
        await self._ready()
        for external_id in list(self._libraries[name]):
            item = self._items.get(external_id)
            if item is not None:
                yield item
```

`FakeSourceHarness`, after `given_item` (add `from collections.abc import Sequence` to the module's imports):

```python
    async def given_item_in_libraries(
        self, item: SourceItem, libraries: Sequence[str], *, changed_at: AwareDatetime
    ) -> None:
        self._adapter.seed(item, changed_at)
        self._adapter.place(item.external_id, *libraries)
```

`tests/fakes/emby_harness.py`, after `given_item` (and `from collections.abc import Sequence`):

```python
    async def given_item_in_libraries(
        self, item: SourceItem, libraries: Sequence[str], *, changed_at: AwareDatetime
    ) -> None:
        """Libraries are ignored until the server can serve views.

        `EmbyAdapter` plans the port's single unit for now, which covers every item
        wherever it is placed.
        """
        self._server.add_item(item, changed_at)
```

- [ ] **Step 7: Run the tests**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_sync.py tests/unit/test_ports_source.py tests/unit/test_source_adapter_contract.py tests/unit/test_adapters_emby_contract.py`
Expected: PASS — the two contract cases run on both subclasses, and the count case reads 54.

- [ ] **Step 8: Plant and verify**

1. In `pages_of`, yield `resume_at=received - start_index` from the in-loop `yield` only, the one under `if len(held) >= size:`. Expect `test_a_resume_point_counts_the_items_it_skipped` to fail on `[(['d', 'e'], 2)] == [(['d', 'e'], 5)]`.
2. In `pages_of`'s `except`, drop the partial page (`raise` alone). Expect `test_a_walk_that_fails_mid_page_yields_what_it_had_and_then_raises` to fail, missing `(['c'], 3)`.
3. In `FakeSourceAdapter.plan_walk`, plan every library but the last (`list(self._libraries.items())[:-1]`). Expect `TestFakeSourceAdapter::test_the_plan_s_units_together_yield_what_a_whole_walk_yields` to fail on `walked == whole`.
4. In `SourceAdapter.list_unit`, drop the key check. Expect `test_the_default_unit_refuses_a_key_it_never_planned` and `TestEmbyAdapter::test_a_unit_the_plan_never_named_is_refused` to fail with `DID NOT RAISE`.
5. In `pages_of`, drop the `finally:` and its body. Expect `test_a_consumer_that_stops_early_closes_the_walk_it_was_paging` to fail on `[] == [True]`: the walk is closed only later, by the event loop's finalizer.

- [ ] **Step 9: Commit**

```bash
git add src/usher/domain/sync.py src/usher/ports/source.py tests/contract/source_harness.py \
  tests/contract/source_adapter_contract.py tests/fakes/source_adapter.py tests/fakes/emby_harness.py \
  tests/unit/test_domain_sync.py tests/unit/test_ports_source.py tests/unit/test_adapters_emby_contract.py
git commit -m "sources: a whole-library walk plan on the port, one unit by default"
```

---

### Task 12: Units and the heartbeat, persisted

Spec §2.5, "The migration". A table `sync_run_units` keyed on `(run_id, unit_key)`, a nullable `sync_runs.heartbeat_at`, and three repository methods. A unit's save follows `SyncRunRepository.save`'s two rules: `position` only rises and `completed` is final. Nothing writes either yet.

**Files:**
- Modify: `src/usher/domain/sync.py` (`SyncRunUnitStatus`, `SyncRunUnit`, `SyncRun.heartbeat_at`)
- Modify: `src/usher/ports/repository/sync.py` (`add_units`, `save_unit`, `units_for`)
- Modify: `src/usher/db/models/sync.py` (`SyncRunUnitRow`, `SyncRunRow.heartbeat_at`), `src/usher/db/models/__init__.py`
- Create: `src/usher/db/migrations/versions/m10g_sync_run_units.py`
- Modify: `src/usher/db/repositories/sync.py`, `tests/fakes/sync_run_repository.py`
- Modify: `src/usher/db/backup_manifest.py`, `docs/runbooks/disaster-recovery.md`, `scripts/audit_bounded_columns.py`
- Modify: `tests/unit/test_db_migration_status.py`, `tests/unit/test_backup_manifest.py`, `.claude/rules/db-and-sql.md`, `tests/integration/test_migrations.py`
- Test: `tests/contract/sync_run_repository_contract.py`, `tests/integration/test_sync_run_repository.py`, `tests/unit/test_db_models_ingest.py`, `tests/unit/test_domain_sync.py`, `tests/integration/test_bulk_repository.py`
- Modify: `docs/prd/02-data-model.md`

**Interfaces:**
- Consumes: Task 11's `WalkStage`.
- Produces: `SyncRunUnitStatus` (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`); `SyncRunUnit(run_id, unit_key, stage, label, position=0, expected_items=None, items_seen=0, status=PENDING)`; `SyncRun.heartbeat_at: AwareDatetime | None = None`.
- Produces: `SyncRunRepository.add_units(units: Sequence[SyncRunUnit]) -> None` (all or nothing), `.save_unit(unit: SyncRunUnit) -> None`, `.units_for(run_id: uuid.UUID) -> list[SyncRunUnit]` (in `unit_key` order).
- Produces: the contract helper `unit(run_id, key, **changes) -> SyncRunUnit` in `tests/contract/sync_run_repository_contract.py`.
- Produces: alembic head `m10g`.

- [ ] **Step 1: Write the failing domain and contract tests**

Append to `tests/unit/test_domain_sync.py` (import `SyncRunUnit`, `SyncRunUnitStatus`):

```python
def test_a_unit_starts_pending_at_position_zero() -> None:
    unit = SyncRunUnit(run_id=new_id(), unit_key="all", stage=WalkStage.TITLES, label="all")
    assert (unit.status, unit.position, unit.items_seen) == (SyncRunUnitStatus.PENDING, 0, 0)
    assert unit.expected_items is None


@pytest.mark.parametrize(
    "changes", [{"unit_key": ""}, {"position": -1}, {"items_seen": -1}, {"expected_items": -1}]
)
def test_a_unit_refuses_an_empty_key_and_negative_counts(changes: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "run_id": new_id(),
        "unit_key": "all",
        "stage": WalkStage.TITLES,
        "label": "all",
    }
    with pytest.raises(ValidationError):
        SyncRunUnit.model_validate(fields | changes)


def test_a_run_has_no_heartbeat_until_a_writer_gives_it_one() -> None:
    assert SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.FULL).heartbeat_at is None
```

In `tests/contract/sync_run_repository_contract.py`, import `SyncRunUnit, SyncRunUnitStatus, WalkStage` from `usher.domain.sync`, and add below `run`:

```python
def unit(run_id: uuid.UUID, key: str, **changes: object) -> SyncRunUnit:
    return SyncRunUnit.model_validate(
        {"run_id": run_id, "unit_key": key, "stage": WalkStage.TITLES, "label": key, **changes}
    )
```

Append to `SyncRunRepositoryContract`:

```python
    # --- a whole-library walk's units and heartbeat ------------------------

    async def test_a_runs_units_come_back_in_key_order(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        one = run(source_id)
        await repository.add(one)
        await repository.add_units(
            [
                unit(one.id, "charlie"),
                unit(one.id, "alpha", expected_items=40),
                unit(one.id, "bravo"),
            ]
        )
        stored = await repository.units_for(one.id)
        assert [each.unit_key for each in stored] == ["alpha", "bravo", "charlie"]
        assert stored[0] == unit(one.id, "alpha", expected_items=40)

    async def test_a_units_position_rises_and_never_falls(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`save`'s checkpoint rule, per unit, with its positive control first.

        Two attempts can hold one walk at once, and the slower one must not pull a
        unit back to the page it started from.
        """
        one = run(source_id)
        await repository.add(one)
        first = unit(one.id, "alpha")
        await repository.add_units([first])
        running = first.evolve(status=SyncRunUnitStatus.RUNNING)

        await repository.save_unit(running.evolve(position=2_000, items_seen=2_000))
        [stored] = await repository.units_for(one.id)
        assert stored.position == 2_000, "the positive control: a save moves the position"

        await repository.save_unit(running.evolve(position=1_000, items_seen=1_000))
        [stored] = await repository.units_for(one.id)
        assert stored.position == 2_000, "a slower attempt pulled the unit's checkpoint back"
        assert stored.items_seen == 1_000, "the rest of the row is the later write's"

    async def test_a_completed_unit_takes_no_further_write(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        one = run(source_id)
        await repository.add(one)
        first = unit(one.id, "alpha")
        await repository.add_units([first])
        await repository.save_unit(
            first.evolve(status=SyncRunUnitStatus.COMPLETED, position=7, items_seen=7)
        )

        await repository.save_unit(
            first.evolve(status=SyncRunUnitStatus.FAILED, position=3, items_seen=3)
        )

        [stored] = await repository.units_for(one.id)
        assert (stored.status, stored.position, stored.items_seen) == (
            SyncRunUnitStatus.COMPLETED,
            7,
            7,
        )

    async def test_saving_a_unit_that_was_never_added_is_not_found(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        one = run(source_id)
        await repository.add(one)
        with pytest.raises(RepositoryNotFound):
            await repository.save_unit(unit(one.id, "alpha"))

    async def test_a_plan_holding_a_stored_unit_is_refused_whole(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """All or nothing: a refused plan adds none of its units, not the ones before."""
        one = run(source_id)
        await repository.add(one)
        await repository.add_units([unit(one.id, "alpha")])

        with pytest.raises(RepositoryConflict) as caught:
            await repository.add_units([unit(one.id, "bravo"), unit(one.id, "alpha")])

        assert caught.value.constraint == "pk_sync_run_units"
        assert [each.unit_key for each in await repository.units_for(one.id)] == ["alpha"]

    async def test_a_unit_of_a_run_that_does_not_exist_is_refused(
        self, repository: SyncRunRepository
    ) -> None:
        with pytest.raises(RepositoryConflict) as caught:
            await repository.add_units([unit(uuid.uuid4(), "alpha")])
        assert caught.value.constraint == "fk_sync_run_units_run_id_sync_runs"

    async def test_each_run_has_its_own_units(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """One key in two runs is two units."""
        one, other = run(source_id), run(source_id, started_at=LATER)
        await repository.add(one)
        await repository.add(other)
        await repository.add_units([unit(one.id, "alpha", label="first")])
        await repository.add_units([unit(other.id, "alpha", label="second")])

        assert [each.label for each in await repository.units_for(one.id)] == ["first"]
        assert [each.label for each in await repository.units_for(other.id)] == ["second"]
        assert await repository.units_for(uuid.uuid4()) == []

    async def test_the_heartbeat_survives_every_read(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`get`, `latest_incomplete_run` and `list_for_source` each carry it.

        The last two read the row through a `SELECT *` of their own, so a column the
        model lacked, or the model a column, fails there and nowhere else. The run is
        a watch-state one: the kind `latest_incomplete_run` has always served.
        """
        one = run(source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=EARLIER)
        await repository.add(one)
        await repository.save(one.evolve(heartbeat_at=LATER, items_seen=10))

        stored = await repository.get(one.id)
        incomplete = await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE)
        [listed] = await repository.list_for_source(source_id)
        assert stored is not None and incomplete is not None
        assert (stored.heartbeat_at, incomplete.heartbeat_at, listed.heartbeat_at) == (
            LATER,
            LATER,
            LATER,
        )
```

Append to `tests/integration/test_sync_run_repository.py` (import `unit` from the contract module beside `run`, and `SyncRunUnit` beside `SyncRunKind`):

```python
async def test_a_negative_unit_position_is_a_port_error(
    repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """The CHECK mirrors `SyncRunUnit.position`'s `ge=0` for a writer that skips the model."""
    one = run(source_id)
    await repository.add(one)
    broken = SyncRunUnit.model_construct(**{**unit(one.id, "alpha").model_dump(), "position": -1})
    with pytest.raises(RepositoryConflict) as caught:
        await repository.add_units([broken])
    assert caught.value.constraint == "ck_sync_run_units_position_non_negative"


async def test_a_runs_units_go_with_it(
    session: AsyncSession, repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """`ON DELETE CASCADE`: a plan means nothing without its run."""
    one = run(source_id)
    await repository.add(one)
    await repository.add_units([unit(one.id, "alpha"), unit(one.id, "bravo")])
    count = text("SELECT count(*) FROM sync_run_units WHERE run_id = :id")
    assert (await session.execute(count, {"id": one.id})).scalar_one() == 2, "the premise"

    await session.execute(text("DELETE FROM sync_runs WHERE id = :id"), {"id": one.id})

    assert (await session.execute(count, {"id": one.id})).scalar_one() == 0


async def test_a_caught_unit_conflict_leaves_the_session_usable(
    repository: PostgresSyncRunRepository, source_id: uuid.UUID
) -> None:
    """The writer commits a unit with the batch it describes, as it does a run."""
    with pytest.raises(RepositoryConflict):
        await repository.add_units([unit(new_id(), "alpha")])
    one = run(source_id)
    await repository.add(one)
    await repository.add_units([unit(one.id, "alpha")])
    assert [each.unit_key for each in await repository.units_for(one.id)] == ["alpha"]
```

In `tests/unit/test_db_models_ingest.py` (import `SyncRunUnitRow`, `SyncRunUnit`, `SyncRunUnitStatus`, `WalkStage`):
- `test_all_ingest_tables_registered`: add `"sync_run_units"` to the set, which then outgrows a line, so the assertion reads:

```python
    assert {
        "seasons",
        "episodes",
        "jobs",
        "sync_runs",
        "sync_run_units",
        "raw_payloads",
    } <= set(Base.metadata.tables)
```

- `test_every_row_matches_its_domain_model_field_for_field`: add `assert _columns(SyncRunUnitRow) == set(SyncRunUnit.model_fields)`.
- `test_every_new_enum_column_stores_values_not_names`: add `(SyncRunUnitRow.__table__.c.stage, WalkStage)` and `(SyncRunUnitRow.__table__.c.status, SyncRunUnitStatus)`.
- `test_every_not_null_column_a_raw_insert_may_omit_has_a_server_default`: add `SyncRunUnitRow.__table__.c.position`, `.items_seen` and `.status`.
- `test_every_pydantic_bound_is_mirrored_by_a_named_check_constraint`: add

```python
    assert _constraint_names(SyncRunUnitRow, "CheckConstraint") == {
        "ck_sync_run_units_unit_key_not_empty",
        "ck_sync_run_units_position_non_negative",
        "ck_sync_run_units_expected_items_non_negative",
        "ck_sync_run_units_items_seen_non_negative",
    }
```

- `test_the_naming_convention_still_leaves_check_names_alone`: add `(SyncRunUnitRow, "ck_sync_run_units_ck_")`.

Bump the migration bookkeeping, which is red until the migration exists: in `tests/unit/test_db_migration_status.py`, `assert code_head_revision() == "m10f"` becomes `"m10g"`, and `"m10g",` is appended to `_REPOINTING_CHAIN`; in `.claude/rules/db-and-sql.md`, `**Sixteen landings, sixteen loud breaks.**` becomes `**Seventeen landings, seventeen loud breaks.**`.

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_sync.py tests/unit/test_sync_run_repository_contract.py tests/unit/test_db_models_ingest.py tests/unit/test_db_migration_status.py`
Expected: collection errors, which interrupt the run before any case: `ImportError: cannot import name 'SyncRunUnit'` from `test_domain_sync.py` and the contract runner, and `cannot import name 'SyncRunUnitRow'` from `test_db_models_ingest.py`.

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_db_migration_status.py`
Expected: two failures, `test_code_head_revision_matches_the_head_migration_on_disk` on `'m10f' == 'm10g'` and `test_the_repointing_chain_on_disk_is_the_one_three_documents_spell_out` on the chain's extra `'m10g'`. The landing-count case passes: the prose and the chain already agree on seventeen.

- [ ] **Step 3: The domain**

`src/usher/domain/sync.py`, after `STAGE_ORDER`:

```python
class SyncRunUnitStatus(StrEnum):
    """Where one unit of a whole-library walk stands.

    `COMPLETED` is final: the unit's walk ended and its last page committed.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
```

In `SyncRun`, after `finished_at`:

```python
    #: Moved on every commit by a whole-library walk's writer, and by the watch lane.
    #: A running walk whose heartbeat has gone stale is a dead one. An item walk with
    #: none never planned; a watch-state run with none predates heartbeats.
    heartbeat_at: AwareDatetime | None = None
```

After `SyncRun`:

```python
class SyncRunUnit(DomainModel):
    """One unit of a whole-library walk's plan, as its writer records it.

    `unit_key` is the adapter's own opaque key. `position` is where the unit
    resumes, in the adapter's own offsets, and only ever rises.
    """

    run_id: uuid.UUID
    unit_key: str = Field(min_length=1)
    stage: WalkStage
    label: str
    position: int = Field(default=0, ge=0)
    expected_items: int | None = Field(default=None, ge=0)
    items_seen: int = Field(default=0, ge=0)
    status: SyncRunUnitStatus = SyncRunUnitStatus.PENDING
```

- [ ] **Step 4: The port**

`src/usher/ports/repository/sync.py` — `from collections.abc import Sequence`; import `SyncRunUnit` beside `SyncRun`. Append to `SyncRunRepository`, before `list_for_source`:

```python
    @abstractmethod
    async def add_units(self, units: Sequence[SyncRunUnit]) -> None:
        """Insert a whole-library walk's plan, all of it or none of it.

        A unit already stored, or one whose run does not exist, raises
        `RepositoryConflict` and adds nothing.
        """

    @abstractmethod
    async def save_unit(self, unit: SyncRunUnit) -> None:
        """Update one stored unit, under `save`'s two rules.

        `position` never moves back, and a `completed` unit takes no further write.
        An unknown `(run_id, unit_key)` raises `RepositoryNotFound`.
        """

    @abstractmethod
    async def units_for(self, run_id: uuid.UUID) -> list[SyncRunUnit]:
        """A run's units in `unit_key` order; empty for a run without a plan."""
```

- [ ] **Step 5: The table and the column**

`src/usher/db/models/sync.py` — import `SyncRunUnitStatus, WalkStage` beside `SyncRunKind`. In `SyncRunRow`, after `finished_at`:

```python
    # Nullable with no server default: a run written before a writer heartbeat
    # existed has none, and that absence is what marks it a walk without a plan.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

After `SyncRunRow`:

```python
class SyncRunUnitRow(Base):
    """One unit of a whole-library walk's plan.

    Written only by `ReconcileService`'s writer: the plan in the commit after the
    run's first heartbeat, then each unit's progress with the batch it describes.
    `position` only rises and `completed` is final, the two rules `SyncRunRow`
    follows, and `PostgresSyncRunRepository.save_unit` enforces both.
    """

    __tablename__ = "sync_run_units"

    # CASCADE: a plan means nothing without the run it belongs to.
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True
    )
    # The adapter's own opaque key, stored the way `media_items.external_id` is.
    unit_key: Mapped[str] = mapped_column(Text, primary_key=True)
    stage: Mapped[WalkStage] = mapped_column(enum_column(WalkStage, length=16), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    # Where the unit resumes, in the adapter's offsets.
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    expected_items: Mapped[int | None] = mapped_column(Integer)
    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[SyncRunUnitStatus] = mapped_column(
        enum_column(SyncRunUnitStatus, length=16),
        nullable=False,
        server_default=text("'pending'"),
    )

    __table_args__ = (
        CheckConstraint("unit_key <> ''", name="ck_sync_run_units_unit_key_not_empty"),
        CheckConstraint('"position" >= 0', name="ck_sync_run_units_position_non_negative"),
        CheckConstraint(
            "expected_items >= 0", name="ck_sync_run_units_expected_items_non_negative"
        ),
        CheckConstraint("items_seen >= 0", name="ck_sync_run_units_items_seen_non_negative"),
    )
```

`src/usher/db/models/__init__.py`: `from usher.db.models.sync import RawPayloadRow, SyncRunRow, SyncRunUnitRow`, and `"SyncRunUnitRow",` after `"SyncRunRow",` in `__all__` — `scripts/audit_bounded_columns.py` attributes ORM writes through that list.

`src/usher/db/migrations/versions/m10g_sync_run_units.py`:

```python
"""A whole-library walk's plan: `sync_run_units`, and `sync_runs.heartbeat_at`."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m10g"
down_revision: str | Sequence[str] | None = "m10f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Never a server default: a run written before this revision has no heartbeat,
    # and that is what marks it as a walk without a plan.
    op.add_column("sync_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "sync_run_units",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("unit_key", sa.Text(), nullable=False),
        sa.Column(
            "stage",
            sa.Enum("seed", "titles", "episodes", name="walkstage", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expected_items", sa.Integer(), nullable=True),
        sa.Column("items_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                name="syncrununitstatus",
                native_enum=False,
                length=16,
            ),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.CheckConstraint("unit_key <> ''", name="ck_sync_run_units_unit_key_not_empty"),
        sa.CheckConstraint('"position" >= 0', name="ck_sync_run_units_position_non_negative"),
        sa.CheckConstraint(
            "expected_items >= 0", name="ck_sync_run_units_expected_items_non_negative"
        ),
        sa.CheckConstraint("items_seen >= 0", name="ck_sync_run_units_items_seen_non_negative"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sync_runs.id"],
            name=op.f("fk_sync_run_units_run_id_sync_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "unit_key", name=op.f("pk_sync_run_units")),
    )


def downgrade() -> None:
    op.drop_table("sync_run_units")
    op.drop_column("sync_runs", "heartbeat_at")
```

- [ ] **Step 6: The two repositories**

`src/usher/db/repositories/sync.py` — `from collections.abc import Sequence`; `select` beside `update` in the `sqlalchemy` import; `SyncRunUnitRow` beside `SyncRunRow`; `SyncRunUnit, SyncRunUnitStatus` beside `SyncRun`. Below `_MUTABLE`:

```python
_UNIT_MUTABLE = tuple(
    column.name
    for column in SyncRunUnitRow.__table__.columns
    if column.name not in {"run_id", "unit_key"}
)
```

Add to `PostgresSyncRunRepository`, before `list_for_source`:

```python
    async def add_units(self, units: Sequence[SyncRunUnit]) -> None:
        if not units:
            return
        try:
            # One SAVEPOINT around the whole plan, so a refused unit takes the units
            # before it with it and leaves the caller's other pending work alone.
            async with self._session.begin_nested():
                for unit in units:
                    self._session.add(SyncRunUnitRow(**unit.model_dump()))
                await self._session.flush()
        except DBAPIError as exc:
            if not is_row_refusal(exc):
                raise
            raise RepositoryConflict(
                f"the plan for sync run {units[0].run_id} conflicts with stored units",
                constraint=constraint_name(exc),
            ) from exc

    async def save_unit(self, unit: SyncRunUnit) -> None:
        stored = unit.model_dump()
        values: dict[str, Any] = {name: stored[name] for name in _UNIT_MUTABLE}
        # `save`'s two rules, for `save`'s reasons: the checkpoint only rises, and a
        # completed unit refuses the whole write.
        values["position"] = func.greatest(SyncRunUnitRow.position, unit.position)
        try:
            async with self._session.begin_nested():
                result = await self._session.execute(
                    update(SyncRunUnitRow)
                    .where(
                        SyncRunUnitRow.run_id == unit.run_id,
                        SyncRunUnitRow.unit_key == unit.unit_key,
                        SyncRunUnitRow.status != SyncRunUnitStatus.COMPLETED,
                    )
                    .values(**values)
                    .execution_options(synchronize_session="fetch")
                )
                if cast("CursorResult[Any]", result).rowcount:
                    return
                if await self._session.get(SyncRunUnitRow, (unit.run_id, unit.unit_key)) is None:
                    raise RepositoryNotFound(
                        f"no unit {unit.unit_key!r} of sync run {unit.run_id} to update"
                    )
        except DBAPIError as exc:
            if not is_row_refusal(exc):
                raise
            raise RepositoryConflict(
                f"unit {unit.unit_key!r} of sync run {unit.run_id} was refused",
                constraint=constraint_name(exc),
            ) from exc

    async def units_for(self, run_id: uuid.UUID) -> list[SyncRunUnit]:
        # `COLLATE "C"`: byte order, so the order is the fake's and never a locale's.
        statement = (
            select(SyncRunUnitRow)
            .where(SyncRunUnitRow.run_id == run_id)
            .order_by(SyncRunUnitRow.unit_key.collate("C"))
            .execution_options(populate_existing=True)
        )
        with self._session.no_autoflush:
            rows = (await self._session.execute(statement)).scalars().all()
        return [_unit_to_domain(row) for row in rows]
```

and beside `_to_domain` at the bottom of the module:

```python
def _unit_to_domain(row: SyncRunUnitRow) -> SyncRunUnit:
    return SyncRunUnit.model_validate(
        {column.name: getattr(row, column.name) for column in SyncRunUnitRow.__table__.columns}
    )
```

`populate_existing` is defensive. Nothing holds a unit row in the identity map today, because `add_units` lets its rows go once they are flushed and `save_unit` updates through SQL; a row held there, though, would come back from the select as it was before a `save_unit`, whose `position` is a SQL expression.

`tests/fakes/sync_run_repository.py` — `from collections.abc import Sequence`; import `SyncRunUnit, SyncRunUnitStatus`. In `__init__`: `self._units: dict[tuple[uuid.UUID, str], SyncRunUnit] = {}`. Before `list_for_source`:

```python
    async def add_units(self, units: Sequence[SyncRunUnit]) -> None:
        # All or nothing, as the SAVEPOINT makes it on Postgres.
        keys = [(unit.run_id, unit.unit_key) for unit in units]
        if len(set(keys)) != len(keys) or any(key in self._units for key in keys):
            raise RepositoryConflict(
                "a walk unit is already stored", constraint="pk_sync_run_units"
            )
        if any(unit.run_id not in self._runs for unit in units):
            raise RepositoryConflict(
                "a walk unit names no stored run",
                constraint="fk_sync_run_units_run_id_sync_runs",
            )
        self._units.update(zip(keys, units, strict=True))

    async def save_unit(self, unit: SyncRunUnit) -> None:
        key = (unit.run_id, unit.unit_key)
        stored = self._units.get(key)
        if stored is None:
            raise RepositoryNotFound(
                f"no unit {unit.unit_key!r} of sync run {unit.run_id} to update"
            )
        if stored.status is SyncRunUnitStatus.COMPLETED:
            return
        self._units[key] = unit.evolve(position=max(stored.position, unit.position))

    async def units_for(self, run_id: uuid.UUID) -> list[SyncRunUnit]:
        owned = [unit for (owner, _), unit in self._units.items() if owner == run_id]
        return sorted(owned, key=lambda unit: unit.unit_key)
```

- [ ] **Step 7: The ledgers that every new table joins**

`src/usher/db/backup_manifest.py`, after the `"sync_runs"` entry:

```python
        "sync_run_units": _rebuildable(
            "A whole-library walk's plan; the next walk plans again", "sync"
        ),
```

`tests/unit/test_backup_manifest.py`, `test_the_class_counts_are_the_ones_this_task_argued_for`: `BackupClass.REBUILDABLE: 20,` becomes `BackupClass.REBUILDABLE: 21,`.

`docs/runbooks/disaster-recovery.md`, the rebuilt column of the table: `` `curated_rows`, `user_taste`, `jobs`, `sync_runs`, `import_runs` `` becomes `` `curated_rows`, `user_taste`, `jobs`, `sync_runs`, `sync_run_units`, `import_runs` ``.

`scripts/audit_bounded_columns.py` — in `_DOMAIN_FOR_TABLE`, before `"sync_runs"`:

```python
    "sync_run_units": "usher.domain.sync:SyncRunUnit",
```

and `PUBLISHED` becomes:

```python
PUBLISHED: Mapping[str, Mapping[str, int]] = {
    "closure": {"safe": 22, "translated": 36, "exposed-copy": 30, "exposed-sqlalchemy": 1},
    "path": {"safe": 20, "translated": 37, "exposed-copy": 31, "exposed-sqlalchemy": 1},
    "pydantic": {"safe": 16, "translated": 37, "exposed-copy": 34, "exposed-sqlalchemy": 2},
}
```

Five new bounded columns, the same way on every reading, because none is staged: `stage` and `status` are `VARCHAR(16)` behind enums whose longest members are 8 and 9 characters, so **safe**; `position`, `expected_items` and `items_seen` are `INTEGER` bounded only below, written by `add_units`/`save_unit`, which translate on the SQLSTATE class, so **translated**.

The `safe` column moves by two on every reading, so `readings_table`'s docstring follows it: `` `safe` runs 20/18/14 today `` becomes `` `safe` runs 22/20/16 today ``. Its `m08b` figures stay, because `PUBLISHED_AT_M08B` does.

`tests/integration/test_bulk_repository.py` drives every translated column through its real writer. Import `SyncRunUnit, WalkStage` beside `SyncRun`; after `_refused_sync_run`:

```python
async def _refused_sync_run_unit(bed: _Bed, **changes: object) -> None:
    repository = PostgresSyncRunRepository(bed.session)
    owner = _sync_run(bed.source_id)
    await repository.add(owner)
    await repository.add_units(
        [
            SyncRunUnit(
                run_id=owner.id,
                unit_key="an-invented-unit",
                stage=WalkStage.TITLES,
                label="an invented unit",
                **changes,
            )
        ]
    )
```

and in `_BOUNDED_ARMS`, after the `sync_runs` arms:

```python
    ("sync_run_units", "expected_items"): lambda bed: _refused_sync_run_unit(
        bed, expected_items=_OVER_INT32
    ),
    ("sync_run_units", "items_seen"): lambda bed: _refused_sync_run_unit(
        bed, items_seen=_OVER_INT32
    ),
    ("sync_run_units", "position"): lambda bed: _refused_sync_run_unit(bed, position=_OVER_INT32),
```

`tests/integration/test_migrations.py`, `test_a_full_down_and_up_cycle_restores_every_index` — the `-1` half re-points onto `m10g`, and `m10f`'s assertion moves to a named stop. Replace from `at_m10e_columns = await column_set(url, "sync_runs")` down to the line before `# **A named stop at `m10d`, holding `m10e`'s one.**` with:

```python
        at_m10f_columns = await column_set(url, "sync_runs")
        assert "heartbeat_at" not in at_m10f_columns, "heartbeat_at should not exist below m10g"
        # The premise, for the reason the `m09a` stop below records: an empty
        # column set satisfies the absence above, so without this the block
        # would pass at any depth at which `sync_runs` had ceased to exist.
        assert at_m10f_columns, "the premise: `sync_runs` still exists at `m10f`"
        # One assertion per table a head creates, and `m10g` creates one.
        at_m10f_indexes = await index_set(url)
        assert "pk_sync_run_units" not in at_m10f_indexes, (
            "sync_run_units should not exist below m10g"
        )
        assert "pk_sync_runs" in at_m10f_indexes, "the premise: the index scan sees `sync_runs`"

        # **A named stop at `m10e`, holding `m10f`'s one.**
        await asyncio.to_thread(functools.partial(run_alembic, url, "m10e", direction="down"))
        at_m10e_columns = await column_set(url, "sync_runs")
        assert "error_code" not in at_m10e_columns, "error_code should not exist below m10f"
        assert at_m10e_columns, "the premise: `sync_runs` still exists at `m10e`"

```

- [ ] **Step 8: Say it in the PRD**

`docs/prd/02-data-model.md`, "Supporting tables" — replace the `sync_runs` row and add one after it:

```markdown
| `sync_runs` | Per-source run bookkeeping: kind, cursor, status, stats, and `heartbeat_at` (🔶 nothing moves it yet). One row per walk: an attempt that resumes an unfinished walk continues its row, and every other attempt starts one. `error` is prose and `error_code` is the kind — a nullable `VARCHAR(32)` holding `availability_ceiling` or `gap_delta_ceiling`, null for every failure an operator has no command for |
| `sync_run_units` | A whole-library walk's plan: `(run_id, unit_key, stage, label, position, expected_items, items_seen, status)`, keyed on `(run_id, unit_key)` and deleted with its run. `unit_key` is the adapter's own opaque key and never leaves the database. `position` only rises and `completed` is final |
```

- [ ] **Step 9: Run everything this task touched**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_sync.py tests/unit/test_sync_run_repository_contract.py tests/unit/test_db_models_ingest.py tests/unit/test_db_migration_status.py tests/unit/test_backup_manifest.py tests/unit/test_bounded_column_ledger.py tests/integration/test_sync_run_repository.py tests/integration/test_migrations.py tests/integration/test_bulk_repository.py`
Expected: PASS, with Docker up. Then `uv run python scripts/audit_bounded_columns.py --check` prints `no drift`. A `bucket drift` complaint means a column landed in a bucket other than the one Step 7 names — read which before changing a figure, because the figure is the claim.

- [ ] **Step 10: Plant and verify**

1. In `PostgresSyncRunRepository.save_unit`, drop the `status != COMPLETED` clause. Expect `TestPostgresSyncRunRepository::test_a_completed_unit_takes_no_further_write` to fail.
2. In the fake's `save_unit`, store `unit` as given. Expect `TestFakeSyncRunRepository::test_a_units_position_rises_and_never_falls` to fail on its second position assertion, and not on the positive control.
3. In `add_units` (Postgres), give each unit its own SAVEPOINT. Expect `test_a_plan_holding_a_stored_unit_is_refused_whole` to fail on `['alpha', 'bravo'] == ['alpha']`.
4. In `units_for` (Postgres), drop the `order_by`. Expect `test_a_runs_units_come_back_in_key_order` to fail on `['charlie', 'alpha', 'bravo']`: the case adds `charlie` first so that heap order is not key order.
5. In `m10g.downgrade()`, drop the `drop_table` line. Expect `test_a_full_down_and_up_cycle_restores_every_index` to fail on `sync_run_units should not exist below m10g`.

- [ ] **Step 11: Commit**

```bash
git add src/usher/domain/sync.py src/usher/ports/repository/sync.py src/usher/db/models/sync.py \
  src/usher/db/models/__init__.py src/usher/db/migrations/versions/m10g_sync_run_units.py \
  src/usher/db/repositories/sync.py src/usher/db/backup_manifest.py scripts/audit_bounded_columns.py \
  tests/fakes/sync_run_repository.py tests/contract/sync_run_repository_contract.py \
  tests/integration/test_sync_run_repository.py tests/integration/test_migrations.py \
  tests/integration/test_bulk_repository.py tests/unit/test_db_models_ingest.py \
  tests/unit/test_domain_sync.py tests/unit/test_db_migration_status.py tests/unit/test_backup_manifest.py \
  .claude/rules/db-and-sql.md docs/runbooks/disaster-recovery.md docs/prd/02-data-model.md
git commit -m "sync: persist a whole-library walk's units and its writer's heartbeat (m10g)"
```

---

### Task 13: Emby plans its walk by library

Spec §2.2, "Per library" and "Counts and coverage"; the seed unit is Task 14's. `EmbyAdapter.plan_walk` lists the account's views, skips collections and playlists, counts every library and the whole source at once, and plans one `TITLES` unit per library, then each library's episodes in `StartIndex` chunks — largest library first. When the libraries hold fewer items than the source, it plans one walk of everything instead. `list_unit` walks any unit it planned, from any resume point, through Phase 1's window. Nothing calls either yet.

A library is counted once, over every type a walk lists, so its count rides on its `TITLES` unit and its `EPISODES` units carry none. Splitting it between them would be inventing numbers; counting each kind separately would double the planner's requests, which the 2-minute target cannot afford.

**Files:**
- Create: `src/usher/adapters/emby/planning.py`
- Modify: `src/usher/adapters/emby/paging.py` (`OffsetWindow(stop=…)`, `cursor`, `request_limit`)
- Modify: `src/usher/adapters/emby/adapter.py` (`NOT_LIBRARIES`, `unit_max_items`, `_pages`, `_unit_pages`, `_items_path`, `plan_walk`, `list_unit`, `_library_unit`, `_views`, `_count`, `_known_libraries`; `_walk` and `_read` rebuilt on `_pages`)
- Modify: `src/usher/adapters/emby/session.py` (`_ROUTE_WORDS` gains `Views`)
- Modify: `tests/fakes/emby_server.py` (views, `place`, `ParentId`, `IncludeItemTypes`), `tests/fakes/emby_harness.py` (`given_item_in_libraries` places items in views), `tests/fixtures/emby/README.md` (what the fake renders from `view_item.json`)
- Modify: `tests/contract/source_adapter_contract.py` (the resume case), `tests/unit/test_adapters_emby_contract.py` (55)
- Test: `tests/unit/test_adapters_emby_planning.py` (new), `tests/unit/test_adapters_emby_paging.py`, `tests/unit/test_fakes_emby_server.py`, `tests/unit/test_adapters_emby_adapter.py`

**Interfaces:**
- Consumes: Task 11's `DEFAULT_UNIT_KEY`, `WalkUnit`, `WalkPlan`, `UnitPage`, `WalkStage`; Task 10's `view_item.json`; Phase 1's `OffsetWindow`, `PAGE_OVERLAP`, `_page`, `_settle`, `_listing_query`, `LIBRARY_SINCE_PARAM`.
- Produces, in `usher.adapters.emby.planning`: `LibraryUnit(stage: WalkStage, view_id: str, lower: int = 0, upper: int | None = None)` with `.key`, `.item_types` and `.label(library: str) -> str`; `episode_chunks(view_id: str, held: int, unit_max_items: int) -> list[LibraryUnit]`; `parse_unit_key(key: str) -> LibraryUnit | None`. Keys read `titles:<view>` and `episodes:<view>:<lower>:<upper, or empty for none>`.
- Produces: `OffsetWindow(*, limit, start, stop: int | None = None)`, `.cursor: int`, `.request_limit: int`.
- Produces: `EmbyAdapter(…, unit_max_items: int = 100_000)`; `EmbyAdapter._pages(query, *, start_index, stop=None, path=None) -> AsyncGenerator[tuple[list[dict[str, Any]], int]]`, each page's new entries with the `StartIndex` that resumes after it; `EmbyAdapter._unit_pages(query, *, start_index, stop=None) -> AsyncGenerator[UnitPage]`; `EmbyAdapter._read(path, query, start, limit, *, count)`. Task 14's seed reads through `_pages(path=…)`; Task 15's limiter wraps `_page`.
- Produces: `FakeEmbyServer.add_view(view_id, name, *, collection_type: str | None = "movies")`, `.remove_view(view_id)`, `.place(external_id, *view_ids)`.

- [ ] **Step 1: Write the failing tests for unit keys and chunks**

Create `tests/unit/test_adapters_emby_planning.py`:

```python
"""Emby's library units: their keys, and how a library's episodes split into chunks."""

import pytest

from usher.adapters.emby.planning import LibraryUnit, episode_chunks, parse_unit_key
from usher.domain.sync import WalkStage

VIEW = "0000000000000000000000000000d001"


@pytest.mark.parametrize(
    ("held", "expected"),
    [
        (0, [(0, None)]),
        (1, [(0, None)]),
        (100, [(0, None)]),
        (101, [(0, 100), (100, None)]),
        (250, [(0, 100), (100, 200), (200, None)]),
    ],
)
def test_a_library_s_episodes_split_into_chunks_and_the_last_runs_to_the_end(
    held: int, expected: list[tuple[int, int | None]]
) -> None:
    """The last chunk is open, so a library that grows during the walk is still covered."""
    chunks = episode_chunks(VIEW, held, 100)
    assert [(chunk.lower, chunk.upper) for chunk in chunks] == expected
    assert {(chunk.stage, chunk.view_id) for chunk in chunks} == {(WalkStage.EPISODES, VIEW)}


def test_a_unit_key_names_what_its_unit_lists() -> None:
    assert LibraryUnit(WalkStage.TITLES, VIEW).key == f"titles:{VIEW}"
    assert LibraryUnit(WalkStage.EPISODES, VIEW, 100, 200).key == f"episodes:{VIEW}:100:200"
    assert LibraryUnit(WalkStage.EPISODES, VIEW, 200).key == f"episodes:{VIEW}:200:"
    assert LibraryUnit(WalkStage.TITLES, VIEW).item_types == "Movie,Series"
    assert LibraryUnit(WalkStage.EPISODES, VIEW).item_types == "Episode"


@pytest.mark.parametrize(
    "unit",
    [
        LibraryUnit(WalkStage.TITLES, VIEW),
        LibraryUnit(WalkStage.EPISODES, VIEW),
        LibraryUnit(WalkStage.EPISODES, VIEW, 100, 200),
        LibraryUnit(WalkStage.EPISODES, VIEW, 200),
        LibraryUnit(WalkStage.EPISODES, "a:view:with:colons", 100, 200),
    ],
)
def test_every_unit_key_parses_back_to_its_unit(unit: LibraryUnit) -> None:
    """Parsed from the right, so a view id holding a colon still round-trips."""
    assert parse_unit_key(unit.key) == unit


@pytest.mark.parametrize(
    "key",
    [
        "all",
        "seed",
        "titles:",
        "episodes:",
        "episodes::0:",
        f"movies:{VIEW}",
        f"episodes:{VIEW}:1",
        f"episodes:{VIEW}:x:",
        f"episodes:{VIEW}: 1:",
        f"episodes:{VIEW}:-1:",
        f"episodes:{VIEW}:5:5",
        f"episodes:{VIEW}:6:5",
    ],
)
def test_a_key_that_names_no_library_unit_parses_to_none(key: str) -> None:
    assert parse_unit_key(key) is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_planning.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.emby.planning'`.

- [ ] **Step 3: The planning module**

Create `src/usher/adapters/emby/planning.py`:

```python
"""Emby's library units: what each key names, and a library's episodes in chunks."""

import re
from dataclasses import dataclass

from usher.domain.sync import WalkStage

_OFFSET = re.compile(r"[0-9]+")


@dataclass(frozen=True, slots=True)
class LibraryUnit:
    """One unit of a library: its titles, or a `StartIndex` range of its episodes.

    `upper` is exclusive, and `None` runs to the end of the library.
    """

    stage: WalkStage
    view_id: str
    lower: int = 0
    upper: int | None = None

    @property
    def key(self) -> str:
        if self.stage is WalkStage.TITLES:
            return f"titles:{self.view_id}"
        return f"episodes:{self.view_id}:{self.lower}:{'' if self.upper is None else self.upper}"

    @property
    def item_types(self) -> str:
        """The `IncludeItemTypes` this unit lists."""
        return "Movie,Series" if self.stage is WalkStage.TITLES else "Episode"

    def label(self, library: str) -> str:
        """How logs and the CLI name this unit."""
        if self.stage is WalkStage.TITLES:
            return f"titles in {library}"
        if self.upper is not None:
            return f"episodes in {library}, {self.lower:,} to {self.upper:,}"
        if self.lower:
            return f"episodes in {library}, from {self.lower:,}"
        return f"episodes in {library}"


def episode_chunks(view_id: str, held: int, unit_max_items: int) -> list[LibraryUnit]:
    """A library's episodes in chunks of `unit_max_items`, as many as it holds items for.

    The last chunk has no upper bound.
    """
    count = max(1, (held + unit_max_items - 1) // unit_max_items)
    return [
        LibraryUnit(
            WalkStage.EPISODES,
            view_id,
            index * unit_max_items,
            None if index == count - 1 else (index + 1) * unit_max_items,
        )
        for index in range(count)
    ]


def parse_unit_key(key: str) -> LibraryUnit | None:
    """The library unit `key` names, or `None` if it names none."""
    kind, _, rest = key.partition(":")
    if kind == "titles":
        return LibraryUnit(WalkStage.TITLES, rest) if rest else None
    if kind != "episodes":
        return None
    parts = rest.rsplit(":", 2)
    if len(parts) != 3:
        return None
    view_id, lower, upper = parts
    if not view_id or not _OFFSET.fullmatch(lower) or (upper and not _OFFSET.fullmatch(upper)):
        return None
    bounded = int(upper) if upper else None
    if bounded is not None and bounded <= int(lower):
        return None
    return LibraryUnit(WalkStage.EPISODES, view_id, int(lower), bounded)
```

Run the Step 2 command. Expected: PASS.

- [ ] **Step 4: Write the failing tests for a bounded window**

Append to `tests/unit/test_adapters_emby_paging.py`:

```python
def test_a_bounded_window_asks_for_no_more_than_its_stop_allows() -> None:
    """Neither the total nor a drained tail ends this walk; only the stop does."""
    window = OffsetWindow(limit=4, start=0, stop=5)
    assert window.request_limit == 4
    assert not window.receive(_entries("a", "b", "c", "d"), 9).ended
    assert window.advance() == 2
    assert window.request_limit == 3
    assert window.receive(_entries("c", "d", "e"), 0).ended


def test_a_bounded_window_already_at_its_stop_asks_for_nothing() -> None:
    assert OffsetWindow(limit=4, start=5, stop=5).request_limit == 0


def test_an_unbounded_window_always_asks_for_its_limit() -> None:
    assert OffsetWindow(limit=4, start=10_000).request_limit == 4


def test_the_cursor_is_where_the_page_just_served_ends() -> None:
    window = OffsetWindow(limit=4, start=10)
    window.receive(_entries("a", "b", "c"), 20)
    assert window.cursor == 13
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_paging.py`
Expected: FAIL — `TypeError: OffsetWindow.__init__() got an unexpected keyword argument 'stop'`, and `AttributeError` for `request_limit` and `cursor`.

- [ ] **Step 5: Bound the window**

`src/usher/adapters/emby/paging.py` — append to `OffsetWindow`'s docstring:

```python
    A bounded window never asks past `stop`, and reaching it ends the walk.
```

`__init__` takes the bound:

```python
    def __init__(self, *, limit: int, start: int, stop: int | None = None) -> None:
        self.limit = limit
        self.start = start
        self.stop = stop
```

(the rest of `__init__` unchanged). Add, after `__init__`:

```python
    @property
    def cursor(self) -> int:
        """Where the page just served ends."""
        return self._cursor

    @property
    def request_limit(self) -> int:
        """The `Limit` for the request at `start`: `limit`, cut short at `stop`."""
        if self.stop is None:
            return self.limit
        return max(0, min(self.limit, self.stop - self.start))
```

In `receive`, `stopped` joins the `ended` line's conditions; `short`, `reached` and `drained` stay as they are:

```python
        stopped = self.stop is not None and self._cursor >= self.stop
        ended = not entries or (short and reached) or drained or stopped
```

Run the Step 4 command. Expected: PASS.

- [ ] **Step 6: Teach the fake views, `ParentId` and `IncludeItemTypes`**

Append to `tests/unit/test_fakes_emby_server.py`:

```python
# --- views, ParentId and IncludeItemTypes --------------------------------


async def _listed(driver: _Driver, **params: str) -> list[str]:
    # A page wide enough for every item, as `_filtered` asks for: the fake's default
    # page of two would cut these three-item listings short.
    body = await driver.session.json_body(
        "GET",
        f"/Users/{USER_ID}/Items",
        params={"SortBy": "SortName", "Limit": "10", **params},
        op="list",
    )
    return [entry["Id"] for entry in body["Items"]]


def _given_three_movies(driver: _Driver) -> None:
    for index in range(3):
        driver.server.add_item(replace(MOVIE, external_id=f"m{index}", name=f"M {index}"), ADDED_AT)


async def test_views_are_listed_with_their_collection_types(driver: _Driver) -> None:
    films, sets, mixed = (f"{0xD001 + offset:032x}" for offset in range(3))
    driver.server.add_view(films, "Films")
    driver.server.add_view(sets, "Sets", collection_type="boxsets")
    driver.server.add_view(mixed, "Mixed", collection_type=None)
    body = await driver.session.json_body("GET", f"/Users/{USER_ID}/Views", op="views")
    assert [(view["Id"], view["Name"], view.get("CollectionType")) for view in body["Items"]] == [
        (films, "Films", "movies"),
        (sets, "Sets", "boxsets"),
        (mixed, "Mixed", None),
    ]
    assert {view["Type"] for view in body["Items"]} == {"CollectionFolder"}


async def test_a_parent_id_lists_only_what_was_placed_in_that_view(driver: _Driver) -> None:
    films, shows = f"{0xD001:032x}", f"{0xD002:032x}"
    driver.server.add_view(films, "Films")
    driver.server.add_view(shows, "Shows")
    _given_three_movies(driver)
    driver.server.place("m0", films)
    driver.server.place("m1", films, shows)
    assert await _listed(driver, ParentId=films) == ["m0", "m1"]
    assert await _listed(driver, ParentId=shows) == ["m1"]


async def test_a_parent_id_no_view_carries_lists_the_whole_library(driver: _Driver) -> None:
    """The worst answer a server can give, so the adapter must never send one."""
    films = f"{0xD001:032x}"
    driver.server.add_view(films, "Films")
    _given_three_movies(driver)
    driver.server.place("m0", films)
    assert await _listed(driver, ParentId=films) == ["m0"], "the control: a known view filters"
    driver.server.remove_view(films)
    assert await _listed(driver, ParentId=films) == ["m0", "m1", "m2"]


async def test_include_item_types_lists_only_those_types(driver: _Driver) -> None:
    driver.server.add_item(replace(MOVIE, external_id="m0", name="M 0"), ADDED_AT)
    driver.server.add_item(
        SourceItem(external_id="s0", name="S 0", kind=SourceItemKind.SERIES), ADDED_AT
    )
    driver.server.add_item(
        SourceItem(
            external_id="e0",
            name="E 0",
            kind=SourceItemKind.EPISODE,
            series_external_id="s0",
            season_number=1,
            episode_number=1,
        ),
        ADDED_AT,
    )
    assert await _listed(driver, IncludeItemTypes="Episode") == ["e0"]
    assert await _listed(driver, IncludeItemTypes="Movie,Series") == ["m0", "s0"]
    assert await _listed(driver, IncludeItemTypes="Movie,Series,Episode") == ["e0", "m0", "s0"]
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_fakes_emby_server.py -k "views or parent_id or include_item_types"`
Expected: FAIL — `AttributeError: 'FakeEmbyServer' object has no attribute 'add_view'`, and the types case listing all three items.

`tests/fakes/emby_server.py` — beside `_ITEMS`:

```python
_VIEWS = re.compile(r"^/Users/(?P<user>[^/]+)/Views$")
```

and after `_EMBY_PROVIDER_KEYS`:

```python
# What `IncludeItemTypes` calls each kind.
_TYPE_NAMES = {
    SourceItemKind.MOVIE: "Movie",
    SourceItemKind.SERIES: "Series",
    SourceItemKind.EPISODE: "Episode",
}
```

In `__init__`, after `self._states`:

```python
        # View id -> (name, `CollectionType` or `None`), and view id -> the external
        # ids placed in it, in placement order.
        self._views: dict[str, tuple[str, str | None]] = {}
        self._placed: dict[str, list[str]] = {}
```

After `remove_item`:

```python
    def add_view(self, view_id: str, name: str, *, collection_type: str | None = "movies") -> None:
        """A view the account sees. `None` is a library of mixed content, which has no type."""
        self._views[view_id] = (name, collection_type)
        self._placed.setdefault(view_id, [])

    def remove_view(self, view_id: str) -> None:
        self._views.pop(view_id, None)
        self._placed.pop(view_id, None)

    def place(self, external_id: str, *view_ids: str) -> None:
        """Put a seeded item in each named view."""
        for view_id in view_ids:
            placed = self._placed[view_id]
            if external_id not in placed:
                placed.append(external_id)
```

In `handle`, before the `_ITEMS` route:

```python
        if request.method == "GET" and _VIEWS.match(path):
            return self._list_views()
```

In `_ordered`, beside `since` and `wanted`:

```python
        parent = params.get("ParentId")
        # A `ParentId` no view carries filters nothing: the worst answer a server can
        # give, and the one the adapter must never provoke.
        placed = set(self._placed[parent]) if parent in self._placed else None
        types = {name for name in (params.get("IncludeItemTypes") or "").split(",") if name}
```

and in its loop, after the `_passes` check:

```python
            if placed is not None and external_id not in placed:
                continue
            if types and _TYPE_NAMES.get(item.kind) not in types:
                continue
```

After `_list`:

```python
    def _list_views(self) -> httpx.Response:
        """`GET /Users/{userId}/Views`, each view rendered from `view_item.json`."""
        entries: list[dict[str, Any]] = []
        for view_id, (name, collection_type) in self._views.items():
            entry = load_emby_fixture("view_item")
            entry.update(Id=view_id, Name=name)
            if collection_type is None:
                entry.pop("CollectionType", None)
            else:
                entry["CollectionType"] = collection_type
            entries.append(entry)
        return httpx.Response(200, json={"Items": entries, "TotalRecordCount": len(entries)})
```

`tests/fixtures/emby/README.md` names what the fake renders from these files, and from here that includes the views. Its lines `` identity fields from the seeded `SourceItem` — the item routes from the four `` and `` item fixtures, and `user_data_changed_frame`/`library_changed_frame`/ `` become:

```markdown
identity fields from the seeded `SourceItem` — the item routes from the four
item fixtures, `/Users/{id}/Views` from `view_item.json`, and
`user_data_changed_frame`/`library_changed_frame`/
```

Run the command above. Expected: PASS.

- [ ] **Step 7: Write the failing adapter tests**

In `tests/unit/test_adapters_emby_adapter.py`: add `from contextlib import aclosing`; `PAGE_OVERLAP` from `usher.adapters.emby.paging`; `from usher.domain.sync import WalkStage`; `DEFAULT_UNIT_KEY, WalkUnit` to the `usher.ports.source` import. Give `_adapter` a `unit_max_items: int = 100_000` keyword it passes to `EmbyAdapter`. After `_numbered`, add:

```python
def _series(index: int) -> SourceItem:
    return SourceItem(
        external_id=f"series-{index}",
        name=f"Series {index}",
        kind=SourceItemKind.SERIES,
        added_at=T0,
    )


def _episode(index: int) -> SourceItem:
    """Episode `index` of `series-0`; zero-padded, so listing order is index order."""
    return SourceItem(
        external_id=f"episode-{index:03d}",
        name=f"Episode {index:03d}",
        kind=SourceItemKind.EPISODE,
        series_external_id="series-0",
        season_number=1,
        episode_number=index + 1,
        added_at=T0,
    )


def _library(
    server: FakeEmbyServer,
    number: int,
    name: str,
    items: Sequence[SourceItem],
    *,
    collection_type: str | None = "movies",
) -> str:
    """A view holding `items`, each seeded into the server. Returns the view's id."""
    view_id = f"{0xD000 + number:032x}"
    server.add_view(view_id, name, collection_type=collection_type)
    for item in items:
        server.add_item(item, T0)
        server.place(item.external_id, view_id)
    return view_id


def _recorded(
    server: FakeEmbyServer, *, page_size: int = 2, unit_max_items: int = 100_000
) -> tuple[EmbyAdapter, list[httpx.Request]]:
    """An adapter over `server`, and every request it sends, query included."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return server.handle(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url=SOURCE.base_url)
    adapter = EmbyAdapter(
        SOURCE, CREDENTIALS, client=client, page_size=page_size, unit_max_items=unit_max_items
    )
    return adapter, seen


def _listings(seen: Sequence[httpx.Request]) -> list[httpx.Request]:
    return [request for request in seen if request.url.path.endswith("/Items")]
```

Then a new section, before `# --- the redacted request path`:

```python
# --- the walk plan -----------------------------------------------------


async def test_each_library_is_planned_as_its_titles_then_its_episodes_largest_first() -> None:
    """Every TITLES unit before every EPISODES unit, each stage largest library first.

    The smaller library is added first, so the order is the planner's, not the server's.
    """
    server = FakeEmbyServer()
    films = _library(server, 1, "Films", [_movie(0)])
    shows = _library(
        server,
        2,
        "Shows",
        [_series(0), *(_episode(index) for index in range(3))],
        collection_type="tvshows",
    )
    adapter = _adapter(server)
    try:
        plan = await adapter.plan_walk()
    finally:
        await adapter.aclose()
    walks = tuple(unit for unit in plan.units if unit.stage is not WalkStage.SEED)
    assert walks == (
        WalkUnit(f"titles:{shows}", WalkStage.TITLES, "titles in Shows", 4),
        WalkUnit(f"titles:{films}", WalkStage.TITLES, "titles in Films", 1),
        WalkUnit(f"episodes:{shows}:0:", WalkStage.EPISODES, "episodes in Shows"),
        WalkUnit(f"episodes:{films}:0:", WalkStage.EPISODES, "episodes in Films"),
    )
    assert plan.expected_total == 5


async def test_a_titles_unit_lists_titles_and_an_episodes_unit_lists_episodes() -> None:
    """The stage barrier rests on this: an episode in a TITLES unit lands before its series."""
    server = FakeEmbyServer()
    shows = _library(
        server, 2, "Shows", [_series(0), _episode(0), _episode(1)], collection_type="tvshows"
    )
    adapter = _adapter(server)
    try:
        titles = [
            item.external_id
            async for page in adapter.list_unit(f"titles:{shows}")
            for item in page.items
        ]
        episodes = [
            item.external_id
            async for page in adapter.list_unit(f"episodes:{shows}:0:")
            for item in page.items
        ]
    finally:
        await adapter.aclose()
    assert titles == ["series-0"]
    assert sorted(episodes) == ["episode-000", "episode-001"]


async def test_a_library_s_episodes_are_planned_in_chunks_of_the_unit_size() -> None:
    server = FakeEmbyServer()
    shows = _library(
        server,
        2,
        "Shows",
        [_series(0), *(_episode(index) for index in range(6))],
        collection_type="tvshows",
    )
    adapter = _adapter(server, unit_max_items=3)
    try:
        plan = await adapter.plan_walk()
    finally:
        await adapter.aclose()
    episodes = [unit for unit in plan.units if unit.stage is WalkStage.EPISODES]
    assert [(unit.key, unit.label) for unit in episodes] == [
        (f"episodes:{shows}:0:3", "episodes in Shows, 0 to 3"),
        (f"episodes:{shows}:3:6", "episodes in Shows, 3 to 6"),
        (f"episodes:{shows}:6:", "episodes in Shows, from 6"),
    ]


async def test_a_bounded_chunk_reads_past_its_end_by_the_overlap_and_no_further() -> None:
    """Chunk 0 to 100 reads episodes 0 to 149, and no request reaches past 150.

    The reach past its end covers a shift at the boundary with the next chunk.
    """
    server = FakeEmbyServer()
    shows = _library(
        server,
        2,
        "Shows",
        [_series(0), *(_episode(index) for index in range(249))],
        collection_type="tvshows",
    )
    adapter, seen = _recorded(server, page_size=40, unit_max_items=100)
    try:
        read = {
            item.external_id
            async for page in adapter.list_unit(f"episodes:{shows}:0:100")
            for item in page.items
        }
    finally:
        await adapter.aclose()
    ends = [
        int(request.url.params["StartIndex"]) + int(request.url.params["Limit"])
        for request in _listings(seen)
    ]
    assert max(ends) == 100 + PAGE_OVERLAP
    assert read == {f"episode-{index:03d}" for index in range(100 + PAGE_OVERLAP)}


async def test_a_chunk_resumed_at_its_stop_asks_for_nothing() -> None:
    """Its last page committed and the attempt died before the unit did."""
    server = FakeEmbyServer()
    shows = _library(
        server, 2, "Shows", [_series(0), *(_episode(index) for index in range(6))]
    )
    adapter, seen = _recorded(server, unit_max_items=3)
    try:
        pages = [
            page
            async for page in adapter.list_unit(
                f"episodes:{shows}:0:3", start_index=3 + PAGE_OVERLAP
            )
        ]
    finally:
        await adapter.aclose()
    assert pages == []
    assert _listings(seen) == []


async def test_a_page_s_resume_point_is_the_next_request_s_start_reach_back_included() -> None:
    """Resuming there re-reads the page's tail, so a deletion between attempts skips nothing."""
    server = FakeEmbyServer()
    for item in _numbered(10):
        server.add_item(item, T0)
    adapter, seen = _recorded(server, page_size=4)
    try:
        async with aclosing(adapter.list_unit(DEFAULT_UNIT_KEY)) as pages:
            first = await anext(pages)
            await anext(pages)
    finally:
        await adapter.aclose()
    assert first.resume_at == 4 - 2, "a page of four reaches back two"
    assert int(_listings(seen)[1].url.params["StartIndex"]) == first.resume_at


async def test_a_movie_library_s_episodes_unit_ends_on_one_empty_page() -> None:
    """Every library gets an EPISODES unit, so most hold nothing; each costs one request."""
    server = FakeEmbyServer()
    _library(server, 1, "Films", [_movie(index) for index in range(5)])
    adapter, seen = _recorded(server)
    try:
        plan = await adapter.plan_walk()
        [episodes] = [unit for unit in plan.units if unit.stage is WalkStage.EPISODES]
        before = len(seen)
        pages = [page async for page in adapter.list_unit(episodes.key)]
    finally:
        await adapter.aclose()
    listings = _listings(seen[before:])
    assert pages == []
    assert len(listings) == 1
    assert listings[0].url.params["IncludeItemTypes"] == "Episode"


async def test_libraries_that_hold_less_than_the_total_fall_back_to_one_walk() -> None:
    """An item in no library is still walked, and the WARNING names both numbers.

    A full walk over the libraries alone would sweep that item as unavailable.
    """
    server = FakeEmbyServer()
    _library(server, 1, "Films", [_movie(0), _movie(1)])
    server.add_item(_movie(2), T0)
    lines: list[str] = []
    handle = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter = _adapter(server)
        try:
            plan = await adapter.plan_walk()
            walks = tuple(unit for unit in plan.units if unit.stage is not WalkStage.SEED)
            walked = {
                item.external_id
                async for page in adapter.list_unit(walks[0].key)
                for item in page.items
            }
        finally:
            await adapter.aclose()
    finally:
        logger.remove(handle)
    assert walks == (WalkUnit(DEFAULT_UNIT_KEY, WalkStage.TITLES, "the whole library", 3),)
    assert plan.expected_total == 3
    assert walked == {"movie-0", "movie-1", "movie-2"}
    assert [line.rstrip("\n") for line in lines] == [
        "Living Room Emby's libraries hold 2 items against a total of 3; "
        "walking the whole library as one unit"
    ]


async def test_collection_and_playlist_views_are_not_libraries() -> None:
    """Neither is planned or counted: both only point at items held in libraries.

    Counted, the collection's two items would lift the libraries' sum to the total
    and hide that `movie-2` is in no library at all.
    """
    server = FakeEmbyServer()
    films = _library(server, 1, "Films", [_movie(0), _movie(1)])
    _library(server, 2, "Sets", [_movie(0), _movie(1)], collection_type="boxsets")
    _library(server, 3, "Mix", [_movie(1)], collection_type="playlists")
    server.add_item(_movie(2), T0)
    adapter, seen = _recorded(server)
    try:
        plan = await adapter.plan_walk()
    finally:
        await adapter.aclose()
    assert {request.url.params.get("ParentId") for request in _listings(seen)} == {None, films}
    walks = [unit.key for unit in plan.units if unit.stage is not WalkStage.SEED]
    assert walks == [DEFAULT_UNIT_KEY]


async def test_a_library_removed_between_attempts_ends_its_unit_empty() -> None:
    """A resumed unit whose view is gone yields nothing and never sends that view's id.

    The fake answers an unknown `ParentId` with the whole library, the worst case.
    """
    server = FakeEmbyServer()
    films = _library(server, 1, "Films", [_movie(0), _movie(1)])
    shows = _library(server, 2, "Shows", [_series(0)], collection_type="tvshows")
    planner = _adapter(server)
    try:
        plan = await planner.plan_walk()
    finally:
        await planner.aclose()
    assert f"titles:{shows}" in {unit.key for unit in plan.units}, "the premise: it was planned"

    server.remove_view(shows)
    resumed, seen = _recorded(server)
    try:
        gone = [page async for page in resumed.list_unit(f"titles:{shows}")]
        kept = [
            item.external_id
            async for page in resumed.list_unit(f"titles:{films}")
            for item in page.items
        ]
    finally:
        await resumed.aclose()
    assert gone == []
    assert sorted(kept) == ["movie-0", "movie-1"], "the control: a library still there walks"
    assert {request.url.params.get("ParentId") for request in _listings(seen)} == {films}


async def test_walkers_resuming_together_read_the_libraries_once() -> None:
    server = FakeEmbyServer()
    films = _library(server, 1, "Films", [_movie(0)])
    shows = _library(server, 2, "Shows", [_series(0)], collection_type="tvshows")
    transport = SlowTransport(server.handle)
    adapter = EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(transport=transport, base_url=SOURCE.base_url),
        page_size=2,
    )

    async def walk(key: str) -> list[str]:
        return [item.external_id async for page in adapter.list_unit(key) for item in page.items]

    try:
        await asyncio.gather(walk(f"titles:{films}"), walk(f"titles:{shows}"))
    finally:
        await adapter.aclose()
    assert transport.max_in_flight >= 2, "the premise: the two walkers overlapped"
    assert sum(1 for request in server.requests if request.endswith("/Views")) == 1


async def test_a_count_that_fails_fails_the_plan() -> None:
    """A library left uncounted is not a library of nothing."""
    server = FakeEmbyServer()
    films = _library(server, 1, "Films", [_movie(0)])

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("ParentId") == films:
            return httpx.Response(400, json={"Error": "refused"})
        return server.handle(request)

    adapter = _on(handle)
    try:
        with pytest.raises(PortUnavailable):
            await adapter.plan_walk()
    finally:
        await adapter.aclose()


async def test_a_count_without_a_total_fails_the_plan() -> None:
    """Read as zero, a missing source total would pass the coverage check."""
    server = FakeEmbyServer()
    _library(server, 1, "Films", [_movie(0)])

    def handle(request: httpx.Request) -> httpx.Response:
        response = server.handle(request)
        if request.url.params.get("Limit") != "0":
            return response
        body = response.json()
        del body["TotalRecordCount"]
        return httpx.Response(200, json=body)

    adapter = _on(handle)
    try:
        with pytest.raises(PortDataMalformed):
            await adapter.plan_walk()
    finally:
        await adapter.aclose()
```

In `test_every_path_this_adapter_issues_redacts_to_a_route_with_no_identifier`, after `server.set_watch_state(...)`:

```python
    films = f"{0xD001:032x}"
    server.add_view(films, "Films")
    server.place("movie-1", films)
```

after the `push_watch_state` line inside the `try`:

```python
        plan = await adapter.plan_walk()
        _ = [page async for page in adapter.list_unit(plan.units[0].key)]
```

and add `"/Users/{user_id}/Views",` to the pinned set.

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py`
Expected: FAIL, four ways, the last three because the adapter still plans with the port's default:
- `TypeError: EmbyAdapter.__init__() got an unexpected keyword argument 'unit_max_items'` in every case built through `_adapter` or `_recorded`, the existing `_adapter` cases among them;
- `test_walkers_resuming_together_read_the_libraries_once`, which builds its own adapter, with `PortDataMalformed`: the default plan holds no `titles:` unit;
- `test_a_count_that_fails_fails_the_plan` and `test_a_count_without_a_total_fails_the_plan`, built through `_on`, with `DID NOT RAISE`: the default plan counts nothing;
- the redaction case on the pinned `/Users/{user_id}/Views`, which nothing has issued yet.

- [ ] **Step 8: Plan, and walk the plan**

`src/usher/adapters/emby/session.py` — add `"Views",` to `_ROUTE_WORDS`.

`src/usher/adapters/emby/adapter.py` — import `PAGE_OVERLAP` beside `OffsetWindow`, `from usher.adapters.emby.planning import LibraryUnit, episode_chunks, parse_unit_key`, `from usher.domain.sync import WalkStage`, and `DEFAULT_UNIT_KEY, UnitPage, WalkPlan, WalkUnit` from `usher.ports.source`. After `ITEM_TYPES`:

```python
# Views that only point at items held in libraries. Walking them reads those
# items twice, and counting them can lift the libraries' sum to the source's
# total while an item sits in no library at all.
NOT_LIBRARIES = frozenset({"boxsets", "playlists"})
```

`__init__` takes `unit_max_items: int = 100_000` after `max_pages`, and stores, after `self._max_pages`:

```python
        self._unit_max_items = unit_max_items
        # The library ids, read by the plan or by the first unit a resumed walk asks for.
        self._library_ids: frozenset[str] | None = None
        self._library_lock = asyncio.Lock()
```

Replace `_walk` and `_read` with:

```python
    async def _walk(
        self, query: Mapping[str, str], *, start_index: int
    ) -> AsyncGenerator[dict[str, Any]]:
        """Every new entry of one listing, from `start_index` to its end."""
        async with aclosing(self._pages(query, start_index=start_index)) as pages:
            async for entries, _ in pages:
                for entry in entries:
                    yield entry

    async def _pages(
        self,
        query: Mapping[str, str],
        *,
        start_index: int,
        stop: int | None = None,
        path: str | None = None,
    ) -> AsyncGenerator[tuple[list[dict[str, Any]], int]]:
        """Page one listing to its end, one request ahead of the consumer.

        Yields each page's new entries with the `StartIndex` that resumes after it,
        which is the next request's start, reach-back included. The next page is
        asked for as soon as a page arrives and before it is yielded; the request
        outstanding when the consumer stops is cancelled. `stop` bounds the walk,
        and one already at its stop sends nothing. `start_index` is the resume
        point (#41), never defaulted: every caller states its own.
        """
        if path is None:
            path = await self._items_path()
        window = OffsetWindow(limit=self._page_size, start=start_index, stop=stop)
        if window.request_limit == 0:
            return
        pending = self._read(path, query, window.start, window.request_limit, count=True)
        try:
            # `for`, not `while True`: the bound is part of the loop, and the raise
            # below is reachable only by a walk that never ended.
            for number in range(1, self._max_pages + 1):
                body = await pending
                entries = body.get("Items")
                if not isinstance(entries, list):
                    # Not a truncation: a caller must be able to tell "the library
                    # ended" from "that was not a listing at all".
                    raise PortDataMalformed(
                        "Emby's item listing carried no Items array",
                        detail=f"StartIndex={window.start}",
                    )
                page = window.receive(entries, body.get("TotalRecordCount"))
                if page.shifted:
                    logger.warning(
                        "{source}'s listing shifted by at least {overlap} items before "
                        "StartIndex={start}; an item shifted further was not read, and the "
                        "next full walk reads it",
                        source=self._source.name,
                        overlap=window.overlap,
                        start=window.start,
                    )
                resume_at = window.cursor
                if not page.ended and number < self._max_pages:
                    resume_at = window.advance()
                    pending = self._read(path, query, resume_at, window.request_limit, count=False)
                yield page.fresh, resume_at
                if page.ended:
                    return
        finally:
            await _settle(pending)
        raise PortDataMalformed(
            "Emby's item listing never ended; the server appears to ignore StartIndex "
            "or to cap Limit far below the page size",
            detail=f"gave up after {self._max_pages} pages at StartIndex={window.start}",
        )

    def _read(
        self, path: str, query: Mapping[str, str], start: int, limit: int, *, count: bool
    ) -> asyncio.Task[dict[str, Any]]:
        """One page's request, started now and awaited when the walk reaches it."""
        params = {
            **query,
            "StartIndex": str(start),
            "Limit": str(limit),
            "EnableTotalRecordCount": "true" if count else "false",
        }
        return asyncio.create_task(self._page(path, params, start))

    async def _items_path(self) -> str:
        return f"/Users/{_segment(await self._session.user_id())}/Items"

    async def _unit_pages(
        self, query: Mapping[str, str], *, start_index: int, stop: int | None = None
    ) -> AsyncGenerator[UnitPage]:
        """`_pages` as the port's pages; one holding no item Usher models is not yielded."""
        async with aclosing(self._pages(query, start_index=start_index, stop=stop)) as pages:
            async for entries, resume_at in pages:
                items = tuple(item for item in map(to_source_item, entries) if item is not None)
                if items:
                    yield UnitPage(items, resume_at)
```

After `_list_items`:

```python
    async def plan_walk(self) -> WalkPlan:
        """Each library's titles, then its episodes in chunks, largest library first.

        A library is a view other than a collection or a playlist. Every library and
        the whole source are counted at once. When the libraries hold fewer items
        than the source, the plan is one walk of everything, and a WARNING names
        both numbers. A library is counted once, over every type a walk lists, so
        its count rides on its TITLES unit.
        """
        libraries = await self._views()
        self._library_ids = frozenset(view_id for view_id, _ in libraries)
        answers = await asyncio.gather(
            self._count(None),
            *(self._count(view_id) for view_id, _ in libraries),
            return_exceptions=True,
        )
        counts: list[int] = []
        for answer in answers:
            # Raised only once every count has settled, so none is left running with
            # nobody to read what it raised.
            if isinstance(answer, BaseException):
                raise answer
            counts.append(answer)
        total, held = counts[0], counts[1:]
        if not libraries or sum(held) < total:
            logger.warning(
                "{source}'s libraries hold {held} items against a total of {total}; "
                "walking the whole library as one unit",
                source=self._source.name,
                held=sum(held),
                total=total,
            )
            whole = WalkUnit(DEFAULT_UNIT_KEY, WalkStage.TITLES, "the whole library", total)
            return WalkPlan((whole,), expected_total=total)
        ranked = sorted(zip(libraries, held, strict=True), key=lambda pair: pair[1], reverse=True)
        units: list[WalkUnit] = []
        for (view_id, name), count in ranked:
            titles = LibraryUnit(WalkStage.TITLES, view_id)
            units.append(WalkUnit(titles.key, WalkStage.TITLES, titles.label(name), count))
        for (view_id, name), count in ranked:
            for chunk in episode_chunks(view_id, count, self._unit_max_items):
                units.append(WalkUnit(chunk.key, WalkStage.EPISODES, chunk.label(name)))
        return WalkPlan(tuple(units), expected_total=total)

    def list_unit(self, key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]:
        if key == DEFAULT_UNIT_KEY:
            query = _listing_query(LIBRARY_SINCE_PARAM, None)
            return self._unit_pages(query, start_index=start_index)
        unit = parse_unit_key(key)
        if unit is None:
            raise PortDataMalformed(f"no walk unit {key!r} in this adapter's plan")
        return self._library_unit(unit, start_index)

    async def _library_unit(
        self, unit: LibraryUnit, start_index: int
    ) -> AsyncGenerator[UnitPage]:
        # A library gone since the plan was made has nothing left to walk, and its
        # id is never sent: a server can answer a `ParentId` it does not know with
        # the whole library.
        if unit.view_id not in await self._known_libraries():
            return
        query = {
            **_listing_query(LIBRARY_SINCE_PARAM, None),
            "ParentId": unit.view_id,
            "IncludeItemTypes": unit.item_types,
        }
        # A bounded chunk reads `PAGE_OVERLAP` items past its end, so a shift at the
        # boundary with the next chunk is covered the way one between pages is.
        stop = None if unit.upper is None else unit.upper + PAGE_OVERLAP
        pages = self._unit_pages(query, start_index=max(start_index, unit.lower), stop=stop)
        async with aclosing(pages) as unit_pages:
            async for page in unit_pages:
                yield page

    async def _views(self) -> list[tuple[str, str]]:
        """The account's libraries, as `(id, name)`."""
        user_id = await self._session.user_id()
        body = await self._page(f"/Users/{_segment(user_id)}/Views", {}, 0)
        entries = body.get("Items")
        if not isinstance(entries, list):
            raise PortDataMalformed("Emby's view listing carried no Items array")
        libraries: list[tuple[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("CollectionType") in NOT_LIBRARIES:
                continue
            view_id, name = entry.get("Id"), entry.get("Name")
            if isinstance(view_id, str) and view_id:
                libraries.append((view_id, name if isinstance(name, str) and name else view_id))
        return libraries

    async def _known_libraries(self) -> frozenset[str]:
        """The library ids, read once per adapter however many walkers ask at once."""
        async with self._library_lock:
            if self._library_ids is None:
                self._library_ids = frozenset(view_id for view_id, _ in await self._views())
            return self._library_ids

    async def _count(self, view_id: str | None) -> int:
        """How many items a walk lists: in one library, or with none, in the whole source."""
        params = {
            "Recursive": "true",
            "IncludeItemTypes": ITEM_TYPES,
            "Limit": "0",
            "EnableTotalRecordCount": "true",
        }
        if view_id is not None:
            params["ParentId"] = view_id
        body = await self._page(await self._items_path(), params, 0)
        total = body.get("TotalRecordCount")
        # A count the server left out is not a count of nothing: a source total
        # read as zero would pass every coverage check.
        if not isinstance(total, int) or total < 0:
            raise PortDataMalformed("Emby's count carried no TotalRecordCount")
        return total
```

Run the Step 7 command. Expected: PASS.

- [ ] **Step 9: The harness places items in views, and the contract resumes a unit**

`tests/fakes/emby_harness.py` — in `EmbyHarness.__init__`, after `self._server`:

```python
        # Library name -> the view id minted for it the first time it was named.
        self._view_ids: dict[str, str] = {}
```

and replace Task 11's `given_item_in_libraries`:

```python
    async def given_item_in_libraries(
        self, item: SourceItem, libraries: Sequence[str], *, changed_at: AwareDatetime
    ) -> None:
        self._server.add_item(item, changed_at)
        for name in libraries:
            if name not in self._view_ids:
                self._view_ids[name] = f"{0xD001 + len(self._view_ids):032x}"
                self._server.add_view(self._view_ids[name], name)
        self._server.place(item.external_id, *(self._view_ids[name] for name in libraries))
```

`tests/contract/source_adapter_contract.py`, after `test_a_unit_the_plan_never_named_is_refused`:

```python
    async def test_a_unit_resumes_after_a_page_from_its_resume_at(
        self, harness: SourceHarness
    ) -> None:
        """`start_index=page.resume_at` continues after that page and loses nothing.

        Seven items over pages of two, so the first page cannot be the whole unit.
        """
        await self._seed_library(harness)
        plan = await harness.adapter.plan_walk()
        [unit] = [one for one in plan.units if one.stage is not WalkStage.SEED]
        async with aclosing(harness.adapter.list_unit(unit.key)) as pages:
            first = await anext(pages)
        assert len(first.items) < 7, "the premise: the unit takes more than one page"
        rest: set[str] = set()
        async with aclosing(
            harness.adapter.list_unit(unit.key, start_index=first.resume_at)
        ) as pages:
            async for page in pages:
                rest.update(item.external_id for item in page.items)
        seen = {item.external_id for item in first.items} | rest
        assert seen == {f"filler-{index}" for index in range(7)}
```

`tests/unit/test_adapters_emby_contract.py`: `== 54` becomes `== 55`, and both `54`s in the docstring become `55`.

- [ ] **Step 10: Run everything this task touched**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_planning.py tests/unit/test_adapters_emby_paging.py tests/unit/test_fakes_emby_server.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_session.py tests/unit/test_adapters_emby_contract.py tests/unit/test_source_adapter_contract.py`
Expected: PASS. On the Emby arm the plan case now walks three real libraries; the resume case walks the fallback unit, because `_seed_library` places nothing.

- [ ] **Step 11: Plant and verify**

1. `episode_chunks` with `held // unit_max_items` (floor). Expect the planning chunk case to fail at `101` and `250`, and `test_a_library_s_episodes_are_planned_in_chunks_of_the_unit_size` on two chunks where it expects three: the library holds seven items, the series among them.
2. `rest.split(":", 2)` in place of `rsplit`. Expect only the colon-bearing round-trip case to fail.
3. `request_limit` returning `self.limit` always. Expect four cases to fail: `test_a_bounded_window_asks_for_no_more_than_its_stop_allows` (at `== 3`), `test_a_bounded_window_already_at_its_stop_asks_for_nothing` (`4 == 0`), `test_a_bounded_chunk_reads_past_its_end_by_the_overlap_and_no_further` (`160 == 150`: the requests start at 0, 20, …, 120, and the one at 120 now ends at 160), and `test_a_chunk_resumed_at_its_stop_asks_for_nothing` (a listing goes out at `StartIndex=53`).
4. `stopped = False`. Expect `test_a_bounded_window_asks_for_no_more_than_its_stop_allows` to fail on its last line.
5. `stop = unit.upper` (no reach past the end). Expect the bounded-chunk case to fail on `100 == 150`.
6. `resume_at = window.cursor` with the `advance()` result unassigned, so each request starts at the cursor while the window steps back. Expect eight cases to fail: `test_a_page_s_resume_point_is_the_next_request_s_start_reach_back_included` on `4 == 2`; `test_a_bounded_chunk_reads_past_its_end_by_the_overlap_and_no_further` on `170 == 150`; and, in `tests/unit/test_adapters_emby_adapter.py`, Phase 1's `test_the_walk_pages_until_the_library_is_exhausted`, `test_only_the_first_page_asks_for_the_total`, `test_the_page_that_ends_the_walk_is_the_last_one_asked_for`, `test_a_deletion_behind_the_cursor_skips_nothing`, `test_the_last_item_of_a_page_leaving_the_listing_is_not_a_shift` and `test_a_walk_that_deletions_left_short_of_its_total_ends_on_its_tail`.
7. Drop `_library_unit`'s known-library check. Expect `test_a_library_removed_between_attempts_ends_its_unit_empty` to fail on `gone == []`, and `test_walkers_resuming_together_read_the_libraries_once` on `0 == 1`: no walker reads the views at all.
8. Drop the `NOT_LIBRARIES` test in `_views`. Expect `test_collection_and_playlist_views_are_not_libraries` to fail on the `ParentId` set.
9. The fallback condition reduced to `if not libraries:`. Expect `test_libraries_that_hold_less_than_the_total_fall_back_to_one_walk` and the collection case's last assertion to fail.
10. `_known_libraries` without its `async with self._library_lock:`. Expect `test_walkers_resuming_together_read_the_libraries_once` to fail on `2 == 1`.
11. `counts.append(answer if isinstance(answer, int) else 0)` with the raise removed. Expect `test_a_count_that_fails_fails_the_plan` and `test_a_count_without_a_total_fails_the_plan` to fail with `DID NOT RAISE`.
12. `_count` returning `0` in place of its raise. Expect `test_a_count_without_a_total_fails_the_plan` to fail with `DID NOT RAISE`.
13. Drop `if window.request_limit == 0: return`. Expect `test_a_chunk_resumed_at_its_stop_asks_for_nothing` to fail on `_listings(seen) == []`.
14. Drop `_unit_pages`' `if items:`. Expect `test_a_movie_library_s_episodes_unit_ends_on_one_empty_page` to fail on `pages == []`.
15. Remove `"Views"` from `_ROUTE_WORDS`. Expect the redaction case to fail on `/Users/{user_id}/{id}`.
16. In the fake, an unknown `ParentId` filters everything out: `placed = set(self._placed[parent]) if parent in self._placed else (set() if parent is not None else None)`, so a listing with no `ParentId` still lists everything. Expect `test_a_parent_id_no_view_carries_lists_the_whole_library` to fail.

- [ ] **Step 12: Commit**

```bash
git add src/usher/adapters/emby/planning.py src/usher/adapters/emby/paging.py \
  src/usher/adapters/emby/adapter.py src/usher/adapters/emby/session.py \
  tests/fakes/emby_server.py tests/fakes/emby_harness.py tests/fixtures/emby/README.md \
  tests/contract/source_adapter_contract.py \
  tests/unit/test_adapters_emby_planning.py tests/unit/test_adapters_emby_paging.py \
  tests/unit/test_fakes_emby_server.py tests/unit/test_adapters_emby_adapter.py \
  tests/unit/test_adapters_emby_contract.py
git commit -m "emby: plan a whole-library walk as each library's titles, then its episodes in chunks"
```

---

### Task 14: The seed — what the account is watching, first

Spec §2.2, "SEED". One unit, first in the plan: the `Filters=IsPlayed` items, the `Filters=IsResumable` items and `/Shows/NextUp`, each with full `ITEM_FIELDS`, with the series they belong to fetched by `Ids` and yielded ahead of their episodes. Task 18 runs the watch lane as soon as it has committed, which is what puts a household's own shelves within the 2-minute target.

**Departures from the spec's letter (8, 9, 10):**
- **The seed streams.** Each page is led by the series its episodes need that neither it nor an earlier page holds, fetched by `Ids` there and then, instead of every listing first and every series after. Memory stays at one page however much the account has watched, and an episode still lands with its series or after it — which is all ingest needs, because it resolves a series from the whole batch it arrives in.
- **A resumed seed starts again; every seed page resumes at 0.** A count into listings that may have changed since could skip an item the watch lane is about to look for. The seed is small and ingest upserts, so starting again costs seconds.
- **The seed is planned only when both watch filters narrow the library.** Two more counts — `Filters=IsPlayed` and `Filters=IsResumable` over the whole source — ride the planner's concurrent batch, 18 counts in all. A server that ignored a filter, or a library watched end to end, would make the seed a whole-library walk ahead of the real one.
- **A fallback plan keeps its seed.** The fallback replaces the library units only, so a library whose views do not cover it still gets its watch state early.

**Files:**
- Modify: `src/usher/adapters/emby/planning.py` (`SEED_KEY`)
- Modify: `src/usher/adapters/emby/adapter.py` (`IDS_PER_REQUEST`, `NEXT_UP_PATH`, `SEED_UNIT`; `plan_walk`'s filtered counts and seed; `list_unit`'s seed; `_seed`, `_series_for`; `_count(filters=…)`)
- Modify: `src/usher/adapters/emby/session.py` (`_ROUTE_WORDS` gains `NextUp`)
- Modify: `tests/fakes/emby_server.py` (`Ids`, `/Shows/NextUp`, `set_next_up`)
- Test: `tests/unit/test_fakes_emby_server.py`, `tests/unit/test_adapters_emby_adapter.py`

**Interfaces:**
- Consumes: Task 13's `plan_walk`, `list_unit`, `_pages(…, path=…)`, `_items_path`, `_count`, and its test helpers `_library`, `_recorded`, `_listings`, `_series`, `_episode`, `_listed`, `_given_three_movies`; Phase 1's `FIRST_WALK_FILTERS`, `_listing_query(…, filters=…)`, `ITEM_FIELDS`.
- Produces: `usher.adapters.emby.planning.SEED_KEY = "seed"`; `usher.adapters.emby.adapter.SEED_UNIT = WalkUnit("seed", WalkStage.SEED, "what the account is watching")`, `IDS_PER_REQUEST = 100`, `NEXT_UP_PATH = "/Shows/NextUp"`; `FakeEmbyServer.set_next_up(*external_ids)`. Every Emby plan whose watch filters narrow the library starts with `SEED_UNIT`. Task 16's writer commits a seed page like any other; Task 18 fires `after_seed` once the `SEED` stage has committed.

- [ ] **Step 1: Write the failing tests for the fake**

Append to `tests/unit/test_fakes_emby_server.py`:

```python
# --- Ids and NextUp ------------------------------------------------------


async def test_ids_lists_only_the_named_items(driver: _Driver) -> None:
    _given_three_movies(driver)
    assert await _listed(driver, Ids="m0,m2") == ["m0", "m2"]


def _given_next_up(driver: _Driver) -> None:
    driver.server.add_item(
        SourceItem(external_id="s0", name="S 0", kind=SourceItemKind.SERIES), ADDED_AT
    )
    for index in range(3):
        driver.server.add_item(
            SourceItem(
                external_id=f"e{index}",
                name=f"E {index}",
                kind=SourceItemKind.EPISODE,
                series_external_id="s0",
                season_number=1,
                episode_number=index + 1,
            ),
            ADDED_AT,
        )
    driver.server.set_next_up("e2", "e0", "e1")


async def test_next_up_lists_its_episodes_in_its_own_order_and_in_pages(driver: _Driver) -> None:
    _given_next_up(driver)
    body = await driver.session.json_body(
        "GET",
        "/Shows/NextUp",
        params={"UserId": USER_ID, "StartIndex": "1", "Limit": "1"},
        op="next_up",
    )
    assert [entry["Id"] for entry in body["Items"]] == ["e0"]
    assert body["TotalRecordCount"] == 3


async def test_next_up_refuses_a_request_that_names_no_user(driver: _Driver) -> None:
    """Stricter than the real server may be, deliberately: the adapter must say whose it means."""
    _given_next_up(driver)
    with pytest.raises(PortUnavailable):
        await driver.session.json_body("GET", "/Shows/NextUp", op="next_up")
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_fakes_emby_server.py -k "ids or next_up"`
Expected: FAIL — `Ids` lists all three movies, and `AttributeError: 'FakeEmbyServer' object has no attribute 'set_next_up'`.

- [ ] **Step 3: Teach the fake `Ids` and NextUp**

`tests/fakes/emby_server.py` — in `__init__`, after `self._placed`:

```python
        # The episodes `/Shows/NextUp` answers, in the order it answers them.
        self._next_up: list[str] = []
```

After `place`:

```python
    def set_next_up(self, *external_ids: str) -> None:
        """What `/Shows/NextUp` answers: seeded episodes, in this order."""
        self._next_up = list(external_ids)
```

In `handle`, before the `_VIEWS` route:

```python
        if request.method == "GET" and path == "/Shows/NextUp":
            return self._list_next_up(request)
```

In `_ordered`, beside `parent`:

```python
        ids = {external_id for external_id in (params.get("Ids") or "").split(",") if external_id}
```

and in its loop, after the `types` check:

```python
            if ids and external_id not in ids:
                continue
```

After `_list_views`:

```python
    def _list_next_up(self, request: httpx.Request) -> httpx.Response:
        """`GET /Shows/NextUp`, cut and counted the way `_list` is.

        Stricter than the real server may be, deliberately: a request naming no
        user is refused, because the adapter must say whose next-up it means.
        """
        params = request.url.params
        if params.get("UserId") != USER_ID:
            return httpx.Response(400, json={"Error": "UserId is required"})
        listed = [external_id for external_id in self._next_up if external_id in self._items]
        start = int(params.get("StartIndex", "0"))
        limit = int(params.get("Limit", str(self.page_size)))
        counted = (params.get("EnableTotalRecordCount") or "true").lower() != "false"
        page = listed[start : start + limit]
        return httpx.Response(
            200,
            json={
                "Items": [self._payload(external_id, for_listing=True) for external_id in page],
                "TotalRecordCount": len(listed) if counted else 0,
            },
        )
```

Run the Step 2 command. Expected: PASS.

- [ ] **Step 4: Write the failing adapter tests**

In `tests/unit/test_adapters_emby_adapter.py`, import `IDS_PER_REQUEST, SEED_UNIT` beside `MAX_PAGES, EmbyAdapter`, and `from usher.adapters.emby.planning import SEED_KEY`. After the walk-plan section, before `# --- the redacted request path`:

```python
# --- the seed -----------------------------------------------------------


def _watching(server: FakeEmbyServer) -> None:
    """Three movies and two episodes watched, one of each untouched, one episode up next.

    `movie-3` is both played and part-way through, so two listings carry it.
    `next-0` belongs to `series-1`, which no watched item does.
    """
    for index in range(4):
        server.add_item(_movie(index), T0)
    server.add_item(_series(0), T0)
    server.add_item(_series(1), T0)
    for index in range(3):
        server.add_item(_episode(index), T0)
    server.add_item(
        replace(_episode(9), external_id="next-0", name="Next 0", series_external_id="series-1"),
        T0,
    )
    server.set_next_up("next-0")
    for external_id, played, position in [
        ("movie-0", True, 0),
        ("movie-2", False, 640),
        ("movie-3", True, 320),
        ("episode-000", True, 0),
        ("episode-001", False, 640),
    ]:
        server.set_watch_state(
            SourceWatchState(external_id=external_id, position_seconds=position, played=played)
        )


async def test_a_plan_starts_with_the_seed_and_so_does_its_fallback() -> None:
    """The fallback replaces the library units only; the seed still runs the watch lane early."""
    server = FakeEmbyServer()
    _library(server, 1, "Films", [_movie(0), _movie(1)])
    covering = _adapter(server)
    try:
        covered = await covering.plan_walk()
    finally:
        await covering.aclose()
    server.add_item(_movie(2), T0)
    falling_back = _adapter(server)
    try:
        fallback = await falling_back.plan_walk()
    finally:
        await falling_back.aclose()
    assert [unit.key for unit in fallback.units] == [SEED_KEY, DEFAULT_UNIT_KEY], (
        "the premise: the second plan fell back"
    )
    for plan in (covered, fallback):
        assert plan.units[0] == SEED_UNIT
        assert [unit.stage for unit in plan.units].count(WalkStage.SEED) == 1


@pytest.mark.parametrize("ignored", ["IsPlayed", "IsResumable"])
async def test_a_filter_the_server_ignores_leaves_the_plan_without_a_seed(ignored: str) -> None:
    """A seed its filter did not narrow would walk the whole library ahead of the walk.

    Each filter is ignored on its own, so neither half of the check can go missing;
    the library is the one the case above planned a seed for.
    """
    server = FakeEmbyServer()
    _library(server, 1, "Films", [_movie(0), _movie(1)])

    def ignoring(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("Filters") != ignored:
            return server.handle(request)
        unfiltered = request.url.copy_remove_param("Filters")
        return server.handle(httpx.Request(request.method, unfiltered, headers=request.headers))

    adapter = _on(ignoring)
    try:
        plan = await adapter.plan_walk()
    finally:
        await adapter.aclose()
    assert [unit.stage for unit in plan.units] == [WalkStage.TITLES, WalkStage.EPISODES]


async def test_the_seed_holds_what_the_account_watched_and_watches_next_each_series_first() -> None:
    """Each item once, every episode after its series, and no untouched item.

    The fake refuses a NextUp that names no user, so this case also holds the
    adapter to sending one.
    """
    server = FakeEmbyServer()
    _watching(server)
    adapter = _adapter(server, page_size=100)
    try:
        pages = [page async for page in adapter.list_unit(SEED_KEY)]
    finally:
        await adapter.aclose()
    walked = [item for page in pages for item in page.items]
    assert sorted(item.external_id for item in walked) == [
        "episode-000",
        "episode-001",
        "movie-0",
        "movie-2",
        "movie-3",
        "next-0",
        "series-0",
        "series-1",
    ]
    before: set[str] = set()
    for item in walked:
        if item.kind is SourceItemKind.EPISODE:
            assert item.series_external_id in before, f"{item.external_id} came before its series"
        before.add(item.external_id)
    assert {page.resume_at for page in pages} == {0}


async def test_the_seed_asks_only_for_series_no_page_held_a_hundred_at_a_time() -> None:
    """`series-done` is played, so its own listing page carries it and `Ids` never names it."""
    server = FakeEmbyServer()
    count = IDS_PER_REQUEST + 1
    server.add_item(replace(_series(0), external_id="series-done", name="Series Done"), T0)
    server.add_item(
        replace(
            _episode(0),
            external_id="episode-done",
            name="Episode Done",
            series_external_id="series-done",
        ),
        T0,
    )
    played = ["series-done", "episode-done"]
    for index in range(count):
        server.add_item(_series(index), T0)
        server.add_item(
            replace(
                _episode(index),
                external_id=f"episode-of-{index:03d}",
                name=f"Episode of {index:03d}",
                series_external_id=f"series-{index}",
            ),
            T0,
        )
        played.append(f"episode-of-{index:03d}")
    for external_id in played:
        server.set_watch_state(
            SourceWatchState(external_id=external_id, position_seconds=0, played=True)
        )
    adapter, seen = _recorded(server, page_size=1_000)
    try:
        walked = {
            item.external_id async for page in adapter.list_unit(SEED_KEY) for item in page.items
        }
    finally:
        await adapter.aclose()
    asked = [
        request.url.params["Ids"].split(",")
        for request in _listings(seen)
        if "Ids" in request.url.params
    ]
    assert sorted(len(ids) for ids in asked) == [1, IDS_PER_REQUEST]
    assert {one for ids in asked for one in ids} == {f"series-{index}" for index in range(count)}
    assert {"series-done", *(f"series-{index}" for index in range(count))} <= walked


async def test_a_resumed_seed_starts_again() -> None:
    """A count into listings that changed since could skip an item the watch lane needs."""
    server = FakeEmbyServer()
    _watching(server)
    adapter = _adapter(server, page_size=100)
    try:
        first = [page async for page in adapter.list_unit(SEED_KEY)]
        resumed = [page async for page in adapter.list_unit(SEED_KEY, start_index=5)]
    finally:
        await adapter.aclose()
    assert len(first) == 3, "the premise: three listings, a page each"
    assert [[item.external_id for item in page.items] for page in resumed] == [
        [item.external_id for item in page.items] for page in first
    ]
```

In `test_every_path_this_adapter_issues_redacts_to_a_route_with_no_identifier`, after Task 13's two lines inside the `try`:

```python
        _ = [page async for page in adapter.list_unit(SEED_KEY)]
```

and add `"/Shows/NextUp",` to the pinned set. (By then `push_watch_state` has played `movie-1`, the library's only item, so the plan holds no seed — which is why the seed is walked by name.)

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py`
Expected: FAIL — `ImportError: cannot import name 'IDS_PER_REQUEST'`.

- [ ] **Step 5: Plan the seed and walk it**

`src/usher/adapters/emby/planning.py`, after `_OFFSET`:

```python
#: The unit holding what the account is watching. It is no library's, so
#: `parse_unit_key` refuses it.
SEED_KEY = "seed"
```

`src/usher/adapters/emby/session.py` — add `"NextUp",` to `_ROUTE_WORDS`.

`src/usher/adapters/emby/adapter.py` — `Sequence` joins the `collections.abc` import, `SEED_KEY` joins the `usher.adapters.emby.planning` import, and `from itertools import batched` is added. After `NOT_LIBRARIES`:

```python
# Series asked for by `Ids` in one request: a hundred ids keep the URL short.
IDS_PER_REQUEST = 100
NEXT_UP_PATH = "/Shows/NextUp"
SEED_UNIT = WalkUnit(SEED_KEY, WalkStage.SEED, "what the account is watching")
```

Replace Task 13's `plan_walk` with:

```python
    async def plan_walk(self) -> WalkPlan:
        """The seed, each library's titles, then its episodes in chunks, largest first.

        A library is a view other than a collection or a playlist. Every library,
        the whole source and each watch filter's share of it are counted at once.
        The seed is planned only when both filters narrow the library. When the
        libraries hold fewer items than the source, the seed is followed by one
        walk of everything, and a WARNING names both numbers. A library is counted
        once, over every type a walk lists, so its count rides on its TITLES unit.
        """
        libraries = await self._views()
        self._library_ids = frozenset(view_id for view_id, _ in libraries)
        filtered = len(FIRST_WALK_FILTERS)
        answers = await asyncio.gather(
            self._count(None),
            *(self._count(None, filters=filters) for filters in FIRST_WALK_FILTERS),
            *(self._count(view_id) for view_id, _ in libraries),
            return_exceptions=True,
        )
        counts: list[int] = []
        for answer in answers:
            # Raised only once every count has settled, so none is left running with
            # nobody to read what it raised.
            if isinstance(answer, BaseException):
                raise answer
            counts.append(answer)
        total, watched, held = counts[0], counts[1 : 1 + filtered], counts[1 + filtered :]
        # The seed is what each watch filter narrows the library to. A server that
        # ignored one, or a library watched end to end, would make it a
        # whole-library walk ahead of the real one.
        seed = (SEED_UNIT,) if all(count < total for count in watched) else ()
        if not libraries or sum(held) < total:
            logger.warning(
                "{source}'s libraries hold {held} items against a total of {total}; "
                "walking the whole library as one unit",
                source=self._source.name,
                held=sum(held),
                total=total,
            )
            whole = WalkUnit(DEFAULT_UNIT_KEY, WalkStage.TITLES, "the whole library", total)
            return WalkPlan((*seed, whole), expected_total=total)
        ranked = sorted(zip(libraries, held, strict=True), key=lambda pair: pair[1], reverse=True)
        units: list[WalkUnit] = list(seed)
        for (view_id, name), count in ranked:
            titles = LibraryUnit(WalkStage.TITLES, view_id)
            units.append(WalkUnit(titles.key, WalkStage.TITLES, titles.label(name), count))
        for (view_id, name), count in ranked:
            for chunk in episode_chunks(view_id, count, self._unit_max_items):
                units.append(WalkUnit(chunk.key, WalkStage.EPISODES, chunk.label(name)))
        return WalkPlan(tuple(units), expected_total=total)
```

`list_unit` checks the seed first:

```python
    def list_unit(self, key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]:
        if key == SEED_KEY:
            # Every seed page resumes at 0, so `start_index` is always 0 here or
            # stale; see `_seed`.
            return self._seed()
        if key == DEFAULT_UNIT_KEY:
            query = _listing_query(LIBRARY_SINCE_PARAM, None)
            return self._unit_pages(query, start_index=start_index)
        unit = parse_unit_key(key)
        if unit is None:
            raise PortDataMalformed(f"no walk unit {key!r} in this adapter's plan")
        return self._library_unit(unit, start_index)
```

After `_library_unit`:

```python
    async def _seed(self) -> AsyncGenerator[UnitPage]:
        """What the account is watching: played, then in progress, then up next.

        Each page is led by the series its episodes need that neither it nor an
        earlier page holds, fetched by `Ids`, so every episode lands with its
        series or after it, and only one page is ever held. Every page resumes at
        0, so a resumed seed starts again: a count into listings that may have
        changed since could skip an item the watch lane is about to look for.
        """
        user_id = await self._session.user_id()
        listings: list[tuple[str | None, dict[str, str]]] = [
            (None, _listing_query(LIBRARY_SINCE_PARAM, None, filters=filters))
            for filters in FIRST_WALK_FILTERS
        ]
        listings.append((NEXT_UP_PATH, {"UserId": user_id, "Fields": ITEM_FIELDS}))
        yielded: set[str] = set()
        for path, query in listings:
            async with aclosing(self._pages(query, start_index=0, path=path)) as pages:
                async for entries, _ in pages:
                    fresh = [
                        item
                        for item in map(to_source_item, entries)
                        if item is not None and item.external_id not in yielded
                    ]
                    series = await self._series_for(fresh, yielded)
                    page = (*series, *fresh)
                    if page:
                        yielded.update(item.external_id for item in page)
                        yield UnitPage(page, resume_at=0)

    async def _series_for(self, items: Sequence[SourceItem], yielded: set[str]) -> list[SourceItem]:
        """The series of `items`' episodes that neither they nor an earlier page hold."""
        held = yielded | {item.external_id for item in items}
        missing = sorted(
            {item.series_external_id for item in items if item.series_external_id} - held
        )
        series: list[SourceItem] = []
        for chunk in batched(missing, IDS_PER_REQUEST):
            # `Limit` is the chunk's length: every id asked for comes back, and no more.
            params = {"Ids": ",".join(chunk), "Fields": ITEM_FIELDS, "Limit": str(len(chunk))}
            body = await self._page(await self._items_path(), params, 0)
            entries = body.get("Items")
            if not isinstance(entries, list):
                raise PortDataMalformed("Emby's series listing carried no Items array")
            series.extend(item for item in map(to_source_item, entries) if item is not None)
        return series
```

If Task 10 recorded that only `Recursive=true` finds items by `Ids`, add `"Recursive": "true", "IncludeItemTypes": "Series"` to `params` there. NextUp needs nothing of the kind: it is small, so its first page is short and at its total and ends the walk whatever Task 10 found about its paging.

`_count` takes the filter:

```python
    async def _count(self, view_id: str | None, *, filters: str | None = None) -> int:
        """How many items a walk lists: in one library, the whole source, or its filtered share."""
        params = {
            "Recursive": "true",
            "IncludeItemTypes": ITEM_TYPES,
            "Limit": "0",
            "EnableTotalRecordCount": "true",
        }
        if view_id is not None:
            params["ParentId"] = view_id
        if filters is not None:
            params["Filters"] = filters
        body = await self._page(await self._items_path(), params, 0)
        total = body.get("TotalRecordCount")
        # A count the server left out is not a count of nothing: a source total
        # read as zero would pass every coverage check.
        if not isinstance(total, int) or total < 0:
            raise PortDataMalformed("Emby's count carried no TotalRecordCount")
        return total
```

- [ ] **Step 6: Run everything this task touched**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_fakes_emby_server.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_planning.py tests/unit/test_adapters_emby_contract.py tests/unit/test_source_adapter_contract.py`
Expected: PASS. Task 13's planner cases still hold: each filters out the seed, or reads only `EPISODES` units, and the two new counts carry no `ParentId`.

- [ ] **Step 7: Plant and verify**

1. `seed = (SEED_UNIT,)` unconditionally. Expect both parametrisations of `test_a_filter_the_server_ignores_leaves_the_plan_without_a_seed` to fail.
2. `all(count < total for count in watched[:1])`. Expect only the `IsResumable` parametrisation to fail.
3. Append the seed after the library units (`units.extend(seed)` at the end, `units: list[WalkUnit] = []` at the start). Expect `test_a_plan_starts_with_the_seed_and_so_does_its_fallback` to fail on `plan.units[0] == SEED_UNIT`.
4. `WalkPlan((whole,), …)` in the fallback. Expect the same case to fail on its premise line.
5. `series: list[SourceItem] = []; return series` at the top of `_series_for`. Expect `test_the_seed_holds_what_the_account_watched_and_watches_next_each_series_first` to fail on the sorted ids (`series-0` and `series-1` missing).
6. `held = yielded` in `_series_for`. Expect `test_the_seed_asks_only_for_series_no_page_held_a_hundred_at_a_time` to fail first on `[2, 100] == [1, 100]`, the sizes line ahead of the asked set: `series-done` is asked for too, so the second chunk holds two.
7. `batched(missing, len(missing) or 1)`. Expect the same case to fail on `[101] == [1, 100]`.
8. Drop the `item.external_id not in yielded` filter. Expect the watched case to fail on the sorted ids (`movie-3` twice).
9. Drop the `listings.append(...)` line. Expect the watched case to fail (`next-0` and `series-1` missing).
10. `page = (*fresh, *series)`. Expect the watched case to fail on `episode-000 came before its series`.
11. Make the seed honour `start_index` by skipping that many items. Expect `test_a_resumed_seed_starts_again` to fail.
12. Remove `"NextUp"` from `_ROUTE_WORDS`. Expect the redaction case to fail on `/Shows/{id}`.
13. In the fake, drop the `ids` check. Expect `test_ids_lists_only_the_named_items` to fail.
14. In the fake, drop the `UserId` check. Expect `test_next_up_refuses_a_request_that_names_no_user` to fail with `DID NOT RAISE`.

- [ ] **Step 8: Commit**

```bash
git add src/usher/adapters/emby/planning.py src/usher/adapters/emby/adapter.py \
  src/usher/adapters/emby/session.py tests/fakes/emby_server.py \
  tests/unit/test_fakes_emby_server.py tests/unit/test_adapters_emby_adapter.py
git commit -m "emby: seed a whole-library walk with what the account is watching"
```

---

### Task 15: The listing limit, and `USHER_SYNC_WALKERS`

Spec §2.6 and the `usher.source.listing.concurrency` half of §2.8. One `ListingLimit` per adapter caps the listing requests in flight — every walker's and every read-ahead's — at `USHER_SYNC_WALKERS`. A failure `_page` will ask again after drops the cap to one; each run of ten pages that succeed raises it by one. Each attempt holds a slot; the wait between attempts holds none. Nothing runs walkers yet (Task 16), so today the cap binds Phase 1's read-ahead only when it is 1, and the counts `plan_walk` sends at once.

`USHER_SYNC_UNIT_MAX_ITEMS` is Task 16's: nothing walks a plan before then, so its setting and its documentation land with the walk.

**Files:**
- Create: `src/usher/adapters/emby/limit.py`, `tests/unit/test_adapters_emby_limit.py`
- Modify: `src/usher/adapters/emby/adapter.py` (`__init__`, `_page`)
- Modify: `src/usher/adapters/factory.py`, `src/usher/composition.py` (`adapter_factory`), `src/usher/config.py`
- Modify: `.env.example`, `web/src/features/operator/Config.settings.ts`, `docs/guide/configuration.md`
- Modify: `docs/prd/03-sources-and-sync.md`, `docs/prd/08-operations.md`, `docs/prd/10-telemetry-and-dashboards.md`, `CHANGELOG.md`
- Test: `tests/unit/test_adapters_emby_adapter.py`, `tests/unit/test_adapters_factory.py`, `tests/unit/test_composition.py`, `tests/unit/test_config.py`, `tests/unit/test_telemetry_metric_names.py`, `tests/unit/test_dashboards.py`

**Interfaces:**
- Consumes: Phase 1's `_page` (with `LISTING_READ_SECONDS`), `PAGE_RETRY_WAITS`; Task 13's `LibraryUnit`, `list_unit`, and the test helpers `_library`, `_movie`, `_Waits`.
- Produces: `usher.adapters.emby.limit.ListingLimit(ceiling: int, *, source: str)` with `.limit: int`, `.slot()` (an async context manager), `.failed()`, `.succeeded()`; `RAISE_AFTER = 10`.
- Produces: `EmbyAdapter(…, listing_concurrency: int = 4)`; `ConfiguredSourceAdapterFactory(…, listing_concurrency: int = 4)`; `Settings.sync_walkers: int` (default 4, from 1 to 16). Task 16 passes `settings.sync_walkers` to `ReconcileService(walkers=…)` and extends the setting's description to the walkers.

- [ ] **Step 1: Write the failing limit tests**

Create `tests/unit/test_adapters_emby_limit.py`:

```python
"""`ListingLimit`: the listing requests in flight, dropped to one by a failure and raised back."""

import asyncio

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from usher.adapters.emby.limit import ListingLimit

SOURCE_NAME = "Shared Emby"


async def _turns(count: int = 20) -> None:
    """Let every task that can make progress make it."""
    for _ in range(count):
        await asyncio.sleep(0)


class _Holders:
    """Tasks that each take a slot and keep it until let go, logging both moments."""

    def __init__(self, limit: ListingLimit) -> None:
        self._limit = limit
        self._let_go: dict[int, asyncio.Event] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self.log: list[tuple[str, int]] = []

    def start(self, number: int) -> None:
        self._let_go[number] = asyncio.Event()
        self._tasks.append(asyncio.create_task(self._hold(number)))

    async def _hold(self, number: int) -> None:
        async with self._limit.slot():
            self.log.append(("in", number))
            await self._let_go[number].wait()
            self.log.append(("out", number))

    def let_go(self, number: int) -> None:
        self._let_go[number].set()

    def inside(self) -> set[int]:
        """The holders that have taken a slot and not yet given it back."""
        return {n for event, n in self.log if event == "in"} - {
            n for event, n in self.log if event == "out"
        }

    async def finish(self) -> None:
        for event in self._let_go.values():
            event.set()
        await asyncio.gather(*self._tasks)


def _caps(reader: InMemoryMetricReader) -> list[tuple[float, dict[str, str]]]:
    data = reader.get_metrics_data()
    points = [
        point
        for resource in (data.resource_metrics if data else ())
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "usher.source.listing.concurrency"
        for point in metric.data.data_points
    ]
    readings: list[tuple[float, dict[str, str]]] = []
    for point in points:
        assert isinstance(point, NumberDataPoint), "the cap is a gauge reading"
        attributes = {str(key): str(value) for key, value in dict(point.attributes or {}).items()}
        readings.append((point.value, attributes))
    return readings


async def test_a_new_limit_lets_its_ceiling_in_and_holds_the_next_until_one_leaves() -> None:
    limit = ListingLimit(3, source=SOURCE_NAME)
    holders = _Holders(limit)
    for number in range(4):
        holders.start(number)
    await _turns()
    assert holders.inside() == {0, 1, 2}, "three slots: three inside together, the fourth waiting"
    holders.let_go(1)
    await _turns()
    assert holders.inside() == {0, 2, 3}
    assert holders.log.index(("in", 3)) > holders.log.index(("out", 1))
    await holders.finish()


async def test_a_failure_lets_no_page_in_until_every_page_in_flight_has_left() -> None:
    """Dropped to one with three in flight, a fourth waits for all three, not for one."""
    limit = ListingLimit(3, source=SOURCE_NAME)
    holders = _Holders(limit)
    for number in range(3):
        holders.start(number)
    await _turns()
    assert holders.inside() == {0, 1, 2}, "the premise: three in flight when the failure lands"
    limit.failed()
    holders.start(3)
    holders.let_go(0)
    holders.let_go(1)
    await _turns()
    assert limit.limit == 1
    assert holders.inside() == {2}, "one still in flight, so the fourth still waits"
    holders.let_go(2)
    await _turns()
    assert holders.inside() == {3}
    await holders.finish()


async def test_ten_successes_in_a_row_raise_the_limit_by_one_up_to_its_ceiling() -> None:
    limit = ListingLimit(3, source=SOURCE_NAME)
    limit.failed()
    readings: list[int] = []
    for _ in range(30):
        limit.succeeded()
        readings.append(limit.limit)
    assert readings == [1] * 9 + [2] * 10 + [3] * 11


async def test_a_failure_starts_the_run_of_successes_again() -> None:
    limit = ListingLimit(3, source=SOURCE_NAME)
    limit.failed()
    for _ in range(9):
        limit.succeeded()
    limit.failed()
    readings: list[int] = []
    for _ in range(10):
        limit.succeeded()
        readings.append(limit.limit)
    assert readings == [1] * 9 + [2]


async def test_a_raised_limit_lets_a_waiting_page_in_while_the_first_still_holds() -> None:
    """Nobody has left, so only the raise itself can let the waiter in."""
    limit = ListingLimit(2, source=SOURCE_NAME)
    limit.failed()
    holders = _Holders(limit)
    holders.start(0)
    holders.start(1)
    await _turns()
    assert holders.inside() == {0}, "the premise: one slot, so the second waits"
    for _ in range(10):
        limit.succeeded()
    await _turns()
    assert holders.inside() == {0, 1}
    await holders.finish()


async def test_the_limit_reports_its_cap_after_every_page_and_nothing_before_the_first() -> None:
    """An adapter built for a playback request never lists, so it never writes the series.

    One that lists reports its cap after each page, labelled by source, so an idle
    adapter built during a walk that backed off cannot paint over the walk's reading.
    """
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    limit = ListingLimit(3, source=SOURCE_NAME)
    assert _caps(reader) == [], "no page yet, so no reading"
    limit.succeeded()
    readings = [_caps(reader)]
    limit.failed()
    readings.append(_caps(reader))
    for _ in range(10):
        limit.succeeded()
    readings.append(_caps(reader))
    labels = {"source": SOURCE_NAME}
    assert readings == [[(3, labels)], [(1, labels)], [(2, labels)]]


def test_a_limit_with_no_room_is_refused() -> None:
    """A cap of zero would park every listing request forever."""
    with pytest.raises(ValueError, match="room for one"):
        ListingLimit(0, source=SOURCE_NAME)
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_limit.py`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'usher.adapters.emby.limit'`.

- [ ] **Step 3: Write the limit**

Create `src/usher/adapters/emby/limit.py`:

```python
"""How many listing requests one Emby adapter has in flight, and how that backs off."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry import metrics

_meter = metrics.get_meter("usher.source.emby")
# PRD 10's catalogue. Written after every listing page, never at construction,
# so an adapter that never lists leaves no reading behind.
_concurrency = _meter.create_gauge(
    "usher.source.listing.concurrency",
    unit="1",
    description="Listing requests one source's adapter may have in flight at once",
)

# Successes in a row that raise a dropped limit by one.
RAISE_AFTER = 10


class ListingLimit:
    """The cap on listing requests in flight, shared by every walker and read-ahead.

    It starts at its ceiling. A failure that will be asked again drops it to one,
    and each run of `RAISE_AFTER` successes in a row raises it by one, back up to
    the ceiling. A page waiting for a slot holds no request; a raise lets a
    waiter in at once, without anyone leaving.
    """

    def __init__(self, ceiling: int, *, source: str) -> None:
        if ceiling < 1:
            raise ValueError(f"a listing limit needs room for one request, not {ceiling}")
        self._ceiling = ceiling
        self._labels = {"source": source}
        self._limit = ceiling
        self._in_flight = 0
        self._streak = 0
        self._wake = asyncio.Event()

    @property
    def limit(self) -> int:
        return self._limit

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """One of the limit's slots, held for the body of the `async with`."""
        while self._in_flight >= self._limit:
            self._wake.clear()
            await self._wake.wait()
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            self._wake.set()

    def failed(self) -> None:
        """A request failed in a way that is asked again: one at a time from here."""
        self._limit = 1
        self._streak = 0
        _concurrency.set(self._limit, self._labels)

    def succeeded(self) -> None:
        """A request succeeded; every `RAISE_AFTER`-th in a row raises the limit by one."""
        self._streak += 1
        if self._streak == RAISE_AFTER:
            self._streak = 0
            if self._limit < self._ceiling:
                self._limit += 1
                self._wake.set()
        _concurrency.set(self._limit, self._labels)
```

- [ ] **Step 4: Run them to see them pass**

Run the Step 2 command. Expected: PASS, 7 tests.

- [ ] **Step 5: Write the failing adapter tests**

In `tests/unit/test_adapters_emby_adapter.py`, import `from usher.adapters.emby.limit import RAISE_AFTER` and `from usher.adapters.emby.planning import LibraryUnit`. Before `# --- the redacted request path`, add:

```python
# --- the listing limit -----------------------------------------------

# Turns of the event loop a listing page waits for company before it is answered.
COMPANY = 20


class _Crowd:
    """An async handler logging each listing page's arrival and departure.

    Each page waits `COMPANY` turns of the loop before it is answered, so requests
    that may overlap do. A page is `(ParentId, StartIndex)`; those in `fail` are
    answered 503, once each. Everything else goes straight to the server.
    """

    def __init__(self, server: FakeEmbyServer, *, fail: Sequence[tuple[str, int]] = ()) -> None:
        self._server = server
        self._fail = set(fail)
        self._in_flight = 0
        self.log: list[tuple[str, tuple[str, int], int]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if not request.url.path.endswith("/Items") or params.get("Limit") == "0":
            return self._server.handle(request)
        page = (params.get("ParentId", ""), int(params["StartIndex"]))
        self._in_flight += 1
        self.log.append(("in", page, self._in_flight))
        try:
            for _ in range(COMPANY):
                await asyncio.sleep(0)
            if page in self._fail:
                self._fail.remove(page)
                return httpx.Response(503)
            return self._server.handle(request)
        finally:
            self._in_flight -= 1
            self.log.append(("out", page, self._in_flight))

    def widths(self, start: int = 0, end: int | None = None) -> list[int]:
        """How many pages were in flight as each in `log[start:end]` arrived, itself included."""
        return [width for event, _, width in self.log[start:end] if event == "in"]

    def index(self, event: str, page: tuple[str, int], *, after: int = -1) -> int:
        """Where `event` for `page` is first logged after position `after`."""
        return next(
            at
            for at, (logged, which, _) in enumerate(self.log)
            if at > after and (logged, which) == (event, page)
        )


class _TurningWaits(_Waits):
    """`_Waits` whose wait also lets the event loop turn, as a real one would."""

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        for _ in range(COMPANY):
            await asyncio.sleep(0)


def _crowded(
    crowd: _Crowd, *, listing_concurrency: int, waits: _Waits | None = None
) -> EmbyAdapter:
    waits = waits if waits is not None else _Waits()
    return EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(crowd), base_url=SOURCE.base_url),
        page_size=2,
        listing_concurrency=listing_concurrency,
        sleep=waits.sleep,
        clock=waits.clock,
    )


async def _walk_together(adapter: EmbyAdapter, view_ids: Sequence[str]) -> None:
    """Each library's TITLES unit walked by a task of its own, all at once."""

    async def walk(view_id: str) -> None:
        async with aclosing(adapter.list_unit(LibraryUnit(WalkStage.TITLES, view_id).key)) as pages:
            _ = [page async for page in pages]

    await asyncio.gather(*(walk(view_id) for view_id in view_ids))


@pytest.mark.parametrize("cap", [1, 2, 4])
async def test_listing_pages_in_flight_never_pass_the_listing_concurrency(cap: int) -> None:
    """Five walkers share one limit; at every cap they fill it and go no further.

    Five, one more than the largest cap, so a walk with no limit reaches five.
    """
    server = FakeEmbyServer()
    view_ids = [
        _library(server, n, f"Library {n}", [_movie(n * 10 + i) for i in range(4)])
        for n in range(5)
    ]
    crowd = _Crowd(server)
    adapter = _crowded(crowd, listing_concurrency=cap)
    try:
        await _walk_together(adapter, view_ids)
    finally:
        await adapter.aclose()
    assert max(crowd.widths()) == cap


async def test_a_retried_failure_lets_pages_in_one_at_a_time_until_ten_succeed() -> None:
    """A 503 drops the limit to one, and the tenth success after it raises it to two.

    Two walkers share a limit of two, and one walker's first page is answered 503.
    Before the failure both are in flight. Between the failure and the tenth page that
    succeeds after it, every page arrives alone, and after the tenth, two fly again.
    """
    server = FakeEmbyServer()
    films = _library(server, 1, "Films", [_movie(index) for index in range(16)])
    more = _library(server, 2, "More Films", [_movie(index) for index in range(20, 36)])
    crowd = _Crowd(server, fail=[(films, 0)])
    adapter = _crowded(crowd, listing_concurrency=2)
    try:
        await _walk_together(adapter, [films, more])
    finally:
        await adapter.aclose()
    failure = crowd.index("out", (films, 0))
    successes = [
        at for at, (event, _, _) in enumerate(crowd.log) if event == "out" and at > failure
    ]
    tenth = successes[RAISE_AFTER - 1]
    assert 2 in crowd.widths(end=failure), "the premise: both walkers were in flight together"
    assert set(crowd.widths(failure, tenth)) == {1}
    assert 2 in crowd.widths(tenth)


async def test_a_page_waiting_to_ask_again_holds_no_slot() -> None:
    """The wait before a retry is spent outside the limit, so the other walker's page goes.

    One slot, two walkers, and one walker's first page answered 503. A page of the
    other library arrives between that answer and the retry.
    """
    server = FakeEmbyServer()
    films = _library(server, 1, "Films", [_movie(index) for index in range(4)])
    more = _library(server, 2, "More Films", [_movie(index) for index in range(20, 24)])
    crowd = _Crowd(server, fail=[(films, 0)])
    adapter = _crowded(crowd, listing_concurrency=1, waits=_TurningWaits())
    try:
        await _walk_together(adapter, [films, more])
    finally:
        await adapter.aclose()
    failure = crowd.index("out", (films, 0))
    retry = crowd.index("in", (films, 0), after=failure)
    between = [page for event, page, _ in crowd.log[failure:retry] if event == "in"]
    assert {view for view, _ in between} == {more}
```

- [ ] **Step 6: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_adapter.py -k "listing_concurrency or retried_failure or waiting_to_ask_again"`
Expected: FAIL — `TypeError: EmbyAdapter.__init__() got an unexpected keyword argument 'listing_concurrency'`, in all five.

- [ ] **Step 7: Put the limit around each attempt**

`src/usher/adapters/emby/adapter.py` — `from usher.adapters.emby.limit import ListingLimit`. `__init__` takes `listing_concurrency: int = 4` after `unit_max_items`, and stores, after `self._library_lock`:

```python
        # Every listing request this adapter sends, each walker's and each
        # read-ahead's, takes its turn from this one limit.
        self._listing_limit = ListingLimit(listing_concurrency, source=source.name)
```

`_page`'s docstring gains a paragraph of its own, between the one that ends `is shutting down.` and the sentence `Giving up raises …`, which becomes the closing paragraph. The docstring then reads:

```python
        """One page of a walk, asked for again while its failure is one a wait can fix.

        That is an outage -- a 5xx, a 408, a refused or dropped connection, a timeout -- or
        a 429, whose wait is its `Retry-After` when that is longer, up to the longest
        scheduled one. Nothing else is: a refused request and an answer that is not a
        listing would be the same answer next time, a rejected credential needs an
        operator, and a closed adapter -- which raises `PortUnavailable` too -- is shutting
        down.

        Each attempt holds one of the listing limit's slots, and the wait between
        attempts holds none. A failure of the kind asked again drops the limit to
        one; a success counts towards raising it.

        Giving up raises `PortUnavailable` naming the attempts, whatever the last
        failure was.
        """
```

and its loop body becomes:

```python
        while True:
            attempt += 1
            try:
                async with self._listing_limit.slot():
                    body = await self._session.json_body(
                        "GET", path, params=params, op="list", read_timeout=LISTING_READ_SECONDS
                    )
            except RequestRefused:
                raise
            except (PortUnavailable, PortRateLimited) as exc:
                if self._closed:
                    raise
                self._listing_limit.failed()
                if first_failure is None:
                    first_failure = self._clock()
                if attempt == attempts:
                    elapsed = self._clock() - first_failure
                    raise PortUnavailable(
                        f"{exc} (gave up after {attempt} attempts over {elapsed:.0f}s)"
                    ) from exc
                wait = PAGE_RETRY_WAITS[attempt - 1]
                if isinstance(exc, PortRateLimited) and exc.retry_after is not None:
                    wait = min(max(wait, exc.retry_after), PAGE_RETRY_WAITS[-1])
                logger.warning(
                    "{source}'s listing failed at StartIndex={start} "
                    "(attempt {attempt} of {attempts}): {error}; asking again in {wait:.0f}s",
                    source=self._source.name,
                    start=start,
                    attempt=attempt,
                    attempts=attempts,
                    error=str(exc),
                    wait=wait,
                )
                await self._sleep(wait)
            else:
                self._listing_limit.succeeded()
                return body
```

- [ ] **Step 8: Run them to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_limit.py tests/unit/test_adapters_emby_adapter.py`
Expected: PASS — the new five, and every Phase 1 retry case unchanged: a walk alone never has more than one page in flight, so a cap of four never binds it.

- [ ] **Step 9: Write the failing settings tests**

`tests/unit/test_config.py` — in `test_ingest_settings_have_usable_defaults`, after the `sync_max_retract_fraction` line:

```python
    assert settings.sync_walkers == 4
```

and after `test_sync_max_retract_fraction_is_a_fraction`:

```python
@pytest.mark.parametrize(
    ("value", "accepted"), [("0", False), ("1", True), ("16", True), ("17", False)]
)
def test_sync_walkers_is_from_one_to_sixteen(
    monkeypatch: pytest.MonkeyPatch, value: str, accepted: bool
) -> None:
    """Zero would park every listing request, and past sixteen is a typo, not a tuning."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_SYNC_WALKERS", value)
    if accepted:
        assert Settings().sync_walkers == int(value)
    else:
        with pytest.raises(ValidationError):
            Settings()
```

`tests/unit/test_adapters_factory.py`, `test_the_deployment_tuning_reaches_the_adapter` — the factory takes `listing_concurrency=3`, and after the `_page_size` assertion:

```python
        assert adapter._listing_limit.limit == 3
```

`tests/unit/test_composition.py` — `adapter_factory` and `source_gates` join the `usher.composition` import. After `_gate_of`:

```python
async def test_the_walker_setting_reaches_every_adapter_the_deployment_builds() -> None:
    """`USHER_SYNC_WALKERS`, from `Settings` through `adapter_factory` to the listing limit.

    The factory's own test hands it the value directly, so a composition root that
    dropped it would leave every deployment on the adapter's default of four.
    """
    settings = _settings(sync_walkers=3)
    adapter = adapter_factory(settings, source_gates(settings)).build(_GATED, _GATE_CREDENTIALS)
    try:
        assert isinstance(adapter, EmbyAdapter), "the premise: the factory built an Emby adapter"
        assert adapter._listing_limit.limit == 3
    finally:
        await adapter.aclose()
```

`tests/unit/test_telemetry_metric_names.py`: `len(catalogue) == 42` becomes `== 43`.

`tests/unit/test_dashboards.py`, `test_the_catalogue_scan_reaches_the_row_that_carries_no_usher_prefix` — PRD 10's table has a second reader: `assert len(catalogue) == 42, (` becomes `== 43`, and `not the 42 its own header` in its message becomes `not the 43 its own header`.

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_config.py tests/unit/test_adapters_factory.py tests/unit/test_composition.py tests/unit/test_telemetry_metric_names.py tests/unit/test_dashboards.py`
Expected: FAIL —
- `AttributeError: 'Settings' object has no attribute 'sync_walkers'`, in the defaults case and in `test_sync_walkers_is_from_one_to_sixteen[1-True]` and `[16-True]`;
- `test_sync_walkers_is_from_one_to_sixteen[0-False]` and `[17-False]` with `DID NOT RAISE`: an environment variable no field declares is ignored;
- the factory's `TypeError` on `listing_concurrency`;
- the composition case's `ValidationError` (`sync_walkers`: extra inputs are not permitted);
- both catalogue counts on 42 rows: `the catalogue table parse found 42 rows`, which stops that case before its declared-names line, and the dashboards reader's `PRD 10's metric table parsed to 42 rows`.

- [ ] **Step 10: The setting, wired through, and said everywhere a setting is**

`src/usher/config.py`, after `sync_max_retract_fraction`:

```python
    # The most listing requests one walk has in flight to a source; 1 is one at a time.
    sync_walkers: int = Field(default=4, ge=1, le=16)
```

`src/usher/adapters/factory.py` — `__init__` takes `listing_concurrency: int = 4` after `push_poll_seconds` and stores `self._listing_concurrency = listing_concurrency`; `build` passes `listing_concurrency=self._listing_concurrency` to `EmbyAdapter`.

`src/usher/composition.py`, `adapter_factory`: `listing_concurrency=settings.sync_walkers,` after `push_poll_seconds=…`.

`.env.example`, after `USHER_SYNC_MAX_RETRACT_FRACTION=0.25`:

```
# The most listing requests one walk has in flight to a source. A failure that is
# asked again drops it to one, and each ten pages that succeed raise it a step,
# back to this. 1 is one request at a time.
USHER_SYNC_WALKERS=4
```

`web/src/features/operator/Config.settings.ts`, after the `USHER_SYNC_MAX_RETRACT_FRACTION` entry. `measured` is true because Task 10 set the default; end `about` with one sentence carrying Task 10's two readings from the fixtures README's "Concurrent pages at the deployment gate" row:

```ts
  {
    key: 'USHER_SYNC_WALKERS',
    group: 'ingest',
    def: '4',
    about:
      'The most listing requests one walk has in flight to a source. A failure that is asked again drops it to one, and each ten pages that succeed raise it a step, back to this. 1 is one request at a time. Against a real Emby at the default gate, four walkers read <p4> pages/s and two read <p2>.',
    secret: false,
    measured: true,
  },
```

`docs/guide/configuration.md`, a new section before `## Semantic search`:

```markdown
## Walking a large library

A sync reads the media server's library a page at a time. `USHER_SYNC_WALKERS`
(default 4) caps how many of those requests are in flight at once. When the
server fails one, the cap drops to one by itself and climbs back a step for
every ten pages that succeed.

Every request is work for the media server. If you don't administer it, or a
sync slows down somebody's playback, set `USHER_SYNC_WALKERS=1` for one request
at a time.
```

`docs/prd/03-sources-and-sync.md`, "Walking the library", a bullet after the retry bullet:

```markdown
- **At most `USHER_SYNC_WALKERS` listing requests are in flight** (default
  **4**), the read-ahead included. A failure that is asked again drops that to
  one, and each run of ten pages that succeed raises it by one, back up to the
  setting. A page waiting for its turn, or waiting to ask again, holds no
  request.
```

`docs/prd/08-operations.md`, "Configuration", a paragraph before `Two things are **not** settings:`:

```markdown
**`USHER_SYNC_WALKERS` caps the listing requests in flight to a source** (default
4; 1 is one at a time), and the cap backs off by itself
([03](03-sources-and-sync.md#walking-the-library)).
```

`docs/prd/10-telemetry-and-dashboards.md` — the lead-in becomes `**Every row is emitted today. 43 rows: 42 instruments Usher declares, plus one `FastAPIInstrumentor` supplies.**`; a row after `usher.source.throttle.wait`:

```markdown
| `usher.source.listing.concurrency` | gauge | source | ✅ fast first sync |
```

and, before `**The three scheduler rows.**`:

```markdown
**`usher.source.listing.concurrency` is written after every listing page**, by
the adapter that sent it, so an adapter that never lists leaves no reading. It
reads `USHER_SYNC_WALKERS` while a walk is healthy, 1 straight after a failure,
and climbs back a step per ten pages. Two walks of one source at once write one
series.
```

`CHANGELOG.md`, under `## [Unreleased]`, a `### Added` heading above `### Changed` if there is none yet, with:

```markdown
- Listing requests to a source are capped at `USHER_SYNC_WALKERS` (default 4)
  and back off on their own: a failure that is asked again drops the cap to one,
  and every ten pages that succeed raise it by one. The gauge
  `usher.source.listing.concurrency` shows the cap.
```

- [ ] **Step 11: Run everything this task touched**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_emby_limit.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_factory.py tests/unit/test_composition.py tests/unit/test_config.py tests/unit/test_deployment_config.py tests/unit/test_console_settings_catalogue.py tests/unit/test_telemetry_metric_names.py tests/unit/test_dashboards.py tests/unit/test_docs_pointers.py`
Expected: PASS. `test_deployment_config.py` holds `.env.example` to `Settings`, both of which this task changed.

Run, from `web/`: `npm run verify && npm run e2e && npm run e2e:visual`
Expected: PASS.

- [ ] **Step 12: Plant and verify**

1. `while self._in_flight > self._limit:` in `slot()`. Expect `test_a_new_limit_lets_its_ceiling_in_and_holds_the_next_until_one_leaves` to fail on `{0, 1, 2, 3}` and every `cap` parametrisation of the adapter's cap case to fail on `cap + 1 == cap`.
2. `failed()` without `self._streak = 0`. Expect `test_a_failure_starts_the_run_of_successes_again` to fail, its first reading 2.
3. `succeeded()` with `if self._limit < self._ceiling:` removed (the raise unconditional). Expect `test_ten_successes_in_a_row_raise_the_limit_by_one_up_to_its_ceiling` to fail on its last reading, 4.
4. `succeeded()` without `self._wake.set()`. Expect `test_a_raised_limit_lets_a_waiting_page_in_while_the_first_still_holds` to fail on `{0}`.
5. `RAISE_AFTER = 9`. Expect the ten-successes case to fail on `[1] * 8 + …`. (The adapter's drop case imports the constant and moves with it; the literal tens in the limit's cases are the spec's number.)
6. `_concurrency.set(...)` removed from `failed()`. Expect the gauge case to fail, its second reading `[]`: the last-value aggregation resets on every collect, so a gauge nobody set since reads nothing.
7. `_concurrency.set(ceiling, self._labels)` added to `__init__`. Expect the gauge case to fail on `no page yet, so no reading`.
8. `_page`'s request sent without `async with self._listing_limit.slot():`. Expect all three `cap` parametrisations to fail on `5 == cap`.
9. `_page` without `self._listing_limit.failed()`. Expect `test_a_retried_failure_lets_pages_in_one_at_a_time_until_ten_succeed` to fail on `{1, 2} == {1}`.
10. `_page` without `self._listing_limit.succeeded()`. Expect the same case to fail on its last line: the cap never climbs back.
11. The slot moved to wrap the whole `while True:` loop, so it is held through the wait. Expect `test_a_page_waiting_to_ask_again_holds_no_slot` to fail on `set() == {more}`.
12. The factory's `build` without `listing_concurrency=`. Expect `test_the_deployment_tuning_reaches_the_adapter` to fail on `4 == 3`.
13. `adapter_factory` without `listing_concurrency=`. Expect `test_the_walker_setting_reaches_every_adapter_the_deployment_builds` to fail on `4 == 3`.
14. `sync_walkers` with `le=17`, then separately with `ge=0`. Expect the `17` parametrisation, then the `0` one, to fail with `DID NOT RAISE`.

- [ ] **Step 13: Commit**

```bash
git add src/usher/adapters/emby/limit.py src/usher/adapters/emby/adapter.py \
  src/usher/adapters/factory.py src/usher/composition.py src/usher/config.py \
  .env.example web/src/features/operator/Config.settings.ts docs/guide/configuration.md \
  docs/prd/03-sources-and-sync.md docs/prd/08-operations.md docs/prd/10-telemetry-and-dashboards.md \
  CHANGELOG.md tests/unit/test_adapters_emby_limit.py tests/unit/test_adapters_emby_adapter.py \
  tests/unit/test_adapters_factory.py tests/unit/test_composition.py tests/unit/test_config.py \
  tests/unit/test_telemetry_metric_names.py tests/unit/test_dashboards.py
git commit -m "emby: cap the listing requests in flight, and back off on a failure"
```

---

### Task 16: The planned walk — walkers, one writer, the stage barrier, the heartbeat

Spec §2.3, §2.4, the heartbeat half of §2.5 and the failure half of §2.6. A whole-library walk — a full walk, or a delta with no cursor — asks the adapter for its plan, stores the units, and walks them a stage at a time. Up to `walkers` walker tasks fetch a stage's units, largest first, and put their pages on a bounded queue; the reconcile task is the only writer. A unit's pages are committed once they add up to `USHER_SYNC_BATCH_SIZE` items and when the unit ends, each commit carrying the unit's position and a new heartbeat, and the writer beats the heartbeat at least once a minute between commits, whether pages are arriving or not. A unit that fails stops the walk. Resume (Task 17), `after_seed` (Task 18) and the event's new fields (Task 19) are not here: every planned run in this task is a fresh one.

Departures 3, 4 and 7 happen here. **3:** a unit's pages are committed once they add up to `USHER_SYNC_BATCH_SIZE` items, and when the unit ends — not after every page. **4:** the run and its first heartbeat are committed before `plan_walk` is called; the units ride the next commit. **7:** on a failure, pages fetched but not yet committed are dropped, and the failing unit is saved `failed` at its committed position.

The gap-closer keeps the single walk however it is configured (spec §2.3), so it passes `plan=False`. With `USHER_PUSH_GAP_CLOSE=always` and `USHER_PUSH_GAP_MAX_ITEMS=0` its delta is cursorless and unbounded, which the planned rule alone would plan.

`tests/integration/test_ingest_end_to_end.py::test_statements_do_not_grow_with_the_page` moves to `FakeSourceAdapter`. The real adapter's overlapping pages carry two or three new items each, so batches of whole pages give its two walks eight and nine commits, and the counts would differ for a reason that is not a statement per item.

**Files:**
- Modify: `src/usher/services/reconcile.py`
- Modify: `src/usher/config.py`, `src/usher/adapters/factory.py`, `src/usher/composition.py` (`adapter_factory`, `build_pipeline`), `src/usher/api/deps.py` (`get_reconcile_service`), `src/usher/api/lanes.py` (the gap-closer's `reconcile` call)
- Modify: `tests/fakes/source_adapter.py` (`stage`, `fail_unit_after`, `hold`, `journal`)
- Modify: `.env.example`, `web/src/features/operator/Config.settings.ts`, `docs/guide/configuration.md`, `docs/prd/02-data-model.md`, `docs/prd/03-sources-and-sync.md`, `docs/prd/08-operations.md`, `CHANGELOG.md`, `.claude/rules/emby-push-and-ingest.md`
- Test: `tests/unit/test_services_reconcile.py`, `tests/unit/test_api_lanes.py`, `tests/unit/test_services_handlers.py` (`_RecordingReconcile`'s override), `tests/unit/test_config.py`, `tests/unit/test_adapters_factory.py`, `tests/unit/test_composition.py`, `tests/integration/test_services_reconcile.py`, `tests/integration/test_pipeline_spans.py`, `tests/integration/test_pipeline_deps.py`, `tests/integration/test_ingest_end_to_end.py`

**Interfaces:**
- Consumes: Task 11's `WalkPlan`, `UnitPage`, `pages_of`, `WHOLE_LIBRARY`, `DEFAULT_UNIT_KEY`, `SourceAdapter.plan_walk()` and `.list_unit(key, *, start_index=0)`, `WalkStage`, `STAGE_ORDER`, and `FakeSourceAdapter`'s `place`, `page_size`, `plan_walk` and `_walk_library`; Task 12's `SyncRunUnit`, `SyncRunUnitStatus`, `SyncRun.heartbeat_at`, and `SyncRunRepository.add_units`/`save_unit`/`units_for`; Task 13's `EmbyAdapter(…, unit_max_items=…)`; Task 15's `Settings.sync_walkers` and `ConfiguredSourceAdapterFactory(…, listing_concurrency=…)`.
- Produces: `ReconcileService(…, walkers: int = 4, heartbeat_seconds: float = 60.0, clock: Callable[[], datetime] = _now)`, refusing `walkers < 1`; `reconcile(…, max_items: int = 0, plan: bool = True)`; the private `_walk_plan(source, progress, adapter)`, `_walk_stage`, `_fetch`, `_write`, `_commit_unit`, `_beat(progress)`, `_Fetched(unit_key, page=None, error=None)` and `_flush(…, unit=None)`. Task 17 splits `_walk_plan`'s first half into a fresh run's plan and a resumed run's stored units; Task 18 awaits `after_seed` between its stages.
- Produces: `Settings.sync_unit_max_items: int` (default 100,000, from 1,000 to 1,000,000); `ConfiguredSourceAdapterFactory(…, unit_max_items: int = 100_000)`.
- Produces (tests): `FakeSourceAdapter.stage(library, stage)`, `.fail_unit_after(library, count)`, `.hold(library) -> asyncio.Event` and `.journal: list[tuple[str, str]]`; in `tests/unit/test_services_reconcile.py`, `_Ticks`, `_Fixture(…, walkers=4, heartbeat_seconds=60.0)` with `.ingest`, `.journal`, `.checkpoints` and `.unit_states`, and the helpers `_shelve`, `_fetch_window` and `_progress`. Tasks 17–19 reuse all of them.

If Task 10 moved the unit size to 50,000, write 50,000 wherever this task writes 100,000.

- [ ] **Step 1: Teach the fake stages, failing units, held units and a journal**

`tests/fakes/source_adapter.py`. In `FakeSourceAdapter.__init__`, after `self.page_size = 2`:

```python
        # Library name -> the stage its unit is planned in; absent means TITLES.
        self._stages: dict[str, WalkStage] = {}
        # Library name -> how many items its unit yields before it raises.
        self._unit_failures: dict[str, int] = {}
        # Library name -> an event its unit waits on before its first item.
        self._holds: dict[str, asyncio.Event] = {}
        #: `("fetched", "library:<name>")` for every item a library's unit yields.
        self.journal: list[tuple[str, str]] = []
```

After `place`:

```python
    def stage(self, library: str, stage: WalkStage) -> None:
        """Plan `library`'s unit in `stage` instead of `TITLES`."""
        self._stages[library] = stage

    def fail_unit_after(self, library: str, count: int) -> None:
        """Have `library`'s unit raise `PortUnavailable` after yielding `count` items."""
        self._unit_failures[library] = count

    def hold(self, library: str) -> asyncio.Event:
        """Hold `library`'s unit before its first item until the event returned is set."""
        event = asyncio.Event()
        self._holds[library] = event
        return event
```

`clear_failure`'s summary line becomes `"""Undo `fail_after` and `fail_unit_after`.` (the rest of its docstring unchanged), and its body gains `self._unit_failures.clear()`. In `plan_walk`, the unit's `WalkStage.TITLES` becomes `self._stages.get(name, WalkStage.TITLES)`. `_walk_library` becomes:

```python
    async def _walk_library(self, name: str) -> AsyncIterator[SourceItem]:
        await self._ready()
        if name in self._holds:
            await self._holds[name].wait()
        yielded = 0
        for external_id in list(self._libraries[name]):
            item = self._items.get(external_id)
            if item is None:
                continue
            if yielded == self._unit_failures.get(name):
                raise PortUnavailable(f"library {name} went away mid-walk")
            # A turn of the loop per item, as a request gives, so walkers interleave.
            await asyncio.sleep(0)
            self.journal.append(("fetched", f"library:{name}"))
            yield item
            yielded += 1
```

- [ ] **Step 2: Write the failing service tests**

`tests/unit/test_services_reconcile.py` — imports: `import asyncio`; `AsyncGenerator` beside `Iterator`; `from itertools import groupby, pairwise`; `SyncRunUnitStatus, WalkStage` beside `SyncRunKind, SyncRunStatus`; `UnitPage, WalkPlan` beside `SourceItem, SourceItemKind`. Replace `_Fixture` with:

```python
class _Ticks:
    """A clock that moves on a second every time it is read."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return LATER + timedelta(seconds=self.reads)


class _Fixture:
    def __init__(
        self,
        *,
        batch_size: int = 1_000,
        max_retract_fraction: float = 0.25,
        walkers: int = 4,
        heartbeat_seconds: float = 60.0,
    ) -> None:
        self.source = Source(
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-1",
            device_id=str(new_id()),
        )
        self.adapter = FakeSourceAdapter(self.source)
        self.titles = FakeTitleRepository()
        self.matching = FakeTitleMatchRepository(titles=self.titles)
        self.queue = FakeJobQueue()
        self.media_items = FakeMediaItemRepository()
        self.runs = FakeSyncRunRepository()
        self.events = FakeEventPublisher()
        self.commits = 0
        # The adapter's own list, so its `fetched` entries and the `completed`
        # entries `_commit` adds are ordered against each other.
        self.journal = self.adapter.journal
        # Read back at every commit: the newest run's items and heartbeat, and
        # its units' statuses by key.
        self.checkpoints: list[tuple[int, datetime | None]] = []
        self.unit_states: list[dict[str, SyncRunUnitStatus]] = []
        self._completed: set[tuple[uuid.UUID, str]] = set()
        self.ingest = IngestService(
            matcher=MatchService(titles=self.titles, matching=self.matching, queue=self.queue),
            matching=self.matching,
            media_items=self.media_items,
            episodes=FakeEpisodeRepository(),
            queue=self.queue,
        )
        self.service = ReconcileService(
            ingest=self.ingest,
            media_items=self.media_items,
            runs=self.runs,
            events=self.events,
            commit=self._commit,
            batch_size=batch_size,
            max_retract_fraction=max_retract_fraction,
            walkers=walkers,
            heartbeat_seconds=heartbeat_seconds,
            clock=_Ticks(),
        )

    async def _commit(self) -> None:
        self.commits += 1
        newest = await self.runs.list_for_source(self.source.id, limit=1)
        if not newest:
            return
        run = newest[0]
        self.checkpoints.append((run.items_seen, run.heartbeat_at))
        units = await self.runs.units_for(run.id)
        self.unit_states.append({unit.unit_key: unit.status for unit in units})
        for unit in units:
            key = (run.id, unit.unit_key)
            if unit.status is SyncRunUnitStatus.COMPLETED and key not in self._completed:
                self._completed.add(key)
                self.journal.append(("completed", unit.unit_key))
```

Append to the file:

```python
# -- a whole-library walk, as a plan ---------------------------------------


def _shelve(
    fixture: _Fixture, library: str, ids: range, *, stage: WalkStage = WalkStage.TITLES
) -> list[str]:
    """Seed `m<id>` for each id into `library`, whose unit the fake plans in `stage`."""
    external_ids = [f"m{index}" for index in ids]
    for external_id in external_ids:
        fixture.adapter.seed(_item(external_id), T0)
        fixture.adapter.place(external_id, library)
    fixture.adapter.stage(library, stage)
    return external_ids


def _fetch_window(journal: list[tuple[str, str]], key: str) -> tuple[int, int]:
    """Where `key`'s first and last fetched items sit in the journal."""
    at = [index for index, entry in enumerate(journal) if entry == ("fetched", key)]
    return at[0], at[-1]


def _progress(fixture: _Fixture) -> list[object]:
    """`items_seen` of every `sync.progress` the walk published, in order."""
    return [
        event.data["items_seen"]
        for event in fixture.events.published
        if event.kind is ClientEventKind.SYNC_PROGRESS
    ]


async def test_a_whole_library_walk_stores_its_plan_and_completes_every_unit() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Films", range(3))
    _shelve(fixture, "Shows", range(10, 15))
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_seen == 8
    units = await fixture.runs.units_for(run.id)
    assert [
        (unit.unit_key, unit.status, unit.position, unit.items_seen, unit.expected_items)
        for unit in units
    ] == [
        ("library:Films", SyncRunUnitStatus.COMPLETED, 3, 3, 3),
        ("library:Shows", SyncRunUnitStatus.COMPLETED, 5, 5, 5),
    ]
    assert await fixture.media_items.count_for_source(fixture.source.id) == 8


async def test_no_unit_of_a_stage_is_fetched_before_every_unit_ahead_of_it_has_committed() -> None:
    """The stage barrier, with a walker free for every unit.

    Four walkers for three units, so without the barrier all three start at once.
    The episodes library is also planned first.
    """
    fixture = _Fixture(walkers=4)
    _shelve(fixture, "Episodes", range(20, 22), stage=WalkStage.EPISODES)
    _shelve(fixture, "Films", range(4))
    _shelve(fixture, "Shows", range(10, 14))
    plan = await fixture.adapter.plan_walk()
    assert plan.units[0].stage is WalkStage.EPISODES, "the premise: episodes are planned first"
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    first_episode = fixture.journal.index(("fetched", "library:Episodes"))
    titles_done = max(
        fixture.journal.index(("completed", key)) for key in ("library:Films", "library:Shows")
    )
    assert first_episode > titles_done


async def test_within_a_stage_the_largest_unit_is_fetched_first() -> None:
    """One walker, so the fetch order is the claim order."""
    fixture = _Fixture(walkers=1)
    _shelve(fixture, "Shorts", range(2))
    _shelve(fixture, "Films", range(10, 15))
    plan = await fixture.adapter.plan_walk()
    assert [unit.expected_items for unit in plan.units] == [2, 5], (
        "the premise: the plan lists the smaller unit first"
    )
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fetched = [key for event, key in fixture.journal if event == "fetched"]
    assert fetched == ["library:Films"] * 5 + ["library:Shorts"] * 2


@pytest.mark.parametrize(("walkers", "together"), [(1, False), (2, True)])
async def test_walkers_fetch_units_at_once_up_to_their_number(walkers: int, together: bool) -> None:
    """Two units' fetch windows overlap with two walkers and never with one."""
    fixture = _Fixture(walkers=walkers)
    _shelve(fixture, "Films", range(6))
    _shelve(fixture, "Shows", range(10, 16))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    films = _fetch_window(fixture.journal, "library:Films")
    shows = _fetch_window(fixture.journal, "library:Shows")
    assert (films[0] < shows[1] and shows[0] < films[1]) is together, (films, shows)


async def test_a_units_pages_are_committed_once_they_add_up_to_a_batch_and_when_it_ends() -> None:
    """Pages of two at a batch of three: a commit after four items, then one at the end.

    A commit per page reads [2, 4, 5], and a commit only at the end [5].
    """
    fixture = _Fixture(batch_size=3)
    _shelve(fixture, "Films", range(5))
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert _progress(fixture) == [4, 5]
    [unit] = await fixture.runs.units_for(run.id)
    assert (unit.position, unit.items_seen, unit.status) == (5, 5, SyncRunUnitStatus.COMPLETED)
    statuses = [states["library:Films"] for states in fixture.unit_states if states]
    assert [status for status, _ in groupby(statuses)] == [
        SyncRunUnitStatus.PENDING,
        SyncRunUnitStatus.RUNNING,
        SyncRunUnitStatus.COMPLETED,
    ]


async def test_a_failing_unit_fails_the_run_and_keeps_every_units_committed_position() -> None:
    """Shows raises after three of its four items: its first page committed, its third dropped.

    One walker, and Films is the larger, so Films has completed before Shows starts.
    """
    fixture = _Fixture(batch_size=2, walkers=1)
    _shelve(fixture, "Films", range(10, 15))
    shows = _shelve(fixture, "Shows", range(4))
    fixture.adapter.fail_unit_after("Shows", 3)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.status is SyncRunStatus.FAILED
    assert "library Shows went away" in (run.error or "")
    assert run.items_seen == 7
    assert run.items_retracted == 0
    stored = await fixture.runs.get(run.id)
    assert stored is not None and stored.items_seen == 7
    units = {unit.unit_key: unit for unit in await fixture.runs.units_for(run.id)}
    films, failed = units["library:Films"], units["library:Shows"]
    assert (films.status, films.position) == (SyncRunUnitStatus.COMPLETED, 5)
    assert (failed.status, failed.position, failed.items_seen) == (SyncRunUnitStatus.FAILED, 2, 2)
    assert await fixture.media_items.get_by_external_id(fixture.source.id, shows[2]) is None


async def test_the_run_and_its_first_heartbeat_are_committed_before_the_plan_is_made() -> None:
    """A walk that dies while planning still leaves a row saying when it was last alive."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    seen: list[tuple[int, datetime | None]] = []
    original = fixture.adapter.plan_walk

    async def _peek() -> WalkPlan:
        [run] = await fixture.runs.list_for_source(fixture.source.id)
        seen.append((fixture.commits, run.heartbeat_at))
        return await original()

    fixture.adapter.plan_walk = _peek  # type: ignore[method-assign]
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    [(commits, heartbeat)] = seen
    assert commits == 1, "the run's insert is committed before the plan is asked for"
    assert heartbeat is not None


async def test_every_commit_of_a_planned_walk_moves_the_heartbeat() -> None:
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Films", range(5))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    rises = [
        (before[1], after[1])
        for before, after in pairwise(fixture.checkpoints)
        if after[0] > before[0]
    ]
    assert len(rises) == 3, "the premise: three commits that each brought items"
    for earlier, later in rises:
        assert earlier is not None and later is not None and later > earlier


async def test_a_writer_waiting_on_its_walkers_still_heartbeats() -> None:
    """A page under retry can take minutes, and the run must not look dead meanwhile."""
    fixture = _Fixture(heartbeat_seconds=0.01)
    _shelve(fixture, "Films", range(2))
    release = fixture.adapter.hold("Films")
    walk = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    try:
        async with asyncio.timeout(5):
            while fixture.commits < 5:
                await asyncio.sleep(0.01)
        assert fixture.journal == [], "the premise: nothing was fetched while the writer waited"
        beats = [beat for _, beat in fixture.checkpoints[2:5] if beat is not None]
        assert len(beats) == 3
        assert beats == sorted(set(beats)), "a beat that did not move the heartbeat"
    finally:
        release.set()
        run = await walk
    assert run.status is SyncRunStatus.COMPLETED


async def test_pages_that_keep_arriving_below_a_batch_still_let_the_heartbeat_move() -> None:
    """A beat falls due a fixed time after the last commit, not after a silence.

    Forty pages of one item, a hundredth of a second apart, never fill a batch, so a
    writer that beat only when nothing arrived would not beat once in the walk.
    """
    fixture = _Fixture(heartbeat_seconds=0.1)
    _shelve(fixture, "Films", range(1))

    async def _trickle(key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]:
        for index in range(40):
            await asyncio.sleep(0.01)
            yield UnitPage((_item(f"t{index}"),), resume_at=index + 1)

    fixture.adapter.list_unit = _trickle  # type: ignore[method-assign]
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    carried = [seen for seen, _ in fixture.checkpoints if seen]
    assert carried[0] == 40, "the premise: the unit's forty items rode one commit"
    beats = [beat for seen, beat in fixture.checkpoints[2:] if seen == 0]
    assert len(beats) >= 2, "no beat while pages kept arriving"


async def test_a_unit_that_ends_on_a_batch_boundary_publishes_no_second_frame() -> None:
    """Its end commits an empty batch to save the unit `completed`, and that is no frame.

    PRD 07 publishes one `sync.progress` per committed batch, and an empty one moved
    no counter: four items in batches of two are two frames, not three.
    """
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Films", range(4))
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert _progress(fixture) == [2, 4]
    [unit] = await fixture.runs.units_for(run.id)
    assert unit.status is SyncRunUnitStatus.COMPLETED


async def test_a_delta_with_no_cursor_walks_the_plan_and_never_sweeps() -> None:
    """A console-triggered first sync.

    The row the source no longer has is a fifth of what Usher holds, under the
    retraction ceiling, so only the rule that a delta never sweeps can spare it.
    """
    fixture = _Fixture()
    _shelve(fixture, "Films", range(4))
    await fixture.ingest.ingest_batch(fixture.source.id, [_item("m90")], observed_at=T0)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert run.cursor_at is None, "the premise: no walk has completed, so there is no cursor"
    assert [unit.unit_key for unit in await fixture.runs.units_for(run.id)] == ["library:Films"]
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_retracted == 0
    kept = await fixture.media_items.get_by_external_id(fixture.source.id, "m90")
    assert kept is not None and kept.available is True


async def test_a_delta_with_a_cursor_keeps_the_single_walk() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    delta = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert delta.cursor_at is not None, "the premise: the full walk gave the delta its cursor"
    assert delta.heartbeat_at is None
    assert await fixture.runs.units_for(delta.id) == []


async def test_a_bounded_walk_keeps_the_single_walk_even_with_no_cursor() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.DELTA, fixture.adapter, max_items=10
    )
    assert run.cursor_at is None, "the premise: a cursorless delta, which would otherwise plan"
    assert run.status is SyncRunStatus.COMPLETED
    assert run.heartbeat_at is None
    assert await fixture.runs.units_for(run.id) == []


async def test_a_walk_told_not_to_plan_keeps_the_single_walk() -> None:
    """The gap-closer's walk, which stays one stream even unbounded and cursorless."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False
    )
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_seen == 2
    assert run.heartbeat_at is None
    assert await fixture.runs.units_for(run.id) == []


async def test_a_failing_unit_cancels_the_walkers_still_fetching() -> None:
    """Films fails at once while Shows is held; the walk ends without waiting for Shows."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    _shelve(fixture, "Shows", range(10, 12))
    fixture.adapter.fail_unit_after("Films", 0)
    release = fixture.adapter.hold("Shows")
    try:
        async with asyncio.timeout(5):
            run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    finally:
        release.set()
    assert run.status is SyncRunStatus.FAILED
    walkers = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__qualname__", "") == "ReconcileService._fetch"
    ]
    assert walkers == []


async def test_a_bug_in_a_walker_is_raised_not_recorded() -> None:
    """A walker hands the writer whatever it raised, so a bug ends the walk loudly.

    A walker that died of it silently would leave the writer waiting for an end that
    never comes, heartbeating forever; the deadline turns that into a failure.
    """
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))

    def _broken(key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]:
        raise ZeroDivisionError("a bug, not an outage")

    fixture.adapter.list_unit = _broken  # type: ignore[method-assign]
    with pytest.raises(ZeroDivisionError):
        async with asyncio.timeout(5):
            await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)


def test_a_service_with_no_walkers_is_refused() -> None:
    """No walker would fetch, and the writer would wait on an empty queue forever."""
    with pytest.raises(ValueError, match="at least one walker"):
        _Fixture(walkers=0)
```

`tests/integration/test_services_reconcile.py` — `AsyncGenerator` beside `AsyncIterator, Iterator`; `SyncRunUnitStatus` beside `SyncRunKind, SyncRunStatus`; `DEFAULT_UNIT_KEY, WHOLE_LIBRARY, UnitPage, WalkPlan, pages_of` beside `SourceItem, SourceItemKind`. `_Adapter`'s summary line becomes `"""The smallest `list_items`, `plan_walk` and `list_unit` that satisfy `ReconcileService`.`, and after `_walk` it gains:

```python
    async def plan_walk(self) -> WalkPlan:
        return WHOLE_LIBRARY

    def list_unit(self, key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]:
        # Pages of two, so at the fixture's batch of two a planned walk commits
        # page by page, as its single walk commits item pair by item pair.
        return pages_of(self._walk(), start_index=start_index, size=2)
```

and after `test_a_walk_that_raises_leaves_every_row_available`:

```python
async def test_a_whole_library_walk_persists_its_units_against_real_sql(
    service: ReconcileService,
    runs: PostgresSyncRunRepository,
    source: Source,
    adapter: _Adapter,
) -> None:
    """The writer's unit saves and heartbeats ride its commits, through real SQL."""
    for index in range(5):
        adapter.items[f"m{index}"] = _item(f"m{index}")
    run = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    assert run.status is SyncRunStatus.COMPLETED
    [unit] = await runs.units_for(run.id)
    assert (unit.unit_key, unit.status, unit.position, unit.items_seen) == (
        DEFAULT_UNIT_KEY,
        SyncRunUnitStatus.COMPLETED,
        5,
        5,
    )
    stored = await runs.get(run.id)
    assert stored is not None
    assert stored.heartbeat_at is not None and stored.heartbeat_at == run.heartbeat_at
```

`tests/integration/test_pipeline_spans.py` — `AsyncGenerator` beside `AsyncIterator`; `WHOLE_LIBRARY, UnitPage, WalkPlan, pages_of` beside `SourceItem, SourceItemKind`. `_Adapter`'s summary line becomes `"""The smallest `list_items`, `plan_walk` and `list_unit` `ReconcileService` uses, with no network.`, and after `_walk` it gains:

```python
    async def plan_walk(self) -> WalkPlan:
        return WHOLE_LIBRARY

    def list_unit(self, key: str, *, start_index: int = 0) -> AsyncGenerator[UnitPage]:
        return pages_of(self._walk(), start_index=start_index)
```

- [ ] **Step 3: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_reconcile.py tests/integration/test_services_reconcile.py::test_a_whole_library_walk_persists_its_units_against_real_sql`
Expected: FAIL — every unit case at `_Fixture`, with `TypeError: ReconcileService.__init__() got an unexpected keyword argument 'walkers'`; the integration case on `[unit] = …` (`ValueError: not enough values to unpack`), because nothing plans yet.

- [ ] **Step 4: Write the planned walk**

`src/usher/services/reconcile.py`. The imports become:

```python
import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import metrics, trace
from pydantic import AwareDatetime

from usher.domain.source import Source
from usher.domain.sync import (
    STAGE_ORDER,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunUnit,
    SyncRunUnitStatus,
)
from usher.ports.errors import UsherPortError
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.ingest import AvailabilitySweepRefused
from usher.ports.repository import MediaItemRepository, SyncRunRepository
from usher.ports.source import SourceAdapter, SourceItem, UnitPage
from usher.services.ingest import IngestService
```

After `_recorded_failure`:

```python
def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Fetched:
    """What a walker hands the writer: a page of a unit, the unit's end, or its error."""

    unit_key: str
    page: UnitPage | None = None
    error: Exception | None = None
```

`__init__` takes, after `max_retract_fraction`:

```python
        walkers: int = 4,
        heartbeat_seconds: float = 60.0,
        clock: Callable[[], datetime] = _now,
```

and its body begins:

```python
        if walkers < 1:
            raise ValueError(f"a whole-library walk needs at least one walker, not {walkers}")
```

and ends:

```python
        self._walkers = walkers
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
```

`reconcile` takes `plan: bool = True` after `max_items`. Its docstring becomes:

```python
        """Walk `source` and reconcile it.

        A whole-library walk -- a full walk, or a delta with no cursor -- walks the
        adapter's plan, unless `max_items` bounds it or `plan` is false; every other
        walk is one stream.

        Never raises a `UsherPortError`.
        """
```

Replace everything from `cursor = await self.cursor_for(source, kind)` through `truncated = await self._walk(source, progress, adapter, cursor, max_items)` with:

```python
            cursor = await self.cursor_for(source, kind)
            planned = plan and not max_items and (kind is SyncRunKind.FULL or cursor is None)
            run = SyncRun(
                source_id=source.id,
                kind=kind,
                cursor_at=cursor,
                # A planned walk's first heartbeat rides the insert, so one that dies
                # while planning still leaves a row saying when it was last alive.
                heartbeat_at=self._clock() if planned else None,
            )
            # Inserted and committed before the walk begins, `RUNNING`: an
            # operator watching a six-hour sync needs a row to watch, and a
            # process killed mid-walk must leave a trace rather than nothing.
            await self._runs.add(run)
            await self._commit()
            progress = _Progress(run)
            try:
                truncated = False
                if planned:
                    await self._walk_plan(source, progress, adapter)
                else:
                    truncated = await self._walk(source, progress, adapter, cursor, max_items)
```

After `_walk`:

```python
    async def _walk_plan(self, source: Source, progress: _Progress, adapter: SourceAdapter) -> None:
        """Walk the adapter's plan, one stage at a time.

        The units are stored with the heartbeat that follows the plan. A stage
        starts only once every unit of the stages before it has committed
        complete, so every episode finds its series.
        """
        plan = await adapter.plan_walk()
        units = [
            SyncRunUnit(
                run_id=progress.run.id,
                unit_key=unit.key,
                stage=unit.stage,
                label=unit.label,
                expected_items=unit.expected_items,
            )
            for unit in plan.units
        ]
        await self._runs.add_units(units)
        await self._beat(progress)
        for stage in STAGE_ORDER:
            staged = [unit for unit in units if unit.stage is stage]
            if staged:
                await self._walk_stage(source, progress, adapter, staged)

    async def _walk_stage(
        self,
        source: Source,
        progress: _Progress,
        adapter: SourceAdapter,
        units: Sequence[SyncRunUnit],
    ) -> None:
        """Up to `walkers` walkers fetch `units`, the largest first, while this task writes.

        Returns once every unit has committed complete. However it ends, the
        walkers still fetching are cancelled and awaited first.
        """
        claimable = deque(sorted(units, key=lambda unit: unit.expected_items or 0, reverse=True))
        queue: asyncio.Queue[_Fetched] = asyncio.Queue(maxsize=self._walkers)
        walkers = [
            asyncio.create_task(self._fetch(adapter, claimable, queue))
            for _ in range(min(self._walkers, len(units)))
        ]
        try:
            await self._write(source, progress, units, queue)
        finally:
            for walker in walkers:
                walker.cancel()
            await asyncio.gather(*walkers, return_exceptions=True)

    @staticmethod
    async def _fetch(
        adapter: SourceAdapter, claimable: deque[SyncRunUnit], queue: asyncio.Queue[_Fetched]
    ) -> None:
        """A walker: claim the next unit, put its pages on `queue`, then its end.

        A walker never touches the database. A unit that raises puts the error on
        the queue in place of its end, and the walker stops.
        """
        while claimable:
            unit = claimable.popleft()
            try:
                pages = adapter.list_unit(unit.unit_key, start_index=unit.position)
                async with aclosing(pages):
                    async for page in pages:
                        await queue.put(_Fetched(unit.unit_key, page))
            except Exception as exc:
                await queue.put(_Fetched(unit.unit_key, error=exc))
                return
            await queue.put(_Fetched(unit.unit_key))

    async def _write(
        self,
        source: Source,
        progress: _Progress,
        units: Sequence[SyncRunUnit],
        queue: asyncio.Queue[_Fetched],
    ) -> None:
        """The one writer: each unit's pages, committed once they add up to a batch.

        A unit's pages are held until they reach `batch_size` items or the unit
        ends, then committed with its position, the run's counters and a new
        heartbeat. A beat of the heartbeat alone falls due `heartbeat_seconds`
        after the last commit or beat, whether or not pages are arriving.
        Returns once every unit has committed complete. A unit that failed is
        saved `failed` at its committed position and its error raised; the pages
        held for it are dropped.
        """
        current = {unit.unit_key: unit for unit in units}
        held: dict[str, list[UnitPage]] = {key: [] for key in current}
        open_units = len(units)
        # A deadline, not a silence: pages that keep arriving below a batch would
        # otherwise hold every beat off.
        due = time.monotonic() + self._heartbeat_seconds
        while open_units:
            if time.monotonic() >= due:
                await self._beat(progress)
                due = time.monotonic() + self._heartbeat_seconds
                continue
            try:
                fetched = await asyncio.wait_for(queue.get(), max(0.0, due - time.monotonic()))
            except TimeoutError:
                continue
            unit = current[fetched.unit_key]
            if fetched.error is not None:
                await self._runs.save_unit(unit.evolve(status=SyncRunUnitStatus.FAILED))
                raise fetched.error
            pages = held[unit.unit_key]
            if fetched.page is not None:
                pages.append(fetched.page)
                if sum(len(page.items) for page in pages) < self._batch_size:
                    continue
            done = fetched.page is None
            current[unit.unit_key] = await self._commit_unit(
                source, progress, unit, pages, done=done
            )
            due = time.monotonic() + self._heartbeat_seconds
            held[unit.unit_key] = []
            if done:
                open_units -= 1

    async def _commit_unit(
        self,
        source: Source,
        progress: _Progress,
        unit: SyncRunUnit,
        pages: Sequence[UnitPage],
        *,
        done: bool,
    ) -> SyncRunUnit:
        """Ingest `pages` and commit them with the unit's position and a new heartbeat.

        Returns the unit as saved: `completed` once its walk has ended, which an
        empty `pages` can carry on its own.
        """
        items = [item for page in pages for item in page.items]
        unit = unit.evolve(
            position=pages[-1].resume_at if pages else unit.position,
            items_seen=unit.items_seen + len(items),
            status=SyncRunUnitStatus.COMPLETED if done else SyncRunUnitStatus.RUNNING,
        )
        progress.run = await self._flush(
            source, progress.run.evolve(heartbeat_at=self._clock()), items, unit=unit
        )
        return unit

    async def _beat(self, progress: _Progress) -> None:
        """Move the run's heartbeat and commit it, with nothing else to write."""
        progress.run = progress.run.evolve(heartbeat_at=self._clock())
        await self._runs.save(progress.run)
        await self._commit()
```

`_flush` takes the unit whose pages it commits:

```python
    async def _flush(
        self,
        source: Source,
        run: SyncRun,
        batch: Sequence[SourceItem],
        *,
        unit: SyncRunUnit | None = None,
    ) -> SyncRun:
```

and, in its body, after the `run = run.evolve(…)` whose last line is `items_unmatched=run.items_unmatched + result.unmatched,` and before the `await self._runs.save(run)` that follows it — `run = run.evolve(` and that save each occur twice in the module, and this pair is `_flush`'s:

```python
        if unit is not None:
            # A planned walk's unit moves in the commit that holds the items it read.
            await self._runs.save_unit(unit)
```

and its `await self._publish_progress(source, run)`, the line before `return run`, becomes:

```python
        if batch:
            # A unit's end can commit an empty batch, to save the unit `completed`;
            # it moved no counter, so it is no frame (PRD 07: one per batch).
            await self._publish_progress(source, run)
```

A single walk never flushes an empty batch, so the guard changes only a planned walk's frames.

- [ ] **Step 5: Run them to see them pass**

Run the Step 3 command, then `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/integration/test_services_reconcile.py tests/integration/test_pipeline_spans.py`.
Expected: PASS — the new cases and every existing one. A `FakeSourceAdapter` with no libraries plans the port's one unit, so every full walk in the unit file now walks a one-unit plan in pages of two; at the batch sizes those cases set (two, three, four), the commits and `sync.progress` counts they assert are unchanged. One comment's count does change: in `test_a_run_checkpoints_every_batch`, `# Four batches, plus the run's own insert and its final save.` becomes `# Four batches, plus the run's insert, the beat that stores its plan, and its final save.` — seven commits, which its `>= 6` still holds.

- [ ] **Step 6: Keep the statement count's premise true**

`tests/integration/test_ingest_end_to_end.py` — `from tests.fakes.source_adapter import FakeSourceAdapter`. Replace `test_statements_do_not_grow_with_the_page` with:

```python
async def test_statements_do_not_grow_with_the_page(
    session: AsyncSession,
    media_items: PostgresMediaItemRepository,
    episodes: PostgresEpisodeRepository,
    runs: PostgresSyncRunRepository,
    queue: PostgresJobQueue,
    statement_counter: list[str],
    source: Source,
    catalog: uuid.UUID,
) -> None:
    """A flat statement count, at the library's own shape.

    Walked through `FakeSourceAdapter`, whose pages are exactly `page_size` items, so
    each measured walk is nine batches of one page. The real adapter's overlapping
    pages carry two or three new items, and batches of whole pages would give the two
    walks different commit counts for a reason that is not a statement per item.
    """
    adapter = FakeSourceAdapter(source)
    commits: list[int] = []

    def _stock(items: list[SourceItem]) -> None:
        # Name order, as the real listing's `SortName` gives: each batch holds one
        # kind of item, and the two walks' batches hold the same kinds.
        for item in sorted(items, key=lambda one: one.name):
            adapter.forget(item.external_id)
            adapter.seed(item, CHANGED_AT)

    def _walker(batch_size: int) -> ReconcileService:
        adapter.page_size = batch_size

        async def commit() -> None:
            commits.append(batch_size)
            await session.flush()

        matching = PostgresTitleMatchRepository(session)
        return ReconcileService(
            ingest=IngestService(
                matcher=MatchService(
                    titles=PostgresTitleRepository(session), matching=matching, queue=queue
                ),
                matching=matching,
                media_items=media_items,
                episodes=episodes,
                queue=queue,
            ),
            media_items=media_items,
            events=FakeEventPublisher(),
            runs=runs,
            commit=commit,
            batch_size=batch_size,
        )

    small_library = _mixed(series=5, episodes=20, movies=20, offset=0)
    _stock(small_library)
    await _walker(1_000).reconcile(source, SyncRunKind.FULL, adapter)
    statement_counter.clear()
    await _walker(5).reconcile(source, SyncRunKind.FULL, adapter)
    small = len(statement_counter)

    _stock(small_library + _mixed(series=45, episodes=180, movies=180, offset=1_000))
    await _walker(1_000).reconcile(source, SyncRunKind.FULL, adapter)
    statement_counter.clear()
    await _walker(50).reconcile(source, SyncRunKind.FULL, adapter)
    large = len(statement_counter)

    assert commits.count(5) == commits.count(50), (
        "the premise: both measured walks committed the same number of times"
    )
    assert small == large, (
        f"{small} statements for 9 batches of 5, {large} for 9 batches of 50 -- "
        "something costs a statement per item"
    )
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/integration/test_ingest_end_to_end.py`
Expected: PASS. Every other case in the file walks the real adapter's plan — the seed, empty because nothing is watched, then one unit of everything, because the fake server has no views — and asserts what it asserted before.

- [ ] **Step 7: Write the failing wiring tests**

`tests/unit/test_config.py` — in `test_ingest_settings_have_usable_defaults`, after the `sync_walkers` line:

```python
    assert settings.sync_unit_max_items == 100_000
```

and after `test_sync_walkers_is_from_one_to_sixteen`:

```python
@pytest.mark.parametrize(
    ("value", "accepted"),
    [("999", False), ("1000", True), ("1000000", True), ("1000001", False)],
)
def test_sync_unit_max_items_is_from_one_page_to_a_million(
    monkeypatch: pytest.MonkeyPatch, value: str, accepted: bool
) -> None:
    """Below a page a chunk is all overhead, and past a million it is a typo."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_SYNC_UNIT_MAX_ITEMS", value)
    if accepted:
        assert Settings().sync_unit_max_items == int(value)
    else:
        with pytest.raises(ValidationError):
            Settings()
```

`tests/unit/test_adapters_factory.py`, `test_the_deployment_tuning_reaches_the_adapter` — the factory also takes `unit_max_items=5_000`, and after the `_listing_limit` assertion:

```python
        assert adapter._unit_max_items == 5_000
```

`tests/unit/test_composition.py`, after Task 15's `test_the_walker_setting_reaches_every_adapter_the_deployment_builds`:

```python
async def test_the_walk_settings_reach_the_reconciler_and_the_adapter() -> None:
    """`USHER_SYNC_WALKERS` reaches `ReconcileService`, `USHER_SYNC_UNIT_MAX_ITEMS` the adapter.

    `create_async_engine` does not connect, and nothing here issues a statement.
    """
    engine = create_async_engine("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher")
    try:
        pipeline = build_pipeline(
            AsyncSession(engine), _settings(sync_walkers=3, sync_unit_max_items=5_000)
        )
        assert pipeline.reconcile._walkers == 3
        adapter = pipeline.adapters.build(_GATED, _GATE_CREDENTIALS)
        try:
            assert isinstance(adapter, EmbyAdapter), (
                "the premise: the factory built an Emby adapter"
            )
            assert adapter._unit_max_items == 5_000
        finally:
            await adapter.aclose()
    finally:
        await engine.dispose()
```

`tests/integration/test_pipeline_deps.py`, `test_the_reconcile_service_carries_this_deployments_tuning` — `Settings(…)` also takes `sync_walkers=3`, the probe returns `"walkers": service._walkers` as a third entry, and the last line becomes `assert body == {"batch_size": 7, "max_retract_fraction": 0.5, "walkers": 3}`.

`tests/unit/test_services_handlers.py` — `_RecordingReconcile.reconcile` takes `plan: bool = True` after `max_items` and ignores it: mypy refuses an override that accepts fewer arguments than `ReconcileService.reconcile`.

`tests/unit/test_api_lanes.py` — `_RecordingReconcile.reconcile` takes `plan: bool = True` after `max_items`, and its last line becomes `return await super().reconcile(source, kind, adapter, max_items=max_items, plan=plan)`. After `test_push_gap_close_always_walks_uncursored_and_says_so_first`:

```python
async def test_the_gap_closer_walks_one_stream_even_unbounded_and_uncursored(
    fakes: _Fakes,
) -> None:
    """The gap-closer never walks a plan, whatever it is configured to do.

    A planned walk stores its units and a heartbeat before it reads a page, so the
    gap-closer's delta having neither is the whole claim.
    """
    source = _source("A")
    await _seed(fakes, source)
    fakes.adapters.stock(_item("emby-1"), _CHANGED_AT)
    supervisor = _supervisor(
        fakes, worker_enabled=False, push_gap_close="always", push_gap_max_items=0
    )
    await supervisor.start()
    try:
        await _drain(lambda: _stored(fakes.media_items) == ["emby-1"])
    finally:
        await supervisor.stop()
    assert fakes.gap_ceilings == [0], "the premise: the lane walked with no ceiling"
    [delta] = [
        run for run in await fakes.runs.list_for_source(source.id) if run.kind is SyncRunKind.DELTA
    ]
    assert delta.heartbeat_at is None
    assert await fakes.runs.units_for(delta.id) == []
```

- [ ] **Step 8: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_config.py tests/unit/test_adapters_factory.py tests/unit/test_composition.py tests/unit/test_api_lanes.py tests/integration/test_pipeline_deps.py`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'sync_unit_max_items'`; the factory's `TypeError` on `unit_max_items`; the composition case's `ValidationError` (`sync_unit_max_items`: extra inputs are not permitted); the gap-closer case on `assert delta.heartbeat_at is None`, its walk planned; the deps probe on `'walkers': 4`.

- [ ] **Step 9: Wire it**

`src/usher/config.py` — Task 15's comment above `sync_walkers` becomes `# Units a whole-library walk fetches at once, and the most listing requests in flight to a source; 1 is one at a time.` (wrapped at 100 columns), and after `sync_walkers`:

```python
    # Items per episode chunk when a whole-library walk splits a library.
    sync_unit_max_items: int = Field(default=100_000, ge=1_000, le=1_000_000)
```

`src/usher/adapters/factory.py` — `__init__` takes `unit_max_items: int = 100_000` after `listing_concurrency` and stores `self._unit_max_items = unit_max_items`; `build` passes `unit_max_items=self._unit_max_items` to `EmbyAdapter`.

`src/usher/composition.py` — `adapter_factory`: `unit_max_items=settings.sync_unit_max_items,` after `listing_concurrency=…`. `build_pipeline`'s `ReconcileService(…)`: `walkers=settings.sync_walkers,` after the `batch_size=settings.sync_batch_size,` that is followed by `max_retract_fraction=(` — the other one is the watch lane's.

`src/usher/api/deps.py`, `get_reconcile_service`: `walkers=settings.sync_walkers,` after `max_retract_fraction=…`.

`src/usher/api/lanes.py`, the gap-closer's `pipeline.reconcile.reconcile(…)` call, after `max_items=self._settings.push_gap_max_items,`:

```python
                # One stream, even unbounded and with no cursor: PRD 03 keeps the
                # gap-closer off the planned walk.
                plan=False,
```

Run the Step 8 command. Expected: PASS.

- [ ] **Step 10: Say it everywhere a setting and a behaviour are said**

`.env.example` — Task 15's `USHER_SYNC_WALKERS` block becomes:

```
# How many units a whole-library walk fetches at once, and the most listing
# requests it has in flight to a source. A failure that is asked again drops the
# requests to one, and each ten pages that succeed raise them a step, back to
# this. 1 is one request at a time.
USHER_SYNC_WALKERS=4
# A whole-library walk reads a library's episodes in chunks of this many items,
# several at once.
USHER_SYNC_UNIT_MAX_ITEMS=100000
```

`web/src/features/operator/Config.settings.ts` — `USHER_SYNC_BATCH_SIZE`'s `about` becomes `'Items per committed batch during a sync walk. A whole-library walk commits whole pages, so its batches can run past this by less than a page.'`. `USHER_SYNC_WALKERS`'s `about` replaces its first three sentences with `How many units a whole-library walk fetches at once, and the most listing requests it has in flight to a source. A failure that is asked again drops the requests to one, and each ten pages that succeed raise them a step, back to this. 1 is one request at a time.`, keeping Task 15's closing sentence of readings. After that entry, with `<head>`, `<middle>` and `<deep>` from the fixtures README's "Pages inside one library" row (Task 10):

```ts
  {
    key: 'USHER_SYNC_UNIT_MAX_ITEMS',
    group: 'ingest',
    def: '100000',
    about:
      'A whole-library walk reads a library’s episodes in chunks of this many items, several at once. Against a real Emby, a 1,000-item page inside a library took <head> s at its head, <middle> s in the middle and <deep> s at the deep end.',
    secret: false,
    measured: true,
  },
```

`docs/guide/configuration.md`, "## Walking a large library" — after its first paragraph:

```markdown
A full sync, or the first sync of a source, reads the library as a plan: what
you are watching first, then each library's films and shows, then their
episodes, a library larger than `USHER_SYNC_UNIT_MAX_ITEMS` (default 100,000)
in several pieces. Up to `USHER_SYNC_WALKERS` pieces are read at once.
```

and after it, the bound the page size sets on a piece. The figures follow from `MAX_PAGES` (10,000 pages per `_pages` call) and `OffsetWindow.advance`, whose reach-back is `min(PAGE_OVERLAP, served // 2)`. At a page of p, a piece of N items that ends on a short page needs N < p + 9,999 × (p − reach-back); an episode chunk ends at its bound instead, and needs its span, `USHER_SYNC_UNIT_MAX_ITEMS` + `PAGE_OVERLAP` items, to be at most p + 9,999 × (p − reach-back).

```markdown
`USHER_SOURCE_PAGE_SIZE` (default 1,000, at most 1,000) also bounds how large a
piece can be. A sync reads at most 10,000 pages of one piece, and each page
after the first moves on by the page size less a reach-back of 50 items, or of
half the page below 100. A piece those pages do not finish fails:

| Page size | A piece fails once it holds |
|---|---|
| 1,000 | 9,500,050 items |
| 100 | 500,050 items |
| 21 | 110,010 items |
| 20 | 100,010 items |

A piece of episodes reads 50 items past its end, so at a page size of 20 or
less every full piece of `USHER_SYNC_UNIT_MAX_ITEMS` (100,000) fails. A server
that serves fewer items a page than it is asked for moves on by what it serves.
```

If Task 10 moved the unit size to 50,000, that last paragraph's "20 or less" becomes "10 or less": a 50,050-item chunk needs a page of 11.

`docs/prd/03-sources-and-sync.md`, "Walking the library" — after Task 15's `USHER_SYNC_WALKERS` bullet:

```markdown
- **A whole-library walk is split into units.** Emby plans three stages: what
  the account is watching, then each library's movies and series, then each
  library's episodes in chunks of `USHER_SYNC_UNIT_MAX_ITEMS` (default
  **100,000**), the largest library first. A library is any view but a
  collection or a playlist. The first stage is planned only when the played
  and the in-progress filters each narrow the library. When the libraries hold
  fewer items than the source, everything after the first stage is one walk of
  the whole source, and a WARNING names both numbers.
```

The same section, after the bullet beginning `**Items are walked in ascending creation order**`, gains the bound the page size sets, which the guide's table above spells out:

```markdown
- **A walk fails on a listing that 10,000 pages do not finish**, and each unit
  of a whole-library walk is a listing of its own: at the default page size
  that is one of 9,500,050 items or more, and fewer at a smaller page (the
  [configuration guide](../guide/configuration.md#walking-a-large-library) has
  the table).
```

"Reconciliation is not optional" — after the paragraph beginning `**A delta with no cursor is a walk of the whole library**`:

```markdown
**A whole-library walk — a full walk, or a delta with no cursor — walks the
adapter's plan.** Up to `USHER_SYNC_WALKERS` units are fetched at once, the
largest of a stage first, and one writer commits each unit's pages once they
add up to `USHER_SYNC_BATCH_SIZE` items, and when the unit ends. No unit of a
stage is fetched until every unit of the stages before it has committed, so an
episode always finds its series. A walk's units are kept in `sync_run_units`
with the position each has committed to, and the run's `heartbeat_at` moves on
every commit and at least once a minute between commits. A unit that
fails stops the walk: it is marked `failed` at its committed position, and
pages fetched but not yet committed are dropped. A delta with a cursor, a
bounded walk and the gap-closer keep the single walk.
```

In the paragraph beginning `**Retraction is a separate step, and it can decline.**`, `happens only after a walk returns normally, and even then` becomes `happens only after a walk returns normally — after a whole-library walk, once every unit has completed — and even then`.

`docs/prd/08-operations.md` — Task 15's `USHER_SYNC_WALKERS` paragraph becomes:

```markdown
**`USHER_SYNC_WALKERS` is how many units a whole-library walk fetches at once,
and the cap on listing requests in flight to a source** (default 4; 1 is one at
a time); the cap backs off by itself. **`USHER_SYNC_UNIT_MAX_ITEMS`** (default
100,000) is the size of the episode chunks such a walk splits a library into
([03](03-sources-and-sync.md#walking-the-library)).
```

`docs/prd/02-data-model.md`, "Supporting tables", the `sync_runs` row: `` `heartbeat_at` (🔶 nothing moves it yet) `` becomes `` `heartbeat_at`, which a whole-library walk's writer moves on every commit ([03](03-sources-and-sync.md)) ``.

`CHANGELOG.md`, under `## [Unreleased]`'s `### Added`:

```markdown
- A whole-library walk — a full sync, or a source's first delta — walks a plan:
  what the account is watching, then each library's movies and series, then its
  episodes in chunks of `USHER_SYNC_UNIT_MAX_ITEMS` (default 100,000).
  `USHER_SYNC_WALKERS` units are fetched at once while one writer commits them;
  each unit's position is kept in `sync_run_units`, and the run's
  `heartbeat_at` shows the writer is alive.
```

`.claude/rules/emby-push-and-ingest.md` — the intro paragraph's last sentence (`Beside it … back out.`) is followed by a paragraph of its own:

```markdown
A whole-library walk's walkers only fetch; the reconcile task is the one writer,
because `AsyncSession` is not safe to share between tasks.
```

The file is past its 200-line target, so the same edit cuts the three lines it adds, all of them how a rule was found rather than the rule (`rules-file-maintenance.md`):
- in the write-back route bullet, `never has — and `FakeEmbyServer` implemented the adapter's own guess, so the` and the line after it, `whole contract suite passed against a write-back that had never worked.`, become the one line `never has.`;
- the `PushSupervisor` bullet loses its last line, `Seeing any of this needs a fake with an **unbounded** supply of connections.`;
- the dropped-socket bullet's `is**; no real `429` has ever been seen. Do not let the queue fill during that` and `walk.` become the one line `is**. Do not let the queue fill during that walk.`

- [ ] **Step 11: Run everything this task touched**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit tests/integration/test_services_reconcile.py tests/integration/test_pipeline_spans.py tests/integration/test_pipeline_deps.py tests/integration/test_ingest_end_to_end.py`
Expected: PASS. Every full walk in the unit suite — the CLI's, the job handler's, the lanes' — now walks a plan; a case that goes red here pinned the single walk, and is read before anything is changed.

Run, from `web/`: `npm run verify && npm run e2e && npm run e2e:visual`
Expected: PASS.

- [ ] **Step 12: Plant and verify**

1. `_walk_plan`'s `for stage in STAGE_ORDER:` loop replaced by `await self._walk_stage(source, progress, adapter, units)`. Expect `test_no_unit_of_a_stage_is_fetched_before_every_unit_ahead_of_it_has_committed` to fail on `first_episode > titles_done`.
2. `claimable = deque(units)`. Expect `test_within_a_stage_the_largest_unit_is_fetched_first` to fail, Shorts fetched first.
3. `for _ in range(1)` in `_walk_stage`. Expect the `(2, True)` parametrisation of `test_walkers_fetch_units_at_once_up_to_their_number` to fail on `False is True`.
4. `_write`'s batch threshold removed (every page committed). Expect `test_a_units_pages_are_committed_once_they_add_up_to_a_batch_and_when_it_ends` to fail on `[2, 4, 5] == [4, 5]`, and `test_a_failed_walk_still_reported_the_batches_it_did_finish` on `[2, 3] == [2]`.
5. The error branch commits the held pages first: `unit = await self._commit_unit(source, progress, unit, held[unit.unit_key], done=False)` before the `failed` save. Expect `test_a_failing_unit_fails_the_run_and_keeps_every_units_committed_position` to fail on `8 == 7`.
6. `_walk_stage`'s `finally` without `walker.cancel()`. Expect `test_a_failing_unit_cancels_the_walkers_still_fetching` to fail with `TimeoutError`.
7. `fetched = await queue.get()` in place of the `wait_for`. Expect `test_a_writer_waiting_on_its_walkers_still_heartbeats` to fail with `TimeoutError`.
8. `_commit_unit` passing `progress.run` without `.evolve(heartbeat_at=…)`. Expect `test_every_commit_of_a_planned_walk_moves_the_heartbeat` to fail on `later > earlier`.
9. `planned = plan and not max_items and kind is SyncRunKind.FULL`. Expect `test_a_delta_with_no_cursor_walks_the_plan_and_never_sweeps` to fail on `[] == ['library:Films']`.
10. `planned = plan and (kind is SyncRunKind.FULL or cursor is None)`. Expect `test_a_bounded_walk_keeps_the_single_walk_even_with_no_cursor` to fail on `heartbeat_at is None`.
11. `planned = not max_items and (kind is SyncRunKind.FULL or cursor is None)`. Expect `test_a_walk_told_not_to_plan_keeps_the_single_walk` to fail; and, separately, `plan=False` dropped from `lanes.py` to fail `test_the_gap_closer_walks_one_stream_even_unbounded_and_uncursored`.
12. `heartbeat_at=None` at the insert. Expect `test_the_run_and_its_first_heartbeat_are_committed_before_the_plan_is_made` to fail on `heartbeat is not None`.
13. `_fetch` catching `UsherPortError` only. Expect `test_a_bug_in_a_walker_is_raised_not_recorded` to fail with `TimeoutError`.
14. `_flush` without `save_unit`. Expect `test_a_whole_library_walk_stores_its_plan_and_completes_every_unit` to fail, both units `PENDING` at 0.
15. `_commit_unit` saving `status=SyncRunUnitStatus.COMPLETED` always. Expect the batch case to fail on `(4, 4, …)`: a completed unit takes no further write.
16. The `walkers < 1` check removed. Expect `test_a_service_with_no_walkers_is_refused` to fail with `DID NOT RAISE`.
17. Each wiring dropped in turn. `build_pipeline`'s `walkers=`: expect `test_the_walk_settings_reach_the_reconciler_and_the_adapter` to fail on `4 == 3`. `get_reconcile_service`'s `walkers=`: the deps probe, on `'walkers': 4`. The factory's `unit_max_items=`: both `test_the_deployment_tuning_reaches_the_adapter` and the composition case on `100000 == 5000`, because the composition case builds its adapter through the factory. `adapter_factory`'s `unit_max_items=`: the composition case alone, on `100000 == 5000`.
18. `sync_unit_max_items` with `ge=999`, then separately `le=1_000_001`. Expect the `999`, then the `1000001`, parametrisation to fail with `DID NOT RAISE`.
19. In `_write`, `due = time.monotonic() + self._heartbeat_seconds` also directly after `unit = current[fetched.unit_key]`, so every page puts the beat off. Expect `test_pages_that_keep_arriving_below_a_batch_still_let_the_heartbeat_move` to fail on `no beat while pages kept arriving`.
20. `_flush`'s `if batch:` removed, the publish unconditional. Expect `test_a_unit_that_ends_on_a_batch_boundary_publishes_no_second_frame` to fail on `[2, 4, 4] == [2, 4]`.

- [ ] **Step 13: Commit**

```bash
git add src/usher/services/reconcile.py src/usher/config.py src/usher/adapters/factory.py \
  src/usher/composition.py src/usher/api/deps.py src/usher/api/lanes.py \
  tests/fakes/source_adapter.py .env.example web/src/features/operator/Config.settings.ts \
  docs/guide/configuration.md docs/prd/02-data-model.md docs/prd/03-sources-and-sync.md \
  docs/prd/08-operations.md CHANGELOG.md .claude/rules/emby-push-and-ingest.md \
  tests/unit/test_services_reconcile.py tests/unit/test_api_lanes.py tests/unit/test_config.py \
  tests/unit/test_services_handlers.py tests/unit/test_adapters_factory.py tests/unit/test_composition.py \
  tests/integration/test_services_reconcile.py tests/integration/test_pipeline_spans.py \
  tests/integration/test_pipeline_deps.py tests/integration/test_ingest_end_to_end.py
git commit -m "sync: walk a whole library as its planned units, with walkers and one writer"
```

---

### Task 17: Resume in place, refuse a live walk, supersede the rest

Spec §2.5's table, the resume half of §2.6, and §2.7's `started_at`. A whole-library walk first reads `latest_incomplete_run` for its kind. A `running` row whose heartbeat is under 10 minutes old is a live walk, and the new one raises `WalkRefused`. A row with units is resumed in place: the same row, the same `started_at`, its completed units skipped and every other unit continued from its committed position. Every other unfinished row of a whole-library walk is superseded — closed `failed` with `superseded: a whole-library walk restarts` unless it is `failed` already — and a fresh run starts. A delta row with neither units nor a heartbeat is a single walk's, and is left alone.

`usher sync` prints the refusal and exits non-zero. The worker's `sync_handler` re-raises it as `PortUnavailable`, so `JobWorker` fails the job as retryable instead of logging a crash, and the queue retries it: a walk whose process died with the job's lease is resumed once its heartbeat goes stale, rather than waiting for someone to ask again. The gap-closer passes `plan=False` (Task 16), so its item walk never claims and is never refused.

**The watch lane gets the same heartbeat, and runs beside a live walk instead of over it.** `WatchStateSyncService.sync` sets `heartbeat_at`, the column Task 12 added, when it inserts or reclaims its run and with every batch it commits. The save that ends a run moves no heartbeat, as for a whole-library walk: only a `running` row is ever read as live. A `running` newest watch run whose heartbeat is under `STALE_AFTER` old is another process's walk, alive, and `sync` neither supersedes nor resumes it. It goes on as if no unfinished run existed, in a row of its own: a delta from the latest completed cursor, which Task 18 reads back to `since_at_most`, or a filtered first walk when there is no cursor. A `running` watch run with no heartbeat predates `m10g`, and is taken for dead as before. `watch_sync.py` imports `STALE_AFTER` from `reconcile.py`: the import contracts let one service import another, and nothing `reconcile.py` imports leads back to `watch_sync.py`.

**Two watch runs side by side converge, but for the first residual below.** Both write through `merge_from_source` (`db/repositories/watch_state.py`), which upserts on `(user_id, title_id)` or `(user_id, episode_id)`. Its conflict rule is the `UPDATE`'s `AND ws.updated_at <= d.observed_at`, `updated_at` being what the `BEFORE UPDATE` trigger stamps, the writing transaction's `now()`; its `INSERT` is `ON CONFLICT (user_id, {target}) DO NOTHING`. Two runs that read the same state write the same `position_seconds` and `played`, and `runtime_seconds`, `play_count` and `last_played_at` are COALESCEd, a walk's listing carrying no play history. So whichever write the rule lets land, the row holds the same values. Two residuals stay. A state that changes between the two reads can keep the earlier read until it next changes: the later run's merges carry its own attempt's start, a write the earlier run commits after that start outdates them, and the next delta reads from the later run's start, past the change. Two watch runs that overlap today race the same way. And a run whose heartbeat is still fresh when another starts beside it, and which never finishes, is never closed: no longer the newest, it is neither superseded nor resumed, and it stays `running` — in dashboard 3's panel 6 for good, since that panel counts runs by status with no time filter, and in `usher sync-status` until five newer runs of its source push it out.

`watch.sync` has three callers in the tree: `usher sync`, `sync_handler` and `LaneSupervisor._close_gap`; Task 18 adds a fourth, the after-seed hook, at the first two. None has anything new to handle from the watch lane: `sync` never refuses, so each gets a run, beside a live one if one is running.

Neither check is atomic. `_claim` reads the newest run and writes later, with no lock between, so two whole-library walks that start together can both proceed. The watch lane's check has the same gap, as today: two watch runs that start together can both close one dead first walk, or both resume one dead delta.

Departures 2, 5, 6, 12 and 13 happen here. **2:** a resumed unit's first page can re-read up to `PAGE_OVERLAP` items its last committed page already counted; the cases below run on the fake, which has no overlap, so they assert nothing counted twice. That overlap is all a resume reaches back, so a resume after more than `PAGE_OVERLAP` deletions ahead of a unit's position misses items, as an in-walk shift does, without the WARNING. A full walk's sweep then retracts the items it skipped, up to the sweep's ceiling, until a later full walk reads them again. **5:** a `failed` run with units resumes whatever its heartbeat says. **6:** a superseded row that is `failed` already keeps its own error. **12:** a run whose sweep was refused is not resumed. Its walk is what the refusal doubts — a library unmounted mid-walk reads exactly like one emptied — so the next attempt reads the library again; a resume would re-run the sweep over the same rows and refuse again. **13:** the watch lane's heartbeat, and its run beside a live one, above.

`tests/integration/test_ingest_end_to_end.py::test_a_walk_that_dies_mid_run_leaves_a_resumable_catalog` now resumes its failed run instead of starting another, and its assertions still hold: the failed attempt held its unit's pages below a batch of 1,000 and committed none, so the resume walks all six items once.

**Files:**
- Modify: `src/usher/services/reconcile.py` (`STALE_AFTER`, `WALK_SUPERSEDED_ERROR`, `WalkRefused`, `reconcile`, `_claim`, `_supersede`, `_walk_plan`)
- Modify: `src/usher/services/watch_sync.py` (`_now`, `__init__`, `sync`, `_flush`)
- Modify: `src/usher/ports/repository/sync.py` (two docstrings)
- Modify: `src/usher/services/handlers.py` (`sync_handler`), `src/usher/cli.py` (`_sync`, `_sync_failed`)
- Modify: `tests/fakes/source_adapter.py` (`unit_starts`)
- Modify: `docs/prd/02-data-model.md`, `docs/prd/03-sources-and-sync.md`, `CHANGELOG.md`, `docs/guide/command-line.md`, `.claude/rules/emby-push-and-ingest.md`
- Test: `tests/unit/test_services_reconcile.py`, `tests/unit/test_services_watch_sync.py`, `tests/unit/test_services_handlers.py`, `tests/unit/test_cli.py`, `tests/integration/test_services_reconcile.py`, `tests/integration/test_cli_pipeline.py`

**Interfaces:**
- Consumes: Task 16's `reconcile(…, plan=…)`, `_walk_plan`, `_walk_stage`, `_beat`, the `clock`; `_failed` and `RETRACTION_ERROR_CODE`; Task 12's `units_for`, `save` (it keeps the greater `position` and never rewrites a `completed` row) and `save_unit`; Task 11's `pages_of`, which reaches a unit's `start_index` by reading past the items before it; Task 16's test helpers `_Ticks`, `_Fixture`, `_shelve`; Task 12's `SyncRun.heartbeat_at`; `WatchStateSyncService.sync`'s resume and Task 7's supersede in it.
- Produces (tests): `FakeSourceAdapter.unit_starts: list[tuple[str, int]]`, every `(key, start_index)` `list_unit` was asked for. The journal cannot say where a unit resumed: `pages_of` reads past the items before `start_index`, and `_walk_library` journals each one it yields.
- Produces: `usher.services.reconcile.STALE_AFTER = timedelta(minutes=10)`, `WALK_SUPERSEDED_ERROR = "superseded: a whole-library walk restarts"` (not `SUPERSEDED_ERROR`, which `watch_sync` already names), `class WalkRefused(Exception)`; `_claim(source, kind) -> SyncRun | None`; `_walk_plan(…, *, resumed: bool)`; `WatchStateSyncService(…, clock: Callable[[], datetime] = _now)`. Task 18 adds `after_seed` beside `resumed`.
- Produces (tests): in `tests/unit/test_services_reconcile.py`, `_Fixture(…, clock=None)`, `NOW`, `_Clock(now)` with a settable `.now`, and `_given_walk(fixture, *, heartbeat_at, status=RUNNING, kind=FULL, units=(), error=None)`. Task 19 reuses `_Clock` and `NOW`. In `tests/unit/test_services_watch_sync.py`, its own `NOW` and `_Clock(now, *, step=timedelta(0))`, `_Fixture(…, clock=None)`, and `_given_watch_run(fixture, *, heartbeat_at, status=RUNNING, delta=True)`.

- [ ] **Step 1: Write the failing service tests**

`tests/fakes/source_adapter.py` — in `FakeSourceAdapter.__init__`, after `self.journal`:

```python
        #: `(key, start_index)` for every unit walk the port was asked for, in order.
        self.unit_starts: list[tuple[str, int]] = []
```

and `list_unit`'s body begins:

```python
        # Recorded here rather than in `_walk_library`, so that it is what the port
        # was asked for.
        self.unit_starts.append((key, start_index))
```

`tests/unit/test_services_reconcile.py` — imports: `Callable, Sequence` beside `AsyncGenerator, Iterator`; `SyncRun, SyncRunUnit` beside `SyncRunKind, SyncRunStatus`; `WalkRefused` beside `ReconcileService`. `_Fixture.__init__` takes `clock: Callable[[], datetime] | None = None` after `heartbeat_seconds`, and passes `clock=clock if clock is not None else _Ticks()` where it passed `clock=_Ticks()`.

Append to the file:

```python
# -- an unfinished whole-library walk: resumed, refused or superseded --------

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


class _Clock:
    """A clock that reads whatever the case last set."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _given_walk(
    fixture: _Fixture,
    *,
    heartbeat_at: datetime | None,
    status: SyncRunStatus = SyncRunStatus.RUNNING,
    kind: SyncRunKind = SyncRunKind.FULL,
    units: Sequence[tuple[str, SyncRunUnitStatus, int]] = (),
    error: str | None = None,
) -> SyncRun:
    """An unfinished whole-library walk, as a killed or a failed attempt left it.

    Each unit is `(library, status, position)`, planned in `TITLES`.
    """
    run = SyncRun(
        source_id=fixture.source.id,
        kind=kind,
        status=status,
        error=error,
        heartbeat_at=heartbeat_at,
        started_at=T0,
    )
    await fixture.runs.add(run)
    if units:
        await fixture.runs.add_units(
            [
                SyncRunUnit(
                    run_id=run.id,
                    unit_key=f"library:{library}",
                    stage=WalkStage.TITLES,
                    label=f"library {library}",
                    position=position,
                    items_seen=position,
                    status=unit_status,
                )
                for library, unit_status, position in units
            ]
        )
    return run


async def test_a_failed_whole_library_walk_resumes_in_place_from_each_units_position() -> None:
    """The same row and `started_at`, and nothing fetched or counted twice.

    The first attempt leaves Films complete at 5 and Shows failed at 2. The second
    fetches Shows' last two items only, and its sweep spares all nine rows: under a
    fresh `started_at` it would retract the seven an earlier instant stamped, more
    than the ceiling allows, and the run would fail.
    """
    fixture = _Fixture(batch_size=2, walkers=1)
    _shelve(fixture, "Films", range(10, 15))
    _shelve(fixture, "Shows", range(4))
    fixture.adapter.fail_unit_after("Shows", 3)
    first = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert first.status is SyncRunStatus.FAILED, "the premise: the first attempt failed"
    fixture.adapter.clear_failure()
    fixture.adapter.unit_starts.clear()

    second = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert (second.id, second.started_at) == (first.id, first.started_at)
    assert second.status is SyncRunStatus.COMPLETED
    assert (second.error, second.error_code) == (None, None)
    assert second.items_seen == 9
    assert second.items_retracted == 0
    assert fixture.adapter.unit_starts == [("library:Shows", 2)]
    units = await fixture.runs.units_for(first.id)
    assert [(unit.unit_key, unit.status, unit.position) for unit in units] == [
        ("library:Films", SyncRunUnitStatus.COMPLETED, 5),
        ("library:Shows", SyncRunUnitStatus.COMPLETED, 4),
    ]
    assert [run.id for run in await fixture.runs.list_for_source(fixture.source.id)] == [first.id]


@pytest.mark.parametrize(
    ("age", "refused"),
    [(timedelta(minutes=10) - timedelta(seconds=1), True), (timedelta(minutes=10), False)],
)
async def test_a_running_walk_is_refused_until_its_heartbeat_is_ten_minutes_old(
    age: timedelta, refused: bool
) -> None:
    """A killed walk's row still says `running`; only its heartbeat tells it from a live one."""
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(3))
    walking = await _given_walk(
        fixture, heartbeat_at=NOW - age, units=[("Films", SyncRunUnitStatus.RUNNING, 2)]
    )
    if refused:
        with pytest.raises(
            WalkRefused, match="a whole-library walk of Living Room Emby is already running"
        ):
            await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
        assert fixture.journal == [], "a refused walk asked the source for something"
        assert await fixture.runs.get(walking.id) == walking, "a refused walk wrote the live row"
        assert len(await fixture.runs.list_for_source(fixture.source.id)) == 1
        return
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert (run.id, run.status) == (walking.id, SyncRunStatus.COMPLETED)
    assert fixture.checkpoints[0] == (0, NOW), "the claim's commit kept the dead walk's heartbeat"
    assert fixture.adapter.unit_starts == [("library:Films", 2)]


async def test_a_failed_walk_resumes_however_fresh_its_heartbeat() -> None:
    """A `failed` row recorded its own end, so its heartbeat says nothing about a live walk."""
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(3))
    failed = await _given_walk(
        fixture,
        heartbeat_at=NOW,
        status=SyncRunStatus.FAILED,
        error="GET /Users/{user_id}/Items returned HTTP 502",
        units=[("Films", SyncRunUnitStatus.FAILED, 2)],
    )
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert (run.id, run.status, run.error) == (failed.id, SyncRunStatus.COMPLETED, None)


async def test_a_walk_killed_while_planning_is_superseded_once_its_heartbeat_is_stale() -> None:
    """Its run and heartbeat were committed and its units never were.

    Refused while the heartbeat is fresh, as a walk still planning must be; then closed
    and replaced, because a run with no plan has nothing to resume.
    """
    clock = _Clock(NOW)
    fixture = _Fixture(clock=clock)
    _shelve(fixture, "Films", range(2))
    planning = await _given_walk(fixture, heartbeat_at=NOW)
    clock.now = NOW + timedelta(minutes=10) - timedelta(seconds=1)
    with pytest.raises(WalkRefused):
        await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    clock.now = NOW + timedelta(minutes=10)

    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert run.id != planning.id
    assert run.status is SyncRunStatus.COMPLETED
    assert [unit.unit_key for unit in await fixture.runs.units_for(run.id)] == ["library:Films"]
    closed = await fixture.runs.get(planning.id)
    assert closed is not None
    assert (closed.status, closed.error) == (
        SyncRunStatus.FAILED,
        "superseded: a whole-library walk restarts",
    )


async def test_a_full_walk_left_running_from_before_units_is_superseded() -> None:
    """No units and no heartbeat: an older release's walk, which nothing can resume."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    legacy = await _given_walk(fixture, heartbeat_at=None)

    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert run.id != legacy.id
    assert run.status is SyncRunStatus.COMPLETED
    closed = await fixture.runs.get(legacy.id)
    assert closed is not None
    assert (closed.status, closed.error, closed.error_code) == (
        SyncRunStatus.FAILED,
        "superseded: a whole-library walk restarts",
        None,
    )
    assert closed.finished_at is not None


async def test_a_superseded_walk_that_had_already_failed_keeps_its_own_error() -> None:
    """Closed already, so it is not relabelled: its error says why that walk failed."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    failed = await _given_walk(
        fixture,
        heartbeat_at=None,
        status=SyncRunStatus.FAILED,
        error="GET /Users/{user_id}/Items returned HTTP 502",
    )
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert run.id != failed.id
    assert await fixture.runs.get(failed.id) == failed


async def test_an_unfinished_delta_with_no_plan_is_left_alone() -> None:
    """A single walk's row: a whole-library walk neither resumes nor closes it."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    delta = await _given_walk(fixture, heartbeat_at=None, kind=SyncRunKind.DELTA)
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert run.id != delta.id
    assert [unit.unit_key for unit in await fixture.runs.units_for(run.id)] == [
        "library:Films"
    ], "the premise: a cursorless delta walks the plan"
    assert await fixture.runs.get(delta.id) == delta


async def test_a_walk_whose_sweep_was_refused_walks_again_rather_than_resuming() -> None:
    """Four rows the walk never saw are two thirds of the source, so the sweep refuses.

    Every unit completed, so a resume would re-run the sweep on what that walk saw.
    """
    fixture = _Fixture()
    _shelve(fixture, "Films", range(2))
    await fixture.ingest.ingest_batch(
        fixture.source.id, [_item(f"m9{index}") for index in range(4)], observed_at=T0
    )
    refused = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert refused.error_code == RETRACTION_ERROR_CODE, "the premise: the sweep refused"
    fixture.journal.clear()

    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert run.id != refused.id
    fetched = [key for event, key in fixture.journal if event == "fetched"]
    assert fetched == ["library:Films"] * 2, "the library was not read again"
    assert await fixture.runs.get(refused.id) == refused


async def test_a_resumed_unit_that_yields_nothing_completes() -> None:
    """A unit whose committed position is already its end: one empty walk, then complete."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(3))
    failed = await _given_walk(
        fixture,
        heartbeat_at=None,
        status=SyncRunStatus.FAILED,
        units=[("Films", SyncRunUnitStatus.RUNNING, 3)],
    )
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert (run.id, run.status, run.items_seen) == (failed.id, SyncRunStatus.COMPLETED, 0)
    assert fixture.adapter.unit_starts == [("library:Films", 3)]
    [unit] = await fixture.runs.units_for(run.id)
    assert (unit.status, unit.position) == (SyncRunUnitStatus.COMPLETED, 3)


async def test_a_cursored_delta_walks_beside_a_live_whole_library_walk() -> None:
    """Only a whole-library walk claims; a delta with a cursor never waits on one."""
    fixture = _Fixture(clock=_Clock(NOW))
    _shelve(fixture, "Films", range(2))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    live = await _given_walk(
        fixture,
        heartbeat_at=NOW,
        kind=SyncRunKind.DELTA,
        units=[("Films", SyncRunUnitStatus.RUNNING, 1)],
    )
    delta = await fixture.service.reconcile(fixture.source, SyncRunKind.DELTA, fixture.adapter)
    assert delta.cursor_at is not None, "the premise: the full walk gave the delta its cursor"
    assert delta.status is SyncRunStatus.COMPLETED
    assert await fixture.runs.get(live.id) == live
```

`tests/integration/test_services_reconcile.py`, after Task 16's `test_a_whole_library_walk_persists_its_units_against_real_sql`:

```python
async def test_a_failed_whole_library_walk_resumes_in_place_against_real_sql(
    service: ReconcileService,
    runs: PostgresSyncRunRepository,
    source: Source,
    adapter: _Adapter,
) -> None:
    """Postgres takes the reclaimed row back to `running`, then on to `completed`."""
    for index in range(5):
        adapter.items[f"m{index}"] = _item(f"m{index}")
    adapter.fail_after = 3
    first = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    [unit] = await runs.units_for(first.id)
    assert (first.status, unit.status, unit.position) == (
        SyncRunStatus.FAILED,
        SyncRunUnitStatus.FAILED,
        2,
    ), "the premise: one page committed, then the walk failed"
    adapter.fail_after = None

    second = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]

    assert second.id == first.id
    stored = await runs.get(first.id)
    assert stored is not None
    assert (stored.started_at, stored.status, stored.items_seen) == (
        first.started_at,
        SyncRunStatus.COMPLETED,
        5,
    )
    assert (stored.error, stored.error_code) == (None, None)
    [unit] = await runs.units_for(first.id)
    assert (unit.status, unit.position, unit.items_seen) == (SyncRunUnitStatus.COMPLETED, 5, 5)
```

`tests/unit/test_services_watch_sync.py` — imports: `from datetime import UTC, datetime, timedelta`. After `LAST_PLAYED`:

```python
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


class _Clock:
    """A clock that reads whatever the case last set, moved on `step` at every read."""

    def __init__(self, now: datetime, *, step: timedelta = timedelta(0)) -> None:
        self.now = now
        self.step = step

    def __call__(self) -> datetime:
        read = self.now
        self.now += self.step
        return read
```

`_Fixture.__init__` takes `clock: _Clock | None = None` after `lossy`, sets `self.clock = clock if clock is not None else _Clock(NOW)` before it builds the service, and passes `clock=self.clock` after `batch_size=batch_size`. A fixed clock changes nothing the existing cases see: none leaves a run `running` and then syncs again. Append to the file:

```python
# -- a live watch walk is left alone, and a new one runs beside it ----------


async def _given_watch_run(
    fixture: _Fixture,
    *,
    heartbeat_at: datetime | None,
    status: SyncRunStatus = SyncRunStatus.RUNNING,
    delta: bool = True,
) -> SyncRun:
    """An unfinished watch run two states in, as another process left it, live or dead.

    A delta's cursor is a walk this stores first, completed at `T0`; a first walk has
    none. The run starts an hour after `T0`, so it is the newest.
    """
    if delta:
        await fixture.given_completed_walk(at=T0)
    run = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=status,
        cursor_at=T0 if delta else None,
        position=2,
        items_seen=2,
        heartbeat_at=heartbeat_at,
        started_at=T0 + timedelta(hours=1),
    )
    await fixture.runs.add(run)
    return run


async def test_a_live_first_watch_walk_is_left_alone_and_a_new_one_completes_beside_it(
    fixture: _Fixture,
) -> None:
    """Superseding it would close the row under a live walk, so this walk takes its own."""
    await fixture.given_matched("movie-0")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-0", position_seconds=0, played=True)
    )
    live = await _given_watch_run(fixture, heartbeat_at=NOW, delta=False)

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert await fixture.runs.get(live.id) == live, "the live first walk's row was written"
    assert run.id != live.id
    assert (run.status, run.cursor_at, run.items_matched) == (SyncRunStatus.COMPLETED, None, 1)
    assert fixture.adapter.resumed_from == [0]
    assert len(await fixture.runs.list_for_source(fixture.source.id)) == 2


async def test_a_live_watch_delta_is_not_resumed_and_a_new_delta_runs_beside_it(
    fixture: _Fixture,
) -> None:
    """A heartbeat a second short of ten minutes old is alive: its row is not resumed."""
    for index in range(3):
        await fixture.given_matched(f"movie-{index}")
    live = await _given_watch_run(
        fixture, heartbeat_at=NOW - timedelta(minutes=10) + timedelta(seconds=1)
    )

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert await fixture.runs.get(live.id) == live, "the live delta's row was written"
    assert run.id != live.id, "the live delta was resumed"
    assert (run.status, run.cursor_at, run.items_matched) == (SyncRunStatus.COMPLETED, T0, 3)
    assert fixture.adapter.resumed_from == [0]


@pytest.mark.parametrize("delta", [False, True], ids=["first-walk", "delta"])
@pytest.mark.parametrize(
    "heartbeat_at", [NOW - timedelta(minutes=10), None], ids=["ten-minutes-old", "no-heartbeat"]
)
async def test_a_dead_watch_walk_is_superseded_or_resumed_as_before(
    fixture: _Fixture, heartbeat_at: datetime | None, delta: bool
) -> None:
    """A heartbeat ten minutes old is a walk that died, and so is none, from before heartbeats."""
    dead = await _given_watch_run(fixture, heartbeat_at=heartbeat_at, delta=delta)

    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert run.status is SyncRunStatus.COMPLETED
    if delta:
        assert run.id == dead.id, "a dead delta was not resumed"
        assert fixture.adapter.resumed_from == [2]
        assert fixture.saved[0].heartbeat_at == NOW, "the reclaim kept the dead walk's heartbeat"
        return
    assert run.id != dead.id, "a dead first walk was resumed"
    assert fixture.adapter.resumed_from == [0]
    closed = await fixture.runs.get(dead.id)
    assert closed is not None
    assert (closed.status, closed.error) == (
        SyncRunStatus.FAILED,
        "superseded: a first watch walk restarts",
    )


async def test_a_failed_watch_walk_resumes_however_fresh_its_heartbeat(fixture: _Fixture) -> None:
    """A `failed` row recorded its own end, so its heartbeat says nothing about a live walk."""
    failed = await _given_watch_run(fixture, heartbeat_at=NOW, status=SyncRunStatus.FAILED)
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert (run.id, run.status) == (failed.id, SyncRunStatus.COMPLETED)


async def test_a_watch_walk_that_fails_before_its_first_batch_still_leaves_its_heartbeat(
    fixture: _Fixture,
) -> None:
    """The insert carries the first beat, so a walk that dies at once is still dated."""
    await fixture.given_matched("movie-0")
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-0", position_seconds=0, played=True)
    )
    fixture.adapter.fail_after(0)
    run = await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)
    assert (run.status, run.items_seen) == (SyncRunStatus.FAILED, 0), "the premise: no batch"
    assert run.heartbeat_at == NOW


async def test_every_batch_of_a_watch_walk_moves_its_heartbeat() -> None:
    """A clock a second on at every read: the insert reads `NOW`, and each batch the next."""
    fixture = _Fixture(batch_size=2, clock=_Clock(NOW, step=timedelta(seconds=1)))
    await fixture.given_completed_walk()
    for index in range(5):
        await fixture.given_matched(f"movie-{index}")

    await fixture.service.sync(fixture.source, fixture.adapter, user_id=fixture.user_id)

    assert fixture.positions == [2, 4, 5], "the premise: three batches committed"
    beats = [saved.heartbeat_at for saved in fixture.saved if saved.status is SyncRunStatus.RUNNING]
    assert beats == [NOW + timedelta(seconds=seconds) for seconds in (1, 2, 3)]
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_reconcile.py`, then `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_watch_sync.py`, then `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/integration/test_services_reconcile.py::test_a_failed_whole_library_walk_resumes_in_place_against_real_sql`
Expected: FAIL. The first command stops at collection, `ImportError: cannot import name 'WalkRefused'`, and runs nothing, which is why the two unit modules run apart. The second errors in every case that builds a `_Fixture`, all but three, with `TypeError: WatchStateSyncService.__init__() got an unexpected keyword argument 'clock'`. The third fails on `assert second.id == first.id`, the second walk having started a run of its own.

- [ ] **Step 3: Claim, resume and supersede, and leave a live watch walk alone**

`src/usher/services/reconcile.py` — `from datetime import UTC, datetime, timedelta`. After `RETRACTION_ERROR_CODE`:

```python
#: How long a walk's heartbeat may stand still before the walk is taken for dead.
STALE_AFTER = timedelta(minutes=10)

#: What a superseded whole-library walk's row says.
WALK_SUPERSEDED_ERROR = "superseded: a whole-library walk restarts"


class WalkRefused(Exception):
    """A whole-library walk of this source and kind is already running."""
```

In `reconcile`, the docstring gains a paragraph after `walk is one stream.`, before its closing `Never raises a `UsherPortError`.`:

```python
        A whole-library walk resumes its kind's unfinished run in place and raises
        `WalkRefused` while that run is alive; see `_claim`.
```

and the block Task 16 wrote from `planned = …` through `await self._commit()` becomes:

```python
            planned = plan and not max_items and (kind is SyncRunKind.FULL or cursor is None)
            claimed = await self._claim(source, kind) if planned else None
            if claimed is None:
                run = SyncRun(
                    source_id=source.id,
                    kind=kind,
                    cursor_at=cursor,
                    # A planned walk's first heartbeat rides the insert, so one that dies
                    # while planning still leaves a row saying when it was last alive.
                    heartbeat_at=self._clock() if planned else None,
                )
                # Inserted and committed before the walk begins, `RUNNING`: an
                # operator watching a six-hour sync needs a row to watch, and a
                # process killed mid-walk must leave a trace rather than nothing.
                await self._runs.add(run)
            else:
                # The same row and the same `started_at`: every item either attempt saw
                # carries the instant the sweep compares against.
                run = claimed.evolve(
                    status=SyncRunStatus.RUNNING,
                    error=None,
                    error_code=None,
                    finished_at=None,
                    heartbeat_at=self._clock(),
                )
                await self._runs.save(run)
            await self._commit()
```

and `await self._walk_plan(source, progress, adapter)` becomes `await self._walk_plan(source, progress, adapter, resumed=claimed is not None)`.

After `cursor_for`:

```python
    async def _claim(self, source: Source, kind: SyncRunKind) -> SyncRun | None:
        """The unfinished whole-library walk this one resumes, or `None` for a fresh one.

        A `running` row whose heartbeat is under `STALE_AFTER` old is a live walk, and
        raises `WalkRefused`. A row with units resumes, unless its sweep was refused:
        its walk is what that refusal doubts, so the library is read again. Every
        other unfinished row of a whole-library walk -- one that died before its plan
        was stored, or a full walk from before units existed -- is superseded. A delta
        with neither units nor a heartbeat is a single walk's, and is left alone.

        Not atomic: the check is a read and the claim a later write, with nothing
        locking between them, so two walks that start together can both proceed.
        """
        newest = await self._runs.latest_incomplete_run(source.id, kind)
        if newest is None:
            return None
        if (
            newest.status is SyncRunStatus.RUNNING
            and newest.heartbeat_at is not None
            and self._clock() - newest.heartbeat_at < STALE_AFTER
        ):
            raise WalkRefused(f"a whole-library walk of {source.name} is already running")
        has_units = bool(await self._runs.units_for(newest.id))
        if has_units and newest.error_code != RETRACTION_ERROR_CODE:
            return newest
        if has_units or newest.heartbeat_at is not None or kind is SyncRunKind.FULL:
            await self._supersede(newest)
        return None

    async def _supersede(self, run: SyncRun) -> None:
        """Close an unfinished walk the next one will not resume, in the fresh run's commit.

        One already `failed` is closed already, and keeps its own error.
        """
        if run.status is SyncRunStatus.RUNNING:
            await self._runs.save(self._failed(run, WALK_SUPERSEDED_ERROR, code=None))
```

`_walk_plan` becomes:

```python
    async def _walk_plan(
        self, source: Source, progress: _Progress, adapter: SourceAdapter, *, resumed: bool
    ) -> None:
        """Walk the adapter's plan, or a resumed run's stored units, one stage at a time.

        A fresh plan's units are stored with the heartbeat that follows the plan. A
        stage starts only once every unit of the stages before it has committed
        complete, so every episode finds its series. A unit already complete is skipped.
        """
        if resumed:
            units = await self._runs.units_for(progress.run.id)
        else:
            plan = await adapter.plan_walk()
            units = [
                SyncRunUnit(
                    run_id=progress.run.id,
                    unit_key=unit.key,
                    stage=unit.stage,
                    label=unit.label,
                    expected_items=unit.expected_items,
                )
                for unit in plan.units
            ]
            await self._runs.add_units(units)
            await self._beat(progress)
        for stage in STAGE_ORDER:
            staged = [
                unit
                for unit in units
                if unit.stage is stage and unit.status is not SyncRunUnitStatus.COMPLETED
            ]
            if staged:
                await self._walk_stage(source, progress, adapter, staged)
```

`src/usher/ports/repository/sync.py` — in `SyncRunRepository`'s docstring, `grain, and only for `WATCH_STATE`: it hands a walk back its own unfinished row, which the next attempt continues in place when the walk is a delta.` becomes `grain: it hands the watch lane, or a whole-library walk, back its own unfinished row, which the next attempt continues in place -- the watch lane's only when its walk is a delta.` (rewrapped). In `latest_incomplete_run`'s docstring, the last paragraph becomes:

```python
        The watch lane's, and a whole-library walk's: a cursored delta restarts
        from its cursor, but a walk of the whole library has to cost a page
        rather than the run when it fails.
```

`src/usher/services/watch_sync.py` — the watch lane's heartbeat, and a run beside a live one. `from usher.services.reconcile import STALE_AFTER`. After `SUPERSEDED_ERROR`:

```python
def _now() -> datetime:
    return datetime.now(UTC)
```

`__init__` takes `clock: Callable[[], datetime] = _now` after `batch_size`, and its body ends `self._clock = clock`. `sync`'s docstring becomes:

```python
        """Walk this source's watch state into the catalog.

        A run another process is still walking is left to it, and this walk runs
        beside it in a row of its own. Never raises a `UsherPortError`.
        """
```

After `incomplete = await self._runs.latest_incomplete_run(source.id, SyncRunKind.WATCH_STATE)`, before the supersede:

```python
            if (
                incomplete is not None
                and incomplete.status is SyncRunStatus.RUNNING
                and incomplete.heartbeat_at is not None
                and self._clock() - incomplete.heartbeat_at < STALE_AFTER
            ):
                # Another process's walk, alive. Closing or resuming its row would write
                # under that walk, so this one runs in a row of its own, both merging on
                # one key under one conflict rule. A row with no heartbeat predates
                # heartbeats, and is taken for dead.
                incomplete = None
```

The fresh run's `SyncRun(…)` gains `heartbeat_at=self._clock(),` after `started_at=attempt_started,`. The reclaim, `run = incomplete.evolve(status=SyncRunStatus.RUNNING, error=None, finished_at=None)`, becomes:

```python
                run = incomplete.evolve(
                    status=SyncRunStatus.RUNNING,
                    error=None,
                    finished_at=None,
                    heartbeat_at=self._clock(),
                )
```

`_flush`'s `run.evolve(…)` gains `heartbeat_at=self._clock(),` after `position=position,`. The save that ends the run moves no heartbeat: a `completed` or `failed` row is never live.

- [ ] **Step 4: Run them to see them pass**

Run the Step 2 commands, then `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_reconcile.py tests/unit/test_services_watch_sync.py tests/integration/test_services_reconcile.py tests/integration/test_services_watch_sync.py tests/integration/test_ingest_end_to_end.py tests/contract`
Expected: PASS, including every Task 16 case — each of those starts from an empty history, so each claim finds nothing — and every earlier watch case, none of which leaves a live row behind it.

- [ ] **Step 5: Write the failing caller tests**

`tests/unit/test_services_handlers.py` — `from usher.services.reconcile import ReconcileService, WalkRefused`. After `test_the_sync_handler_closes_the_adapter_even_when_reconcile_raises`:

```python
async def test_a_refused_walk_fails_the_job_so_the_queue_retries_it(
    source: Source, adapter: FakeSourceAdapter
) -> None:
    """`PortUnavailable`, which `JobWorker` fails as retryable rather than logging as a crash.

    A walk whose process died is then resumed once its heartbeat is stale.
    """
    sources = FakeSourceRepository()
    await sources.add(source)
    events: list[str] = []
    refusal = WalkRefused(f"a whole-library walk of {source.name} is already running")
    reconcile = _RecordingReconcile(events, raises=refusal)
    watch = _RecordingWatch(events)

    with pytest.raises(PortUnavailable, match="is already running") as caught:
        await sync_handler(sources, reconcile, watch, _Opener(adapter), user_id=_USER)(
            Job(kind=JobKind.SYNC, key=f"{source.id}:full")
        )

    assert caught.value.__cause__ is refusal
    assert events == ["reconcile"], "the watch lane ran after the walk was refused"
    with pytest.raises(PortUnavailable):
        await adapter.get_item("anything")
```

`tests/unit/test_cli.py` — `_sync_failed` joins the `usher.cli` import. Append:

```python
def test_the_exit_line_names_each_source_whose_walk_was_refused() -> None:
    """No run failed, and the command still must not claim success."""
    assert _sync_failed([], ["cli-a", "cli-b"]) == (
        "refused for cli-a, cli-b: a whole-library walk of each is already running"
    )
```

`tests/integration/test_cli_pipeline.py` — imports: `from tests.fakes.source_adapter import FakeSourceAdapter`; `_sync` joins the `usher.cli` import; `from usher.db.repositories.sync import PostgresSyncRunRepository`; `from usher.domain.sync import SyncRun, SyncRunKind, SyncRunUnit, WalkStage`; `from usher.ports.source import DEFAULT_UNIT_KEY`. After `test_allow_full_retraction_is_the_only_way_past_the_ceiling`:

```python
async def test_usher_sync_exits_non_zero_when_a_live_walk_refuses_it(
    cli_settings: Settings,
    clean_slate: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Another process's walk, its heartbeat this instant: no walk here, and no watch lane."""
    source = Source(
        kind=SourceKind.EMBY,
        name="cli-walking",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    live = SyncRun(source_id=source.id, kind=SyncRunKind.FULL, heartbeat_at=datetime.now(UTC))
    async with _session_for(cli_settings) as session:
        await PostgresSourceRepository(session).add(source)
        runs = PostgresSyncRunRepository(session)
        await runs.add(live)
        await runs.add_units(
            [
                SyncRunUnit(
                    run_id=live.id,
                    unit_key=DEFAULT_UNIT_KEY,
                    stage=WalkStage.TITLES,
                    label="the whole library",
                )
            ]
        )
        await session.commit()
    adapter = FakeSourceAdapter(source)

    async def _opened(pipeline: object, chosen: Source) -> FakeSourceAdapter:
        return adapter

    monkeypatch.setattr("usher.cli._open_adapter", _opened)

    with pytest.raises(SystemExit) as exited:
        await _sync(
            cli_settings, source_name="cli-walking", kind="full", allow_full_retraction=False
        )

    out = capsys.readouterr().out
    assert "cli-walking: refused: a whole-library walk of cli-walking is already running" in out
    assert "watch_state" not in out, "the watch lane ran after the walk was refused"
    assert exited.value.code == (
        "refused for cli-walking: a whole-library walk of each is already running"
    )
```

- [ ] **Step 6: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_handlers.py tests/unit/test_cli.py tests/integration/test_cli_pipeline.py -k "refused or refuses"`
Expected: FAIL — the handler case with `WalkRefused` raised where `PortUnavailable` was expected; the exit-line case with `TypeError: _sync_failed() takes 1 positional argument but 2 were given`; the CLI case with `WalkRefused` propagating out of `_sync`. The other cases the filter selects are older refusals, and pass.

- [ ] **Step 7: Handle the refusal at both callers**

`src/usher/services/handlers.py` — `from usher.ports.errors import PortDataMalformed, PortUnavailable`; `from usher.services.reconcile import ReconcileService, WalkRefused`. `sync_handler`'s docstring gains, after its first paragraph:

```python
    A walk refused because another is alive fails the job, and the queue retries it.
```

and the walk becomes:

```python
        try:
            await reconcile.reconcile(source, lane, adapter)
        except WalkRefused as exc:
            # A port failure, so `JobWorker` fails the job for a retry instead of logging
            # a crash: a walk whose process died is resumed once its heartbeat is stale.
            raise PortUnavailable(str(exc)) from exc
        else:
            await watch.sync(source, adapter, user_id=user_id)
        finally:
            await adapter.aclose()
```

The watch lane moves to the `else:` arm, where it still runs only after a walk that raised nothing: the `except` is the item walk's alone, since `watch.sync` never refuses.

`src/usher/cli.py` — `from usher.services.reconcile import RETRACTION_ERROR_CODE, WalkRefused`. In `_sync`, `failed: list[SyncRun] = []` gains `refused: list[str] = []` below it; the `try:` around the two lanes gains, before its `finally:`:

```python
            except WalkRefused as exc:
                # Another process is walking this source, and runs its watch lane after.
                print(f"{source.name}: refused: {exc}")
                refused.append(source.name)
```

and the last two lines become:

```python
        if failed or refused:
            raise SystemExit(_sync_failed(failed, refused))
```

`_sync_failed` becomes:

```python
def _sync_failed(runs: Sequence[SyncRun], refused: Sequence[str] = ()) -> str:
    """The exit line for a sync in which a run recorded `FAILED` or a walk was refused.

    The per-run detail is already on stdout above -- including each `error`,
    which for a refusal is the two numbers and the ceiling. This says *which*
    lanes failed and stops the command claiming success, rather than repeating
    what was printed a line earlier. A refused walk is another process's, still alive.

    **`--allow-full-retraction` is named only when a refusal is among them**,
    and that is the whole reason `RETRACTION_ERROR_CODE` exists. It is the one
    failure here an operator has a command for; a read timeout is not, and an
    escape hatch offered for every failure is one people learn to paste
    without reading. `error_code` is what is matched rather than the refusal's
    English, because that sentence is built from three numbers in
    `ports/ingest.py` and is a standing candidate for rewording.
    """
    lines: list[str] = []
    if runs:
        lanes = ", ".join(f"{one.kind.value}" for one in runs)
        lines.append(
            f"{len(runs)} sync run(s) failed: {lanes}; see the lines above and `usher sync-status`"
        )
    if refused:
        lines.append(
            f"refused for {', '.join(refused)}: a whole-library walk of each is already running"
        )
    if any(one.error_code == RETRACTION_ERROR_CODE for one in runs):
        lines.append(
            "the availability sweep refused: if the removal was intended, "
            "re-run with `usher sync --allow-full-retraction`"
        )
    return "\n".join(lines)
```

Run the Step 6 command. Expected: PASS.

- [ ] **Step 8: Say it in the PRD, the changelog, the guide and the rules**

`docs/prd/03-sources-and-sync.md`, "Reconciliation is not optional", after the paragraph Task 16 added:

```markdown
**An unfinished whole-library walk is resumed in place.** The next
whole-library walk of the same kind continues the same `sync_runs` row, with
the same `started_at`: completed units are skipped, and every other unit
continues from the position it committed. A run still `running` whose
heartbeat is under 10 minutes old is a live walk, and a second is refused —
`usher sync` exits non-zero, and a worker job fails and is retried. A run whose
sweep was refused is not resumed, and neither is one that stopped before its
units were stored or a full run from before units existed: a fresh walk
starts, and such a run left `running` is closed `failed` with `superseded: a
whole-library walk restarts`.
```

In "Walking the library", the bullet beginning `**Items are walked in ascending creation order**` says how far a resume reaches back. Its last two lines, `` reconcile covers what it missed. Duplicates are permitted; silent truncation `` and `` is not. ``, become:

```markdown
  reconcile covers what it missed. A resumed unit of a whole-library walk
  re-reads only that overlap before where it stopped, and logs nothing: if more
  items vanish ahead of it between attempts, it misses them, a full walk's sweep
  retracts them, and the next full walk reads them again. Duplicates are
  permitted; silent truncation is not, except across such a resume.
```

The same file's paragraph beginning `**A watch-lane delta is resumable.**` changes twice. Its sentence closing an unfinished first walk now names only a dead one: the lines from `` first walk is never resumed: an unfinished one still `running` is closed `` through `` again. Nothing schedules it. `` become

```markdown
first walk is never resumed: an unfinished one still `running` and not alive
(below) is closed `failed` with `superseded: a first watch walk restarts`, and
the next run starts again. Nothing schedules it.
```

and after that last sentence, `Nothing schedules it.`, it gains:

```markdown
A watch run moves its heartbeat when it starts and with every batch it
commits. One still `running` whose heartbeat is under 10 minutes old is alive,
and a second watch run neither closes nor resumes it: it walks beside it in a
run of its own, a delta from the cursor or, with no cursor yet, a first walk.
```

`docs/prd/02-data-model.md`, "Supporting tables", the `sync_runs` row: Task 16's `` `heartbeat_at`, which a whole-library walk's writer moves on every commit ([03](03-sources-and-sync.md)) `` becomes `` `heartbeat_at`, which a whole-library walk's writer moves on every commit and the watch lane when it starts a run and with every batch ([03](03-sources-and-sync.md)) ``.

`CHANGELOG.md`, under `## [Unreleased]`'s `### Added`:

```markdown
- A whole-library walk that fails or is killed resumes where it stopped: the
  same run, each unit from the position it committed. While one is alive — its
  heartbeat under 10 minutes old — a second is refused, and `usher sync` exits
  non-zero. A watch-state walk started while another is alive runs beside it
  instead of closing or resuming the other's run.
```

`docs/guide/command-line.md`, "Syncing a media server". The bullet beginning `` **`usher sync` exits non-zero if any walk failed** `` becomes these two bullets:

```markdown
- **`usher sync` exits non-zero if any walk failed or was refused**, after it
  has tried every source, so cron can notice. A walk of a whole library is
  refused while another of the same kind is still running for that source.
- **A walk of a whole library that stops part-way resumes.** The next walk of
  the same kind continues where each piece stopped, rather than starting again.
```

`.claude/rules/emby-push-and-ingest.md`, "Rules the pipeline enforces", after the `observed_at` bullet:

```markdown
- **A resumed whole-library walk keeps its run's `started_at`**: every item an
  earlier attempt saw carries that instant, and a fresh one retracts them all.
```

The file is past its 200-line target, so the same edit cuts the two lines it adds. Each restates what a source file keeps beside the code, and the Tier 4 one adds a measured figure; `rules-file-maintenance.md` prefers the source to a copy, and deletes a measurement on sight:
- in the history-backfill bullet, `` accepts what Postgres refuses and `now()` is frozen per transaction, so the `` and the line after it, `` integration suite stages it with `clock_timestamp()` in a raw `INSERT`. ``, become the one line `` accepts what Postgres refuses and `now()` is frozen per transaction. `` How the suite stages that row is `_given_stored_history`'s, in `tests/integration/test_services_watch_sync.py`, whose docstring gives the raw `INSERT` and why it reads `clock_timestamp()`;
- in the Tier 4 bullet, `` `tmdb_id`. A probe with **no** year resolves nothing at all: the year `` and the line after it, `` `BETWEEN` propagates `NULL`, so "0.0%" there is not a bug. ``, become the one line `` `tmdb_id`. `` The comment above the `BETWEEN` in `db/repositories/matching.py` says a probe with no year resolves to nothing, `NULL` propagating through it; the "0.0%" is the measured figure.

In "Gap-closing walks are unasked-for work", the paragraph's last sentence, from `` An unfinished first walk is superseded, never resumed (`SUPERSEDED_ERROR`): `` through `` so its row cannot be reset. ``, becomes the lines below. They name `watch_sync.SUPERSEDED_ERROR`, `WALK_SUPERSEDED_ERROR` being the other one, and add the live run this task leaves alone, without which the sentence is no longer true; `rules-file-maintenance.md` allows going a line or two over the target to correct a claim.

```markdown
An unfinished first walk is superseded, never resumed
(`watch_sync.SUPERSEDED_ERROR`): `save` only raises `position`, so its row
cannot be reset. A watch run whose heartbeat is under `STALE_AFTER` old is
alive, first walk or delta, and is left alone: the next run walks beside it.
```

- [ ] **Step 9: Run everything this task touched**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit tests/contract tests/integration/test_services_reconcile.py tests/integration/test_services_watch_sync.py tests/integration/test_ingest_end_to_end.py tests/integration/test_push_lane_end_to_end.py tests/integration/test_cli_pipeline.py`
Expected: PASS.

- [ ] **Step 10: Plant and verify**

1. `_claim`'s live check with `<=` for `<`. Expect the `10:00` parametrisation of `test_a_running_walk_is_refused_until_its_heartbeat_is_ten_minutes_old` to fail with `WalkRefused`.
2. The live check without `newest.status is SyncRunStatus.RUNNING`. Expect `test_a_failed_walk_resumes_however_fresh_its_heartbeat` to fail with `WalkRefused`.
3. The live check's last two conjuncts spelled `self._clock() - (newest.heartbeat_at or self._clock()) < STALE_AFTER`, a row with no heartbeat counting as live. Expect `test_a_full_walk_left_running_from_before_units_is_superseded` to fail with `WalkRefused`.
4. The resumed `evolve` also setting `started_at=self._clock()`. Expect the resume case to fail on its `(id, started_at)` pair; with that line deleted as well, on `FAILED is COMPLETED`, the sweep refusing seven of nine rows.
5. The resumed `evolve` without `error=None, error_code=None`. Expect the resume case to fail on `(second.error, second.error_code)`.
6. The resumed `evolve` without `heartbeat_at=…`. Expect the `10:00` parametrisation to fail on `fixture.checkpoints[0] == (0, NOW)`.
7. `_walk_plan` without the `is not SyncRunUnitStatus.COMPLETED` filter. Expect the resume case to fail on `unit_starts`, Films walked again.
8. `_claim` without `and newest.error_code != RETRACTION_ERROR_CODE`. Expect `test_a_walk_whose_sweep_was_refused_walks_again_rather_than_resuming` to fail on `run.id != refused.id`.
9. `_claim`'s resume condition widened to `(has_units or newest.heartbeat_at is not None) and newest.error_code != RETRACTION_ERROR_CODE`. Expect `test_a_walk_killed_while_planning_is_superseded_once_its_heartbeat_is_stale` to fail on `run.id != planning.id`.
10. The supersede condition without `or kind is SyncRunKind.FULL`. Expect the legacy-full case to fail, its row still `running`.
11. The supersede condition with `or True` for `or kind is SyncRunKind.FULL`. Expect `test_an_unfinished_delta_with_no_plan_is_left_alone` to fail.
12. `_supersede` without its `RUNNING` condition. Expect `test_a_superseded_walk_that_had_already_failed_keeps_its_own_error` to fail.
13. `claimed = await self._claim(source, kind)` for every walk, dropping `if planned else None`. Expect `test_a_cursored_delta_walks_beside_a_live_whole_library_walk` to fail with `WalkRefused`.
14. `sync_handler` without its `except WalkRefused`. Expect the handler case to fail with `WalkRefused` where `PortUnavailable` was expected.
15. `_sync` without its `except WalkRefused`. Expect the CLI case to fail with `WalkRefused` out of `_sync`.
16. `_sync_failed` without the refused line. Expect the exit-line case to fail.
17. The watch lane's live check with `<=` for `<`. Expect both `ten-minutes-old` parametrisations of `test_a_dead_watch_walk_is_superseded_or_resumed_as_before` to fail: the first walk's row is still `running`, and the delta is not resumed.
18. The live check without `incomplete.status is SyncRunStatus.RUNNING`. Expect `test_a_failed_watch_walk_resumes_however_fresh_its_heartbeat` to fail on `run.id`, a fresh run having started beside the failed one.
19. Its last two conjuncts spelled `self._clock() - (incomplete.heartbeat_at or self._clock()) < STALE_AFTER`, a row with no heartbeat counting as live. Expect both `no-heartbeat` parametrisations of `test_a_dead_watch_walk_is_superseded_or_resumed_as_before` to fail, and with them the older cases that start from a `running` row, `test_a_running_run_left_by_a_killed_process_is_reclaimed_not_orphaned` and `test_an_unfinished_first_walk_is_superseded_rather_than_resumed` among them.
20. The live check's body, `incomplete = None`, replaced with `pass`, so a live run falls through to the supersede or the resume. Expect `test_a_live_first_watch_walk_is_left_alone_and_a_new_one_completes_beside_it` to fail on its live row, closed `failed`, and `test_a_live_watch_delta_is_not_resumed_and_a_new_delta_runs_beside_it` on its live row, resumed.
21. The live check moved below the supersede. Expect `test_a_live_first_watch_walk_is_left_alone_and_a_new_one_completes_beside_it` to fail on its live row, which the supersede closes before the check runs. The delta case passes under this plant: a delta is never superseded.
22. The watch lane's insert without `heartbeat_at=…`. Expect `test_a_watch_walk_that_fails_before_its_first_batch_still_leaves_its_heartbeat` to fail on `None == NOW`, and `test_every_batch_of_a_watch_walk_moves_its_heartbeat` on its beats, each a second early.
23. The watch lane's `_flush` without `heartbeat_at=…`. Expect `test_every_batch_of_a_watch_walk_moves_its_heartbeat` to fail, every beat reading `NOW`.
24. The watch lane's reclaim without `heartbeat_at=…`. Expect both `delta` parametrisations of `test_a_dead_watch_walk_is_superseded_or_resumed_as_before` to fail on `fixture.saved[0].heartbeat_at == NOW`.

- [ ] **Step 11: Commit**

```bash
git add src/usher/services/reconcile.py src/usher/services/watch_sync.py \
  src/usher/ports/repository/sync.py src/usher/services/handlers.py src/usher/cli.py \
  tests/fakes/source_adapter.py docs/prd/02-data-model.md docs/prd/03-sources-and-sync.md \
  CHANGELOG.md docs/guide/command-line.md .claude/rules/emby-push-and-ingest.md \
  tests/unit/test_services_reconcile.py tests/unit/test_services_watch_sync.py \
  tests/unit/test_services_handlers.py tests/unit/test_cli.py \
  tests/integration/test_services_reconcile.py tests/integration/test_cli_pipeline.py
git commit -m "sync: resume an unfinished whole-library walk in place, and refuse a second live one"
```

---

### Task 18: The watch lane runs as soon as the seed has committed

Spec §2.4, "The watch lane runs after the seed". `reconcile` takes an optional `after_seed` hook and awaits it once every `SEED` unit has committed complete, before any `TITLES` unit is claimed. A resumed walk whose seed completed in an earlier attempt awaits it too, because that attempt may have died before its watch run did. A plan with no `SEED` unit, and every single walk, never calls it. `usher sync` and the worker's `sync_handler` pass a hook that runs `WatchStateSyncService.sync`, and still run the watch lane after the walk. `usher sync` keeps a failed watch run from the hook for its exit line, like any other failed run.

Departure 11 happens here. **The watch lane after the walk reads back to the instant the walk began.** `WatchStateSyncService.sync` takes `since_at_most`, and a fresh delta's cursor becomes the earlier of its own and that instant; both callers pass the walk's `started_at`. The run after the seed moved the cursor past the walk's start, so a state saved meanwhile for an item the walk had not yet stored would otherwise be skipped for good. A first walk, which has no cursor, and a resumed delta, whose position counts into the stream its own cursor selects, keep theirs.

**Files:**
- Modify: `src/usher/services/reconcile.py` (`reconcile`, `_walk_plan`)
- Modify: `src/usher/services/watch_sync.py` (`sync`)
- Modify: `src/usher/services/handlers.py` (`sync_handler`), `src/usher/cli.py` (`_sync`, `_watch_line`, `_watch_lane`)
- Modify: `docs/prd/03-sources-and-sync.md`, `CHANGELOG.md`, `docs/guide/command-line.md`
- Test: `tests/unit/test_services_reconcile.py`, `tests/unit/test_services_watch_sync.py`, `tests/unit/test_services_handlers.py`, `tests/unit/test_cli_errors.py`, `tests/unit/test_api_lanes.py` (the two recorders' overrides), `tests/integration/test_cli_pipeline.py`

**Interfaces:**
- Consumes: Task 17's `_walk_plan(…, *, resumed)`, `unit_starts`, and the CLI case's `_opened` shape; `_sync`'s `failed`; Task 16's `_Fixture`, `_shelve`, `FakeSourceAdapter.stage` and `.fail_unit_after`; Task 14's `SEED` stage; Task 6's `given_completed_walk`; Task 7's supersede in `sync`.
- Produces: `reconcile(…, after_seed: Callable[[], Awaitable[object]] | None = None)`; `WatchStateSyncService.sync(…, since_at_most: AwareDatetime | None = None)`; `usher.cli._watch_line(source, watch) -> str`; `usher.cli._watch_lane(watch, source, adapter, user_id, failed)`, the after-seed hook, which prints its run and keeps a failed one in `failed`.

- [ ] **Step 1: Write the failing service tests**

Append to `tests/unit/test_services_reconcile.py`:

```python
# -- after_seed: the watch lane, as soon as the seed has committed -----------


async def test_the_hook_runs_once_the_seed_has_committed_and_before_any_title_is_fetched() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Watched", range(2), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 13))

    async def after_seed() -> None:
        fixture.journal.append(("after_seed", "-"))

    await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )

    assert fixture.journal.count(("after_seed", "-")) == 1
    hook = fixture.journal.index(("after_seed", "-"))
    assert fixture.journal.index(("completed", "library:Watched")) < hook
    assert hook < fixture.journal.index(("fetched", "library:Films"))


async def test_a_plan_without_a_seed_never_runs_the_hook() -> None:
    fixture = _Fixture()
    _shelve(fixture, "Films", range(3))
    calls: list[str] = []

    async def after_seed() -> None:
        calls.append("after_seed")

    run = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )
    assert run.status is SyncRunStatus.COMPLETED, "the premise: the plan was walked"
    assert calls == []


async def test_a_resume_whose_seed_had_completed_still_runs_the_hook() -> None:
    """The attempt that committed the seed may have died before its watch run did.

    The hook records how many unit walks had been asked for when it ran: one, the
    seed, on the first attempt; none on the resume, which skips the seed.
    """
    fixture = _Fixture()
    _shelve(fixture, "Watched", range(2), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 13))
    fixture.adapter.fail_unit_after("Films", 1)
    calls: list[int] = []

    async def after_seed() -> None:
        calls.append(len(fixture.adapter.unit_starts))

    first = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )
    assert first.status is SyncRunStatus.FAILED, "the premise: the walk failed after its seed"
    fixture.adapter.clear_failure()
    fixture.adapter.unit_starts.clear()

    second = await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, after_seed=after_seed
    )

    assert second.id == first.id, "the premise: the second attempt resumed the first"
    assert fixture.adapter.unit_starts == [("library:Films", 0)]
    assert calls == [1, 0]
```

`tests/unit/test_services_watch_sync.py` (Task 17 imported `timedelta`) — append to the file:

```python
# -- since_at_most: the run after a walk reads back to the walk's start ------


async def test_since_at_most_moves_a_fresh_deltas_cursor_back(fixture: _Fixture) -> None:
    """A state saved after the walk began, and before this lane's last run, is read."""
    walk_began = datetime(2026, 9, 1, tzinfo=UTC)
    await fixture.given_completed_walk(at=walk_began + timedelta(days=1))
    await fixture.given_matched("movie-0")
    await fixture.given_matched("movie-1", changed_at=walk_began + timedelta(hours=12))
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=640, played=False)
    )

    run = await fixture.service.sync(
        fixture.source, fixture.adapter, user_id=fixture.user_id, since_at_most=walk_began
    )

    assert run.cursor_at == walk_began
    assert run.items_seen == 1, "the walk did not read from the moved cursor"


async def test_since_at_most_never_moves_a_cursor_forward(fixture: _Fixture) -> None:
    await fixture.given_completed_walk(at=T0)
    run = await fixture.service.sync(
        fixture.source, fixture.adapter, user_id=fixture.user_id, since_at_most=LATER
    )
    assert run.cursor_at == T0


async def test_since_at_most_leaves_a_first_walk_without_a_cursor(fixture: _Fixture) -> None:
    """No completed run, so nothing to move back: the walk asks for what was watched."""
    run = await fixture.service.sync(
        fixture.source, fixture.adapter, user_id=fixture.user_id, since_at_most=T0
    )
    assert run.cursor_at is None


async def test_since_at_most_leaves_a_resumed_deltas_cursor_alone(fixture: _Fixture) -> None:
    """Its position counts into the stream its own cursor selects.

    The row keeps its `cursor_at` whatever the walk reads from, so what is
    asserted is the walk: a state changed before that cursor stays unread.
    """
    resumed_from = datetime(2026, 9, 2, tzinfo=UTC)
    await fixture.given_matched("movie-1", changed_at=resumed_from - timedelta(hours=12))
    fixture.adapter.seed_state(
        SourceWatchState(external_id="movie-1", position_seconds=640, played=False)
    )
    failed = SyncRun(
        source_id=fixture.source.id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.FAILED,
        cursor_at=resumed_from,
        started_at=resumed_from + timedelta(days=1),
        finished_at=resumed_from + timedelta(days=1),
    )
    await fixture.runs.add(failed)
    run = await fixture.service.sync(
        fixture.source, fixture.adapter, user_id=fixture.user_id, since_at_most=T0
    )
    assert (run.id, run.cursor_at) == (failed.id, resumed_from), "the premise: the delta resumed"
    assert run.items_seen == 0, "the resumed walk read from a moved cursor"
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_reconcile.py tests/unit/test_services_watch_sync.py -k "hook or since_at_most"`
Expected: FAIL — `TypeError: ReconcileService.reconcile() got an unexpected keyword argument 'after_seed'` in the three hook cases, and `… 'since_at_most'` in the four watch cases.

- [ ] **Step 3: The hook, and the cursor that reads back**

`src/usher/services/reconcile.py` — `WalkStage` joins the `usher.domain.sync` import. `reconcile` takes, after `plan`:

```python
        after_seed: Callable[[], Awaitable[object]] | None = None,
```

its docstring's paragraph about `_claim` gains the sentence `` `after_seed` is awaited once a plan's `SEED` stage has committed.``, and the planned call becomes:

```python
                    await self._walk_plan(
                        source,
                        progress,
                        adapter,
                        resumed=claimed is not None,
                        after_seed=after_seed,
                    )
```

`_walk_plan` takes `after_seed: Callable[[], Awaitable[object]] | None` after `resumed`, its docstring gains:

```python
        `after_seed` is awaited once the `SEED` stage has committed, also when an
        earlier attempt committed it, since that attempt may have died before its hook.
```

and its loop becomes:

```python
        for stage in STAGE_ORDER:
            staged = [
                unit
                for unit in units
                if unit.stage is stage and unit.status is not SyncRunUnitStatus.COMPLETED
            ]
            if staged:
                await self._walk_stage(source, progress, adapter, staged)
            if (
                stage is WalkStage.SEED
                and after_seed is not None
                and any(unit.stage is WalkStage.SEED for unit in units)
            ):
                # Nothing beats while the hook runs, so a watch walk in it that outlasts
                # `STALE_AFTER` leaves this walk looking dead. That watch walk is a filtered
                # first walk or a cursored delta: a seed is planned only when both watch
                # filters narrow the library, so a server ignoring them never gets here.
                await after_seed()
```

`src/usher/services/watch_sync.py` — `sync` takes `since_at_most: AwareDatetime | None = None` after `user_id`, and its docstring becomes:

```python
        """Walk this source's watch state into the catalog.

        A fresh delta reads from `since_at_most` when that is earlier than its own
        cursor, so a caller can cover what its item walk stored after this lane's
        last run began. A run another process is still walking is left to it, and
        this walk runs beside it in a row of its own. Never raises a `UsherPortError`.
        """
```

In the fresh branch, after `cursor = await self._runs.latest_completed_cursor(…)`:

```python
                if cursor is not None and since_at_most is not None:
                    # Never past the instant the caller's item walk began: a state saved
                    # since then may be for an item that walk had not yet stored.
                    cursor = min(cursor, since_at_most)
```

- [ ] **Step 4: Run them to see them pass**

Run the Step 2 command, then `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_reconcile.py tests/unit/test_services_watch_sync.py`
Expected: PASS.

- [ ] **Step 5: Write the failing caller tests**

`tests/unit/test_services_handlers.py` — `_RecordingReconcile.__init__` takes `seeded: bool = False` after `raises`, and stores `self._seeded = seeded` and `self.runs: list[SyncRun] = []`. Its `reconcile` takes `after_seed: Callable[[], Awaitable[object]] | None = None` after `plan` (`from collections.abc import Awaitable, Callable`) and ends:

```python
        if self._seeded and after_seed is not None:
            await after_seed()
        run = SyncRun(source_id=source.id, kind=kind, status=SyncRunStatus.COMPLETED)
        self.runs.append(run)
        return run
```

`_RecordingWatch.__init__` stores `self.since_at_most: list[datetime | None] = []`; its `sync` takes `since_at_most: datetime | None = None` after `user_id` and first appends it to `self.since_at_most`. After Task 17's `test_a_refused_walk_fails_the_job_so_the_queue_retries_it`:

```python
async def test_the_sync_handler_runs_the_watch_lane_after_the_seed_and_again_after_the_walk(
    source: Source, adapter: FakeSourceAdapter
) -> None:
    """The second watch run reads back to the instant the walk began."""
    sources = FakeSourceRepository()
    await sources.add(source)
    events: list[str] = []
    reconcile = _RecordingReconcile(events, seeded=True)
    watch = _RecordingWatch(events)

    await sync_handler(sources, reconcile, watch, _Opener(adapter), user_id=_USER)(
        Job(kind=JobKind.SYNC, key=f"{source.id}:full")
    )

    assert events == ["reconcile", "watch", "watch"]
    [walk] = reconcile.runs
    assert watch.since_at_most == [None, walk.started_at]
```

`tests/unit/test_api_lanes.py` — `_RecordingReconcile.reconcile` takes `after_seed: Callable[[], Awaitable[object]] | None = None` after `plan` and passes `after_seed=after_seed` on to `super().reconcile`; `_RecordingWatchSync.sync` takes `since_at_most: datetime | None = None` after `user_id` and passes it on. Neither is exercised here: the gap-closer passes neither.

`tests/unit/test_cli_errors.py` — imports: `Awaitable` beside `AsyncIterator`; `from tests.fakes.source_adapter import FakeSourceAdapter`; `SourceAdapter` beside `SourceNotSupported`; `from usher.services.watch_sync import WatchStateSyncService`. In `_sync_against`, `_Reconcile.reconcile` takes the hook and ignores it, or every `_sync_against` case raises `TypeError` once `_sync` passes `after_seed=`:

```python
        async def reconcile(
            self, _source: Source, _kind: object, adapter: object, **kwargs: object
        ) -> SyncRun:
```

After `test_a_failed_watch_lane_is_a_non_zero_exit_without_the_retraction_hint`:

```python
class _AnsweringWatch(WatchStateSyncService):
    """A watch lane that gives one answer, and needs none of a real one's collaborators."""

    def __init__(self, answer: SyncRun) -> None:
        self._answer = answer

    async def sync(
        self,
        source: Source,
        adapter: SourceAdapter,
        *,
        user_id: uuid.UUID,
        since_at_most: datetime | None = None,
    ) -> SyncRun:
        return self._answer


def _seed_hook(answer: SyncRun, failed: list[SyncRun]) -> Callable[[], Awaitable[None]]:
    """`_sync`'s after-seed hook for a source called Shared Emby, over that watch lane."""
    source = Source(
        kind=SourceKind.EMBY,
        name="Shared Emby",
        base_url="https://emby.invalid",
        credentials_ref="ref-0",
        device_id="device-0",
    )
    return usher_cli._watch_lane(
        _AnsweringWatch(answer), source, FakeSourceAdapter(source), new_id(), failed
    )


async def test_a_failed_watch_run_after_the_seed_is_kept_for_the_exit_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Printed, and kept, so the command exits non-zero however the runs after it end."""
    run = _run(SyncRunKind.WATCH_STATE, SyncRunStatus.FAILED, error="source is unreachable")
    failed: list[SyncRun] = []

    await _seed_hook(run, failed)()

    assert failed == [run]
    assert "Shared Emby: watch_state failed" in capsys.readouterr().out
```

`tests/integration/test_cli_pipeline.py` — `from usher.ports.source import DEFAULT_UNIT_KEY, SourceItem, SourceItemKind`. After Task 17's `test_usher_sync_exits_non_zero_when_a_live_walk_refuses_it`:

```python
async def test_usher_sync_runs_the_watch_lane_as_soon_as_the_seed_has_committed(
    cli_settings: Settings,
    clean_slate: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The watch lane after the seed, the walk, then the watch lane read back to its start."""
    source = Source(
        kind=SourceKind.EMBY,
        name="cli-seeded",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    async with _session_for(cli_settings) as session:
        await PostgresSourceRepository(session).add(source)
        await session.commit()
    adapter = FakeSourceAdapter(source)
    for index, library in enumerate(("Watched", "Films")):
        item = SourceItem(
            external_id=f"m{index}",
            name=f"cli-movie {index}",
            kind=SourceItemKind.MOVIE,
            year=2021,
        )
        adapter.seed(item, datetime(2026, 7, 1, tzinfo=UTC))
        adapter.place(item.external_id, library)
    adapter.stage("Watched", WalkStage.SEED)

    async def _opened(pipeline: object, chosen: Source) -> FakeSourceAdapter:
        return adapter

    monkeypatch.setattr("usher.cli._open_adapter", _opened)

    await _sync(cli_settings, source_name="cli-seeded", kind="full", allow_full_retraction=False)

    printed = [
        line.split(" ")[1]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("cli-seeded: ")
    ]
    assert printed == ["watch_state", "full", "watch_state"]
    async with _session_for(cli_settings) as session:
        runs = await PostgresSyncRunRepository(session).list_for_source(source.id)
    [walk] = [run for run in runs if run.kind is SyncRunKind.FULL]
    watches = sorted(
        (run for run in runs if run.kind is SyncRunKind.WATCH_STATE), key=lambda run: run.started_at
    )
    assert [run.cursor_at for run in watches] == [None, walk.started_at]
```

- [ ] **Step 6: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_handlers.py tests/unit/test_cli_errors.py tests/integration/test_cli_pipeline.py -k "after_the_seed or as_soon_as_the_seed"`
Expected: FAIL — the seeded handler case on `events == ["reconcile", "watch", "watch"]` (it reads `["reconcile", "watch"]`); the hook case in `test_cli_errors.py` with `AttributeError: module 'usher.cli' has no attribute '_watch_lane'`; the CLI case on `printed`, which reads `["full", "watch_state"]`.

- [ ] **Step 7: Pass the hook, and the instant, at both callers**

`src/usher/services/handlers.py`, `sync_handler`'s walk becomes:

```python
        try:
            run = await reconcile.reconcile(
                source,
                lane,
                adapter,
                after_seed=lambda: watch.sync(source, adapter, user_id=user_id),
            )
        except WalkRefused as exc:
            # A port failure, so `JobWorker` fails the job for a retry instead of logging
            # a crash: a walk whose process died is resumed once its heartbeat is stale.
            raise PortUnavailable(str(exc)) from exc
        else:
            await watch.sync(source, adapter, user_id=user_id, since_at_most=run.started_at)
        finally:
            await adapter.aclose()
```

and its docstring gains `The watch lane also runs as soon as a whole-library walk's seed has committed.`

`src/usher/cli.py` — `from collections.abc import AsyncIterator, Awaitable, Callable, Sequence`; `from usher.services.watch_sync import WatchStateSyncService`. After `_sync_failed`:

```python
def _watch_line(source: Source, watch: SyncRun) -> str:
    """One watch-state run, as `usher sync` prints it."""
    return (
        f"{source.name}: watch_state {watch.status.value} "
        f"seen={watch.items_seen} merged={watch.items_matched} "
        f"unmatched={watch.items_unmatched}" + (f" error={watch.error}" if watch.error else "")
    )


def _watch_lane(
    watch: WatchStateSyncService,
    source: Source,
    adapter: SourceAdapter,
    user_id: uuid.UUID,
    failed: list[SyncRun],
) -> Callable[[], Awaitable[None]]:
    """The watch lane as a walk's `after_seed`, printed and kept like the run after the walk."""

    async def run() -> None:
        watched = await watch.sync(source, adapter, user_id=user_id)
        print(_watch_line(source, watched))
        if watched.status is SyncRunStatus.FAILED:
            failed.append(watched)

    return run
```

In `_sync`, the docstring becomes `"""Walk each selected source: items, then watch state, which also runs once a seed lands."""`, the item call becomes:

```python
                hook = _watch_lane(pipeline.watch, source, adapter, user_id, failed)
                run = await pipeline.reconcile.reconcile(
                    source, SyncRunKind(kind), adapter, after_seed=hook
                )
```

and the watch call and its `print` become:

```python
                watch = await pipeline.watch.sync(
                    source, adapter, user_id=user_id, since_at_most=run.started_at
                )
                print(_watch_line(source, watch))
```

Run the Step 6 command. Expected: PASS.

- [ ] **Step 8: Say it in the PRD, the changelog and the guide**

`docs/prd/03-sources-and-sync.md`, "Reconciliation is not optional", after Task 17's paragraph:

```markdown
**The watch lane runs as soon as the seed has committed.** When a
whole-library walk's plan starts with what the account is watching, `usher
sync` and a worker job run the watch lane the moment that stage has committed —
on a resumed walk too — and again after the walk, as before. The second run
reads back to the instant the walk began, so a state saved meanwhile for an
item the walk had not yet stored is not skipped.
```

`CHANGELOG.md`, under `## [Unreleased]`'s `### Added`:

```markdown
- The watch lane runs as soon as a whole-library walk has stored what the
  account is watching — played, in progress and next up — so a household's own
  shelves fill long before the walk ends. It still runs after the walk.
```

`docs/guide/command-line.md`, "Syncing a media server". The bullet `**The watch-state walk follows the item walk.** Each watch state has to match an item first.` becomes:

```markdown
- **The watch-state walk follows the item walk**, because each watch state has
  to match an item first. When it can, a walk of a whole library stores what
  your account is watching first and runs the watch-state walk straight after,
  so your own shelves fill long before the rest of the library has been read.
```

- [ ] **Step 9: Run everything this task touched**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit tests/integration/test_cli_pipeline.py tests/integration/test_services_watch_sync.py tests/integration/test_ingest_end_to_end.py`
Expected: PASS.

- [ ] **Step 10: Plant and verify**

1. The hook's `if` moved above `if staged:`, so it runs before the seed is walked. Expect `test_the_hook_runs_once_the_seed_has_committed_and_before_any_title_is_fetched` to fail on `completed … < hook`.
2. The hook's condition without `stage is WalkStage.SEED`. Expect the same case to fail on `count(...) == 1`: the hook runs after every stage.
3. The hook's condition without `any(unit.stage is WalkStage.SEED for unit in units)`. Expect `test_a_plan_without_a_seed_never_runs_the_hook` to fail.
4. The hook's condition gaining `and staged`. Expect `test_a_resume_whose_seed_had_completed_still_runs_the_hook` to fail on `[1] == [1, 0]`.
5. `sync` without the clamp. Expect `test_since_at_most_moves_a_fresh_deltas_cursor_back` to fail on `cursor_at`.
6. `max` for `min` in the clamp. Expect the same case to fail, and `test_since_at_most_never_moves_a_cursor_forward` with it.
7. The clamp spelled `cursor = since_at_most if cursor is None else min(cursor, since_at_most)` (with `since_at_most is not None` kept). Expect `test_since_at_most_leaves_a_first_walk_without_a_cursor` to fail.
8. The resumed branch's `cursor = incomplete.cursor_at` clamped the same way. Expect `test_since_at_most_leaves_a_resumed_deltas_cursor_alone` to fail on `items_seen`, `movie-1` read: the row's own `cursor_at` is unchanged under this plant, so its premise still holds.
9. `sync_handler` without `after_seed=`. Expect `test_the_sync_handler_runs_the_watch_lane_after_the_seed_and_again_after_the_walk` to fail on `events`.
10. `sync_handler`'s second watch call without `since_at_most=`. Expect `test_the_sync_handler_runs_the_watch_lane_after_the_seed_and_again_after_the_walk` to fail on `watch.since_at_most`.
11. `_sync` without `after_seed=`. Expect `test_usher_sync_runs_the_watch_lane_as_soon_as_the_seed_has_committed` to fail on `printed`.
12. `_sync`'s watch call without `since_at_most=`. Expect `test_usher_sync_runs_the_watch_lane_as_soon_as_the_seed_has_committed` to fail on the cursors: the second watch run's is the first's `started_at`.
13. `_watch_lane` without `failed.append(watched)`. Expect `test_a_failed_watch_run_after_the_seed_is_kept_for_the_exit_line` to fail on `[] == [run]`.

- [ ] **Step 11: Commit**

```bash
git add src/usher/services/reconcile.py src/usher/services/watch_sync.py \
  src/usher/services/handlers.py src/usher/cli.py docs/prd/03-sources-and-sync.md CHANGELOG.md \
  docs/guide/command-line.md tests/unit/test_services_reconcile.py \
  tests/unit/test_services_watch_sync.py tests/unit/test_services_handlers.py \
  tests/unit/test_cli_errors.py tests/unit/test_api_lanes.py \
  tests/integration/test_cli_pipeline.py
git commit -m "sync: run the watch lane as soon as a whole-library walk's seed has committed"
```

---

### Task 19: Visibility — where a whole-library walk stands

Spec §2.8. Where a whole-library walk's plan stands is one computation over its units, `walk_progress`: the stage being walked, units done of units planned, and the items the plan expected. Three readers show it. Every `sync.progress` frame carries it. `GET /admin/sources/{id}/status` gains `last_sync`, the source's newest full or delta run, which carries it. `usher sync-status` prints it under each planned run. A histogram, `usher.sync.unit.duration`, times each unit from the walker's claim to its last commit. Dashboard 3 gains panel 11, which draws that histogram's mean beside Task 15's listing limit.

Four choices the spec leaves open are made here:

- **`stage`** is the first stage, in walking order, that still holds an unfinished unit. Once every unit is complete it is the last stage the plan has.
- **`items_expected`** sums the counts the plan knew. It is `null` only when the plan knew none, because Emby's seed never has a count. It is not a denominator for `items_seen`: the seed's items are read again in their libraries.
- **A single walk's frames** carry all four keys as `null`, so every frame has the same shape.
- **`last_sync`** adds `heartbeat_at` to the run's own fields. That is how a reader tells a live `running` walk from a dead one.

Panel 11 ships as unbacked: nothing has walked a real source since it existed. Task 21 records its observation and flips the PRD 10 statement.

**Files:**
- Modify: `src/usher/domain/sync.py` (`WalkProgress`, `walk_progress`)
- Modify: `src/usher/ports/repository/sync.py` (`latest_run`; `latest_incomplete_run` built on it), `src/usher/db/repositories/sync.py`, `tests/fakes/sync_run_repository.py`
- Modify: `src/usher/services/reconcile.py` (`_unit_duration`, `_Progress.units`, `_walk_plan`, `_walk_stage`, `_fetch`, `_write`, `_commit_unit`, `_flush`, `_publish_progress`)
- Modify: `src/usher/api/dto/source.py` (`SyncRunResponse`, `SourceStatusResponse.last_sync`), `src/usher/api/routers/sources.py` (`source_status`, `_last_sync`)
- Modify: `src/usher/cli.py` (`_sync_status`, `_plan_line`)
- Modify: `tests/fakes/source_adapter.py` (`uncounted`)
- Regenerate: `web/src/api/schema.d.ts`. Modify: `web/src/test/fixtures/admin.ts`
- Modify: `dashboards/03-pipeline.json` (panel 11, the description), `dashboards/README.md`
- Modify: `docs/prd/03-sources-and-sync.md`, `docs/prd/07-client-api.md`, `docs/prd/10-telemetry-and-dashboards.md`, `CHANGELOG.md`, `docs/guide/command-line.md`
- Test: `tests/unit/test_domain_sync.py`, `tests/contract/sync_run_repository_contract.py`, `tests/unit/test_services_reconcile.py`, `tests/unit/test_cli.py`, `tests/unit/test_telemetry_metric_names.py`, `tests/unit/test_dashboards.py`, `tests/unit/test_alerts.py`, `tests/integration/test_admin_sources.py`, `tests/integration/test_cli_pipeline.py`

**Interfaces:**
- Consumes:
  - Task 11: `WalkStage`, `STAGE_ORDER`.
  - Task 12: `SyncRunUnit`, `SyncRunUnitStatus`, `units_for`, `SyncRun.heartbeat_at`.
  - Task 15: the gauge `usher.source.listing.concurrency` (`usher_source_listing_concurrency_ratio`, labelled `source`).
  - Task 16: `_walk_stage`, `_fetch(adapter, claimable, queue)`, `_write(source, progress, units, queue)`, `_commit_unit`, `_flush(…, unit=None)`, `_Fixture`, `_shelve`, `_item`, and `FakeSourceAdapter.hold`.
  - Task 17: `_walk_plan(…, resumed, …)`, `_Fixture(…, clock=…)`, `_Clock`, `NOW`, `FakeSourceAdapter.unit_starts`, and the CLI test's imports.
  - Task 18: `after_seed`.
- Produces:
  - In `usher.domain.sync`: `WalkProgress(stage: WalkStage, units_done: int, units_total: int, items_expected: int | None = None)` and `walk_progress(units: Iterable[SyncRunUnit]) -> WalkProgress | None`.
  - On `SyncRunRepository`: the abstract `latest_run(source_id, kind) -> SyncRun | None`; `latest_incomplete_run` becomes concrete.
  - The `sync.progress` keys `stage`, `units_done`, `units_total`, `items_expected`.
  - `SyncRunResponse`, and `SourceStatusResponse.last_sync: SyncRunResponse | None`.
  - The histogram `usher.sync.unit.duration` (`unit="s"`, labels `source` and `stage`).
  - `usher.cli._plan_line(walk: WalkProgress) -> str`.
  - `FakeSourceAdapter.uncounted(library)`.
  - Dashboard 3's panel 11, titled `Whole-library walks — listing limit and mean unit duration`. Task 21 records its observation and flips PRD 10's backing statement for it.

- [ ] **Step 1: Write the failing domain and repository tests**

`tests/unit/test_domain_sync.py` — `walk_progress` joins the `usher.domain.sync` import. Append:

```python
# -- where a whole-library walk's plan stands --------------------------------

_RUN = new_id()


def _unit(
    key: str,
    stage: WalkStage,
    status: SyncRunUnitStatus = SyncRunUnitStatus.PENDING,
    expected_items: int | None = None,
) -> SyncRunUnit:
    return SyncRunUnit(
        run_id=_RUN,
        unit_key=key,
        stage=stage,
        label=key,
        status=status,
        expected_items=expected_items,
    )


def test_a_walk_without_units_has_no_progress() -> None:
    assert walk_progress([]) is None


def test_a_walk_stands_at_the_first_stage_still_holding_an_open_unit() -> None:
    """In walking order: neither the list's order nor the stages' spelling decides it."""
    units = [
        _unit("episodes:a", WalkStage.EPISODES),
        _unit("titles:a", WalkStage.TITLES, SyncRunUnitStatus.RUNNING),
        _unit("titles:b", WalkStage.TITLES, SyncRunUnitStatus.COMPLETED),
        _unit("seed", WalkStage.SEED, SyncRunUnitStatus.COMPLETED),
    ]
    assert min(WalkStage.EPISODES, WalkStage.TITLES) is WalkStage.EPISODES, (
        "the premise: the stages' spelling alone would answer episodes"
    )

    progress = walk_progress(units)

    assert progress is not None
    assert (progress.stage, progress.units_done, progress.units_total) == (WalkStage.TITLES, 2, 4)


@pytest.mark.parametrize(
    ("stages", "last"),
    [
        ((WalkStage.SEED, WalkStage.TITLES), WalkStage.TITLES),
        ((WalkStage.EPISODES, WalkStage.TITLES), WalkStage.EPISODES),
    ],
)
def test_a_finished_walk_stands_at_the_last_stage_its_plan_has(
    stages: tuple[WalkStage, ...], last: WalkStage
) -> None:
    """In walking order, and over the stages this plan has rather than every stage."""
    units = [_unit(f"{stage.value}:a", stage, SyncRunUnitStatus.COMPLETED) for stage in stages]

    progress = walk_progress(units)

    assert progress is not None
    assert (progress.stage, progress.units_done, progress.units_total) == (last, 2, 2)


@pytest.mark.parametrize(
    ("counts", "expected"), [((None, 4, 6), 10), ((None, 0), 0), ((None, None), None)]
)
def test_items_expected_sums_the_counts_the_plan_knew(
    counts: tuple[int | None, ...], expected: int | None
) -> None:
    """A unit with no count adds nothing; a plan that knew none has no total, and zero is one."""
    units = [
        _unit(f"titles:{index}", WalkStage.TITLES, expected_items=count)
        for index, count in enumerate(counts)
    ]

    progress = walk_progress(units)

    assert progress is not None
    assert progress.items_expected == expected
```

`tests/contract/sync_run_repository_contract.py` — append to `SyncRunRepositoryContract`:

```python
    # --- the newest run of a kind, whatever its status ----------------------

    async def test_the_latest_run_is_the_newest_of_its_kind_whatever_its_status(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """Added newest first, so neither insertion order nor id order gives it away."""
        newer = run(source_id, status=SyncRunStatus.COMPLETED, started_at=LATER)
        await repository.add(newer)
        older = run(source_id, status=SyncRunStatus.FAILED, started_at=EARLIER)
        await repository.add(older)
        assert older.started_at < newer.started_at, "the premise: the completed run is the newer"
        assert newer.id < older.id, "the premise: the newer run holds the smaller id"

        found = await repository.latest_run(source_id, SyncRunKind.FULL)

        assert found is not None
        assert found.id == newer.id
        assert found.status is SyncRunStatus.COMPLETED

    async def test_the_latest_run_is_scoped_by_kind_and_by_source(
        self, repository: SyncRunRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """Both decoys are newer than the run that answers, so only the scope keeps them out."""
        await repository.add(run(source_id, kind=SyncRunKind.DELTA, started_at=LATER))
        await repository.add(run(other_source_id, started_at=LATER))
        assert await repository.latest_run(source_id, SyncRunKind.FULL) is None

        own = run(source_id, started_at=EARLIER)
        await repository.add(own)
        found = await repository.latest_run(source_id, SyncRunKind.FULL)

        assert found is not None
        assert found.id == own.id
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_sync.py tests/unit/test_sync_run_repository_contract.py tests/integration/test_sync_run_repository.py`
Expected: FAIL:
- `test_domain_sync.py` errors at collection with `ImportError: cannot import name 'walk_progress' from 'usher.domain.sync'`.
- Both arms of the two contract cases fail with `AttributeError: … has no attribute 'latest_run'`.

- [ ] **Step 3: The progress, and the newest run of a kind**

`src/usher/domain/sync.py` — `from collections.abc import Iterable`. After `SyncRunUnit`:

```python
class WalkProgress(DomainModel):
    """Where a whole-library walk's plan stands, as its units say."""

    stage: WalkStage
    units_done: int = Field(ge=0)
    units_total: int = Field(ge=1)
    items_expected: int | None = Field(default=None, ge=0)


def walk_progress(units: Iterable[SyncRunUnit]) -> WalkProgress | None:
    """Where the walk these units plan stands, or `None` for a walk without a plan.

    `stage` is the first stage, in walking order, still holding a unit that has not
    completed, or the last stage the plan has once every unit has. `items_expected`
    sums the counts the plan knew, and is `None` when it knew none.
    """
    stored = list(units)
    if not stored:
        return None
    open_stages = {unit.stage for unit in stored if unit.status is not SyncRunUnitStatus.COMPLETED}
    planned = [stage for stage in STAGE_ORDER if any(unit.stage is stage for unit in stored)]
    counts = [unit.expected_items for unit in stored if unit.expected_items is not None]
    return WalkProgress(
        stage=next((stage for stage in planned if stage in open_stages), planned[-1]),
        units_done=sum(unit.status is SyncRunUnitStatus.COMPLETED for unit in stored),
        units_total=len(stored),
        items_expected=sum(counts) if counts else None,
    )
```

`src/usher/ports/repository/sync.py` — `SyncRunStatus` joins the `usher.domain.sync` import. Before `latest_incomplete_run`:

```python
    @abstractmethod
    async def latest_run(self, source_id: uuid.UUID, kind: SyncRunKind) -> SyncRun | None:
        """The newest run of this kind, whatever its status; `None` if there is none.

        Newest by `started_at`, then by `id`, which is the order `list_for_source` uses.
        """
```

`latest_incomplete_run` loses `@abstractmethod` and keeps its docstring, which ends in the paragraph Task 17 wrote. Its body:

```python
        newest = await self.latest_run(source_id, kind)
        # The newest row, and *then* the status test -- never "the newest that is not".
        return None if newest is None or newest.status is SyncRunStatus.COMPLETED else newest
```

`src/usher/db/repositories/sync.py` — `_INCOMPLETE` is renamed `_NEWEST`, with its comment:

```python
# The newest run of a kind, whatever its status. `latest_incomplete_run` tests the status on
# the one row this returns; see the port for why it never filters on it.
```

`PostgresSyncRunRepository.latest_incomplete_run` is replaced by:

```python
    async def latest_run(self, source_id: uuid.UUID, kind: SyncRunKind) -> SyncRun | None:
        with self._session.no_autoflush:
            found = (
                (
                    await self._session.execute(
                        text(_NEWEST), {"source_id": source_id, "kind": kind.value}
                    )
                )
                .mappings()
                .one_or_none()
            )
        # `model_validate(dict(row))`, which is what `list_for_source` does with
        # a `text()` mapping row -- `_to_domain` takes a `SyncRunRow`, and this
        # statement returns no ORM entity to hand it.
        return None if found is None else SyncRun.model_validate(dict(found))
```

`tests/fakes/sync_run_repository.py` — `latest_incomplete_run` is replaced by:

```python
    async def latest_run(self, source_id: uuid.UUID, kind: SyncRunKind) -> SyncRun | None:
        # Newest by `(started_at, id)`, the Postgres arm's `ORDER BY`.
        found = [
            one for one in self._runs.values() if one.source_id == source_id and one.kind is kind
        ]
        return max(found, key=lambda one: (one.started_at, one.id)) if found else None
```

- [ ] **Step 4: Run them to see them pass**

Run the Step 2 command.
Expected: PASS, including every `latest_incomplete_run` case already in the contract. They now go through `latest_run`.

- [ ] **Step 5: Write the failing walk tests**

`tests/fakes/source_adapter.py` — in `FakeSourceAdapter.__init__`, after `self.unit_starts`:

```python
        # Libraries whose unit the fake plans with no expected count.
        self._uncounted: set[str] = set()
```

after `hold`:

```python
    def uncounted(self, library: str) -> None:
        """Plan `library`'s unit with no expected count, as Emby plans its seed."""
        self._uncounted.add(library)
```

and in `plan_walk`, the unit's `len(placed)` becomes `None if name in self._uncounted else len(placed)`.

`tests/unit/test_services_reconcile.py` — imports: `metrics` joins the existing `from opentelemetry import trace`, which becomes `from opentelemetry import metrics, trace`; `from opentelemetry.sdk.metrics import MeterProvider` and `from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader` join the `opentelemetry.sdk` imports below it. Append:

```python
# -- where a planned walk stands: its frames, and each unit's duration -------


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A provider of this test's own; `tests/conftest.py` resets the set-once global."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


def _unit_durations(reader: InMemoryMetricReader) -> dict[tuple[str, str], tuple[int, float]]:
    """`usher.sync.unit.duration` as `{(source, stage): (count, sum)}`."""
    found: dict[tuple[str, str], tuple[int, float]] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data is not None else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != "usher.sync.unit.duration":
                    continue
                for point in metric.data.data_points:
                    assert isinstance(point, HistogramDataPoint)
                    labels = dict(point.attributes or {})
                    found[(str(labels["source"]), str(labels["stage"]))] = (point.count, point.sum)
    return found


async def test_every_frame_of_a_planned_walk_says_where_its_plan_stands() -> None:
    """The seed's own frames say `seed`; from its last commit on, the walk is in `titles`.

    Watched holds the seed and no count, as Emby's seed does, so `items_expected` is
    Films' three alone. `items_seen` leads each frame as the premise of its order.
    """
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Watched", range(3), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 13))
    fixture.adapter.uncounted("Watched")

    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    frames = [
        (
            event.data["items_seen"],
            event.data["stage"],
            event.data["units_done"],
            event.data["units_total"],
            event.data["items_expected"],
        )
        for event in fixture.events.published
        if event.kind is ClientEventKind.SYNC_PROGRESS
    ]
    assert frames == [
        (2, "seed", 0, 2, 3),
        (3, "titles", 1, 2, 3),
        (5, "titles", 1, 2, 3),
        (6, "titles", 2, 2, 3),
    ]


async def test_a_single_walks_frames_carry_no_plan() -> None:
    """Every frame has the same keys; a walk without a plan says `None` for all four."""
    fixture = _Fixture(batch_size=2)
    for index in range(3):
        fixture.adapter.seed(_item(f"m{index}"), T0)

    await fixture.service.reconcile(
        fixture.source, SyncRunKind.FULL, fixture.adapter, plan=False
    )

    frames = [
        event.data
        for event in fixture.events.published
        if event.kind is ClientEventKind.SYNC_PROGRESS
    ]
    assert [frame["items_seen"] for frame in frames] == [2, 3], "the premise: two commits"
    assert {
        (frame["stage"], frame["units_done"], frame["units_total"], frame["items_expected"])
        for frame in frames
    } == {(None, None, None, None)}


async def test_a_unit_is_timed_from_its_walkers_claim_to_its_last_commit(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Once per completed unit, under its stage, from the claim rather than the stage's start.

    One walker, so Shorts is claimed only once Films has completed: the 90 s that pass
    while Films is held are Films' alone, and Shorts records none of them.
    """
    clock = _Clock(NOW)
    fixture = _Fixture(batch_size=1, walkers=1, clock=clock)
    _shelve(fixture, "Watched", range(1), stage=WalkStage.SEED)
    _shelve(fixture, "Films", range(10, 12))
    _shelve(fixture, "Shorts", range(20, 21))
    release = fixture.adapter.hold("Films")

    walk = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    async with asyncio.timeout(5):
        while ("library:Films", 0) not in fixture.adapter.unit_starts:
            await asyncio.sleep(0)
    clock.now = NOW + timedelta(seconds=90)
    release.set()
    run = await asyncio.wait_for(walk, 5)

    assert run.status is SyncRunStatus.COMPLETED, "the premise: the walk finished"
    assert fixture.adapter.unit_starts == [
        ("library:Watched", 0),
        ("library:Films", 0),
        ("library:Shorts", 0),
    ], "the premise: Films, the larger, was claimed before Shorts"
    assert _unit_durations(meter_reader) == {
        (fixture.source.name, "seed"): (1, 0.0),
        (fixture.source.name, "titles"): (2, 90.0),
    }
```

Task 16's `test_a_unit_that_ends_on_a_batch_boundary_publishes_no_second_frame` is replaced by the case below. A frame now counts units, so the empty batch a unit's end commits moves something a reader sees:

```python
async def test_a_unit_that_ends_on_a_batch_boundary_still_says_where_its_plan_stands() -> None:
    """Its end commits an empty batch to save the unit `completed`, and that is a frame.

    The empty batch moved no item counter, but it moved `units_done`: four items in
    batches of two are three frames, the last saying the unit is done.
    """
    fixture = _Fixture(batch_size=2)
    _shelve(fixture, "Films", range(4))
    run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    frames = [
        (event.data["items_seen"], event.data["units_done"])
        for event in fixture.events.published
        if event.kind is ClientEventKind.SYNC_PROGRESS
    ]
    assert frames == [(2, 0), (4, 0), (4, 1)]
    [unit] = await fixture.runs.units_for(run.id)
    assert unit.status is SyncRunUnitStatus.COMPLETED
```

`tests/unit/test_telemetry_metric_names.py`: `len(catalogue) == 43` becomes `== 44`.

`tests/unit/test_dashboards.py`, `test_the_catalogue_scan_reaches_the_row_that_carries_no_usher_prefix` — PRD 10's table has a second reader: `assert len(catalogue) == 43, (` becomes `== 44`, and `not the 43 its own header` in its message becomes `not the 44 its own header`.

- [ ] **Step 6: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_reconcile.py tests/unit/test_telemetry_metric_names.py tests/unit/test_dashboards.py -k "plan_stands or carry_no_plan or timed_from or catalogue"`
Expected: FAIL:
- the two frame cases above with `KeyError: 'stage'`, and the boundary case with `KeyError: 'units_done'`;
- the duration case on `{} == {…}`;
- both catalogue counts on 43 rows: `the catalogue table parse found 43 rows`, and the dashboards reader's `PRD 10's metric table parsed to 43 rows`. The filter's four other `catalogue` cases in `test_dashboards.py` pass.

- [ ] **Step 7: The walk says where it stands, and times each unit**

`src/usher/services/reconcile.py` — `Mapping` joins the `collections.abc` import, and `WalkProgress, walk_progress` the `usher.domain.sync` import. After `_retraction_fraction`:

```python
#: A whole-library walk's unit, from the walker's claim to the unit's last commit.
_unit_duration = _meter.create_histogram(
    "usher.sync.unit.duration",
    unit="s",
    description="Wall time per unit of a whole-library walk, from claim to last commit",
)
```

`_Progress.__slots__` becomes `("run", "units")`, and `__init__` gains:

```python
        # A planned walk's units by key, as last committed; empty for a single walk.
        self.units: dict[str, SyncRunUnit] = {}
```

In `_walk_plan`, directly above `for stage in STAGE_ORDER:`:

```python
        progress.units = {unit.unit_key: unit for unit in units}
```

In `_walk_stage`, the `claimable = …` line through the `walkers = [...]` list becomes:

```python
        claimable = deque(sorted(units, key=lambda unit: unit.expected_items or 0, reverse=True))
        claimed: dict[str, datetime] = {}

        def claim() -> SyncRunUnit | None:
            # A unit's duration runs from here, the moment a walker takes it.
            if not claimable:
                return None
            unit = claimable.popleft()
            claimed[unit.unit_key] = self._clock()
            return unit

        queue: asyncio.Queue[_Fetched] = asyncio.Queue(maxsize=self._walkers)
        walkers = [
            asyncio.create_task(self._fetch(adapter, claim, queue))
            for _ in range(min(self._walkers, len(units)))
        ]
```

and its `await self._write(source, progress, units, queue)` becomes `await self._write(source, progress, units, queue, claimed)`.

`_fetch` takes `claim: Callable[[], SyncRunUnit | None]` in place of `claimable: deque[SyncRunUnit]`, and its loop head becomes:

```python
        while (unit := claim()) is not None:
```

(the `unit = claimable.popleft()` line goes, and the rest of the body is unchanged).

`_write` takes `claimed: Mapping[str, datetime]` after `queue`. Its docstring gains the sentence `Each unit that completes records the seconds since a walker claimed it.` before `Returns once every unit has committed complete.`, and its `if done:` block becomes:

```python
            if done:
                open_units -= 1
                _unit_duration.record(
                    (self._clock() - claimed[unit.unit_key]).total_seconds(),
                    {"source": source.name, "stage": unit.stage.value},
                )
```

`_commit_unit` — between the `unit = unit.evolve(…)` and the `progress.run = await self._flush(…)` call:

```python
        progress.units[unit.unit_key] = unit
```

and the call becomes:

```python
        progress.run = await self._flush(
            source,
            progress.run.evolve(heartbeat_at=self._clock()),
            items,
            unit=unit,
            walk=walk_progress(progress.units.values()),
        )
```

`_flush` takes `walk: WalkProgress | None = None` after `unit`, and Task 16's `if batch:` block, before `return run`, becomes the lines below; `return run` stays. A unit's end that commits an empty batch now moves `units_done`, and can move `stage`, so it is a frame.

```python
        if batch or (unit is not None and unit.status is SyncRunUnitStatus.COMPLETED):
            # A batch with items is a frame (PRD 07: one per batch), and so is the empty
            # one a unit's end commits to save the unit `completed`: that moves no item
            # counter, but it moves `units_done`, and can move `stage`.
            await self._publish_progress(source, run, walk)
```

`_publish_progress` takes `walk: WalkProgress | None = None` after `run`, and its `data` gains, after `"items_unmatched"`:

```python
                    # Where a planned walk's plan stands; a single walk's frame says `None`.
                    "stage": walk.stage.value if walk is not None else None,
                    "units_done": walk.units_done if walk is not None else None,
                    "units_total": walk.units_total if walk is not None else None,
                    "items_expected": walk.items_expected if walk is not None else None,
```

`docs/prd/10-telemetry-and-dashboards.md`, "Metrics". The lead-in becomes:

```markdown
**Every row is emitted today. 44 rows: 43 instruments Usher declares, plus one `FastAPIInstrumentor` supplies.**
```

Add a row after `usher.sync.retraction.fraction`:

```markdown
| `usher.sync.unit.duration` | histogram | source, stage | ✅ fast first sync |
```

After Task 15's `usher.source.listing.concurrency` paragraph, add:

```markdown
**`usher.sync.unit.duration` is seconds per unit of a whole-library walk**,
from the moment a walker claims the unit to the unit's last commit, recorded
once for each unit that completes; a unit that fails records nothing. `stage` is
`seed`, `titles` or `episodes`.
```

- [ ] **Step 8: Run them to see them pass**

Run the Step 6 command, then `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_reconcile.py tests/integration/test_services_reconcile.py`
Expected: PASS. Each existing case still passes: none reads a frame by its whole dict, none passes `_fetch` a deque, and of the cases that read frames, only the boundary case replaced above has a unit that ends on an empty batch.

- [ ] **Step 9: Write the failing status and `sync-status` tests**

`tests/integration/test_admin_sources.py` — `from usher.db.repositories.sync import PostgresSyncRunRepository`; `SyncRun, SyncRunUnit, SyncRunUnitStatus, WalkStage` join the `usher.domain.sync` import. `test_status_reports_a_healthy_source` gains a last line:

```python
    assert body["last_sync"] is None, "no walk has run, and the field is there to say so"
```

After `test_status_reports_the_running_lanes_push_health`:

```python
async def test_status_carries_the_newest_item_walk_and_where_its_plan_stands(
    client: AsyncClient, app: FastAPI
) -> None:
    """The newest full or delta run, never the watch lane's, which is newer than both."""
    created = (await client.post("/admin/sources", json=_payload())).json()
    source_id = uuid.UUID(created["id"])
    older = SyncRun(
        source_id=source_id,
        kind=SyncRunKind.DELTA,
        status=SyncRunStatus.COMPLETED,
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        finished_at=datetime(2026, 9, 1, 1, tzinfo=UTC),
    )
    walk = SyncRun(
        source_id=source_id,
        kind=SyncRunKind.FULL,
        items_seen=7,
        items_matched=6,
        items_unmatched=1,
        started_at=datetime(2026, 9, 2, tzinfo=UTC),
        heartbeat_at=datetime(2026, 9, 2, 0, 5, tzinfo=UTC),
    )
    watch = SyncRun(
        source_id=source_id,
        kind=SyncRunKind.WATCH_STATE,
        status=SyncRunStatus.COMPLETED,
        started_at=datetime(2026, 9, 3, tzinfo=UTC),
    )
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with factory() as session:
        runs = PostgresSyncRunRepository(session)
        for one in (older, walk, watch):
            await runs.add(one)
        await runs.add_units(
            [
                SyncRunUnit(
                    run_id=walk.id,
                    unit_key="seed",
                    stage=WalkStage.SEED,
                    label="seed",
                    status=SyncRunUnitStatus.COMPLETED,
                ),
                SyncRunUnit(
                    run_id=walk.id,
                    unit_key="titles:a",
                    stage=WalkStage.TITLES,
                    label="titles a",
                    expected_items=4,
                    status=SyncRunUnitStatus.COMPLETED,
                ),
                SyncRunUnit(
                    run_id=walk.id,
                    unit_key="episodes:a",
                    stage=WalkStage.EPISODES,
                    label="episodes a",
                    expected_items=6,
                ),
            ]
        )
        await session.commit()

    body = (await client.get(f"/admin/sources/{created['id']}/status")).json()

    assert body["last_sync"] == {
        "kind": "full",
        "status": "running",
        "started_at": "2026-09-02T00:00:00Z",
        "finished_at": None,
        "heartbeat_at": "2026-09-02T00:05:00Z",
        "items_seen": 7,
        "items_matched": 6,
        "items_unmatched": 1,
        "items_retracted": 0,
        "error": None,
        "stage": "episodes",
        "units_done": 2,
        "units_total": 3,
        "items_expected": 10,
    }


async def test_status_of_a_single_walk_carries_no_plan(client: AsyncClient, app: FastAPI) -> None:
    """A delta newer than the last full walk is the one reported, and it planned nothing.

    The full walk had a plan, so reading its units would put one on the delta.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    source_id = uuid.UUID(created["id"])
    planned = SyncRun(
        source_id=source_id,
        kind=SyncRunKind.FULL,
        status=SyncRunStatus.COMPLETED,
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        finished_at=datetime(2026, 9, 1, 1, tzinfo=UTC),
    )
    single = SyncRun(
        source_id=source_id,
        kind=SyncRunKind.DELTA,
        status=SyncRunStatus.COMPLETED,
        started_at=datetime(2026, 9, 2, tzinfo=UTC),
        finished_at=datetime(2026, 9, 2, 0, 1, tzinfo=UTC),
    )
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with factory() as session:
        runs = PostgresSyncRunRepository(session)
        await runs.add(planned)
        await runs.add(single)
        await runs.add_units(
            [
                SyncRunUnit(
                    run_id=planned.id,
                    unit_key="titles:a",
                    stage=WalkStage.TITLES,
                    label="titles a",
                    expected_items=4,
                    status=SyncRunUnitStatus.COMPLETED,
                )
            ]
        )
        await session.commit()

    last = (await client.get(f"/admin/sources/{created['id']}/status")).json()["last_sync"]

    assert (last["kind"], last["started_at"]) == ("delta", "2026-09-02T00:00:00Z")
    assert [last[key] for key in ("stage", "units_done", "units_total", "items_expected")] == [
        None,
        None,
        None,
        None,
    ]
```

`tests/unit/test_cli.py` — `_plan_line` joins the `usher.cli` import; `from usher.domain.sync import WalkProgress, WalkStage`. Append:

```python
def test_a_plan_that_knew_no_counts_prints_its_expectation_as_unknown() -> None:
    line = _plan_line(WalkProgress(stage=WalkStage.SEED, units_done=0, units_total=1))
    assert line.split() == ["plan:", "stage=seed", "units=0/1", "expected=unknown"]
```

`tests/integration/test_cli_pipeline.py` — `SyncRunStatus, SyncRunUnitStatus` join the `usher.domain.sync` import that Task 17 added. After `test_sync_status_works_before_any_sync_has_run`:

```python
async def test_sync_status_prints_where_a_whole_library_walks_plan_stands(
    cli_settings: Settings, clean_slate: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A planned run gets a plan line under it; a single walk's run gets none."""
    source = Source(
        kind=SourceKind.EMBY,
        name="cli-planned",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    planned = SyncRun(
        source_id=source.id, kind=SyncRunKind.FULL, started_at=datetime(2026, 9, 2, tzinfo=UTC)
    )
    single = SyncRun(
        source_id=source.id,
        kind=SyncRunKind.DELTA,
        status=SyncRunStatus.COMPLETED,
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    async with _session_for(cli_settings) as session:
        await PostgresSourceRepository(session).add(source)
        runs = PostgresSyncRunRepository(session)
        await runs.add(single)
        await runs.add(planned)
        await runs.add_units(
            [
                SyncRunUnit(
                    run_id=planned.id,
                    unit_key="seed",
                    stage=WalkStage.SEED,
                    label="seed",
                    status=SyncRunUnitStatus.COMPLETED,
                ),
                SyncRunUnit(
                    run_id=planned.id,
                    unit_key="titles:a",
                    stage=WalkStage.TITLES,
                    label="titles a",
                    expected_items=4,
                    status=SyncRunUnitStatus.RUNNING,
                ),
                SyncRunUnit(
                    run_id=planned.id,
                    unit_key="episodes:a",
                    stage=WalkStage.EPISODES,
                    label="episodes a",
                    expected_items=6,
                ),
            ]
        )
        await session.commit()

    await _sync_status(cli_settings)

    lines = capsys.readouterr().out.splitlines()
    at = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("cli-planned") and " full " in line
    )
    assert lines[at + 1].strip() == "plan: stage=titles units=1/3 expected=10"
    assert lines[at + 2].startswith("cli-planned") and " delta " in lines[at + 2], (
        "the premise: the single walk is listed next"
    )
    assert not lines[at + 3].strip().startswith("plan:"), "a single walk printed a plan line"
```

- [ ] **Step 10: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_cli.py tests/integration/test_admin_sources.py tests/integration/test_cli_pipeline.py -k "status or plan"`
Expected: FAIL:
- the three status cases with `KeyError: 'last_sync'`;
- `test_cli.py` at collection with `ImportError: cannot import name '_plan_line'`;
- the CLI case on its first assertion: the line after the full run is the delta's.

- [ ] **Step 11: `last_sync` on the status route, and the plan line**

`src/usher/api/dto/source.py` — `from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus, WalkProgress, WalkStage`. Above `SourceStatusResponse`:

```python
class SyncRunResponse(BaseModel):
    """`last_sync`: a source's newest full or delta walk, as the status route reports it.

    `stage`, `units_done`, `units_total` and `items_expected` say where a
    whole-library walk's plan stands -- `walk_progress` -- and are `null` for a walk
    without one. `heartbeat_at` tells a live `running` walk from a dead one: a
    whole-library walk's writer moves it at least once a minute. `error` is the
    run's own sentence, built like `detail` below from translated port errors.
    """

    kind: SyncRunKind
    status: SyncRunStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    heartbeat_at: AwareDatetime | None
    items_seen: int
    items_matched: int
    items_unmatched: int
    items_retracted: int
    error: str | None
    stage: WalkStage | None
    units_done: int | None
    units_total: int | None
    items_expected: int | None

    @classmethod
    def of(cls, run: SyncRun, walk: WalkProgress | None) -> "SyncRunResponse":
        return cls(
            kind=run.kind,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            heartbeat_at=run.heartbeat_at,
            items_seen=run.items_seen,
            items_matched=run.items_matched,
            items_unmatched=run.items_unmatched,
            items_retracted=run.items_retracted,
            error=run.error,
            stage=None if walk is None else walk.stage,
            units_done=None if walk is None else walk.units_done,
            units_total=None if walk is None else walk.units_total,
            items_expected=None if walk is None else walk.items_expected,
        )
```

`SourceStatusResponse` — its docstring gains a last paragraph:

```python
    `last_sync` is the source's newest full or delta walk -- never the watch
    lane's -- or `null` before the first.
```

It gains a field after `detail`, required so that every response states it:

```python
    last_sync: SyncRunResponse | None
```

and `of` becomes `def of(cls, status: SourceStatus, last_sync: SyncRunResponse | None) -> "SourceStatusResponse":`, passing `last_sync=last_sync`.

`src/usher/api/routers/sources.py`:
- Imports: `SyncRunRepositoryDep` joins the `usher.api.deps` import, and `SyncRunResponse` the `usher.api.dto.source` import. Add `from usher.domain.sync import SyncRunKind, walk_progress` and `from usher.ports.repository import SyncRunRepository`.
- After `_DISABLED_DETAIL`, add:

```python
#: The walks `last_sync` reports: the item lanes', never the watch lane's.
_ITEM_WALKS: Final = (SyncRunKind.FULL, SyncRunKind.DELTA)
```

`source_status` becomes:

```python
@router.get("/{source_id}/status", response_model=SourceStatusResponse, responses=_SOURCE_FAILURES)
async def source_status(
    source_id: uuid.UUID, sources: SourceServiceDep, runs: SyncRunRepositoryDep
) -> SourceStatusResponse:
    result = await sources.status(source_id)
    if result is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="source not found",
        )
    return SourceStatusResponse.of(result, await _last_sync(runs, source_id))


async def _last_sync(runs: SyncRunRepository, source_id: uuid.UUID) -> SyncRunResponse | None:
    """The source's newest item walk, with where its plan stands; `None` before the first."""
    found = [
        run
        for kind in _ITEM_WALKS
        if (run := await runs.latest_run(source_id, kind)) is not None
    ]
    if not found:
        return None
    newest = max(found, key=lambda run: (run.started_at, run.id))
    return SyncRunResponse.of(newest, walk_progress(await runs.units_for(newest.id)))
```

`src/usher/cli.py` — `WalkProgress, walk_progress` join the `usher.domain.sync` import. Before `_sync_status`:

```python
def _plan_line(walk: WalkProgress) -> str:
    """Where a whole-library walk's plan stands, as `sync-status` prints it under its run."""
    expected = "unknown" if walk.items_expected is None else str(walk.items_expected)
    return (
        f"{'':<24} plan: stage={walk.stage.value} "
        f"units={walk.units_done}/{walk.units_total} expected={expected}"
    )
```

In `_sync_status`, the summary line of the docstring becomes `"""Every source's recent runs and their plans, plus queue depth and parked count.`. Inside `for run in runs:`, after the `report.append(…)` of the run line, add:

```python
                walk = walk_progress(await pipeline.runs.units_for(run.id))
                if walk is not None:
                    report.append(_plan_line(walk))
```

- [ ] **Step 12: Run them to see them pass**

Run the Step 10 command.
Expected: PASS.

- [ ] **Step 13: The console's types and fixtures**

`web/src/api/schema.d.ts` is generated, and `.claude/hooks/guard-generated.sh` refuses a hand edit to it. `npm run gen:types` reads a running server, and its default origin on this host is production. So generate from the app's own document instead:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python - <<'EOF'
import json

from usher.api.app import create_app
from usher.config import Settings

settings = Settings(
    _env_file=None,
    database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
    secret_key="0123456789abcdef0123456789abcdef",
    push_enabled=False,
    worker_enabled=False,
)
with open("/var/tmp/ffs-openapi.json", "w", encoding="utf-8") as out:
    json.dump(create_app(settings).openapi(), out)
EOF
(cd web && npx -y --package=typescript@5.9 --package=openapi-typescript@7 openapi-typescript /var/tmp/ffs-openapi.json -o src/api/schema.d.ts)
git diff --stat web/src/api/schema.d.ts
```

Expected: `git diff web/src/api/schema.d.ts` adds the `SyncRunResponse` schema; the `SyncRunKind`, `SyncRunStatus` and `WalkStage` schemas it names, each a `StrEnum` and so a component of its own, as `SourceKind` is; and `last_sync` on `SourceStatusResponse`, whose `@description` gains the docstring's new `last_sync` paragraph. It changes nothing else. A diff that touches anything beyond those means the committed file was already stale against `main`. In that case stop and report it; don't commit someone else's drift under this task.

`web/src/test/fixtures/admin.ts`:
- `sourceStatusHealthy`'s comment gains the sentence `` `last_sync` is a whole-library walk part-way through its episodes. ``, and the object gains, after `detail: null,`:

```ts
  last_sync: {
    kind: 'full',
    status: 'running',
    started_at: '2026-10-01T03:00:00Z',
    finished_at: null,
    heartbeat_at: '2026-10-01T03:41:12Z',
    items_seen: 412000,
    items_matched: 398211,
    items_unmatched: 13789,
    items_retracted: 0,
    error: null,
    stage: 'episodes',
    units_done: 9,
    units_total: 14,
    items_expected: 1130000,
  },
```

- `sourceStatusUnreachable`'s comment gains `` `last_sync` is `null`: nothing has walked this source. ``, and the object gains `last_sync: null,` after `detail`.

Run, from `web/`: `npm run verify && npm run e2e && npm run e2e:visual`
Expected: PASS, with no screenshot moved: no screen renders `last_sync`, so a moved baseline is a finding to read, not one to update.

- [ ] **Step 14: Write the failing panel tests**

`tests/unit/test_dashboards.py`. In `test_the_queue_depth_panels_are_two_panels_over_two_datasources`, the count assertion becomes:

```python
    assert len(panels) == 11, (
        "dashboard 3 is eleven panels -- PRD 10's ten items, with queue depth drawn twice -- "
        f"not {len(panels)}: {titles}"
    )
```

In `test_the_dashboard_3_prose_claims_are_falsifiable`, the last block becomes:

```python
    with pytest.raises(AssertionError, match="eleven panels"):
        titles = ["only", "ten", "of", "them", "here", "and", "no", "more", "at", "all"]
        assert len(titles) == 11, (
            "dashboard 3 is eleven panels -- PRD 10's ten items, with queue depth drawn "
            f"twice -- not {len(titles)}"
        )
```

After `test_the_tmdb_panel_counts_429s_and_denominates_on_every_status_including_error`:

```python
def test_the_walk_panel_draws_the_listing_limit_and_a_mean_unit_duration_by_stage() -> None:
    """The two series a whole-library walk adds, each split by the labels it carries.

    The unit duration is its sum over its count, both halves keeping `source` and
    `stage`: the histogram is on the SDK's default boundaries, and a stage summed away
    hides which part of the plan is slow.
    """
    walk = [
        panel
        for panel in _live_panels(_PIPELINE)
        if str(panel["title"]).startswith("Whole-library walks")
    ]
    assert len(walk) == 1, f"dashboard 3 has {len(walk)} whole-library walk panels, not one"
    by_metric = {
        normalise_metric(token): expr for expr in _exprs(walk[0]) for token in _metric_tokens(expr)
    }
    assert set(by_metric) == {"usher_source_listing_concurrency", "usher_sync_unit_duration"}, (
        f"the walk panel queries {sorted(by_metric)}"
    )

    duration = by_metric["usher_sync_unit_duration"]
    assert _metric_tokens(duration) == {
        "usher_sync_unit_duration_seconds_sum",
        "usher_sync_unit_duration_seconds_count",
    }, f"the unit duration is not a sum over a count: {duration}"
    assert [labels for labels, _operand in _aggregations(duration)] == [
        {"source", "stage"},
        {"source", "stage"},
    ], f"both halves of the mean must keep source and stage: {duration}"

    limit = by_metric["usher_source_listing_concurrency"]
    assert [labels for labels, _operand in _aggregations(limit)] == [{"source"}], limit
```

`tests/unit/test_alerts.py`, `test_every_rule_carries_a_window_a_severity_and_a_description_naming_its_series_and_panel`: `== 10` becomes `== 11`, and `"Dashboard 3 has ten panels; this scan found "` becomes `"Dashboard 3 has eleven panels; this scan found "`.

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_dashboards.py tests/unit/test_alerts.py`
Expected: FAIL:
- the queue-depth case on `dashboard 3 is eleven panels`;
- the walk-panel case on `0 whole-library walk panels`;
- the alerts case on `Dashboard 3 has eleven panels`.

- [ ] **Step 15: Panel 11**

Write `/var/tmp/ffs-panel-11.json`:

```json
{
  "id": 11,
  "type": "timeseries",
  "title": "Whole-library walks — listing limit and mean unit duration",
  "description": "usher.source.listing.concurrency, as usher_source_listing_concurrency_ratio, labelled source: the cap the listing limiter holds now. It reads USHER_SYNC_WALKERS while pages succeed, drops to 1 when a page has to be asked again, and climbs back a step for every ten pages in a row that succeed, so a sawtooth is the backoff working. A line held at 1 means pages keep needing another try, or USHER_SYNC_WALKERS is 1.\n\nusher.sync.unit.duration, as usher_sync_unit_duration_seconds, labelled source and stage: the seconds from a walker's claim of a unit to that unit's last commit, recorded once for each unit that completes. A unit that fails records nothing. **It is plotted as a mean, not a quantile**: the histogram declares no bucket boundaries, so it has the SDK's second-scale defaults, and a quantile over those is not a measurement.\n\n⚠️ **Both series exist only in a process that has walked a source.** The gauge is first written by a listing page and the histogram by a unit's completion, so a quiet server shows no series rather than a zero.",
  "datasource": {"type": "prometheus", "uid": "usher-prometheus"},
  "gridPos": {"h": 8, "w": 24, "x": 0, "y": 32},
  "options": {
    "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"], "showLegend": true},
    "tooltip": {"mode": "multi", "sort": "desc"}
  },
  "fieldConfig": {
    "defaults": {"unit": "short", "min": 0, "custom": {"drawStyle": "line", "lineWidth": 1, "showPoints": "auto"}},
    "overrides": [
      {
        "matcher": {"id": "byRegexp", "options": ".*mean unit duration.*"},
        "properties": [{"id": "unit", "value": "s"}, {"id": "custom.axisPlacement", "value": "right"}]
      }
    ]
  },
  "targets": [
    {
      "refId": "A",
      "editorMode": "code",
      "datasource": {"type": "prometheus", "uid": "usher-prometheus"},
      "expr": "max by (source) (usher_source_listing_concurrency_ratio)",
      "legendFormat": "{{source}} · listing limit",
      "range": true,
      "instant": false
    },
    {
      "refId": "B",
      "editorMode": "code",
      "datasource": {"type": "prometheus", "uid": "usher-prometheus"},
      "expr": "sum by (source, stage) (rate(usher_sync_unit_duration_seconds_sum[15m])) / sum by (source, stage) (rate(usher_sync_unit_duration_seconds_count[15m]))",
      "legendFormat": "{{source}} {{stage}} · mean unit duration",
      "range": true,
      "instant": false
    }
  ]
}
```

Append it, keeping the file's own formatting. The file round-trips byte for byte through `json.dumps(…, indent=2)` plus a newline:

```bash
uv run python - <<'EOF'
import json
import pathlib

path = pathlib.Path("dashboards/03-pipeline.json")
dashboard = json.loads(path.read_text(encoding="utf-8"))
assert dashboard["description"].startswith("Ten panels"), "the description moved"
dashboard["description"] = "Eleven panels" + dashboard["description"].removeprefix("Ten panels")
panel = json.loads(pathlib.Path("/var/tmp/ffs-panel-11.json").read_text(encoding="utf-8"))
dashboard["panels"].append(panel)
path.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
EOF
git diff --stat dashboards/03-pipeline.json
```

Expected: the diff adds panel 11 and changes the description's first two words, nothing else.

`dashboards/README.md`:
- "# Dashboard 3 — Pipeline": `Ten panels, and the mixed one:` becomes `Eleven panels, and the mixed one:`.
- After the "### 10 — TMDb requests/sec against the ceiling, with 429 count" section, add:

````markdown
### 11 — Whole-library walks: listing limit and mean unit duration

`timeseries`, Prometheus, two targets.

```
max by (source) (usher_source_listing_concurrency_ratio)
sum by (source, stage) (rate(usher_sync_unit_duration_seconds_sum[15m])) / sum by (source, stage) (rate(usher_sync_unit_duration_seconds_count[15m]))
```

**Not yet observed.** Both series exist only in a process that has walked a
source: the gauge is first written by a listing page, the histogram by a unit's
completion. No whole-library walk has run against a real source since this
panel was committed, so there is no reading to record.
````

- The heading `## ⚠️ Two Dashboard 3 panels plot a mean, not a quantile, and the reason is an instrument` becomes `## ⚠️ Three Dashboard 3 panels plot a mean, not a quantile, and the reason is an instrument`. At the end of that section, add:

```markdown
`Whole-library walks — listing limit and mean unit duration` was a mean from the
start: `usher.sync.unit.duration` declares no boundaries either, so it sits on
the same defaults.
```

Run the Step 14 command.
Expected: PASS.

- [ ] **Step 16: Say it in the PRDs, the changelog and the guide**

`docs/prd/03-sources-and-sync.md`, "Health and status" — after its first paragraph:

```markdown
The status also carries `last_sync`, the source's newest full or delta walk,
and for a whole-library walk where its plan stands: the stage being walked,
units done of units planned, and the items the plan expected. `usher
sync-status` prints the same under each of a source's recent runs, and every
`sync.progress` frame carries it ([07](07-client-api.md)).
```

`docs/prd/07-client-api.md`:
- "Admin": the paragraph beginning `**`GET /admin/sources/{id}/status`** renders a `SourceStatus`` gains, after `([03](03-sources-and-sync.md)).`:

```markdown
Its `last_sync` is the source's newest full or delta walk — never the watch
lane's — or `null` before the first: `kind`, `status`, `started_at`,
`finished_at`, `heartbeat_at`, the four item counts and `error`, then `stage`,
`units_done`, `units_total` and `items_expected`, which say where a
whole-library walk's plan stands and are `null` for a walk without one.
```

- "Streaming updates (SSE)": the `sync.progress` row becomes:

```markdown
| `sync.progress` | Source, kind, counts; a whole-library walk's stage, units and expected items | Admin UI only |
```

- The paragraph beginning `**`sync.progress` and `bootstrap.progress` are one frame per committed batch,` gains, after its first sentence:

```markdown
A whole-library walk's `sync.progress` says where its plan stands: `stage` is
the stage being walked (the last one once the walk has finished),
`units_done` and `units_total` count units, and `items_expected` sums the item
counts the plan knew, `null` when it knew none. A single walk sends those four
as `null`. `items_seen` can pass `items_expected`, because the items the seed
reads are read again in their libraries.
```

`docs/prd/10-telemetry-and-dashboards.md`, "### 3 — Pipeline":
- The item list's last line, `` `USHER_TMDB_REQUESTS_PER_SECOND` ceiling, with 429 count. ``, becomes `` `USHER_TMDB_REQUESTS_PER_SECOND` ceiling, with 429 count · **whole-library walks: the listing limit and the mean unit duration, by stage**. ``
- The backing paragraph becomes:

```markdown
✅ **Every panel here is backed by real data as of M9, except the
whole-library walk panel, which is unbacked until a planned walk runs against
a real source.** ⚠️ A panel that drains the whole unmatched queue should page
with the keyset cursor; the `OFFSET` form is quadratic in queue depth.
```

`CHANGELOG.md`, under `## [Unreleased]`'s `### Added`:

```markdown
- `GET /admin/sources/{id}/status` carries `last_sync`, the source's newest
  full or delta walk. It, every `sync.progress` frame and `usher sync-status`
  say where a whole-library walk's plan stands: the stage being walked, units
  done of units planned, and the items the plan expected. A histogram,
  `usher.sync.unit.duration`, and a new Dashboard 3 panel show how long each
  unit takes beside the listing limit's backoff.
```

`docs/guide/command-line.md`, "Syncing a media server":
- In the command block, the `usher sync-status` line's comment `# recent walks, queue depth, parked jobs` becomes `# recent walks and their plans, queue depth, parked jobs`.
- After the bullet beginning `**`usher sync` exits non-zero`, add:

```markdown
- **`usher sync-status` shows how far a large walk has got.** Under a walk of a
  whole library it prints the stage being read, the pieces done of the pieces
  planned, and how many items the plan expected.
```

- [ ] **Step 17: Run everything this task touched**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit tests/integration/test_admin_sources.py tests/integration/test_cli_pipeline.py tests/integration/test_sync_run_repository.py tests/integration/test_services_reconcile.py tests/integration/test_services_watch_sync.py`
Expected: PASS. That includes `test_docs_pointers.py`, `test_docs_currency.py`, `test_prd_10_panel_backing.py`, `test_dashboards.py`, `test_alerts.py` and `test_api_dto.py`. The last one discovers `SyncRunResponse` by its suffix.

Run, from `web/`: `npm run verify && npm run e2e && npm run e2e:visual`
Expected: PASS.

- [ ] **Step 18: Plant and verify**

1. `walk_progress` taking `stage=min(open_stages)` when one is open. Expect `test_a_walk_stands_at_the_first_stage_still_holding_an_open_unit` to fail on `EPISODES`.
2. `walk_progress` falling back to `STAGE_ORDER[-1]`. Expect the `(SEED, TITLES)` parametrisation of `test_a_finished_walk_stands_at_the_last_stage_its_plan_has` to fail on `EPISODES`.
3. The fallback spelled `max(planned)`. Expect the `(EPISODES, TITLES)` parametrisation to fail on `TITLES`.
4. `items_expected=sum(counts) or None`. Expect the `(None, 0)` parametrisation to fail on `None == 0`.
5. `items_expected=None if any(unit.expected_items is None for unit in stored) else sum(counts)`. Expect the `(None, 4, 6)` parametrisation to fail.
6. The fake's `latest_run` with `min` for `max`. Expect the fake arm of `test_the_latest_run_is_the_newest_of_its_kind_whatever_its_status` to fail.
7. `_NEWEST` without `AND kind = :kind`, anchored on `WHERE source_id = :source_id AND kind = :kind` with the `ORDER BY started_at DESC, id DESC` line after it: the clause alone occurs three times in the file. Expect the Postgres arm of `test_the_latest_run_is_scoped_by_kind_and_by_source` to fail on its `is None`.
8. `latest_incomplete_run` without `or newest.status is SyncRunStatus.COMPLETED`. Expect `test_a_completed_newest_run_offers_nothing_to_resume` to fail on both arms.
9. `_commit_unit` without `progress.units[unit.unit_key] = unit`. Expect the frames case to fail: every frame reads `seed`, `0`.
10. `_walk_plan` without `progress.units = {…}`. Expect the frames case to fail on `units_total`.
11. `_flush` calling `self._publish_progress(source, run)`, dropping `walk`. Expect the frames case to fail, every planned field `None`.
12. The duration recorded at every commit, outside `if done:`. Expect the duration case to fail on the counts `(2, …)` and `(4, …)`.
13. The claim instant taken for every unit when the stage starts: `claimed = {unit.unit_key: self._clock() for unit in units}`, with `claim()` no longer writing it. Expect the duration case to fail on `(2, 180.0)`.
14. `_last_sync` reading only `SyncRunKind.FULL`. Expect `test_status_of_a_single_walk_carries_no_plan` to fail on `('full', …)`.
15. `_last_sync`'s comprehension spelled `found = await runs.list_for_source(source_id, limit=1)`, the newest run of any kind, so a source with no runs still gets `None`. Expect the plan case to fail on `kind`, the watch run reported.
16. `_sync_status` without the plan line. Expect the CLI case to fail on its first assertion.
17. `_plan_line` printing `expected={walk.items_expected}`. Expect the `unknown` case to fail on `expected=None`.
18. Panel 11's target B grouped `by (source)`. Expect the walk-panel case to fail on its aggregations.
19. `sourceStatusHealthy` without `last_sync`. Expect `npm run verify` to fail in typecheck naming `last_sync`.
20. `_flush`'s guard back to Task 16's `if batch:`. Expect `test_a_unit_that_ends_on_a_batch_boundary_still_says_where_its_plan_stands` to fail on `[(2, 0), (4, 0)] == [(2, 0), (4, 0), (4, 1)]`.

- [ ] **Step 19: Commit**

```bash
git add src/usher/domain/sync.py src/usher/ports/repository/sync.py \
  src/usher/db/repositories/sync.py src/usher/services/reconcile.py src/usher/api/dto/source.py \
  src/usher/api/routers/sources.py src/usher/cli.py tests/fakes/source_adapter.py \
  tests/fakes/sync_run_repository.py tests/contract/sync_run_repository_contract.py \
  tests/unit/test_domain_sync.py tests/unit/test_services_reconcile.py tests/unit/test_cli.py \
  tests/unit/test_telemetry_metric_names.py tests/unit/test_dashboards.py tests/unit/test_alerts.py \
  tests/integration/test_admin_sources.py tests/integration/test_cli_pipeline.py \
  web/src/api/schema.d.ts web/src/test/fixtures/admin.ts dashboards/03-pipeline.json \
  dashboards/README.md docs/prd/03-sources-and-sync.md docs/prd/07-client-api.md \
  docs/prd/10-telemetry-and-dashboards.md CHANGELOG.md docs/guide/command-line.md
git commit -m "sync: say where a whole-library walk stands, in its frames, the status route and sync-status"
```

---

### Task 20: Phase 2's gate, status rows and PR

Every command runs in the Phase 2 worktree, `~/code/.worktrees/usher/fast-first-sync-units`.

**Files:**
- Modify: `docs/plans/progress.md`, `docs/prd/README.md` (this plan's status cells)

- [ ] **Step 1: The full Python gate**

Run: `uv sync --extra eval && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly`
Expected: every command exits 0, and `lint-imports` reports its contracts kept and none broken. Pytest reports no failures and no errors, with Docker up for `tests/integration/`. Record the pass count in the PR body.

- [ ] **Step 2: The console gate**

Run, from `web/`: `npm run verify && npm run e2e && npm run e2e:visual`
Expected: PASS. Three Phase 2 tasks touched `web/`: Tasks 15 and 16 the settings catalogue, and Task 19 the generated types and the admin fixtures. Each ran this gate before its own commit; this run is the whole branch's.

- [ ] **Step 3: The network guard**

Write `/var/tmp/usher-netguard/sitecustomize.py` as `.claude/rules/fixtures-and-fakes.md` gives it, if it is not there. Then run both halves in one environment:

```bash
PYTHONPATH=/var/tmp/usher-netguard uv run python -c "import socket; socket.getaddrinfo('api.themoviedb.org', 443)"
PYTHONPATH=/var/tmp/usher-netguard PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit 2>&1 | head -1
```

Expected: the probe raises `RuntimeError: NETWORK BLOCKED`, and the suite's first line is `[netguard] installed`.

- [ ] **Step 4: Status cells**

Task 9's row first. Its item walk on production passed on 2026-10-04 in 2.36 h against 6.00 h, so Task 10's fix round already flipped it: in `docs/plans/progress.md`, this plan's table, the Task 9 row reads `✅ passed: the first watch walk in 5 s, the item walk on production in 2.36 h`. Check that it still does.

The Tasks 10–20 row → `🔨 in review (PR #<n>)` until the PR merges. Task 21 flips it to `✅ landed (PR #<n>)`.

`docs/prd/README.md` holds one row for this plan, and says both in its one cell: `🔨 Phase 1 landed (PR #94) and passed: its first watch walk in 5 s, its item walk on production in 2.36 h. Phase 2 in review (PR #<n>)`. Task 10's fix round wrote everything before `Phase 2`, so only `in progress` changes, to `in review (PR #<n>)`.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin feat/fast-first-sync-units
```

Open the PR against `main` titled `A whole-library walk in resumable units, several pages at a time`. Follow the repository's PR template if there is one. The body states:
- what Phase 2 changes for an operator: the 2-, 10- and 75-minute targets it is built for, resume in place, the refusal, and `last_sync`;
- the two new settings and their defaults, as Task 10 fixed them;
- the gate results, with counts;
- the thirteen departures from the spec's letter;
- the migration. `m10g` is purely additive. The previous image refuses the newer schema, so a rollback goes through the pre-deploy backup;
- the deploy rule: never merge while an old-image full walk is running, and `docker ps --filter name=usher-prod-sync` is the check.

The body ends with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 6: Post Task 10's raw output as the PR's first comment**

All three files were written before any Phase 2 code existed. Check them again before they go anywhere public:

```bash
grep -c -E '[0-9a-f]{32}' /var/tmp/sync-speed/facts2.out /var/tmp/sync-speed/facts2b.out /var/tmp/sync-speed/measure_ingest.out
grep -n -E 'https?://|([0-9]{1,3}\.){3}[0-9]{1,3}' /var/tmp/sync-speed/facts2.out /var/tmp/sync-speed/facts2b.out /var/tmp/sync-speed/measure_ingest.out
sha256sum -c /var/tmp/sync-speed/facts2.out.sha256 /var/tmp/sync-speed/facts2b.out.sha256 /var/tmp/sync-speed/measure_ingest.out.sha256
```

Expected: every count is `0`, the second command prints nothing, and every checksum reads `OK`: Task 10 saved each when it wrote the file, and a checksum taken now would attest nothing. If any of these fails, post nothing: fix the output (or the script) and say why on the PR.

Then post one comment, each file raw in its own fence followed by its `sha256sum` line:

```bash
gh pr comment <n> --repo anirudhlath/usher --body-file /var/tmp/sync-speed/pr2-facts-comment.md
```

where `/var/tmp/sync-speed/pr2-facts-comment.md` holds the three fences and three sha lines, and nothing else.

- [ ] **Step 7: Commit the status cells, and ask**

```bash
git add docs/plans/progress.md docs/prd/README.md
git commit -m "docs(plan): fast first sync phase 1 passed, and phase 2 is in review"
git push
```

Merging deploys to production, and its migration runs with the app stopped. Before asking the owner, run `docker ps --filter name=usher-prod-sync --format '{{.Names}} {{.Status}}'`. If it prints a container, the ask says the merge has to wait for that walk to end. Ask the owner; do not merge.

---

### Task 21: Phase 2 live acceptance — owner-gated

Spec "Measurement and acceptance" 3 (Phase 2) and the Targets table. On Shared Emby at default settings:
- the watch state, with the series and next-up episodes behind it, within **2 minutes**;
- every movie and series within **10 minutes**;
- the whole library within **75 minutes**;
- a resume after the process is killed mid-EPISODES.

Every step that loads the source or touches production says so and waits for the owner. Panel 11's observation and PRD 10's backing statement go in a docs follow-up PR, which is never merged while a sync is running.

- [ ] **Step 1: The merge, on an idle production — with the owner's go-ahead**

```bash
docker ps --filter name=usher-prod-sync --format '{{.Names}} {{.Status}}'
grep -v '^{"text"' ~/code/usher-deploy/data/bulk/sync.log | tail -5
```

Expected: the first command prints nothing. If a walk is running, wait for it with one background wait, never by polling, before the owner merges PR 2. A walk started on the old image has no units and no heartbeat, so the first whole-library walk after the deploy would supersede it while the old process is still walking.

After the owner merges, the deploy runs on the self-hosted runner. Then confirm the new image is serving and its schema is in place:

```bash
gh run list --repo anirudhlath/usher --branch main --limit 3
curl -fsS http://localhost:8100/health/ready
(cd ~/code/usher-deploy && docker compose exec -T usher usher sync-status)
```

Expected: the deploy run succeeded, readiness answers 200, and `sync-status` prints without error. On this image it reads `sync_run_units` for every run it lists, so a missing `m10g` fails it.

- [ ] **Step 2: Write the timeline script**

`/var/tmp/sync-speed/timeline.py` (outside the repository; never committed). It reads Usher's own database every 15 s and prints a line whenever the newest full walk's state changes. It prints statuses, stages, counts and seconds only — no ids, names, tokens, user ids or hosts:

```python
# Throwaway, read-only: a whole-library walk's timeline, from Usher's own database.
# Prints statuses, stages, counts and seconds only -- no ids, names, tokens or hosts.
import asyncio
import sys
from datetime import UTC, datetime

from usher.cli import _session_for, build_pipeline, selected_sources
from usher.config import Settings
from usher.domain.sync import SyncRunKind, walk_progress

SINCE = datetime.fromisoformat(sys.argv[1])  # the walk is the first full run started after this


async def main() -> None:
    settings = Settings()
    last = None
    watched = None
    while True:
        async with _session_for(settings) as session:
            pipeline = build_pipeline(session, settings)
            [source] = await selected_sources(pipeline, "Shared Emby")
            run = await pipeline.runs.latest_run(source.id, SyncRunKind.FULL)
            if run is None or run.started_at < SINCE:
                await asyncio.sleep(15)
                continue
            walk = walk_progress(await pipeline.runs.units_for(run.id))
            watch = await pipeline.runs.latest_run(source.id, SyncRunKind.WATCH_STATE)
        # The first watch run seen finishing after the walk began is the seed hook's; keep it.
        if (
            watched is None
            and watch is not None
            and watch.started_at >= run.started_at
            and watch.finished_at is not None
        ):
            watched = round((watch.finished_at - run.started_at).total_seconds())
        state = (
            run.status.value,
            None if walk is None else (walk.stage.value, walk.units_done, walk.units_total),
            watched,
        )
        if state != last:
            elapsed = round((datetime.now(UTC) - run.started_at).total_seconds())
            print(
                f"t+{elapsed}s started={run.started_at.isoformat()} status={run.status.value} "
                f"stage={None if walk is None else walk.stage.value} "
                f"units={None if walk is None else f'{walk.units_done}/{walk.units_total}'} "
                f"seen={run.items_seen} first_watch_done_at=t+{watched}s",
                flush=True,
            )
            last = state
        if run.status.value != "running":
            if run.finished_at is not None:
                total = round((run.finished_at - run.started_at).total_seconds())
                print(f"finished at t+{total}s", flush=True)
            return
        await asyncio.sleep(15)


asyncio.run(main())
```

`first_watch_done_at` is when the first watch run to start after the walk began had finished, counted from the walk's start. That run is the one the seed's hook starts: the watch lane after the walk cannot start until the walk has ended. The script keeps the first value it sees, so the later run cannot overwrite it.

- [ ] **Step 3: The three targets — with the owner's go-ahead**

A full walk loads a server the operator may not administer, so ask first. Then start the timeline in the production stack, in the background:

```bash
SINCE=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
(cd ~/code/usher-deploy && docker compose cp /var/tmp/sync-speed/timeline.py usher:/tmp/timeline.py >/dev/null 2>&1 \
  && docker compose exec -T usher python /tmp/timeline.py "$SINCE" 2>&1 | grep --line-buffered -v '^{"text"') \
  | tee /var/tmp/sync-speed/timeline-1.out
```

Start the walk detached, as Task 9 Step 4 did:

```bash
cd ~/code/usher-deploy
[ -f data/bulk/sync.log ] && mv data/bulk/sync.log "data/bulk/sync-$(date -u +%Y%m%dT%H%M).log"
docker compose run -d --rm --no-deps --name usher-prod-sync usher \
  sh -c 'usher sync --source "Shared Emby" --kind full > /data/bulk/sync.log 2>&1; echo "exit=$?" >> /data/bulk/sync.log'
```

Wait for the timeline's `finished at` line with one background wait, never by polling.

Pass, read off `timeline-1.out`:
- `first_watch_done_at` is at most `t+120s`;
- the first line whose stage is `episodes`, or the `finished` line if no unit holds episodes, is at most `t+600s`, to the timeline's 15 s resolution;
- `status=completed`, and `finished at` is at most `t+4500s`.

A `failed` status fails all three, whatever the times say. If the sweep refused, `sync-status` shows the retraction error, and that is a finding to post, not a run to repeat.

Post on PR 2, raw: `timeline-1.out`, its `sha256sum`, and `sync-status`'s output. Before posting, run the same 32-hex and host checks as Task 20 Step 6 on the file.

- [ ] **Step 4: Panel 11 while a walk runs**

While Step 3's walk is in its titles or episodes stage, query both of panel 11's series from the telemetry stack:

```bash
docker exec observability-prometheus-1 promtool query instant http://localhost:9090 \
  'max by (source) (usher_source_listing_concurrency_ratio)'
docker exec observability-prometheus-1 promtool query instant http://localhost:9090 \
  'sum by (source, stage) (rate(usher_sync_unit_duration_seconds_sum[15m])) / sum by (source, stage) (rate(usher_sync_unit_duration_seconds_count[15m]))'
```

Then open Dashboard 3 in Grafana and look at panel 11 over the walk's window. Record what the limit read and whether it moved (a sawtooth, or flat at the walker count). Record each stage's mean unit duration.

If neither series exists, the walk's process is not exporting. Check whether `usher-prod-sync` sits on the `observability` network:

```bash
docker inspect usher-prod-sync --format '{{json .NetworkSettings.Networks}}' | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)))'
```

Record what you found. Either way, the panel stays unbacked until it has drawn a real walk.

- [ ] **Step 5: A resume after a kill mid-EPISODES — with the owner's go-ahead**

This is a second full walk, so ask again. Start a second timeline, with a fresh `SINCE`, in the background:

```bash
SINCE=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
(cd ~/code/usher-deploy && docker compose cp /var/tmp/sync-speed/timeline.py usher:/tmp/timeline.py >/dev/null 2>&1 \
  && docker compose exec -T usher python /tmp/timeline.py "$SINCE" 2>&1 | grep --line-buffered -v '^{"text"') \
  | tee /var/tmp/sync-speed/timeline-2.out
```

Its grep is `--line-buffered`, as Step 3's is. GNU grep, the one bash and fish reach, holds what it writes into a pipe until its buffer fills. Without the flag, the wait below fires only when the buffer fills or the walk ends, and the kill misses EPISODES. Then start the walk detached, as in Step 3.

Kill the walk once an episodes unit has completed and others have not: once a `status=running stage=episodes` line shows `units=` past the count on the first such line, which is the count when EPISODES began. Counting `stage=episodes` lines is not that test: the timeline also prints when `first_watch_done_at` changes, so a second such line can come with no unit completed. Wait for it in the background, not with a polling loop in the foreground:

```bash
timeout 7200 sh -c '
  f=/var/tmp/sync-speed/timeline-2.out
  until grep -q "status=running stage=episodes" "$f"; do sleep 5; done
  begun=$(grep -m1 "status=running stage=episodes" "$f" | grep -o "units=[0-9]*" | cut -d= -f2)
  now=$begun
  until [ "$now" -gt "$begun" ]; do
    sleep 5
    now=$(grep "status=running stage=episodes" "$f" | tail -1 | grep -o "units=[0-9]*" | cut -d= -f2)
  done
'
docker kill usher-prod-sync
```

`docker kill` sends SIGKILL, so nothing in the walk gets to close its run, and the run stays `running`. Then, at once, try the walk again in the foreground:

```bash
(cd ~/code/usher-deploy && docker compose exec -T usher usher sync --source "Shared Emby" --kind full; echo "exit=$?")
```

The killed run's heartbeat is still fresh, so this attempt must be refused: expect a non-zero `exit=` and a line saying the walk was refused. A refusal writes no run, so `timeline-2.out` goes on following the killed one.

Wait until the killed run's heartbeat is more than 10 minutes old: one background `sleep 660`, never a polling loop. Then start the walk detached once more, as in Step 3. `--rm` has already removed the killed container, so its name is free.

Pass, read off `timeline-2.out` and `sync-status`:
- every line carries the same `started=`, because the resume keeps the run's `started_at`;
- `units` never falls below the count printed before the kill;
- no line after the kill shows a stage before `episodes`, so no seed or title unit is walked again;
- `status=completed`;
- `sync-status` lists the walk as one completed `full` run, with no `superseded` row beside it.

Post on PR 2 (or its follow-up, if PR 2 has merged by then): `timeline-2.out`, its `sha256sum`, the refused attempt's exit code and log tail, and `sync-status`'s output, all raw and checked as in Step 3.

- [ ] **Step 6: Record panel 11, and close Phase 2 — a docs follow-up PR**

If any target missed, or the resume failed, post the numbers and stop. Nothing below is written over a failed acceptance.

Otherwise, on a branch from the updated `main`:

```bash
git -C ~/code/usher fetch origin main
git -C ~/code/usher worktree add ~/code/.worktrees/usher/fast-first-sync-accept -b docs/fast-first-sync-accept origin/main
cd ~/code/.worktrees/usher/fast-first-sync-accept && uv sync --extra eval
```

The branch makes three changes:

- `dashboards/README.md`, "### 11 — Whole-library walks: listing limit and mean unit duration": the `**Not yet observed.**` paragraph is replaced by what Step 4 recorded. Use the form the other panel sections use: the frames and query times, the limit's range and shape, and each stage's mean unit duration with the walk it came from. If Step 4 found no series, write that, and leave PRD 10's statement as it is.
- `docs/prd/10-telemetry-and-dashboards.md`, "### 3 — Pipeline": the backing paragraph becomes the one below, keeping its ⚠️ sentence:

```markdown
✅ **Every panel here is backed by real data as of M9, and the whole-library
walk panel as of the fast first sync's acceptance walk.** ⚠️ A panel that drains
the whole unmatched queue should page with the keyset cursor; the `OFFSET` form
is quadratic in queue depth.
```

- `docs/plans/progress.md`: the Tasks 10–20 row → `✅ landed (PR #<n>)` and the Task 21 row → `✅ passed`, each carrying the three times Step 3 measured. `docs/prd/README.md` holds one row for this plan, and its cell becomes one sentence carrying the same three times: `✅ landed (PR #<p1>, PR #<n>), and its acceptance walk passed: the watch state in <w> s, every movie and series in <m> s, the whole library in <l> s`, with `<p1>` Phase 1's PR number, already in the cell.

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_prd_10_panel_backing.py tests/unit/test_dashboards.py tests/unit/test_docs_pointers.py tests/unit/test_docs_currency.py`
Expected: PASS.

```bash
git add dashboards/README.md docs/prd/10-telemetry-and-dashboards.md docs/plans/progress.md docs/prd/README.md
git commit -m "docs: record the whole-library walk panel's first real walk, and close fast first sync"
git push -u origin docs/fast-first-sync-accept
```

Open the PR against `main` titled `Fast first sync passed its acceptance walk`, its body linking PR 2's acceptance comments and ending with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Merging it deploys, and a deploy restarts the container. Run `docker ps --filter name=usher-prod-sync` first; if a walk is running, the ask says the merge waits for it. Ask the owner; do not merge.
