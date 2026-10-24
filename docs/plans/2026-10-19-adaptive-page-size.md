# Adaptive Page Size Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Emby adapter's listing pages start at `USHER_SOURCE_PAGE_SIZE` and climb to the size that hands the writer the most items a second, up to `USHER_SOURCE_PAGE_SIZE_MAX`. An outage on a listing page halves the size.

**Architecture:**
- **The sizer.** A new `PageSizer` in `adapters/emby/sizer.py` holds one adapter's size, its trial and its measurement. It is built and shared the way `ListingLimit` is.
- **The adapter.** `_pages` asks the sizer for each request's size, tells the window the size each page was asked at, and reports clean hand-offs timed by the adapter's clock. `_page` reports outages on listing pages.
- **The window.** `OffsetWindow` loses `limit` and judges each page short against its own `asked`.
- **Wiring and visibility.** Settings, the factory and the composition root carry the ceiling. A gauge, INFO lines and a Dashboard 3 panel show the size.

**Tech Stack:** Python 3.13, httpx, OpenTelemetry metrics, loguru, pydantic-settings, pytest (strict mypy over `tests/` too), Grafana JSON, and the console's TypeScript settings catalogue.

**Spec:** `docs/specs/2026-10-19-adaptive-page-size-design.md` (commit 989774e2). Read it before any task. It is a point-in-time record, so never edit it.

## Decisions this plan makes

1. **The gain test is `rate >= GAIN * previous`. Its boundary case uses rates that are exact in binary.**
   - On the boundary: four hand-offs of (1,000 items, 1.0 s) give 1,000.0 items/s, then four of (2,200 items, 2.0 s) give 1,100.0. The ratio is exactly 1.10, so it climbs.
   - The neighbour: four of (2,199 items, 2.0 s) give 1,099.5, so it settles.
   - `1.10 * 1000.0 == 1100.0` holds in IEEE doubles; this was checked. Do not use 1,500 → 1,650 for the boundary: `1.1 * 1500.0` rounds up past 1,650.
2. **A hand-off's items are `len(page.fresh)`.** These are the new items handed over; the reach-back is not counted.
3. **A hand-off reports the trial of the previous hand-off's request,** the request for the page this listing handed off last. Trials only grow, so it equals the sizer's trial only when both pages were requested in the current trial.
4. **`_pages` reports a hand-off only when all of these hold:**
   - its listing has handed off a page before;
   - its page did not end the listing;
   - no shifted page came after that previous hand-off.

   A shifted page clears the previous hand-off. The ending page is never reported. The time is `self._clock()`, read just before the `yield`, after the read-ahead request is issued.
5. **The sizer's rules.** Task 1 implements exactly these.
   1. **Start.** The sizer starts at `floor`, climbing, at trial 0, with no previous rate.
   2. **`handed_off(items, seconds, *, trial, concurrency)`**, checked in this order:
      1. If `trial != self.trial`, ignore it.
      2. If `seconds <= 0`, ignore it.
      3. If the trial has a concurrency and this one differs, start a new trial at this concurrency and forget the previous rate. Do not count the hand-off.
      4. Otherwise count it. The trial takes this concurrency if it had none, and the hand-off joins the last `TRIAL_HANDOFFS` kept (a `deque`).
      5. While climbing, at the `TRIAL_HANDOFFS`th counted hand-off, **decide** on their rate, Σitems / Σseconds.
      6. While settled below the ceiling, at the `REPROBE_AFTER`th counted hand-off, **climb**, taking the kept hand-offs' rate as the previous rate. This logs the one-rate line.
   3. **Decide(rate):**
      - With no previous rate, climb if below the ceiling (one-rate line); otherwise settle.
      - If `rate >= GAIN * previous`, climb if below the ceiling (two-rate line); otherwise settle where it is, with no line.
      - Otherwise, settle at the size it climbed from (settle line).
   4. **Climb:** remember the size and the rate, set size = `min(2 * size, ceiling)`, keep climbing, and start a new trial.
   5. **Settle:** become settled and forget the previous rate. If the size changed, start a new trial. A settle at the same size happens only at the ceiling, where nothing re-probes, so its trial simply goes on.
   6. **`failed(trial)`:** ignore it unless `trial == self.trial`. Otherwise set size = `max(floor, size // 2)`, logging the drop line only if the size changed. Become settled, forget the previous rate, and always start a new trial.
   7. **A new trial** increments `trial`, clears the kept hand-offs and the count, and clears the trial's concurrency. A trial started by a concurrency change takes that concurrency instead.
6. **A hand-off's concurrency is `self._listing_limit.limit`, read at the hand-off.**
7. **The gauge.**
   - `usher.source.listing.page_size` uses `unit="1"`, the repo's spelling for a count, as `usher.source.listing.concurrency` does. Prometheus therefore names it `usher_source_listing_page_size_ratio`.
   - It is declared in `sizer.py` on the meter `usher.source.emby`.
   - Only `PageSizer.report()` writes it. `_pages` calls `report()` each time a listing page's body arrives.
8. **The INFO lines.** The copy is in Global Constraints.
   - The two-rate climb line is the spec's example.
   - A climb that compares nothing names one rate. That covers the first climb from the floor, the first after a concurrency change, and a re-probe.
   - Holding at the ceiling, or a failure at the floor, changes nothing and logs nothing.
9. **The adapter keeps no `_page_size`.** It builds `self._sizer = PageSizer(page_size, page_size if page_size_max is None else page_size_max, source=source.name)`. A ceiling below the floor raises the sizer's `ValueError` at construction.
10. **Tests set a climbed size through the sizer's public methods.** They use a shared helper, `climb(sizer, size)`. Adapter cases reach `adapter._sizer`, as the factory tests already reach `adapter._page_size`.
    - **Every no-change assertion starts from a climbed size whose half is not the floor.** At the floor a halving clamps to the floor, so "unchanged" proves nothing.
11. **Time in tests.**
    - A case that needs the size to move uses `ticking()`, a clock that moves one second per call. With tiny pages, each doubling then doubles the rate.
    - A case that must hold the size still uses a frozen clock (`_Waits`, or `lambda: 1_000.0`). The sizer ignores its zero intervals.
12. **Panel 13 sits beside panel 11.**
    - Panel 11 narrows to `w=12`. Panel 13 takes `x=12, y=32, w=12, h=8`.
    - Its title must not start "Whole-library walks": `test_the_walk_panel_draws_…` requires exactly one panel that does.
13. **`ConfiguredSourceAdapterFactory`'s own default is `page_size_max=10_000`.** It is pinned equal to `Settings.model_fields["source_page_size_max"].default`, the way `page_size` is pinned today.
14. **Production pages at the same size until Task 5.** The adapter's own default does not adapt, so nothing the factory builds adapts until it passes the setting; from Task 3 the gauge reads the floor. The branch merges as one PR, so no intermediate commit deploys.

