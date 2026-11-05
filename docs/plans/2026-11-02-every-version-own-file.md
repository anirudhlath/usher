# Every Version Playable As Itself, Slice 1: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every copy Usher holds of an Emby item describes and plays its own file. The sweep re-describes the hidden versions it keeps, and `/play` orders copies by the quality `/ways` already ranks by.

**Architecture:**
- **One selection.** `mapping.primary_media_source` becomes the own-entry rule, with today's best-version pick kept only as its fallback. `to_source_item` and `build_stream_targets` already share it, so the description and the playback URL change together.
- **The confirmation carries what it read.** `SourceAdapter.confirm_gone` returns a `Confirmation` holding the gone ids and the served entries, each a `SourceItem`. Emby's `Ids` listing asks for `ITEM_FIELDS` to fill them.
- **The sweep writes them.** `ReconcileService._sweep` ingests the served entries at `run.started_at`, in the commit that stamps the run completed. Push ignores them.
- **`/play` sorts once.** `PlaybackService._resolve` orders copies available first, then by `domain.ways.quality_of(copy).score` descending, with the repository's order as the tiebreak.

**Tech Stack:** Python 3.13, httpx, SQLAlchemy async over asyncpg, PostgreSQL 17 (testcontainers), loguru, OpenTelemetry metrics, pytest with strict mypy over `tests/`.

**Spec:** `docs/specs/2026-10-28-every-version-playable-design.md`, **slice 1 only**. Read it before any task. It is a point-in-time record, so never edit it. Slice 2, the versions pass, gets its own plan after slice 1 passes acceptance.

## Decisions this plan makes

The spec leaves these to the builder. Each is settled here so no task re-opens it.

1. **`confirm_gone` keeps its name and returns a `Confirmation`.** It is `Confirmation(gone: frozenset[str] = frozenset(), served: tuple[SourceItem, ...] = ())`, a frozen dataclass in `ports/source.py` beside `SourceItem`. `served` holds at most one entry per asked id the source still serves, each describing only its own file. An id the source serves whose entry the adapter does not model is in neither field. `__post_init__` raises `ValueError` for an id served twice and for one both gone and served. `services/retraction.confirmed_gone` keeps its name too, and returns the same type narrowed to the asked ids. *Why:* one listing answers both questions, so a second port method would read it twice.
2. **The fallback counter is `usher.source.versions.fallbacks`, with no labels.** *Why:* the selection runs in `mapping`, which never learns which source it maps for. The spec has the WARNING name no id.
3. **The own entry wins even with no `Container`.** That copy then has no direct target. *Why:* playing a sibling's file is the defect slice 1 removes, and the sibling is a copy of its own.
4. **The sweep ingests the served entries first, then retracts and stamps as today.** `_sweep` ingests them in chunks of the service's `batch_size`, at `run.started_at`, before `retract` and `mark_seen`, so `reconcile()`'s one stamping commit carries all three writes. `mark_seen` still stamps every served id, covering one whose entry the mapping does not model. The run's counters and the sweep's INFO line stay the walk's own. *Why:* the failure handler commits what is pending without a rollback, and this order never leaves a row stamped seen by a sweep that did not re-describe it.
5. **Push reads `.gone` only.** *Why:* the spec lets a push removal's confirmation ignore the entries.
6. **The fixture is `tests/fixtures/emby/version_group_by_id.json`: one version group read by id once as each version.** Both reads list the same two entries, the 4K version's first. Its ids are numeric strings in the band `tests/fixtures/README.md` allocates it. *Why:* reading as the 1080p version puts the own entry second and worse, which `MediaSources[0]` and today's best pick both get wrong.
7. **The shape comes from a new `--item ID` flag on `scripts/capture_emby_fixture.py`,** which prints type names only, as the listing capture does. Only the controller runs it, since it needs the live `.env`. The script has no test, as today; Task 1 Step 1's run is its check. *Why:* the README's regeneration recipe then covers the new fixture too.
8. **The two best-version tests become the fallback's tests, renamed.** The mapping one gains the WARNING and the counter. Their fixture `multi_version_movie.json` names no own entry, so it is exactly the fallback's shape.
9. **`/play` sorts in `PlaybackService._resolve`, once, before the loop.** The key is `(copy.available, quality_of(copy).score)` with `reverse=True`. Python's sort is stable under `reverse`, so the repository's order breaks ties with no third key.
10. **The fake server renders the own entry as Emby's by-id read does.** The seeded item's entry gains `ItemId: <id>` and `Id: mediasource_<id>`. A by-id read of a grouped item lists every present member's own entry, in group order. Listings are unchanged. *Why:* the contract suite's `get_item` and `stream_targets` then meet the real shape, and every test seeding an alternate keeps passing, because the seeded item's own entry now wins by its id.

## Global Constraints

**Scope and safety**