## Global Constraints

**Values**

- **Constants**, module level in `sizer.py`: `TRIAL_HANDOFFS = 4`, `GAIN = 1.10`, `REPROBE_AFTER = 100`.
- **The gauge:**
  - name `usher.source.listing.page_size`, `unit="1"`;
  - description `"Items one source's adapter asks for in a listing page"`;
  - attributes `{"source": <the source's name>}`;
  - meter `usher.source.emby`.
- **The INFO lines**, through loguru. `{source}` is the source's name, sizes are formatted `:,` and rates `:,.0f`:
  - `"{source}'s listing pages grow from {size:,} to {to:,} items ({before:,.0f} → {rate:,.0f} items/s a walker)"`
  - `"{source}'s listing pages grow from {size:,} to {to:,} items ({rate:,.0f} items/s a walker)"`
  - `"{source}'s listing pages settle at {to:,} items: {size:,} gave {rate:,.0f} items/s against {before:,.0f}"`
  - `"{source}'s listing pages drop from {size:,} to {to:,} items after a failed listing"`
- **`PageSizer` refuses** with:
  - `ValueError(f"a page sizer needs a floor of at least one item, not {floor}")`;
  - `ValueError(f"a page sizer's ceiling {ceiling} is below its floor {floor}")`.
- **Settings:**
  - `source_page_size: int = Field(default=1000, ge=1, le=50_000)`;
  - `source_page_size_max: int = Field(default=10_000, ge=1, le=50_000)`;
  - a validator that raises `ValueError(f"USHER_SOURCE_PAGE_SIZE_MAX ({ceiling}) must be at least USHER_SOURCE_PAGE_SIZE ({floor}); set them equal to turn adaptation off")`.
- **Panel 13:** id 13, `timeseries`, title `Listing page size, by source`, expr `max by (source) (usher_source_listing_page_size_ratio)`, legend `{{source}} · page size`.

**Scope and safety**

- Never undo with `git checkout`, `restore`, `stash` or `reset`; `reset --soft` is fine. Every plant gets a `cp` backup in `/var/tmp/slice-2/`, and each restore is checked with `diff`.
- Nothing in this plan talks to a real server. No credential, token, user id or host enters the repo.
- **Merging deploys to production.** Merge only on the owner's say-so, and only with no walk running (`docker ps --filter name=usher-prod-sync`). Shared Emby is not the household's library; nothing here writes to it.

**Writing rules**

- The `guard-prose` hook caps Python prose:
  - a module docstring at 3 lines;
  - a def docstring at 20 lines;
  - a comment block at 5 lines.

  No dates, hex, "measured", `ADR-` or 🔴 in Python comments.
- A PRD edit rides the commit whose behaviour it describes. The PRD states behaviour, not reasoning.
- Never edit an earlier spec or plan.
- Commit messages read `area: sentence`, with no trailers and no model identifiers.

**Commands**

- **Iterate:** `PYTHONDONTWRITEBYTECODE=1 uv run pytest <file> -p no:randomly`
- **Gate,** once per task, before its commit:
  ```
  uv sync --extra eval && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly
  ```
- **Web gate,** Task 5 only, from `web/`: `npm run verify`. Run `npm ci` first if `node_modules` is missing.
- **A plant's failure:** `uv run pytest <case> -p no:randomly 2>&1 | grep -E '^E .*<the assertion or its message>'`

## Review Focus

1. **A server that caps `Limit` below the climbed size,** such as a proxy or a server setting that serves at most N items. Every item should still be read once. The size should settle back to what the server serves, since bigger asks gain nothing. Task 3: `test_a_server_capping_its_pages_reads_every_item_and_settles_where_it_serves`.
2. **A page asked for before a climb that arrives after it,** once a drop has left the longest page served larger than that page and the walk is past a stale total. The page should be judged against the size it was asked at, so a library that grew mid-walk loses nothing. Task 3: `test_a_page_asked_before_a_climb_is_judged_by_its_own_size`.
3. **Which hand-offs `_pages` reports.** Never the first page, never the ending page, never the page after a moved one, and always with the earlier request's trial. Task 3: `test_pages_reports_hand_offs_with_the_earlier_requests_trial_and_never_the_first_or_last_page` and `test_the_hand_off_after_a_moved_page_is_not_reported`.
4. **Two listings paging at once,** the production shape with four walkers. Each hand-off should be timed against its own listing's previous hand-off. Task 3: `test_each_listing_times_its_own_hand_offs`.
5. **Pages larger than `USHER_SYNC_BATCH_SIZE`.** A commit should hold the whole page, and the walk should commit every item. Task 4: the whole-library case runs with `batch_size=4` against pages of up to 16.

## Residuals

- **A 429 at listing concurrency 1 leaves the trial running,** so one counted interval can include its `Retry-After` wait.
  - Above 1 this cannot happen: `ListingLimit.failed()` drops the limit to 1, so the next hand-off starts a new trial.
  - Accepted: an average of four absorbs one such interval, and the next decision is wrong by at most one step.
- **The spec's caveats stand:**
  - the per-walker rate rises as a walk winds down;
  - a page too big for the server fails its retries at the same size;
  - one failure resets every walker's measurement;
  - every new adapter starts at the floor.
- **Peak memory and the walk's rate become known only in the acceptance walk** (Task 8).

## File map

| File | Task | What changes |
|---|---|---|
| `src/usher/adapters/emby/sizer.py` | 1 | new: `PageSizer`, its constants, the gauge |
| `tests/unit/test_adapters_emby_sizer.py` | 1 | new: the sizer's cases |
| `tests/fakes/emby_harness.py` | 1 | `climb(sizer, size)`, `ticking()` |
| `docs/prd/10-telemetry-and-dashboards.md` | 1, 5, 6 | the catalogue row and the 46-row count (1); the gauge's paragraph (5); Dashboard 3's list (6) |
| `tests/unit/test_telemetry_metric_names.py` | 1 | 45 → 46 |
| `src/usher/adapters/emby/paging.py` | 2 | `OffsetWindow` loses `limit`; `receive(..., *, asked)` |
| `tests/unit/test_adapters_emby_paging.py` | 2 | every window and `receive` call; two new cases |
| `src/usher/adapters/emby/adapter.py` | 2, 3 | `asked=` (2); the sizer (3) |
| `.claude/rules/emby-push-and-ingest.md` | 2 | the paging bullet |
| `tests/unit/test_adapters_emby_adapter.py` | 3 | helpers take `page_size_max` and `clock`; the adapter cases; a docstring naming `self._page_size` |
| `tests/unit/test_adapters_factory.py` | 3, 5 | its two `adapter._page_size` reads become `adapter._sizer.floor` (3); the ceiling (5) |
| `src/usher/services/push.py` | 3 | its docstring's page size |
| `tests/unit/test_services_reconcile.py` | 4 | the two walk cases |
| `src/usher/config.py` | 5 | both bounds and the validator |
| `src/usher/adapters/factory.py`, `src/usher/composition.py` | 5 | `page_size_max` |
| `tests/unit/test_config.py`, `tests/unit/test_composition.py` | 5 | the settings and composition-root cases |
| `web/src/features/operator/Config.settings.ts` | 5 | the new row; both `about` texts |
| `.env.example`, `docs/guide/configuration.md`, `docs/prd/03-sources-and-sync.md`, `CHANGELOG.md` | 5 | the settings and the behaviour |
| `dashboards/03-pipeline.json`, `dashboards/README.md` | 6 | panel 13; panel 11 narrows |
| `tests/unit/test_dashboards.py`, `tests/unit/test_alerts.py` | 6 | thirteen panels; the new panel's case |
| `docs/plans/progress.md`, `docs/prd/README.md` | 7, 8 | the status rows |