- **Slice 1 only.** No versions pass, no setting for one, no port method naming other versions (that is slice 2's).
- Never undo with `git checkout`, `restore`, `stash` or `reset`; `reset --soft` is fine. Every plant gets a `cp` backup in `/var/tmp/every-version/`, and each restore is checked with `diff`.
- **No implementer talks to a real server.** Task 1's shape capture and Task 7's acceptance are the controller's, GETs only. **No credential, token, user id or host enters the repo**, and every fixture is shape-recorded and value-synthetic: ids in the reserved bands `tests/fixtures/README.md` allocates, names and paths invented.
- **Merging deploys to production.** The merge is the owner's, and only with no walk running. Shared Emby is GETs only.
- **Layering:** `services/` depends only on `domain/` and `ports/`; `lint-imports` enforces it.

**Writing rules**

- The `guard-prose` hook caps Python prose:
  - a module docstring at 3 lines;
  - a def docstring at 20 lines;
  - a comment block at 5 lines.

  No dates, hex, "measured", `ADR-` or 🔴 in Python comments.
- **TDD.** Every behaviour change starts with a failing test, run and seen failing for the stated reason.
- **The PRD is updated in the same commit as the behaviour it describes**, stating behaviour, not reasoning.
- **Never edit an earlier spec or plan.**
- Commit messages read `area: sentence`, with **no trailers and no model identifiers**.
- **An ordering test asserts its own premise** before its expectation, against the same data the expectation is computed from.
- **SDD agents all use model `opus`**: implementers, reviewers and fixers alike.

**Commands**

- **Iterate:** `PYTHONDONTWRITEBYTECODE=1 uv run pytest <file> -p no:randomly`
- **Gate,** once per task, before its commit:
  ```
  uv sync --extra eval && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly
  ```
- **A plant's failure:** `uv run pytest <case> -p no:randomly 2>&1 | grep -E '^E .*<the assertion or its message>'`

## Review Focus

The five inputs the spec implies and no other case reaches, most likely first. Each one's test is a step in the task named.

1. **A payload with no `Id` whose entries carry `"ItemId": null`.** No entry is its own, since a missing id matches nothing, so it falls back with the WARNING. `None == None` must never pick an entry. Task 2, `test_a_missing_item_id_matches_no_entry`.
2. **An own entry with no `Container` beside a playable sibling.** The copy is described by its own entry and has no direct target. It never plays the sibling's file (Decision 3). Task 2, `test_an_own_entry_with_no_container_still_describes_the_copy` and `test_an_own_entry_with_no_container_has_no_targets_beside_a_playable_sibling`.
3. **A hidden version sharing a chunk of 100 with the sibling that hides it.** It is asked again alone, with `Limit` 1, and served as its own file. Task 3, `test_a_hidden_version_asked_again_is_served_as_its_own_file`.
4. **A stray entry the source lists unasked, with a `Type` and a `Name`.** It is never served, so the sweep never writes a row nobody asked about. Task 3, `test_a_stray_entry_is_never_served`.
5. **More served entries than one batch.** The sweep ingests them in `batch_size` chunks with no commit between them, and the next commit is the stamping one. Task 4, `test_a_sweep_ingests_what_it_keeps_in_batches_inside_one_commit`.

## File map

| File | Task | What changes |
|---|---|---|
| `scripts/capture_emby_fixture.py` | 1 (controller) | `--item ID` reads one item by id and prints its shape |
| `tests/fixtures/emby/version_group_by_id.json` | 1 | new: one version group, read by id as each version |
| `tests/fixtures/README.md`, `tests/fixtures/emby/README.md` | 1 | the new band and file, the stale paragraph replaced, the `--item` recipe |
| `tests/unit/test_no_third_party_data.py` | 1 | a `mediasource_` id is judged by its number; the new fixture is scanned |
| `tests/fakes/emby_server.py`, `tests/unit/test_fakes_emby_server.py` | 1 | the own entry names its item; a grouped by-id read lists every version |
| `tests/fakes/emby_fixtures.py` | 2 | `own_entry_only(read)` |
| `src/usher/adapters/emby/mapping.py` | 2 | `primary_media_source` is the own-entry rule; the fallback logs and counts |
| `tests/unit/test_adapters_emby_mapping.py`, `tests/unit/test_adapters_emby_playback.py` | 2 | own-entry, fallback and one-file-per-copy cases; two renames |
| `tests/unit/test_adapters_emby_adapter.py` | 2 | one stale docstring |
| `tests/contract/source_adapter_contract.py` | 2, 3 | `MOVIE_1080P`; each version read, played and confirmed as its own file |
| `docs/prd/02-data-model.md`, `docs/prd/10-telemetry-and-dashboards.md`, `tests/unit/test_telemetry_metric_names.py` | 2 | a MediaItem is one version; the counter's row; 46 → 47 |
| `docs/prd/03-sources-and-sync.md` | 2, 4 | each copy plays its own file (Playback); the sweep re-describes what it keeps (retraction) |
| `src/usher/ports/source.py`, `tests/unit/test_ports_source.py` | 3 | `Confirmation`; `confirm_gone` returns it |
| `src/usher/adapters/emby/adapter.py`, `tests/unit/test_adapters_emby_confirm.py` | 3 | the `Ids` listing asks for `ITEM_FIELDS` and maps what it was asked about |
| `src/usher/services/retraction.py`, `src/usher/services/push.py`, `tests/unit/test_telemetry_retraction.py` | 3 | `confirmed_gone` returns a `Confirmation`; push reads `.gone` |
| `tests/fakes/source_adapter.py`, `tests/unit/test_services_push.py`, `tests/unit/test_services_reconcile.py`, `tests/integration/test_pipeline_spans.py`, `tests/integration/test_services_reconcile.py` | 3 | every `confirm_gone` double and wrapper returns a `Confirmation` |
| `.claude/rules/emby-push-and-ingest.md` | 3 | "retract only what `confirm_gone` reports `gone`" |
| `src/usher/services/reconcile.py`, `tests/unit/test_services_reconcile.py`, `tests/integration/test_services_reconcile.py` | 4 | the sweep ingests the served entries before it retracts and stamps |
| `src/usher/services/playback.py`, `tests/unit/test_services_playback.py`, `docs/prd/07-client-api.md` | 5 | copies ordered by availability, then `quality_of(copy).score` |
| `docs/plans/progress.md`, `docs/prd/README.md` | 6, 7 | the status cells |

---

### Task 1: Record a version group's by-id read, and teach the fake server it

**Files:**
- Modify: `scripts/capture_emby_fixture.py:36-74` (Step 1, the controller's)
- Create: `tests/fixtures/emby/version_group_by_id.json`
- Modify: `tests/fixtures/README.md:82`; `tests/fixtures/emby/README.md:6`, its file table, `:34-38` and Regenerating
- Modify: `tests/unit/test_no_third_party_data.py:268` (`_is_synthetic_id`), `:383-408` (the scanned list)
- Modify: `tests/fakes/emby_server.py:210-255` (two docstrings), `:634-706` (`_payload`, `_render_media`)
- Test: `tests/unit/test_fakes_emby_server.py`

**Interfaces:**
- Produces: `load_emby_fixture("version_group_by_id")["reads"]`, two by-id reads of one movie. `reads[0]` is read as `"90000131"`, the 2160p version, and `reads[1]` as `"90000132"`, the 1080p one. Both list `MediaSources` as `[2160p entry, 1080p entry]`, each entry's `ItemId` its version's id and its `Id` `"mediasource_"` plus that id.
- Produces: `FakeEmbyServer` renders item `x`'s own entry with `ItemId: "x"` and `Id: "mediasource_x"`. A by-id read of a grouped `x` lists every present member's own entry, in group order. Listings render as today, own entries included.

- [ ] **Step 1 (controller, before dispatch): record the shape and check the premises**
  1. Add `--item ID` to `scripts/capture_emby_fixture.py`. With it, the script sends `GET /Users/{user_id}/Items/{ID}` with `params={"Fields": ITEM_FIELDS}` and `op="capture"` in place of the listing, and prints `_shape(body)` as today.
  2. Take one version group's ids from `/var/tmp/versions/`, source the live `.env` as `CLAUDE.md` says, and run, GET only: `uv run python scripts/capture_emby_fixture.py --item "$ID" > /var/tmp/every-version/by-id-shape.json`
  3. `jq -c '[.. | scalars] | unique' /var/tmp/every-version/by-id-shape.json` prints type names and nothing else.
  4. A throwaway script in `/var/tmp/every-version/`, never in the tree, reads each version of that group by id and prints these booleans and nothing else:
     - every read lists every version, in one order;
     - each entry's `ItemId` is a version's id, and its `Id` is `mediasource_` plus that id;
     - every read holds an entry whose `ItemId` is the read's own `Id`;
     - each read's item-level `RunTimeTicks`, `Width` and `Height`, where present, equal its own entry's `RunTimeTicks` and its own video stream's `Width` and `Height`.
  5. **If any is false, stop and report it.** The fixture below and Task 2's rule both rest on all four.
  6. The implementer may read the shape file, since it holds type names only. The script change is committed with this task.

- [ ] **Step 2: Write the failing guard cases** in `tests/unit/test_no_third_party_data.py`, and add `"tests/fixtures/emby/version_group_by_id.json"` to `test_the_guard_reads_what_it_claims_to_read`'s list, after `ids_version_group.json`:

```python
@pytest.mark.parametrize(("value", "synthetic"), [("mediasource_12345", False), ("mediasource_90000131", True)])
def test_a_media_source_id_is_judged_by_the_item_id_it_carries(value: str, synthetic: bool) -> None:
    assert _is_synthetic_id(value) is synthetic
```

- [ ] **Step 3: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_no_third_party_data.py -p no:randomly`
Expected: FAIL. `mediasource_12345` reads as synthetic, and the new fixture "is gone".

- [ ] **Step 4: Fix the guard and write the fixture**
  - `_is_synthetic_id` strips one leading `mediasource_` from a string before its checks (`str.removeprefix`).
  - `tests/fixtures/emby/version_group_by_id.json` is `{"reads": [<read as "90000131">, <read as "90000132">]}`.
    - Each read is a `Movie` carrying `movie_item.json`'s keys plus item-level `Width` and `Height`. Its `Name` is invented and its `ProviderIds` are `{"Tmdb": "90000130", "Imdb": "tt99000130"}`. Its `Id`, `RunTimeTicks`, `Width` and `Height` are its own version's.
    - Every entry carries `movie_item.json`'s entry keys plus `ItemId`. Both reads list the same two:
      - `ItemId "90000131"`, `Id "mediasource_90000131"`: `movie_item.json`'s entry, mkv, hevc Dolby Vision 3840x2160, default audio truehd 8 channels, `Size` 68719476736, `RunTimeTicks` 93600000000;
      - `ItemId "90000132"`, `Id "mediasource_90000132"`: mp4, h264 1920x1080 with the SDR tokens `emby_server._HDR_WIRE[None]` renders, default audio eac3 6 channels, `Size` 8589934592, `RunTimeTicks` 93000000000.
  - `tests/fixtures/README.md`: after `multi_version_movie.json`'s row, add ``| `tt99000130` / TMDb `90000130` / Emby `90000131`–`90000132` | `emby/version_group_by_id.json` |``.

- [ ] **Step 5: Run the guard to see it pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_no_third_party_data.py -p no:randomly`
Expected: PASS.

- [ ] **Step 6: Write the failing fake-server case** in `tests/unit/test_fakes_emby_server.py`, beside the other version-group cases:

```python
async def test_a_version_group_read_by_id_lists_every_version_as_recorded(driver: _Driver) -> None:
    reads = load_emby_fixture("version_group_by_id")["reads"]
    group = [read["Id"] for read in reads]
    for read in reads:
        assert [(e["ItemId"], e["Id"]) for e in read["MediaSources"]] == [
            (version, f"mediasource_{version}") for version in group
        ], "the premise: every recorded read lists every version"
    for version in ("v0", "v1"):
        driver.server.add_item(replace(MOVIE, external_id=version), ADDED_AT)
    driver.server.group_versions("v0", "v1")
    for version in ("v0", "v1"):
        payload = await driver.payload(version)
        entries = payload["MediaSources"]
        assert [(e.get("ItemId"), e.get("Id")) for e in entries] == [
            ("v0", "mediasource_v0"),
            ("v1", "mediasource_v1"),
        ]
        assert set(reads[0]) <= set(payload)
        assert set(reads[0]["MediaSources"][0]) <= set(entries[0])
        listed = await driver.session.json_body(
            "GET", f"/Users/{USER_ID}/Items", params={"Ids": version, "Limit": "1"}, op="list"
        )
        assert [(e.get("ItemId"), e.get("Id")) for e in listed["Items"][0]["MediaSources"]] == [
            (version, f"mediasource_{version}")
        ]
```

- [ ] **Step 7: Run it to see it fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_fakes_emby_server.py -p no:randomly -k recorded`
Expected: FAIL on the first fake-side assertion, reading `[(None, '0000000000000000000000000000b001')]`.

- [ ] **Step 8: Render the own entry in `tests/fakes/emby_server.py`**
  - Factor `_own_entry(self, item: SourceItem) -> dict[str, Any] | None` out of `_render_media`. It is the entry `_render_media` builds today, from the item's own facts, its `RunTimeTicks` included, plus `ItemId: item.external_id` and `Id: f"mediasource_{item.external_id}"`. It is `None` for an item with no container.
  - `_payload(external_id, for_listing=False)` for a grouped item sets `MediaSources` to the `_own_entry` of each group member still held, in group order, with the read's alternates first as today. Every other payload renders as today.
  - Docstrings:
    - `group_versions` gains one sentence: a read by id of any member lists every present member's own entry, in group order, as `version_group_by_id.json` records.
    - `add_alternate_version`'s "the seeded item stays the *best* version, so it still reads back as itself" becomes: the seeded item's own entry names it, so it still reads back as itself.
    - `_payload`'s "The two differ only in `UserData`" also names a grouped item's `MediaSources`.

- [ ] **Step 9: Run the Emby suites to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_fakes_emby_server.py tests/unit/test_adapters_emby_adapter.py tests/unit/test_adapters_emby_confirm.py tests/unit/test_adapters_emby_contract.py -p no:randomly`
Expected: PASS. Nothing reads a grouped item by id yet, and the own entry's new `Id` reaches only `MediaSourceId`.

- [ ] **Step 10: The fixture README** (`tests/fixtures/emby/README.md`)
  - Line 6: "nine files" becomes "ten files".
  - The file table gains: ``| `version_group_by_id.json` | one movie read by id as each of its two versions: both reads list the same two entries, 2160p first, each naming its version in `ItemId` and `mediasource_<id>` in `Id`; each read's `Id`, `RunTimeTicks`, `Width` and `Height` are its own version's |``.
  - Lines 34–38 become: "`multi_version_movie.json` names none of its entries as its own, so it is the shape of the fallback alone. A real read by id of one version lists every version, each entry naming its version, and `version_group_by_id.json` is that shape." This drops the "never met a real payload" claim and the `CLAUDE.md` pointer, which `CLAUDE.md` does not back.
  - Regenerating gains, after the `--type Episode` line: `uv run python scripts/capture_emby_fixture.py --item ID > /tmp/shape.json`.

- [ ] **Step 11: Gate and commit**

```bash
git add scripts/capture_emby_fixture.py tests/fixtures/emby/version_group_by_id.json tests/fixtures/README.md tests/fixtures/emby/README.md tests/unit/test_no_third_party_data.py tests/fakes/emby_server.py tests/unit/test_fakes_emby_server.py
git commit -m "emby: record a version group read by id, and the fake server serves it"
```

### Task 2: Each copy describes and plays its own file

**Files:**
- Modify: `src/usher/adapters/emby/mapping.py:210-219` (`primary_media_source`) and its imports
- Modify: `tests/fakes/emby_fixtures.py` (`own_entry_only`)
- Test: `tests/unit/test_adapters_emby_mapping.py` (`:519` renamed), `tests/unit/test_adapters_emby_playback.py` (`:206` renamed)
- Modify: `tests/unit/test_adapters_emby_adapter.py:2395-2397` (a docstring only)
- Modify: `tests/contract/source_adapter_contract.py` (`MOVIE_1080P` after `MOVIE`, two cases)
- Modify: `docs/prd/02-data-model.md:256-257`, `docs/prd/03-sources-and-sync.md:602-603`, `docs/prd/10-telemetry-and-dashboards.md:109-110` and `:142`, `tests/unit/test_telemetry_metric_names.py:185`

**Interfaces:**
- Consumes: Task 1's `reads` and the fake server's own entries.
- Produces: `primary_media_source(payload: Mapping[str, Any]) -> Mapping[str, Any] | None`, signature unchanged, deciding in this order:
  1. no entry that is an object → `None`;
  2. exactly one → that one;
  3. the entry whose `ItemId` is the payload's `Id`;
  4. the entry whose `Id` is the payload's `Id` or `mediasource_<Id>`;
  5. today's best pick, after the WARNING and one count.

  A payload with no string `Id` matches nothing at 3 or 4.
- Produces: the counter `usher.source.versions.fallbacks`, unit `"1"`, no attributes, on a module-scope `_meter = metrics.get_meter("usher.source.emby")` in `mapping.py`, as `session.py` declares its own.
- Produces: `own_entry_only(read: dict[str, Any]) -> dict[str, Any]` in `tests/fakes/emby_fixtures.py`: a copy of `read` whose `MediaSources` keeps only the entry whose `ItemId` is the read's `Id`.
- Produces: `MOVIE_1080P` in `tests/contract/source_adapter_contract.py`, which Task 3's contract case reuses.

- [ ] **Step 1: Write the failing cases**

  **Test helpers.** `own_entry_only` goes into `tests/fakes/emby_fixtures.py`. Each case loads the fixture afresh, because cases mutate it. In `tests/unit/test_adapters_emby_mapping.py`, add:
  - a `meter_reader` fixture shaped like `tests/unit/test_telemetry_retraction.py::meter_reader`;
  - `_fallbacks(reader) -> float`, the summed value of every `usher.source.versions.fallbacks` point, `0.0` when there are none;
  - `_pixels(entry) -> int`, the entry's video stream's width times height;
  - a loguru sink at WARNING with `format="{message}"` in each case that reads `lines`.

  **The selection cases**, in `tests/unit/test_adapters_emby_mapping.py`, with `_FALLBACK = "Emby listed 3 media sources for an item and none is its own, so the best version stands in"`:

```python
@pytest.mark.parametrize("found_by", ["item-id", "plain-id", "media-source-id"])
def test_the_own_entry_wins_over_a_better_version_found_by(found_by: str) -> None:
    read = load_emby_fixture("version_group_by_id")["reads"][1]
    sibling, own = read["MediaSources"]
    # "item-id" as recorded; the other two drop both ItemIds, and "plain-id" also sets own["Id"] = read["Id"].
    assert _pixels(own) < _pixels(sibling), "the premise: the best pick takes the sibling"
    assert primary_media_source(read) is own

def test_an_item_id_match_outranks_an_id_match() -> None:
    # reads[1] with sibling["Id"] = read["Id"]
    assert primary_media_source(read) is own

def test_a_lone_entry_is_the_items_own_whatever_its_ids_say(meter_reader) -> None:
    # reads[1] with MediaSources cut to [sibling], whose ItemId names the other version
    assert primary_media_source(read) is read["MediaSources"][0]
    assert lines == [] and _fallbacks(meter_reader) == 0.0

def test_several_entries_naming_none_fall_back_to_the_best_and_say_so(meter_reader) -> None:
    # :519 renamed from test_a_multi_version_item_is_catalogued_at_its_best_playable_version;
    # its assertions stay, and these are added:
    assert [line.rstrip("\n") for line in lines] == [_FALLBACK]
    assert _fallbacks(meter_reader) == 1.0

def test_a_missing_item_id_matches_no_entry(meter_reader) -> None:  # Review Focus 1
    payload = load_emby_fixture("multi_version_movie")
    del payload["Id"]
    for entry in payload["MediaSources"]:
        entry["ItemId"] = None
    assert payload["MediaSources"][0]["Container"] is None, "the premise: a None match takes the first entry"
    chosen = primary_media_source(payload)
    assert chosen is not None and chosen["Id"] == "0000000000000000000000000000b0f1"
    assert [line.rstrip("\n") for line in lines] == [_FALLBACK]
    assert _fallbacks(meter_reader) == 1.0

def test_an_own_entry_with_no_container_still_describes_the_copy() -> None:  # Review Focus 2
    # reads[1] with del read["MediaSources"][1]["Container"]
    item = to_source_item(read)
    assert item is not None and (item.container, item.width, item.height) == (None, 1920, 1080)

def test_a_folder_item_has_no_entry_and_counts_no_fallback(meter_reader) -> None:
    assert primary_media_source(load_emby_fixture("series_item")) is None
    assert primary_media_source({"Id": "90000131", "MediaSources": []}) is None
    assert _fallbacks(meter_reader) == 0.0

@pytest.mark.parametrize("index", [0, 1])
def test_each_version_read_by_id_describes_its_own_file(index: int) -> None:
    reads = load_emby_fixture("version_group_by_id")["reads"]
    alone = [to_source_item(own_entry_only(read)) for read in reads]
    for field in ("container", "video_codec", "audio_codec", "width", "height", "hdr_format",
                  "audio_channels", "file_size_bytes", "runtime_seconds"):
        assert getattr(alone[0], field) != getattr(alone[1], field), f"the premise: the versions differ in {field}"
    assert to_source_item(reads[index]) == alone[index]
```

  **The playback cases**, in `tests/unit/test_adapters_emby_playback.py`:
  - `:206` is renamed `test_several_entries_naming_none_play_the_best_version`, with its body unchanged. Its docstring gains one sentence: `multi_version_movie.json` names no entry as its own, so this is the fallback.

```python
@pytest.mark.parametrize("index", [0, 1])
def test_each_version_read_by_id_plays_its_own_file(index: int) -> None:
    reads = load_emby_fixture("version_group_by_id")["reads"]
    alone = [build_stream_targets(own_entry_only(read), base_url=BASE, access_token=TOKEN) for read in reads]
    assert alone[0][0].resolution != alone[1][0].resolution, "the premise: the versions play differently"
    targets = build_stream_targets(reads[index], base_url=BASE, access_token=TOKEN)
    assert parse_qs(urlparse(targets[0].url).query)["MediaSourceId"] == [f"mediasource_{reads[index]['Id']}"]
    assert urlparse(targets[0].url).path == f"/Videos/{reads[index]['Id']}/stream.{('mkv', 'mp4')[index]}"
    assert targets == alone[index]

def test_an_own_entry_with_no_container_has_no_targets_beside_a_playable_sibling() -> None:  # Review Focus 2
    # reads[1] with del read["MediaSources"][1]["Container"]
    assert read["MediaSources"][0]["Container"] == "mkv", "the premise: the sibling plays"
    assert build_stream_targets(read, base_url=BASE, access_token=TOKEN) == []
```

  **The contract cases**, in `tests/contract/source_adapter_contract.py`:

```python
MOVIE_1080P = replace(
    MOVIE, external_id="movie-1-1080p", container="mp4", video_codec="h264", audio_codec="eac3",
    width=1920, height=1080, hdr_format=None, audio_channels=6,
    file_size_bytes=8_589_934_592, runtime_seconds=9300,
)

async def test_get_item_reads_each_version_as_its_own_file(self, harness: SourceHarness) -> None:
    await harness.given_versions([MOVIE, MOVIE_1080P], changed_at=T0)
    for version in (MOVIE, MOVIE_1080P):
        assert await harness.adapter.get_item(version.external_id) == version

async def test_stream_targets_play_each_version_from_its_own_file(self, harness: SourceHarness) -> None:
    await harness.given_versions([MOVIE, MOVIE_1080P], changed_at=T0)
    for version in (MOVIE, MOVIE_1080P):
        direct = (await harness.adapter.stream_targets(version.external_id))[0]
        assert (direct.container, direct.resolution) == (version.container, f"{version.width}x{version.height}")
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_mapping.py tests/unit/test_adapters_emby_playback.py tests/unit/test_adapters_emby_contract.py tests/unit/test_source_adapter_contract.py -p no:randomly`
Expected: FAIL.
- Every case reading `reads[1]` gets the 2160p sibling.
- The two fallback cases find no WARNING.
- The two contract cases fail on the Emby arm only.
- The lone-entry, folder and `index=0` cases already pass, and they pin what the change must keep.

- [ ] **Step 3: Implement the rule in `mapping.py`**
  - `primary_media_source` keeps `_playback_rank` for step 5.
  - The WARNING is `logger.warning("Emby listed {count} media sources for an item and none is its own, so the best version stands in", count=...)`, counting the entries that are objects. It names no id.
  - It then calls `_versions_fallbacks.add(1)`, with no attributes.
  - Its docstring states the five steps, in under 12 lines.

- [ ] **Step 4: Run them to see them pass**

Run: the Step 2 command, then `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_adapter.py tests/unit/test_fakes_emby_server.py -p no:randomly`
Expected: PASS.

- [ ] **Step 5: The PRD and the stale docstring, in this commit**
  - **PRD 02:256–257.** "A Title with several MediaItems is the same film available in more than one place." becomes: "A MediaItem is one version of a film or episode in one place, and describes that version's own file. A Title with several MediaItems is the same film in more than one version or place."
  - **PRD 03, after the "three query parameters" paragraph (602–603).** Add: "**Each copy plays its own file.** An Emby item's own media source is the entry whose `ItemId` is the item's id, else the one whose `Id` is the item's id or `mediasource_` followed by it. A payload with one entry uses it. `MediaSourceId` names that entry, and the copy's description is read from it. A payload with several entries naming none plays its best version, the most pixels among those with a container and then the most bytes, and is logged as a WARNING and counted (`usher.source.versions.fallbacks`)."
  - **PRD 10.**
    - "46 rows: 45 instruments Usher declares" becomes "47 rows: 46 instruments Usher declares".
    - After the `usher.source.push.events` row, add: ``| `usher.source.versions.fallbacks` | counter | — | ✅ every version playable |``.
    - `tests/unit/test_telemetry_metric_names.py:185` changes `== 46` to `== 47`.
  - **`tests/unit/test_adapters_emby_adapter.py:2396-2397`.** "A fixture cannot reach the choice: each has exactly one `MediaSources` entry, so only a server rendering more than one exercises the selection at all." becomes: "The fake server lists an alternate ahead of the seeded item's own entry, which is the order a `MediaSources[0]` rule gets wrong."

- [ ] **Step 6: Gate and commit**

```bash
git add src/usher/adapters/emby/mapping.py tests/fakes/emby_fixtures.py tests/unit/test_adapters_emby_mapping.py tests/unit/test_adapters_emby_playback.py tests/unit/test_adapters_emby_adapter.py tests/contract/source_adapter_contract.py docs/prd/02-data-model.md docs/prd/03-sources-and-sync.md docs/prd/10-telemetry-and-dashboards.md tests/unit/test_telemetry_metric_names.py
git commit -m "emby: each copy describes and plays its own file"
```

### Task 3: A confirmation carries what it read

**Files:**
- Modify: `src/usher/ports/source.py` (`Confirmation` beside `SourceItem`; `confirm_gone` at 384, its return and docstring)
- Test: `tests/unit/test_ports_source.py`
- Modify: `src/usher/adapters/emby/adapter.py:931-976` (`confirm_gone` and the listing it sends)
- Test: `tests/unit/test_adapters_emby_confirm.py`
- Modify: `src/usher/services/retraction.py` (`confirmed_gone`), `src/usher/services/push.py:297`, `src/usher/services/reconcile.py:892-895` (reads `.gone`; Task 4 reads `.served`)
- Modify: `tests/fakes/source_adapter.py:392-402`, `tests/contract/source_adapter_contract.py:352-392`
- Modify, stubs returning a `Confirmation`: `tests/unit/test_telemetry_retraction.py:72-112`, `tests/unit/test_services_push.py:637-645, 732-738`, `tests/unit/test_services_reconcile.py:531-539, 555-562`, `tests/integration/test_services_reconcile.py:185-186`, `tests/integration/test_pipeline_spans.py:67`
- Modify: `.claude/rules/emby-push-and-ingest.md` (one phrase)

**Interfaces:**
- Consumes: Task 2's own-entry rule and `MOVIE_1080P`.
- Produces, in `src/usher/ports/source.py`:

```python
@dataclass(frozen=True)  # decorated as SourceItem is
class Confirmation:
    gone: frozenset[str] = frozenset()
    served: tuple[SourceItem, ...] = ()
    # __post_init__ raises ValueError("... served twice") when two served items share an
    # external_id, and ValueError("... both gone and served") when an id is in both.

async def confirm_gone(self, external_ids: Collection[str]) -> Confirmation: ...
```

  - `gone` is what the source no longer serves.
  - `served` holds each asked id the source listed and Usher models, described as a walk describes a listed item.
  - An id that is listed but not modelled is in neither.
- Produces: `confirmed_gone(adapter, external_ids, *, source, path) -> Confirmation`.
  - `gone` is `asked & answer.gone`.
  - `served` keeps the answer's items whose `external_id` was asked, in the answer's order.
  - Its counters are unchanged, and an empty ask returns `Confirmation()`.
  - Push keeps its own signature and returns `(await confirmed_gone(...)).gone` (Decision 5).
- Produces: Emby's `confirm_gone` keeps its rounds.
  - Each listing sends `{"Ids": ",".join(chunk), "Fields": ITEM_FIELDS, "Limit": str(len(chunk))}`.
  - `_listed_of(chunk) -> dict[str, SourceItem | None]` maps each listed entry whose `Id` was asked through `to_source_item`; the first entry per id wins, and the rest are ignored. Its raises stay.
  - `served` is the non-`None` items in first-listed order across rounds.
- Produces: the fake's `confirm_gone` → `Confirmation(gone=<as today>, served=tuple(self._items[i] for i in sorted(asked) if i in self._items))`. An empty ask returns `Confirmation()`.

- [ ] **Step 1: The type's failing cases**, in `tests/unit/test_ports_source.py`. Here `item` is a `SourceItem` built as the neighbouring cases build one:

```python
def test_a_confirmation_defaults_to_nothing_gone_and_nothing_served() -> None:
    assert Confirmation() == Confirmation(gone=frozenset(), served=())

def test_a_confirmation_refuses_an_item_served_twice() -> None:
    with pytest.raises(ValueError, match="served twice"):
        Confirmation(served=(item, item))

def test_a_confirmation_refuses_an_id_both_gone_and_served() -> None:
    with pytest.raises(ValueError, match="both gone and served"):
        Confirmation(gone=frozenset({item.external_id}), served=(item,))

def test_confirm_gone_answers_with_a_confirmation() -> None:
    assert inspect.signature(SourceAdapter.confirm_gone, eval_str=True).return_annotation is Confirmation
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_ports_source.py -p no:randomly`
Expected: FAIL, on the import of `Confirmation`.

- [ ] **Step 3: Add `Confirmation` and change the port's return.** The docstring states the three Interfaces bullets on `gone` and `served`. Run Step 2's command again. Expected: PASS.

- [ ] **Step 4: The behaviour's failing cases**

  **The contract** (`tests/contract/source_adapter_contract.py:352-392`).
  - The existing confirm cases assert on `.gone`.
  - The "of nothing" case asserts `== Confirmation()`.
  - Add, beside `test_confirm_gone_returns_none_of_a_served_items_versions`:

```python
async def test_confirm_gone_describes_each_served_version_as_its_own_file(self, harness: SourceHarness) -> None:
    await harness.given_versions([MOVIE, MOVIE_1080P], changed_at=T0)
    answer = await harness.adapter.confirm_gone(["movie-1", "movie-1-1080p", "never-existed"])
    assert answer.gone == {"never-existed"}
    assert sorted(answer.served, key=lambda item: item.external_id) == [MOVIE, MOVIE_1080P]
```

  **The wire** (`tests/unit/test_adapters_emby_confirm.py`, with its `_adapter`, `_movie` and `_asked` helpers). The existing cases read `.gone`. Add:

```python
async def test_a_confirmation_asks_for_the_fields_a_walk_reads() -> None:
    # the server lists kept-1 and not gone-1
    answer = await adapter.confirm_gone(["gone-1", "kept-1"])
    by_ids = [request for request in seen if "Ids" in request.url.params]
    assert by_ids, "the premise: the confirmation listed by Ids"
    assert all(request.url.params["Fields"] == ITEM_FIELDS for request in by_ids)
    assert [item.external_id for item in answer.served] == ["kept-1"]
    assert answer.gone == {"gone-1"}

async def test_a_hidden_version_asked_again_is_served_as_its_own_file() -> None:  # Review Focus 3
    # a-v0 (3840x2160) and a-v1 (1920x1080) are one group. The handler lists every asked filler,
    # but only the first asked version of the group, each entry with its own one media source.
    asked = ["a-v0", "a-v1", *(f"m-{index:03d}" for index in range(IDS_PER_REQUEST - 1))]
    assert sorted(asked)[:2] == ["a-v0", "a-v1"], "the premise: the group shares the first chunk"
    assert len(asked) == IDS_PER_REQUEST + 1, "the premise: a second chunk follows"
    answer = await adapter.confirm_gone(asked)
    assert limits == [str(IDS_PER_REQUEST), "1", "1"]
    assert _asked(seen)[-1] == "a-v1"
    served = {item.external_id: item for item in answer.served}
    assert sorted(served) == sorted(asked)
    assert (served["a-v1"].width, served["a-v1"].height) == (1920, 1080)
    assert answer.gone == frozenset()

async def test_a_stray_entry_is_never_served() -> None:  # Review Focus 4
    listing = {"Items": [{"Id": "kept-1", "Type": "Movie", "Name": "Kept"},
                         {"Id": "stray-1", "Type": "Movie", "Name": "Stray"}], "TotalRecordCount": 2}
    assert to_source_item(listing["Items"][1]) is not None, "the premise: the stray would map"
    answer = await adapter.confirm_gone(["kept-1"])  # the server answers every listing with `listing`
    assert [item.external_id for item in answer.served] == ["kept-1"]
```

  **The service** (`tests/unit/test_telemetry_retraction.py:72-112`).
  - `_StrayAdapter.confirm_gone` returns `Confirmation(gone=answer.gone | {"stray-1"}, served=(*answer.served, <a SourceItem "stray-2">))`.
  - The stray case asserts `== Confirmation(gone=frozenset({"gone-1"}))`.
  - The counted case also asserts that the served ids are `["kept-1"]`.
  - The empty case asserts `== Confirmation()`.

- [ ] **Step 5: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_confirm.py tests/unit/test_telemetry_retraction.py tests/unit/test_adapters_emby_contract.py tests/unit/test_source_adapter_contract.py -p no:randomly`
Expected: FAIL, each on `.gone` or `.served` of a `frozenset`.

- [ ] **Step 6: Implement the adapter, the fake and the service, as Interfaces states.**
  - Push reads `.gone`, and the sweep reads `.gone` at 892–895.
  - Every other stub in the Files list returns `Confirmation(gone=...)`.
  - The integration `_Adapter` (185–186) also returns `served=tuple(self.items[i] for i in sorted(asked) if i in self.items)`.

- [ ] **Step 7: Run them to see them pass**

Run: the Step 5 command plus `tests/unit/test_ports_source.py tests/unit/test_services_push.py tests/unit/test_services_reconcile.py tests/integration/test_services_reconcile.py tests/integration/test_pipeline_spans.py`
Expected: PASS. Then the gate.

- [ ] **Step 8: The rules file, in this commit.**
  - In `.claude/rules/emby-push-and-ingest.md`, "retract only what `confirm_gone` returns" becomes "retract only what `confirm_gone` reports `gone`".
  - The PRD names neither the port nor the listing's fields (`grep -rn confirm_gone docs/prd/` prints nothing), so the PRD is unchanged.

- [ ] **Step 9: Commit**

```bash
git add src/usher/ports/source.py tests/unit/test_ports_source.py src/usher/adapters/emby/adapter.py tests/unit/test_adapters_emby_confirm.py src/usher/services/retraction.py src/usher/services/push.py src/usher/services/reconcile.py tests/fakes/source_adapter.py tests/contract/source_adapter_contract.py tests/unit/test_telemetry_retraction.py tests/unit/test_services_push.py tests/unit/test_services_reconcile.py tests/integration/test_services_reconcile.py tests/integration/test_pipeline_spans.py .claude/rules/emby-push-and-ingest.md
git commit -m "sync: a confirmation carries the entries it read"
```

### Task 4: The sweep re-describes the hidden versions it keeps

**Files:**
- Modify: `src/usher/services/reconcile.py` (`from itertools import batched`; `_sweep` at 847–895 and its docstring at 850–863)
- Test: `tests/unit/test_services_reconcile.py`, `tests/integration/test_services_reconcile.py`
- Modify: `docs/prd/03-sources-and-sync.md:319-321`

**Interfaces:**
- Consumes: Task 3's `Confirmation.served`.
- Produces: `_sweep` runs in this order, with its counters still the walk's own:
  1. the confirmation, inside `_beating`, as today;
  2. `ingest_batch` over `batched(confirmation.served, self._batch_size, strict=False)`, called as the walk calls it, with `observed_at=run.started_at`;
  3. the retraction of `.gone`;
  4. `mark_seen` for the kept ids, as today.

  The stamping commit at 365–366 then commits all of it. A failure before it leaves nothing retracted or stamped (Decision 4).

- [ ] **Step 1: Write the failing cases**, in `tests/unit/test_services_reconcile.py`, with its `_Fixture`, `_item`, `unlist` and `forget`:

```python
async def test_a_sweep_re_describes_a_kept_hidden_version_in_the_stamping_commit() -> None:
    # A _Fixture subclass whose _commit calls super()._commit() and then appends
    # (the newest run's status, m18's stored container) to `seen`.
    # Seed _item(f"m{i}", container="mkv", width=3840, height=2160) for i in range(20); run.
    # Re-seed m18 as mp4 1920x1080, unlist("m18"), forget("m19"); run again as `second`.
    first_mp4 = next(index for index, (_, container) in enumerate(seen) if container == "mp4")
    assert seen[first_mp4 - 1] == (SyncRunStatus.RUNNING, "mkv"), "the premise: the run committed before it completed"
    assert seen[first_mp4][0] is SyncRunStatus.COMPLETED
    assert (second.items_seen, second.items_retracted) == (18, 1)
    # m18 is ("mp4", 1920, 1080) with last_seen_at == second.started_at; m19 has available False

async def test_a_sweep_ingests_what_it_keeps_in_batches_inside_one_commit(monkeypatch) -> None:  # Review Focus 5
    # _Fixture(batch_size=2), 20 items, one run. Then m17, m18 and m19 are re-seeded as
    # mp4 1920x1080 and unlisted, and a recorder replaces fixture.ingest.ingest_batch: it
    # appends ([item.external_id for item in items], fixture.commits), then delegates.
    sweep = [call for call in calls if set(call[0]) <= {"m17", "m18", "m19"}]
    assert len(sweep) < len(calls), "the premise: the walk ingested through the recorder too"
    assert [ids for ids, _ in sweep] == [["m17", "m18"], ["m19"]]
    assert sweep[0][1] == sweep[1][1]
    assert fixture.commits == sweep[-1][1] + 1

async def test_a_served_id_whose_entry_is_not_modelled_is_still_stamped() -> None:
    # confirm_gone wrapped to return Confirmation(gone=answer.gone); m8 unlisted and re-seeded as mp4
    # m8: last_seen_at == second.started_at, available, container still "mkv"

async def test_a_sweep_whose_ingest_fails_retracts_and_stamps_nothing() -> None:
    # m8 unlisted and re-seeded as mp4, m9 forgotten; ingest_batch raises PortUnavailable for a chunk holding m8
    # second.status is SyncRunStatus.FAILED; m9 still available; m8 "mkv" with last_seen_at == first.started_at
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_services_reconcile.py -p no:randomly`
Expected: FAIL. The first two cases find m18 still `mkv` and no sweep calls. The third and fourth already pass, and they pin the stamp and the failure path.

- [ ] **Step 3: Implement the order in `_sweep`.** The docstring states the four steps and the one commit, in under 12 lines.

- [ ] **Step 4: Run them to see them pass.** Run Step 2's command again. Expected: PASS.

- [ ] **Step 5: The integration case**, in `tests/integration/test_services_reconcile.py`, modelled on `test_a_walk_retracts_only_what_the_source_confirms_gone` (`batch_size=2`):

```python
async def test_a_sweep_re_describes_the_hidden_versions_it_keeps(...) -> None:
    # 20 items, one run. Then m16, m17 and m18 are re-set to mp4 1920x1080 and added to
    # `unlisted`, and del items["m19"]. Run again as `second`.
    assert second.items_seen == 16
    # m16–m18: (container, width, height, last_seen_at) == ("mp4", 1920, 1080, second.started_at)
    # m19: available is False
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/integration/test_services_reconcile.py -p no:randomly`
Expected: PASS. Then the gate.

- [ ] **Step 6: The PRD, in this commit.** In `docs/prd/03-sources-and-sync.md:319-321`, "and records the rest as seen at the walk's start, since the source still serves them." becomes "and re-describes the rest from the entries the source returned, as a walk writes a listed item, recording them as seen at the walk's start in the commit that completes the run, since the source still serves them."

- [ ] **Step 7: Commit**

```bash
git add src/usher/services/reconcile.py tests/unit/test_services_reconcile.py tests/integration/test_services_reconcile.py docs/prd/03-sources-and-sync.md
git commit -m "sync: the sweep re-describes the hidden versions it keeps"
```

### Task 5: `/play` offers copies in `/ways`' quality order

**Files:**
- Modify: `src/usher/services/playback.py:172-188` (`from usher.domain.ways import quality_of`; one sort before the loop; the docstring)
- Test: `tests/unit/test_services_playback.py` (`add_copy` at 195 gains `width: int = 3840, height: int = 2160`)
- Modify: `docs/prd/07-client-api.md:523-524`

**Interfaces:**
- Consumes: `quality_of(MediaItem)` at `src/usher/domain/ways.py:172`, whose `score` is a property.
- Produces: the copies are taken in `sorted(copies, key=lambda copy: (copy.available, quality_of(copy).score), reverse=True)` order. The sort is stable, so the repository's order (the most recently seen first) breaks ties (Decision 9).

- [ ] **Step 1: Write the failing cases**, in `tests/unit/test_services_playback.py`:

```python
async def test_a_better_copy_seen_earlier_is_offered_before_a_worse_one_seen_later() -> None:
    # "Attic Emby" is added first and holds a 1080p copy seen at LATER; "Living Room Emby"
    # holds a 4K copy seen at the freshest case's earlier time.
    assert copies[attic.id].last_seen_at > copies[living_room.id].last_seen_at, "the premise: recency puts Attic first"
    assert copies[attic.id].id < copies[living_room.id].id, "the premise: id order puts Attic first too"
    assert quality_of(copies[living_room.id]).score > quality_of(copies[attic.id]).score
    assert list(dict.fromkeys(t.source_name for t in resolution.targets)) == ["Living Room Emby", "Attic Emby"]

async def test_an_available_copy_is_offered_before_a_retracted_one_of_better_quality() -> None:
    # a retracted 4K copy on "Attic Emby" and an available 1080p copy on "Living Room Emby"
    assert quality_of(copies[attic.id]).score > quality_of(copies[living_room.id]).score, "the premise: quality alone would invert it"
    assert resolution.targets[0].source_name == "Living Room Emby"

def test_play_order_reuses_ways_quality() -> None:
    assert usher.services.playback.quality_of is usher.domain.ways.quality_of
```

  `test_the_freshest_of_two_available_copies_is_offered_first` (785) gains the premise `quality_of(copies[stale.id]).score == quality_of(copies[fresh.id]).score`. Its docstring becomes "the tiebreak among copies of equal quality".

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_services_playback.py -p no:randomly`
Expected: FAIL. The first case offers Attic first, and the third finds no `quality_of` in `playback`. The second already passes, and it pins availability ahead of quality.

- [ ] **Step 3: Implement the sort.** The docstring states the three keys in one sentence.

- [ ] **Step 4: Run them to see them pass.** Run Step 2's command again. Expected: PASS. Then the gate.

- [ ] **Step 5: The PRD, in this commit.** In `docs/prd/07-client-api.md:523-524`, "A household with two copies of one film has two sources and one list of targets ranked across both." becomes "Copies of one film, whether two versions or one film in two places, give one list of targets ranked across them: available copies first, then in `/ways`' quality order, then the most recently seen. Each target names its own copy's source."

- [ ] **Step 6: Commit**

```bash
git add src/usher/services/playback.py tests/unit/test_services_playback.py docs/prd/07-client-api.md
git commit -m "playback: /play offers copies in /ways' quality order"
```

### Task 6: Status rows: this plan in review (controller)

**Files:**
- Modify: `docs/plans/progress.md` (this plan's table), `docs/prd/README.md` (this plan's row)

- [ ] **Step 1: Push and open the PR.** Push with `git push -u origin feat/every-version` and open the PR. Its number is `#N` below.
- [ ] **Step 2: Flip the rows.**
  - In `progress.md`, rows 1–6 read `🔨 in review (PR #N)`, and row 7 stays `📋 planned`.
  - The README row reads `🔨 in review (PR #N); Task 7 (live acceptance) 📋 planned`.
- [ ] **Step 3: Run the docs tests.** Run `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_docs_currency.py tests/unit/test_docs_pointers.py`. Expected: PASS.
- [ ] **Step 4: Commit and push.** Commit both files on top, amending nothing, as `docs(plan): every version playable in review`, and push. The merge is the owner's. A deploy restarts the container and kills a running walk, so merge only while no walk runs.

### Task 7: Live acceptance (controller, owner-gated)

**Preconditions:** the PR is merged and deployed, and the owner has said go for one full walk of the shared Emby source. Shared Emby gets GETs only.

- [ ] **Step 1: The walk.** Run one full walk, so every listed copy is re-described by Task 2's rule and every hidden one by Task 4's sweep. Before it and after it, count the retracted rows Emby still serves (`confirm_gone` over the source's `available = false` rows): no sweep asks about them, so they are reported apart and are not a failure of Step 2. Read the run row's last heartbeat against its finish: that gap is how long the heartbeat stood still during the sweep's ingest, and it is weighed against both the 10-minute stale window and the console's 120-second "No heartbeat" line.
- [ ] **Step 2: The sample.** Re-run the spec's sample over the id files in `/var/tmp/versions/`, which are never committed or posted. For each held and available copy in a version group, check:
  1. its stored description equals `to_source_item(own_entry_only(<its by-id read>))`;
  2. `/play`'s direct target for it carries `MediaSourceId=mediasource_<its own id>`.

  Count the by-id reads in which no entry's `ItemId` names the item. For the groups whose copies differ in quality, check that `/play` now offers the better copy first: stored facts converge only at this walk and its sweep, and until then two versions tie.
- [ ] **Step 3: The bytes.** Take 20 groups whose copies' own `Size`s differ, asserting that premise, since a group of equal sizes could pass on the group's best file. Send a one-byte ranged GET (`Range: bytes=0-0`) to each copy's direct target. Each `Content-Range` total must equal that copy's own entry's `Size`.
- [ ] **Step 4: The fallbacks.** Read the server's `usher.source.versions.fallbacks` total after Step 2's `/play` requests, and the version-fallbacks panel's query beside it. Read the walk's window with `max_over_time`. No series is the expected answer, since the walk and the sweep add nothing by construction: a listing names one entry per item, and the write-up says so. What section 14 of `dashboards/README.md` says about a walk's series disappearing stays an inference from panel 12 unless a series appears and tests it. A nonzero count goes on the PR with the rest, and the rows stay 🔨 until the owner rules on it.
- [ ] **Step 5: Post the raw output** on the PR: counts and booleans only, with no ids, names, paths, hosts or tokens.
- [ ] **Step 6: The status tables.** Turn both to ✅, move PRD 10's version-fallbacks panel to the panels backed by real data, and give section 14 of `dashboards/README.md` its "Returned" line, on a fresh docs branch from `main`, never on the merged one, and the owner merges it.

## Spec premises, checked against the tree

**These hold:**
- The selection is shared: `mapping.py:320` and `playback.py:39` both call `primary_media_source`.
- The `Ids` confirmation sends no `Fields` (`adapter.py:961`).
- The sweep's writes commit with the completing row (`reconcile.py:365-366`).
- `/play` takes copies in the repository's order (`playback.py:188`).
- `quality_of(MediaItem)` exists (`domain/ways.py:172`).
- The stream path names the item's own id (`playback.py:63`).
- The upsert overwrites the descriptive columns.
- `ingest_batch` neither commits nor publishes.
- The failure handler commits without a rollback.

**These no longer hold, or the spec does not say:**
- `tests/fixtures/emby/README.md:34-38` says no real payload has been met, and cites a `CLAUDE.md` passage that does not exist (Task 1).
- The `test_adapters_emby_adapter.py:2396` docstring says no fixture reaches the choice (Task 2).
- `multi_version_movie.json` names no own entry, so it shows only the fallback's shape (Decision 8).
- The capture script cannot read by id, and `_shape` collapses lists (Task 1, Decision 7).
- `_is_synthetic_id` passes any `mediasource_` id (Task 1).
- The fake server's own entry has the template `Id` and no `ItemId`, and its grouped by-id read lists one entry (Task 1, Decision 10).
- The fake's listings ignore `Fields`, so the confirmation's fields are pinned on the wire, not by the contract (Task 3).
- `test_docs_currency.py` names each status table's heading, so a new table needs a constant there.
- The spec does not say that item-level `RunTimeTicks`, `Width` and `Height` belong to the version read. Task 1's Step 1 checks it.

## Residuals

- Slice 2, the versions pass, gets its own plan once this slice's live numbers are in.
- Self-healing is the owner's later design.