---

### Task 1: The page sizer

**Files:**
- Create: `src/usher/adapters/emby/sizer.py`, `tests/unit/test_adapters_emby_sizer.py`
- Modify: `tests/fakes/emby_harness.py`, `docs/prd/10-telemetry-and-dashboards.md:108-109,138`, `tests/unit/test_telemetry_metric_names.py:185`

**Interfaces:**
- Consumes: nothing new. The module mirrors `src/usher/adapters/emby/limit.py`: a module-level meter and gauge, and `self._labels = {"source": source}`.
- Produces:
  ```python
  # src/usher/adapters/emby/sizer.py
  TRIAL_HANDOFFS = 4
  GAIN = 1.10
  REPROBE_AFTER = 100

  class PageSizer:
      def __init__(self, floor: int, ceiling: int, *, source: str) -> None: ...
      @property
      def size(self) -> int: ...       # what the next listing request asks for
      @property
      def trial(self) -> int: ...      # stamped on each listing request
      @property
      def floor(self) -> int: ...
      @property
      def ceiling(self) -> int: ...
      def handed_off(self, items: int, seconds: float, *, trial: int, concurrency: int) -> None: ...
      def failed(self, trial: int) -> None: ...
      def report(self) -> None: ...    # writes the gauge; nothing else does

  # tests/fakes/emby_harness.py
  def climb(sizer: PageSizer, size: int) -> None: ...         # see Step 1
  def ticking(start: float = 1_000.0) -> Callable[[], float]: ...   # +1.0 s per call
  ```

- [ ] **Step 1: Write the harness helpers and the failing cases**

  **The helpers,** in `tests/fakes/emby_harness.py`:
  - `climb` feeds `TRIAL_HANDOFFS` hand-offs of `(sizer.size items, 1.0 s)` at the sizer's current trial and concurrency 1, until the size reaches `size`. Since each size's rate is its size, every doubling doubles it.
  - `climb` asserts `size <= sizer.ceiling` first, and `sizer.size == size` at the end. A size off the climb's path then fails loudly.
  - `ticking` wraps `itertools.count(start, 1.0)`.

  **The cases,** in `tests/unit/test_adapters_emby_sizer.py`. A local `_measure(sizer, items, seconds, *, times=TRIAL_HANDOFFS, concurrency=4)` feeds `times` hand-offs at `sizer.trial`. Every case uses floor 1,000, ceiling 10,000 and source `"Shared Emby"` unless it says otherwise.

  ```python
  def test_a_sizer_starts_at_its_floor() -> None:
      assert PageSizer(1_000, 10_000, source="Shared Emby").size == 1_000

  def test_it_climbs_while_each_doubling_gains_and_settles_one_step_back() -> None:
      # 1,000/s at 1,000; 1,500/s at 2,000; 1,600/s at 4,000, short of 1,650
      ...
      assert sizes == [2_000, 4_000, 2_000]

  @pytest.mark.parametrize(("items", "after"), [(2_200, 4_000), (2_199, 1_000)])
  def test_a_gain_of_exactly_ten_percent_climbs(items: int, after: int) -> None:
      # _measure(sizer, 1_000, 1.0), then _measure(sizer, items, 2.0): 1,100.0 or 1,099.5 items/s
      assert sizer.size == after

  def test_the_last_climb_is_clamped_to_the_ceiling_and_holding_there_logs_nothing() -> None:
      # six measurements, each at a rate equal to the size; INFO lines captured
      assert sizes == [2_000, 4_000, 8_000, 10_000, 10_000, 10_000]
      assert lines[-1] == "Shared Emby's listing pages grow from 8,000 to 10,000 items (4,000 → 8,000 items/s a walker)"
      assert len(lines) == 4

  def test_a_sizer_whose_floor_is_its_ceiling_never_moves() -> None:
      # PageSizer(1_000, 1_000): REPROBE_AFTER + TRIAL_HANDOFFS hand-offs, failed(), as many again
      assert sizer.size == 1_000

  def test_it_reprobes_after_a_hundred_clean_hand_offs_and_climbs_again_if_the_doubling_gains() -> None:
      # settled at 2,000 as in the climb case; 99 hand-offs at 1,500/s, then 1 more
      assert sizes == [2_000, 4_000]                # after the 99th, after the 100th
      _measure(sizer, 1_800, 1.0)                  # 1.2× the re-probe's 1,500/s
      assert sizer.size == 8_000

  def test_a_reprobe_rates_its_size_by_the_last_four_hand_offs() -> None:
      # settled at 2,000; 96 hand-offs at 100/s, then 4 at 1,500/s: climbs to 4,000
      _measure(sizer, 1_600, 1.0)                  # 1.07× the last four; 10× all hundred's
      assert sizer.size == 2_000

  def test_failed_halves_it_never_below_the_floor_and_nothing_climbs_until_the_next_reprobe() -> None:
      # climb(sizer, 8_000); four failed(sizer.trial)
      assert sizes == [4_000, 2_000, 1_000, 1_000]
      # 99 hand-offs at 50,000/s: still 1,000; one more: 2,000

  def test_a_failure_of_a_request_made_before_the_last_halving_does_not_halve_again() -> None:
      # climb(sizer, 8_000); stale = sizer.trial; failed(stale) twice
      assert (sizer.size, sizer.trial) == (4_000, stale + 1)

  def test_a_hand_off_from_an_earlier_trial_is_ignored() -> None:
      # earlier = sizer.trial; _measure(1_000): climbs to 2,000
      # 4 hand-offs of (9_000, 1.0) at trial=earlier; then _measure(1_050)
      assert sizer.size == 1_000

  def test_a_hand_off_at_another_concurrency_starts_a_new_trial_and_is_not_counted() -> None:
      # 3 hand-offs at concurrency 4; then one at 3
      assert (sizer.trial, sizer.size) == (first + 1, 1_000)
      # 3 more at 3: still 1,000 ("the switching hand-off was counted"); 1 more: 2,000

  @pytest.mark.parametrize("seconds", [0.0, -1.0])
  def test_a_non_positive_interval_is_ignored(seconds: float) -> None:
      # 3 good, then one at `seconds`: still 1,000; one more good: 2,000

  def test_each_change_logs_one_line_naming_its_sizes_and_rates() -> None:
      # _measure 1,000, then 1,500, then 1,600; failed() twice; INFO captured as in
      # test_services_reconcile.py (logger.add(lines.append, level="INFO", format="{message}"))
      assert [line.rstrip("\n") for line in lines] == [
          "Shared Emby's listing pages grow from 1,000 to 2,000 items (1,000 items/s a walker)",
          "Shared Emby's listing pages grow from 2,000 to 4,000 items (1,000 → 1,500 items/s a walker)",
          "Shared Emby's listing pages settle at 2,000 items: 4,000 gave 1,600 items/s against 1,500",
          "Shared Emby's listing pages drop from 2,000 to 1,000 items after a failed listing",
      ]

  def test_only_report_writes_the_gauge_labelled_by_source() -> None:
      # InMemoryMetricReader as test_adapters_emby_limit.py sets it; _sizes(reader) reads
      # `usher.source.listing.page_size` as (value, attributes), the way _caps reads its gauge
      assert _sizes(reader) == []                  # after construction
      climb(sizer, 4_000)
      assert _sizes(reader) == []                  # climbing writes nothing
      sizer.report()
      assert _sizes(reader) == [(4_000, {"source": "Shared Emby"})]

  @pytest.mark.parametrize(("floor", "ceiling", "message"), [
      (0, 10, "a page sizer needs a floor of at least one item, not 0"),
      (1_000, 999, "a page sizer's ceiling 999 is below its floor 1000"),
  ])
  def test_a_sizer_refuses_a_floor_below_one_or_a_ceiling_below_its_floor(...) -> None: ...
  ```

- [ ] **Step 2: Run the cases and see them fail**

  Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_sizer.py -p no:randomly`

  Expected: a collection error, `ModuleNotFoundError: No module named 'usher.adapters.emby.sizer'`.

- [ ] **Step 3: Implement `PageSizer` in `src/usher/adapters/emby/sizer.py`**

  Implement Decision 5's rules with Global Constraints' constants, copy and gauge. Log through `from loguru import logger`, as `adapter.py` does.

- [ ] **Step 4: Run the cases and see them pass**

  Same command. Expected: every case passes.

- [ ] **Step 5: Catalogue the gauge**

  1. Run `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_telemetry_metric_names.py -p no:randomly`. It fails at `declared == set(catalogue) - {_INHERITED}`, naming `usher.source.listing.page_size`.
  2. In PRD 10, add the row `` | `usher.source.listing.page_size` | gauge | source | ✅ adaptive page size | `` below the concurrency row.
  3. Change "45 rows: 44 instruments" to "46 rows: 45 instruments".
  4. In `test_telemetry_metric_names.py:185`, change 45 to 46.
  5. Run again. Expected: it passes.

- [ ] **Step 6: Plant four defects, one at a time, and see each fail its own case**

  Back up `sizer.py` first, and restore and `diff` after each plant.

  | Plant | The case that fails |
  |---|---|
  | `>=` becomes `>` in the gain test | the `2_200` arm of the exactly-10% case |
  | a re-probe rates all its hand-offs, not the last four | the last-four case |
  | `failed()` skips its trial check | the stale-failure case |
  | the hand-off that switches concurrency is counted | the concurrency case |

- [ ] **Step 7: Run the gate and commit**

  ```bash
  git add src/usher/adapters/emby/sizer.py tests/unit/test_adapters_emby_sizer.py tests/fakes/emby_harness.py docs/prd/10-telemetry-and-dashboards.md tests/unit/test_telemetry_metric_names.py
  git commit -m "emby: a page sizer that climbs to the fastest listing page and halves on an outage"
  ```

### Task 2: The window judges each page by the size it was asked at

**Files:**
- Modify:
  - `src/usher/adapters/emby/paging.py`: `OffsetWindow.__init__`, its docstring, and the `short` line in `receive` with the comment above it;
  - `src/usher/adapters/emby/adapter.py`: the window and the two `self._read` calls in `_pages`, at 423–458;
  - `tests/unit/test_adapters_emby_paging.py`;
  - `.claude/rules/emby-push-and-ingest.md:177-182`.

**Interfaces:**
- Produces:
  - `OffsetWindow(*, start: int, keyed: bool = False, after: int | None = None, until: int | None = None)`;
  - `OffsetWindow.receive(self, entries: list[Any], total: object, *, asked: int) -> Page`.

  `OffsetWindow.limit` no longer exists. `grep -rn 'OffsetWindow(' src tests` must find only `adapter.py` and the paging tests.
- The rule: `short = len(entries) < (min(asked, self._longest) if self._longest else asked)`.

- [ ] **Step 1: Write the two failing cases in `tests/unit/test_adapters_emby_paging.py`**

  The pages are built with the file's `_entries(*ids)`. Each loop asks `window.receive(_entries(*library[window.start : window.start + served]), total, asked=asked)`, collects `page.fresh` ids, stops when `page.ended`, and otherwise calls `window.advance()`.

  ```python
  def test_after_a_size_drop_a_full_page_is_not_short_and_the_walk_reads_past_a_stale_total() -> None:
      # 450 ids; the first page counts 300, so the library grew; pages asked at 200, 200, 100, 100, 100
      assert page.ended
      assert handed == library      # the old rule ends on the third page, at i399

  def test_a_server_serving_fewer_than_asked_at_every_size_reads_past_a_stale_total() -> None:
      # 400 ids, a first-page total of 300, a server serving at most 150; asked 200, 200, 400, 400
      assert all(n < a for n, a in zip(served, asked_at, strict=True)), "the premise: every page held fewer than asked"
      assert handed == library
      assert served[-1] < 150, "the walk ended on its partial last page"
  ```

  The second case also holds under today's rule. It is there to fail a rule that compares against `asked` alone.

- [ ] **Step 2: Move every existing case to `asked=`, and run the file**

  - Drop `limit=N` from each `OffsetWindow(...)`, `_moved_window` included.
  - Give every `receive` on that window `asked=N`, the same N.
  - A careful script is fine, but read the whole diff afterwards.

  Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_paging.py -p no:randomly`

  Expected: every case fails with a `TypeError`, since the window still requires `limit` and refuses `asked`.

- [ ] **Step 3: Change the signature alone, and see only the size-drop case fail**

  Drop `limit` from `OffsetWindow.__init__` and give `receive` its keyword `asked`. Keep today's comparison, with `asked` in `limit`'s place: `short = len(entries) < (self._longest or asked)`. Run the same command.

  Expected: only `test_after_a_size_drop_…` fails, at its `handed == library` line.

- [ ] **Step 4: Implement the rule, and move the adapter onto it**

  - **The rule:** the line in Interfaces above.
  - **Rewrite the docstring's short sentence:** "A page is short when it holds fewer entries than it was asked for, or than the longest page served before it if that is fewer."
  - **Rewrite the comment above `short`:** against the size it was asked at, or the longest page served if that is fewer. A capped server serves nothing longer, and its full pages past a stale total are a library that grew.
  - **In `_pages`,** build `OffsetWindow(start=start_index, keyed=keyed, after=..., until=until)`, pass `asked=self._page_size` to `receive`, and pass `self._page_size` as both reads' limit.

- [ ] **Step 5: Run the paging, adapter and contract files and see them pass**

  Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_paging.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_contract.py -p no:randomly`

  Expected: all pass. If an existing paging case changes outcome, it served pages longer than its limit. Read it and report it; don't adjust it to pass.

- [ ] **Step 6: Plant `short = len(entries) < asked` and see the capped-server case fail**

  Restore and `diff` afterwards.

- [ ] **Step 7: Update the paging bullet in the rules file**

  In `.claude/rules/emby-push-and-ingest.md:177`, the bullet becomes: "**A page's reach-back is clamped to half the page before it**, a page is short against the size it was asked at or the longest page served if that is fewer, a walk ends on a short page that brought nothing new, and **a page that moved is never read** — …". The rest is unchanged.

- [ ] **Step 8: Run the gate and commit**

  ```bash
  git add src/usher/adapters/emby/paging.py src/usher/adapters/emby/adapter.py tests/unit/test_adapters_emby_paging.py .claude/rules/emby-push-and-ingest.md
  git commit -m "emby: judge each listing page short against the size it was asked at"
  ```

### Task 3: The adapter pages at the sizer's size

**Files:**
- Modify:
  - `src/usher/adapters/emby/adapter.py`: the constructor at 234–289, `_pages` at 401–479, `_read` at 481–491 and `_page` at 515–574;
  - `src/usher/services/push.py:46`;
  - `tests/unit/test_adapters_emby_adapter.py`: the helpers at 174–262, the docstring at 907 that names `self._page_size`, and the new cases;
  - `tests/unit/test_adapters_factory.py:75,103`: both `adapter._page_size` reads.

**Interfaces:**
- Consumes:
  - `PageSizer`, `climb` and `ticking` (Task 1);
  - `OffsetWindow.receive(..., asked=)` (Task 2).
- Produces:
  - `EmbyAdapter.__init__(..., page_size: int = 1000, page_size_max: int | None = None, ...)`, which keeps `self._sizer: PageSizer` and no `self._page_size`;
  - `_read(self, path: str, query: Mapping[str, str], start: int, limit: int, *, count: bool, trial: int) -> asyncio.Task[dict[str, Any]]`;
  - `_page(self, path: str, params: Mapping[str, str], start: int, *, op: str = "list", trial: int | None = None) -> dict[str, Any]`.
- Test helpers:
  - `_recorded`, `_adapter` and `_on` gain `page_size_max: int | None = None` and `clock: Callable[[], float] | None = None`. In `_on`, `clock` overrides `waits`' clock.
  - New: `_failing(server, *, at, fail, seen)` returns a handler that records every request in `seen`, answers the `at`-th `/Items` request with `fail()` once, and passes every other request to `server.handle`.
  - New: `_timed_out()` raises `httpx.ReadTimeout`.

- [ ] **Step 1: Write the failing cases**

  The shared fixtures:
  - films are seeded with `server.add_item`;
  - `limits = [int(r.url.params["Limit"]) for r in _listings(seen)]`;
  - `starts` is the same list read from `StartIndex`;
  - "read every item once" means the external ids of `list_items()` equal the library's, with no repeats.

  ```python
  def test_a_listing_whose_size_climbs_partway_reads_every_item_once() -> None:
      # 60 films; page_size=2, page_size_max=8, clock=ticking()
      assert limits[:12] == [2] * 6 + [4] * 6 and set(limits[12:]) == {8}
      # and every item read once

  def test_without_a_ceiling_every_request_asks_for_page_size() -> None:
      # the same fixture with page_size_max omitted
      assert set(limits) == {2}

  @pytest.mark.parametrize(("fail", "halves"), [
      (_bad_gateway, True), (_status(408), True), (_refused, True), (_timed_out, True),
      (_status(429, {"Retry-After": "1"}), False),
  ])
  def test_a_listing_failure_halves_the_size_only_when_it_is_an_outage(fail, halves) -> None:
      # 40 films; _failing(..., at=2, fail=fail); _on(..., waits=_Waits(), page_size=2, page_size_max=16)
      # climb(adapter._sizer, 8) first, so a halving cannot be mistaken for the floor
      assert limits[:4] == ([8, 8, 8, 4] if halves else [8, 8, 8, 8])
      assert starts[1] == starts[2], "the retry asked for the same page"
      # and every item read once

  def test_an_outage_on_an_ids_read_leaves_the_size_alone() -> None:
      # climb(adapter._sizer, 8); confirm_gone([<one film>]) against _failing(at=1, fail=_bad_gateway)
      assert (adapter._sizer.size, adapter._sizer.trial) == (8, trial_before)

  def test_a_listing_whose_size_drops_after_an_outage_reads_every_item_once_through_deletions() -> None:
      # 40 films; climb to 8; the 2nd listing request fails once with _bad_gateway; and, the way
      # the existing reach-back cases do, one film deleted behind the cursor and two added at
      # the end once page 3 is served
      # every surviving and inserted film read once
      assert 4 in limits, "the premise: the size dropped"

  def test_a_server_capping_its_pages_reads_every_item_and_settles_where_it_serves() -> None:    # Review Focus 1
      # 80 films; server.max_limit = 4; page_size=2, page_size_max=16, clock=ticking()
      assert limits[:18] == [2] * 6 + [4] * 6 + [8] * 6 and set(limits[18:]) == {4}
      # and every item read once

  def test_a_page_asked_before_a_climb_is_judged_by_its_own_size() -> None:      # Review Focus 2
      # 10 films; 790 more added (later DateCreated) once the first listing request is served,
      # so the first page's total of 10 goes stale; the 30th listing request fails once with
      # _bad_gateway, dropping 8 to 4; _on(..., waits=_Waits(), page_size=2, page_size_max=8, clock=ticking())
      drop = limits.index(4, limits.index(8))
      assert 8 in limits[drop:], "the premise: a re-probe climbed back after the drop"
      # all 800 films read once

  def test_pages_reports_hand_offs_with_the_earlier_requests_trial_and_never_the_first_or_last_page() -> None:  # Review Focus 3
      # the climbing fixture; monkeypatch.setattr(adapter._sizer, "handed_off", <a recorder that delegates>)
      assert len(calls) == len(limits) - 2
      assert [c.trial for c in calls][:12] == [0] * 6 + [1] * 6
      assert {c.trial for c in calls[12:]} == {2}

  def test_the_hand_off_after_a_moved_page_is_not_reported() -> None:            # Review Focus 3
      # test_a_shift_past_the_overlap_asks_again_earlier_and_skips_nothing's fixture: _numbered(250),
      # page_size=100, the first 60 deleted after page 1, so page 2 moves; WARNING lines captured
      assert len(moved_lines) == 1, "the premise: exactly one page moved"
      assert len(calls) == len(limits) - 4     # not the first page, the moved one, the one after it, or the last

  async def test_each_listing_times_its_own_hand_offs() -> None:                 # Review Focus 4
      # two adapter._pages(<the query list_items(None) pages with>, start_index=0) generators
      # over 20 films, page_size=2, clock=ticking(), stepped alternately with anext() until both end
      assert len(calls) >= 4, "the premise: both listings reported"
      assert {c.seconds for c in calls} == {2.0}

  def test_the_gauge_reads_the_size_after_a_listing_page_and_an_adapter_that_never_lists_writes_none() -> None:
      # reader as in Task 1; adapter B: climb to 8, confirm_gone() only
      assert _sizes(reader) == []
      # adapter A: climb(_sizer, 8), frozen clock, list_items() to the end
      assert _sizes(reader) == [(8, {"source": SOURCE.name})]
  ```

  The pinned Limit sequences assume one clock read per hand-off. Two reads would halve every rate alike and leave the sequences unchanged.

- [ ] **Step 2: Run the cases and see them fail**

  Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_adapter.py -p no:randomly`

  Expected: `TypeError: ... unexpected keyword argument 'page_size_max'`.

- [ ] **Step 3: Implement the sizer in the adapter**

  - **Constructor:** add `page_size_max: int | None = None` beside `page_size`, and build `self._sizer` as in Decision 9.
  - **`_read`:** it carries `trial` to `self._page(path, params, start, trial=trial)`.
  - **`_page`:** right after `self._listing_limit.failed()`, add `if trial is not None and isinstance(exc, PortUnavailable): self._sizer.failed(trial)`. Add one docstring line saying a listing page's outage also halves the page size. Stay under 20 lines.
  - **`_pages`:**
    - Each request carries its task, its size and its trial, taken from `self._sizer` when it is issued.
    - Call `self._sizer.report()` once each body arrives.
    - Pass `asked=` that request's size.
    - Rename the `StartIndex` local `asked` to `at`.
    - Report hand-offs per Decisions 3, 4 and 6. A local `last: tuple[float, int] | None` holds the listing's previous hand-off time and its request's trial. A shifted page clears it. After each hand-off it becomes `(now, this request's trial)`.
  - **`test_adapters_factory.py`:** both `adapter._page_size` reads become `adapter._sizer.floor`.

- [ ] **Step 4: Run the Emby and factory files and see them pass**

  Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_paging.py tests/unit/test_adapters_emby_sizer.py tests/unit/test_adapters_emby_contract.py tests/unit/test_adapters_factory.py -p no:randomly`

  Expected: all pass. The existing `Limit == "1000"` case at line 951 still holds, since there is no ceiling.

- [ ] **Step 5: Update the two docstrings that name the old size**

  - At `push.py:46`, "`USHER_SOURCE_PAGE_SIZE` items" becomes "listing pages that start at `USHER_SOURCE_PAGE_SIZE` items".
  - In the adapter test's docstring at 907, "`start += self._page_size`" becomes "`start += <the size it asked for>`".

- [ ] **Step 6: Plant five defects, one at a time, and see each fail its own case**

  Back up `adapter.py` first, and restore and `diff` after each plant.

  | Plant | The case that fails |
  |---|---|
  | `asked=self._sizer.size` read when the page arrives | Review Focus 2 |
  | the current request's trial reported | the trial-sequence case |
  | `last` kept on `self` | the interleaved case |
  | `failed()` called for a 429 too | the 429 arm |
  | `last` not cleared after a shifted page | the moved-page case |

- [ ] **Step 7: Run the gate and commit**

  ```bash
  git add src/usher/adapters/emby/adapter.py src/usher/services/push.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_factory.py
  git commit -m "emby: page every listing at the sizer's size, timing hand-offs and halving on an outage"
  ```

### Task 4: Walks under a changing page size

**Files:**
- Modify: `tests/unit/test_services_reconcile.py`, beside `test_an_emby_unit_resumes_judged_by_the_checkpoint_its_walk_stored` (2304–2360).

**Interfaces:**
- Consumes:
  - `EmbyAdapter(..., page_size_max=..., clock=...)` (Task 3);
  - `climb` and `ticking` (Task 1).

  These cases test behaviour Tasks 2–3 built, so they pass when written. Step 2's plants are their red.

- [ ] **Step 1: Write both cases**

  Both follow the template's setup: 300 films a second apart in view "films", and a `FakeEmbyServer`. The adapter's client is a recording `httpx.MockTransport`, as `_recorded` builds one. `limits` is the set of `int(Limit)` over the `/Items` requests whose `Limit` is at least 2, which excludes counts and boundary reads.

  ```python
  async def test_a_whole_library_walk_whose_pages_grow_commits_every_item() -> None:   # Review Focus 5
      # _Fixture(batch_size=4, walkers=1); EmbyAdapter(..., page_size=2, page_size_max=16,
      # sleep=instant_sleep, clock=ticking())
      assert run.status is SyncRunStatus.COMPLETED
      assert {2, 16} <= limits, "the premise: the pages grew to the ceiling mid-unit"
      assert missing == []

  async def test_a_walk_killed_mid_unit_resumes_at_its_committed_offset_under_another_page_size() -> None:
      # first attempt: page_size=100, page_size_max=400, clock=lambda: 1_000.0, fail_after=200
      # then the template's 80 removals; second attempt: a fresh adapter on the same server,
      # page_size=50, page_size_max=200, climb(second._sizer, 200), clock=lambda: 1_000.0
      assert (first_limits, second_limits) == ({100}, {200}), "the premise: two page sizes"
      # then the template's assertions, unchanged: Films FAILED at (150, <m199's key>), the
      # same run COMPLETED, the one WARNING "...before StartIndex=150; reading again from
      # StartIndex=50", and missing == []
  ```

- [ ] **Step 2: Plant each case false and see it fail on its own line**

  1. For the growth case, omit `page_size_max`. The premise guard fails.
  2. Plant `_commit_unit` (`services/reconcile.py`) to ingest only the first `batch_size` items it is handed. `missing == []` fails.
  3. For the resume case, give the second attempt `page_size=100` with no climb. The premise guard fails.
  4. Plant the resume in `_unit_pages`' caller (`adapter.py:664`) to pass `after=None`. The case fails at its WARNING-line assertion, since the walk no longer sees the move.

  Restore and `diff` after each plant.

- [ ] **Step 3: Run the gate and commit**

  ```bash
  git add tests/unit/test_services_reconcile.py
  git commit -m "reconcile: walks whose page size changes commit every item and resume in place"
  ```

### Task 5: The ceiling reaches production

**Files:**
- Modify:
  - `src/usher/config.py:171`, and a validator beside `_query_expansion_needs_a_client`;
  - `src/usher/adapters/factory.py`;
  - `src/usher/composition.py:293-305`;
  - `tests/unit/test_config.py`;
  - `tests/unit/test_adapters_factory.py:70-90`;
  - `tests/unit/test_composition.py`, beside 3413;
  - `web/src/features/operator/Config.settings.ts:177-184`;
  - `.env.example:57`;
  - `docs/guide/configuration.md:109-156`;
  - `docs/prd/03-sources-and-sync.md:91-126`;
  - `docs/prd/10-telemetry-and-dashboards.md:160-164`;
  - `CHANGELOG.md`.

**Interfaces:**
- Consumes: `EmbyAdapter(page_size_max=)`, and `PageSizer.floor` and `.ceiling`.
- Produces: `Settings.source_page_size_max: int`, and `ConfiguredSourceAdapterFactory(..., page_size_max: int = 10_000)`.

- [ ] **Step 1: Write the failing cases**

  ```python
  # tests/unit/test_config.py — env set as test_sync_walkers_is_from_one_to_sixteen sets it
  @pytest.mark.parametrize(("value", "accepted"), [(0, False), (1, True), (50_000, True), (50_001, False)])
  def test_source_page_size_is_from_one_to_fifty_thousand(...) -> None: ...      # with USHER_SOURCE_PAGE_SIZE_MAX=50000
  @pytest.mark.parametrize(("value", "accepted"), [(0, False), (1, True), (50_000, True), (50_001, False)])
  def test_source_page_size_max_is_from_one_to_fifty_thousand(...) -> None: ...  # with USHER_SOURCE_PAGE_SIZE=1
  @pytest.mark.parametrize(("ceiling", "accepted"), [(999, False), (1_000, True)])
  def test_a_page_size_ceiling_below_its_floor_is_refused(...) -> None: ...
      # floor 1000; refused with pytest.raises(ValidationError, match="USHER_SOURCE_PAGE_SIZE_MAX")

  # tests/unit/test_adapters_factory.py
  # test_the_deployment_tuning_reaches_the_adapter: page_size=17, page_size_max=170
  assert (adapter._sizer.floor, adapter._sizer.ceiling) == (17, 170)
  # test_an_unconfigured_factory_pages_at_the_settings_default
  assert adapter._sizer.floor == Settings.model_fields["source_page_size"].default == 1000
  assert adapter._sizer.ceiling == Settings.model_fields["source_page_size_max"].default == 10_000

  # tests/unit/test_composition.py, built as line 3413 builds its adapter
  def test_the_composition_root_carries_both_page_size_bounds() -> None:
      # settings with source_page_size=300, source_page_size_max=4321
      assert (adapter._sizer.floor, adapter._sizer.ceiling) == (300, 4321)
  ```

  Two existing guards will also fail until their files change:
  - `test_deployment_config.py::test_env_example_documents_every_setting`;
  - `test_console_settings_catalogue.py`.

- [ ] **Step 2: Run the cases and see them fail**

  Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_config.py tests/unit/test_adapters_factory.py tests/unit/test_composition.py -p no:randomly`

  Expected: the new cases fail. The cap case fails at 50,000 against `le=1000`.

- [ ] **Step 3: Implement the settings, the factory and the composition root**

  - `config.py`: the two fields and the validator, from Global Constraints.
  - `factory.py`: store `page_size_max` and pass it to `EmbyAdapter`.
  - `composition.py`: pass `page_size_max=settings.source_page_size_max` to `adapter_factory`.

- [ ] **Step 4: Update the console row, `.env.example`, the docs and the changelog**

  **`Config.settings.ts`.** Add the new row after `USHER_SOURCE_PAGE_SIZE`'s:

  ```ts
  {
    key: 'USHER_SOURCE_PAGE_SIZE_MAX',
    group: 'sources',
    def: '10000',
    about: 'The largest page a walk adapts up to. Set it equal to USHER_SOURCE_PAGE_SIZE to turn adaptation off.',
    secret: false,
    measured: false,
  },
  ```

  `USHER_SOURCE_PAGE_SIZE`'s `about` becomes: "Items per page a walk of a source’s library starts at, and the smallest it adapts down to."

  **`.env.example`.** Below `USHER_SOURCE_PAGE_SIZE=1000`:

  ```
  # Listing pages start at USHER_SOURCE_PAGE_SIZE and double while that is at least
  # 10% faster, up to USHER_SOURCE_PAGE_SIZE_MAX; a failed listing halves them. Set
  # the two equal to turn adaptation off. Both at most 50000.
  USHER_SOURCE_PAGE_SIZE_MAX=10000
  ```

  **PRD 03, "Walking the library".** "in pages of `USHER_SOURCE_PAGE_SIZE` items (default **1,000**, at most 1,000)" becomes a statement that pages:
  - start at `USHER_SOURCE_PAGE_SIZE` (default 1,000) and adapt up to `USHER_SOURCE_PAGE_SIZE_MAX` (default 10,000), both at most 50,000;
  - double while a doubling hands the writer at least 10% more items a second per walker;
  - settle a step back when one does not, try again after 100 pages, and halve after a failed listing.

  The `MAX_PAGES` bullet stands; it is stated from the floor.

  **`docs/guide/configuration.md`, "Walking a large library".** Both settings, in place of "(default 1,000, at most 1,000)". The 10,000-page table stays, stated as computed from `USHER_SOURCE_PAGE_SIZE`, the floor.

  **PRD 10.** A paragraph after the concurrency gauge's says that `usher.source.listing.page_size`:
  - is written each time a listing page arrives, so an adapter that never lists leaves no reading;
  - moves between the two settings by the rule above;
  - is joined by one INFO line per change.

  **`CHANGELOG.md`, `## [Unreleased]`.**
  - An `### Added` bullet: listing pages adapt, and `USHER_SOURCE_PAGE_SIZE_MAX` and the gauge are new.
  - A `### Changed` bullet: `USHER_SOURCE_PAGE_SIZE`'s cap rises from 1,000 to 50,000. Create the subsection if it is missing.

- [ ] **Step 5: Run the narrow files and the web gate**

  Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_config.py tests/unit/test_adapters_factory.py tests/unit/test_composition.py tests/unit/test_deployment_config.py tests/unit/test_console_settings_catalogue.py tests/unit/test_docs_pointers.py -p no:randomly`

  Then, from `web/`: `npm run verify`.

  Expected: all green.

- [ ] **Step 6: Plant two defects and see each fail its own case**

  | Plant | The case that fails |
  |---|---|
  | the validator's `<` becomes `<=` | the `1_000` arm of the floor case |
  | composition stops passing `page_size_max` | the composition-root case |

  Restore and `diff` after each plant.

- [ ] **Step 7: Run the gate and commit**

  ```bash
  git add src/usher/config.py src/usher/adapters/factory.py src/usher/composition.py tests/unit/test_config.py tests/unit/test_adapters_factory.py tests/unit/test_composition.py web/src/features/operator/Config.settings.ts .env.example docs/guide/configuration.md docs/prd/03-sources-and-sync.md docs/prd/10-telemetry-and-dashboards.md CHANGELOG.md
  git commit -m "config: USHER_SOURCE_PAGE_SIZE_MAX, the size listing pages adapt up to"
  ```

### Task 6: Dashboard 3 draws the page size

**Files:**
- Modify:
  - `dashboards/03-pipeline.json`: panel 11's `gridPos`, and a new panel 13;
  - `dashboards/README.md`: a section `### 13 — Listing page size, by source`, after panel 12's at 1253;
  - `docs/prd/10-telemetry-and-dashboards.md:342-357`;
  - `tests/unit/test_dashboards.py:822-852,1060,1127`;
  - `tests/unit/test_alerts.py:1181-1183`.

**Interfaces:**
- Consumes: the gauge's Prometheus name, `usher_source_listing_page_size_ratio` (Decision 7).

- [ ] **Step 1: Write the failing cases**

  - **Rename** `_assert_dashboard_3_has_twelve_panels` to `_assert_dashboard_3_has_thirteen_panels`. It now expects 13 panels. Its docstring reads "PRD 10's twelve items, with queue depth drawn twice", and its message begins "dashboard 3 is thirteen panels -- PRD 10's twelve items".
  - **Its falsifiability case** becomes `match=r"thirteen panels .* not 12:"`, fed 12 panels.
  - **In `test_alerts.py:1181`,** the count becomes `== 13` and the message "Dashboard 3 has thirteen panels".
  - **A new case,** `test_the_page_size_panel_draws_the_listing_page_size_by_source`, modelled on `test_the_walk_panel_draws_the_listing_limit_and_a_mean_unit_duration_by_stage`:
    - exactly one live panel is titled `Listing page size, by source`;
    - its normalised metric tokens are `{"usher_source_listing_page_size"}`;
    - its aggregation is `max by (source)`.

- [ ] **Step 2: Run the cases and see them fail**

  Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_dashboards.py tests/unit/test_alerts.py -p no:randomly`

  Expected: the count assertions fail at 12, and the new case finds no panel.

- [ ] **Step 3: Add the panel, its README section and the PRD 10 item**

  **The panel.** Panel 13 copies panel 11's datasource and `fieldConfig` shape and takes Global Constraints' expr and legend. Its gridPos is `{h: 8, w: 12, x: 12, y: 32}`. Panel 11 narrows to `w: 12`.

  **Its description,** in panel 11's register, says:
  - what the panel plots;
  - that the size starts at `USHER_SOURCE_PAGE_SIZE` and moves up to `USHER_SOURCE_PAGE_SIZE_MAX`;
  - that it halves after a failed listing;
  - that a size pinned at the ceiling means fetching is still the bottleneck.

  **The README section** says the same.

  **PRD 10.** Dashboard 3's list gains "· **listing page size, by source**" after the whole-library walks item. Correct any panel or item count in the paragraph after it.

- [ ] **Step 4: Run the dashboard, alert and docs files and see them pass**

  Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_dashboards.py tests/unit/test_alerts.py tests/unit/test_docs_pointers.py -p no:randomly`

  Expected: all pass.

- [ ] **Step 5: Run the gate and commit**

  ```bash
  git add dashboards/03-pipeline.json dashboards/README.md docs/prd/10-telemetry-and-dashboards.md tests/unit/test_dashboards.py tests/unit/test_alerts.py
  git commit -m "dashboards: draw each source's listing page size beside the listing limit"
  ```

### Task 7 (controller only): status rows, once the PR is open

- [ ] **Step 1:** Open the PR. Its body ends with "🤖 Generated with [Claude Code](https://claude.com/claude-code)".
- [ ] **Step 2:** Set this plan's rows in `docs/plans/progress.md`, and its row in `docs/prd/README.md`, to "🔍 in review (PR #N)". Then run `uv run pytest tests/unit/test_docs_currency.py tests/unit/test_docs_pointers.py -p no:randomly`, commit `docs(plan): adaptive page size in review`, and push.

### Task 8 (controller only, owner-gated): live acceptance

This runs after the owner merges, since merging deploys. Each check posts its raw output on the PR.

- [ ] **Step 1: Confirm the deploy and that no walk is running**

  Run `docker ps --filter name=usher-prod`. The new image should be up, and there should be no `usher-prod-sync` container.

- [ ] **Step 2: Run a timed full walk, on the owner's go**

  Run it the way PR #105's timed walk was run. Record:
  - the wall time, against 1,772 s;
  - the gauge's trace: `max by (source) (usher_source_listing_page_size_ratio)` over the walk;
  - the INFO lines: `docker logs usher-prod-usher-1 2>&1 | grep "listing pages"`;
  - peak memory: `docker stats --no-stream`, sampled every 10 s into `/var/tmp/adaptive-page-size/stats.log`.

- [ ] **Step 3: Post the raw output and read the result**

  Read it as the spec's Acceptance says:
  - a size that settles below the ceiling means the writer is the bottleneck;
  - a size pinned at 10,000 means fetching still is, and raising the default ceiling becomes a follow-up issue.

- [ ] **Step 4: Flip both status rows to ✅**

  Set both rows to ✅ with the walk's numbers.
