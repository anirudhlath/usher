# Confirmed Retraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Usher marks an item unavailable only when its source confirms it gone,
on a full walk's sweep and on a push `ITEM_REMOVED`, and a removal Emby confirms
reaches the catalog within seconds.

**Architecture:** A new abstract `SourceAdapter.confirm_gone` answers "which of
these ids do you no longer serve?". Emby answers it with `Ids` listings in rounds,
because one listing names only one version of a version group. The sweep reads
the rows a walk did not list, applying the ceiling first. It asks the source,
retracts the confirmed rows and stamps the rest, all in the run's completing
transaction. The push lane asks about the removed rows it holds, and about a
removed series' episodes, then retracts, commits and publishes. Four repository
methods replace `mark_unseen_unavailable`, a fifth finds a series' episodes, and
one counter, `usher.sync.retraction.checks`, counts every id asked.

**Tech Stack:** Python 3.13, asyncio, SQLAlchemy 2 async with asyncpg (`text()`
statements), PostgreSQL 17, httpx, OpenTelemetry, pytest with testcontainers,
and the in-repo fake Emby server.

**Spec:** `docs/specs/2026-10-07-confirmed-retraction-design.md`. Read it before
any task. It is a point-in-time record, so never edit it.

## Decisions this plan makes

The spec leaves these open. Each is settled here so that no task has to settle it.

1. **`list_unseen` is two statements:** a count, then the ids, and the ids only
   when the count is within the ceiling. A refusal reads no id.
2. **`AvailabilitySweepRefused`'s message is unchanged.** Only its docstring
   changes.
3. **`FakeSourceAdapter.confirm_calls` records every question, an empty one
   included.** So "asked nothing" is `confirm_calls == []`, and a caller that
   skipped an empty question is observable.
4. **`FakeSourceAdapter.unlist(id)` models a row the source serves by id but never
   lists.** That is what Emby does with an alternate version.
5. **Emby's request is `{"Ids": ..., "Limit": ...}` and nothing else.** It reads
   each entry's raw `Id`, with no `Fields`, no `Recursive` and no mapper.
6. **A push removal publishes its `title.updated` events sorted** by
   `(str(title_id), str(episode_id))`, so their order is deterministic.
7. **One counter, written in one place.** `usher.services.retraction.confirmed_gone`
   asks, intersects the answer with the question, and counts. An empty question
   asks nothing, and an outcome with no id adds nothing.
8. **`PushOutcome.ignored` is renamed `items_retracted`.**
9. **`PushApplyService` takes `media_items` as a required keyword argument.**
10. **Five contract cases are shared by both adapters**, so the Emby contract's
    pinned case count goes from 55 to 60. The wire-level cases (rounds, `Limit`,
    a malformed body or entry, stray ids alone and beside an asked one, spans) are
    Emby-only, in a new `tests/unit/test_adapters_emby_confirm.py`.
11. **The recorded fixture uses two invented `Id`s:**
    `0000000000000000000000000000e001` is the version the request lists, and
    `0000000000000000000000000000e002` is the other.
12. **The capture asks each pair in both orders.** If the listed version depends on
    the order, Task 1 stops, because the fake would model the wrong rule.
13. **`MediaItemRepository`'s class docstring is rewritten in Task 6**, and gains
    its push sentence in Task 7.
14. **A full walk's sweep logs one INFO line every time, zeros included.** A push
    removal logs one per event.
15. **Each fake affordance arrives with the first task that needs it:**
    `FakeSourceAdapter.fail_confirm()` in Task 3, and `unlist()` in Task 5.
16. **`_beating` is extracted from `_walk_plan`** and shared with the sweep. It
    beats the heartbeat while it awaits work.

## Global Constraints

**The ceiling and errors**

- **The ceiling is a strict count comparison**, `unseen > total *
  max_retract_fraction`. It is consulted only when something is unseen, so a
  ceiling of `0.0` still permits a walk that missed nothing.
- **There is no new error code and no new port error member.**
  `RETRACTION_ERROR_CODE` is unchanged. A confirmation failure is whatever
  `UsherPortError` the adapter raised, recorded with code `None`.

**Names and exact strings**

- **Emby:** `IDS_PER_REQUEST = 100` (unchanged) and the telemetry op `"confirm"`.
  The span is `"source.confirm_gone"`, with attributes `usher.source`,
  `usher.confirm.asked` and `usher.confirm.gone`.
- **The counter:** `usher.sync.retraction.checks`, unit `"1"`, description
  `"Ids asked of a source before retracting them, by path and outcome"`. Its
  labels are `source`, `path` (`sweep` | `push`) and `outcome` (`gone` |
  `served`). Prometheus exports it as `usher_sync_retraction_checks_total`.
- **The sweep's INFO line:** `"{source}'s sweep found {candidates} rows the walk
  did not list: {gone} gone, {served} still served"`
- **The push removal's INFO line:** `"{source} reported {count} items removed:
  {held} held, {gone} gone, {retracted} retracted, {served} still served"`
- **The fake adapter's failure:** `PortUnavailable("source went away while
  confirming removals")`
- **Emby's malformed body:** `PortDataMalformed("Emby's Ids listing carried no
  Items array")`; for an entry with no string `Id`,
  `PortDataMalformed("Emby's Ids listing carried an entry with no string Id")`;
  and for a listing that names only ids nobody asked for,
  `PortDataMalformed("Emby's Ids listing named none of the ids it was asked for")`

**Scope and safety**

- **No migration and no new setting.**
- **No real-server run except Task 1, and only the controller runs that.** No
  implementer runs `docker` against any `usher-prod-*` container, merges, or
  pushes.

**Writing rules**

- **Prose caps, which `guard_prose.py` enforces through a hook:** a module
  docstring is at most 3 lines, a def docstring at most 20 and a comment block at
  most 5. Python comments and docstrings may carry no dates, no 7- or
  40-character hex, no "measured", no `ADR-` numbers and no 🔴.
- **The PRD, the guide and `CHANGELOG.md` change in the same commit as the
  behaviour they describe.**
- **Commits** are `area: sentence`, with no trailers and no model names.
- **Test IMDb ids stay in the `tt99` band.**
- **A plant gets a `cp` backup in `/var/tmp`.** Never use `git checkout`,
  `restore`, `stash` or `reset` to undo one.
- **Never edit an earlier plan or spec.**

**Commands**

- **Iterate with** `PYTHONDONTWRITEBYTECODE=1 uv run pytest <file> -p
  no:randomly`. Do not add `-q`: addopts already carries one.
- **Run the full gate once per task, before its commit.** `tests/integration/`
  needs Docker.
  ```bash
  uv sync --extra eval && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly
  ```

**`text()` binds**

- Write `CAST(:id AS uuid)`, never `::`.
- Pass an array as `= ANY(:external_ids)` with a Python list.
- Read a rowcount through `cast(CursorResult[Any], ...)`.
- Wrap reads in `with self._session.no_autoflush:`.

## Review Focus

1. **A body without `Items` is never read as "nothing listed".** It must raise
   `PortDataMalformed`. Read as an empty listing, it would mark every asked id
   gone. (Task 3, `test_a_body_without_items_raises_rather_than_reading_as_gone`.)
2. **101 ids go as two requests, with `Limit` 100 and 1.** `Limit` bounds what a
   server that ignores `Ids` sends back, so each request carries its own chunk's
   length rather than leaving the server's default to decide.
   (Task 3, `test_a_hundred_and_one_ids_go_as_two_requests_each_limited_to_its_chunk`.)
3. **A push event naming the same id twice handles it once.** It is asked,
   retracted and counted once, and published once. (Task 7,
   `test_a_removal_naming_an_id_twice_asks_and_publishes_once`.)
4. **A push id Usher already shows unavailable is never asked about**, and nothing
   is published for it. (Task 7,
   `test_a_removal_of_an_already_retracted_row_asks_nothing`.)
5. **A full walk with nothing unseen asks nothing, and still logs its zeros.**
   (Task 5, `test_a_sweep_with_nothing_unseen_asks_nothing_and_says_so`.)

## Residuals

These are known and accepted. None blocks this slice.

- **A server that ignored `Ids` is refused, not believed.** A listing that names
  only ids nobody asked for raises `PortDataMalformed`, on walks and on push
  alike. One that names an asked id beside strays is read for the asked id.
  Acceptance check 1 reads every retraction back by id.
- **A push removal expands only a series Usher holds as a matched row.** A
  removed season, folder or library has no row, and an unmatched series' rows
  share no `title_id`, so what they held waits for the next full walk's sweep.
- **The rounds rest on one Emby behaviour**, which Task 1's fixture records.
- **Confirmation scales with the unseen count.** About 290,000 unseen ids on
  Shared Emby take roughly two hours a round at 0.4 requests per second. Slice 2
  bounds this.
- **A removal pushed mid-walk can be undone by that walk.** The next full walk
  confirms and retracts it.
- **Inline confirmation delays the push lane's next event** by however long the
  confirmation takes.
- **Rows earlier sweeps over-retracted are not re-checked.** That is out of scope
  in the spec.

## File map

| File | Task | What changes |
|---|---|---|
| `tests/fixtures/emby/ids_version_group.json` | 1 | a new recording |
| `tests/fixtures/emby/README.md` | 1, 7 | the file count, a file row, the capture note and a protocol row (1); the `LibraryChanged` row's reason (7) |
| `tests/unit/test_no_third_party_data.py` | 1 | the fixture joins the scanned list, and the guard walks every member of an id list |
| `tests/fixtures/README.md`, `.claude/rules/fixtures-and-fakes.md` | 1 | three cases fail if the scans stop scanning |
| `tests/fakes/emby_server.py` | 2 | `group_versions` and `_listed_for` |
| `tests/unit/test_fakes_emby_server.py` | 2 | four cases |
| `src/usher/ports/source.py` | 3 | the abstract `confirm_gone`, and `get_item`'s docstring |
| `src/usher/adapters/emby/adapter.py` | 3 | `confirm_gone` and `_listed_of` |
| `tests/fakes/source_adapter.py` | 3, 5 | `confirm_gone`, `confirm_calls` and `fail_confirm` (3); `unlist` (5) |
| `tests/contract/source_harness.py`, `tests/fakes/emby_harness.py` | 3 | `given_versions` |
| `tests/contract/source_adapter_contract.py` | 3 | five cases |
| `tests/unit/test_adapters_emby_contract.py` | 3 | the case count goes from 55 to 60 |
| `tests/unit/test_adapters_emby_confirm.py` | 3 | new: the wire cases |
| `tests/unit/test_ports_source.py` | 3 | `confirm_gone` is abstract |
| `src/usher/ports/ingest.py` | 4, 5, 6 | `UnseenRows` and `Retraction` (4); two docstrings (5); `SweepResult` goes (6) |
| `src/usher/ports/repository/media_item.py` | 4, 6, 7 | four abstract methods (4); the old method goes and the class docstring changes (6, 7); `available_episodes_of` (7) |
| `src/usher/db/repositories/media_item.py` | 4, 6, 7 | four methods and five statements (4); the old method and its statements go (6); `available_episodes_of` and its statement (7) |
| `tests/fakes/media_item_repository.py` | 4, 6, 7 | four methods (4); the old one goes (6); `available_episodes_of` (7) |
| `tests/contract/media_item_repository_contract.py` | 4, 6, 7 | thirteen cases (4); the old cases go or are re-pointed (6); three cases (7) |
| `tests/integration/test_media_item_repository.py` | 4, 6, 7 | four statement-count cases (4); two old ones go (6); one statement-count case (7) |
| `tests/unit/test_ports_repository_ingest.py` | 4, 6, 7 | the surface set |
| `src/usher/services/retraction.py` | 5 | new: `confirmed_gone` and the counter |
| `src/usher/services/reconcile.py` | 5 | `_beating`, and the sweep |
| `tests/unit/test_services_reconcile.py` | 5 | the sweep cases, with the window case re-pointed |
| `tests/unit/test_telemetry_retraction.py` | 5 | new, with the counter reader the sweep cases import |
| `tests/integration/test_services_reconcile.py` | 5 | `confirm_gone` on the duck adapter, and one case |
| `tests/integration/test_pipeline_spans.py` | 5 | `confirm_gone` on its duck adapter |
| `docs/prd/03-sources-and-sync.md` | 5, 7 | the retraction paragraph, the lanes table and the push paragraph |
| `docs/prd/08-operations.md`, `docs/guide/command-line.md` | 5, 7 | the failure rows and the guide bullets |
| `docs/prd/10-telemetry-and-dashboards.md` | 5, 8 | the counter row and the count (5); Dashboard 3 (8) |
| `tests/unit/test_telemetry_metric_names.py`, `tests/unit/test_dashboards.py` | 5, 8 | the catalogue count goes from 44 to 45 (5); twelve panels and the panel's own case (8) |
| `tests/unit/test_alerts.py` | 8 | dashboard 3's panel count |
| `CHANGELOG.md` | 5, 7, 8 | Fixed, Added and Changed |
| `.claude/rules/emby-push-and-ingest.md`, `CLAUDE.md` | 5, 7 | `paths:`, the rules row and a resumed-walk wording fix (5); `ItemsRemoved` and the `Ids` finding (7) |
| the old sweep's callers, two scripts and a model comment (Task 6's list) | 6 | moved to the new methods |
| `src/usher/services/push.py`, `src/usher/composition.py` | 7 | `_apply_removal`, with `media_items` wired in |
| the push tests (Task 7's list) | 7 | the construction sites and new cases |
| `dashboards/03-pipeline.json`, `dashboards/README.md` | 8 | panel 12 |
| `docs/plans/progress.md`, `docs/prd/README.md` | 9 | the status cells |

---

### Task 1 (controller only): record how an `Ids` listing answers two versions of one item

This task talks to a real server, so **only the controller runs it**. An
implementer subagent never does. It writes nothing to the server, and nothing
that identifies the server or its account goes into the repo.

**Files:**
- Create: `/var/tmp/confirmed-retraction/capture_ids.py`, outside the repo and never committed
- Create: `tests/fixtures/emby/ids_version_group.json`
- Modify: `tests/fixtures/emby/README.md`
- Modify: `tests/unit/test_no_third_party_data.py`
- Modify: `tests/fixtures/README.md` and `.claude/rules/fixtures-and-fakes.md`

**Interfaces:**
- Consumes: `/var/tmp/sync-speed/versions.txt`, an earlier probe's output. Each
  line is `<alternate id> mediasource_<sibling id>`.
- Produces: `tests/fixtures/emby/ids_version_group.json`, shaped
  `{"versions": [{"Id": ...}, {"Id": ...}], "listings": [{"asked": [...], "Items": [{"Id": ...}]}, ...]}`.
  The listed version comes first in `versions`. It holds four listings: both
  versions in one order, both in the other order, the listed version alone, and
  the other version alone. Task 2 replays it.

- [ ] **Step 1: Write the capture script**

Write `/var/tmp/confirmed-retraction/capture_ids.py`:

```python
# Throwaway, read-only: how does an `Ids` listing answer two versions of one item?
# Prints counts and booleans only: no ids, names, tokens, user ids or hosts.
import asyncio
import json
import pathlib

from usher.adapters.emby.adapter import _segment
from usher.cli import _session_for
from usher.composition import build_pipeline, open_adapter, selected_sources
from usher.config import Settings

PAIRS = pathlib.Path("/var/tmp/sync-speed/versions.txt")
OUT = pathlib.Path("/var/tmp/confirmed-retraction/ids_version_group.json")
WINNER, LOSER = "0000000000000000000000000000e001", "0000000000000000000000000000e002"


async def main() -> None:
    settings = Settings()
    async with _session_for(settings) as session:
        pipeline = build_pipeline(session, settings)
        [source] = await selected_sources(pipeline, "Shared Emby")
        adapter = await open_adapter(pipeline, source)
    assert adapter is not None
    recorded = False
    try:
        items = f"/Users/{_segment(await adapter._session.user_id())}/Items"

        async def listed(ids: list[str]) -> list[str]:
            await asyncio.sleep(3.0)
            body = await adapter._session.json_body(
                "GET", items, params={"Ids": ",".join(ids), "Limit": str(len(ids))}, op="probe"
            )
            return [entry["Id"] for entry in body["Items"]]

        for number, line in enumerate(PAIRS.read_text().splitlines()[:5]):
            alternate, sibling = line.split()[0], line.split()[1].removeprefix("mediasource_")
            alone = [await listed([alternate]), await listed([sibling])]
            served = alone == [[alternate], [sibling]]
            print(f"P{number}: both-served-alone={served}", flush=True)
            if not served:
                continue
            forward = await listed([alternate, sibling])
            backward = await listed([sibling, alternate])
            same = forward == backward
            print(f"P{number}: forward={len(forward)} backward={len(backward)} "
                  f"same-winner={same}", flush=True)
            if len(forward) != 1 or not same:
                print(f"STOP: P{number} breaks the one-version-per-request rule", flush=True)
                return
            if recorded:
                continue
            winner = forward[0]
            loser = sibling if winner == alternate else alternate
            invent = {winner: WINNER, loser: LOSER}
            listings = [
                ([alternate, sibling], forward),
                ([sibling, alternate], backward),
                ([winner], alone[0] if winner == alternate else alone[1]),
                ([loser], alone[1] if winner == alternate else alone[0]),
            ]
            OUT.write_text(json.dumps({
                "versions": [{"Id": WINNER}, {"Id": LOSER}],
                "listings": [
                    {"asked": [invent[one] for one in asked],
                     "Items": [{"Id": invent[one]} for one in got]}
                    for asked, got in listings
                ],
            }, indent=2) + "\n")
            recorded = True
    finally:
        print(f"DONE: recorded={recorded}", flush=True)
        await adapter.aclose()


asyncio.run(main())
```

- [ ] **Step 2: Run it against Shared Emby**

```bash
mkdir -p /var/tmp/confirmed-retraction
cd /home/anirudhlath/code/.worktrees/usher/confirmed-retraction && timeout 900 uv run --env-file /home/anirudhlath/code/usher-devdb/.env python /var/tmp/confirmed-retraction/capture_ids.py > /var/tmp/confirmed-retraction/capture.raw 2>&1; cat /var/tmp/confirmed-retraction/capture.raw
```

Expected:
- at least one pair prints `both-served-alone=True`, followed by
  `forward=1 backward=1 same-winner=True`;
- the last line is `DONE: recorded=True`;
- no line starts `STOP`.

**On a `STOP` line, stop the plan** and report the raw output to the owner. The
fake would model the wrong rule.

- [ ] **Step 3: Check that the recording holds only the invented ids**

```bash
grep -oE '"Id": "[^"]+"' /var/tmp/confirmed-retraction/ids_version_group.json | sort -u
```

Expected: exactly two lines, `"Id": "0000000000000000000000000000e001"` and
`"Id": "0000000000000000000000000000e002"`.

- [ ] **Step 4: Copy the recording into the repo**

```bash
cp /var/tmp/confirmed-retraction/ids_version_group.json tests/fixtures/emby/ids_version_group.json
```

- [ ] **Step 5: Describe it in `tests/fixtures/emby/README.md`**

Make four edits.

1. Change `**Every value in these eight files is invented**` to
   `**Every value in these nine files is invented**`.
2. After the file-table row for `view_item.json`, add:

   ```markdown
   | `ids_version_group.json` | an `Ids` listing naming two versions of one item, asked both ways round, then each version alone: the request lists one of them, the same one in either order, and each alone lists itself. Only `Id` keys, both invented |
   ```

3. After the paragraph that ends `The one shape kept is
   `view_item.json`, every value in it invented.`, add this paragraph:

   ```markdown
   The version-group row comes from a third read-only script, which asked the
   same server by `Ids` for pairs of versions an earlier probe had found,
   printed counts and booleans only, and wrote `ids_version_group.json` with
   both ids replaced by invented ones.
   ```

4. After the protocol-table row that begins `` | `Ids=a,b,c` | ``, add this
   row. Its sample is the capture's: four pairs served alone and asked both ways
   round, and a fifth skipped as not served alone.

   ```markdown
   | `Ids` naming two versions of one item | **one** of them, the same one whichever order they are named in, for 4 of 4 pairs; asked alone, each is listed. A fifth pair was skipped, because one of its versions was no longer listed alone | `EmbyAdapter.confirm_gone` asks again, in rounds, for every id a round did not list; `FakeEmbyServer.group_versions`; `ids_version_group.json` |
   ```

- [ ] **Step 6: Put the fixture inside the third-party-data guard**

In `tests/unit/test_no_third_party_data.py`, in the list that
`test_the_guard_reads_what_it_claims_to_read` is parametrized over, add
`"tests/fixtures/emby/ids_version_group.json",` directly after
`"tests/fixtures/emby/view_item.json",`.

The guard reads a scalar only under an id key, so the six ids in the
recording's `asked` arrays would go unread. Teach it to walk id lists. Directly
after `_ID_KEYS`, add:

```python
# Keys whose value is a *list* of entity ids, each member checked as an `_ID_KEYS`
# value is: the ids each `Ids` request named in `emby/ids_version_group.json`, every
# array of a `LibraryChanged` frame, and a TMDb search result's `genre_ids`.
_ID_LIST_KEYS = frozenset(
    {
        "asked",
        "ItemsAdded",
        "ItemsRemoved",
        "ItemsUpdated",
        "FoldersAddedTo",
        "FoldersRemovedFrom",
        "CollectionFolders",
        "genre_ids",
    }
)
```

In `_walk_ids`, change the docstring to
`"""Every value under an id-bearing key, and every member of an id list, with a path to it."""`,
and directly after the `elif key in _ID_KEYS and not isinstance(value, dict | list):`
branch, add:

```python
            elif key in _ID_LIST_KEYS and isinstance(value, list):
                for index, member in enumerate(value):
                    at = f"{where}.{key}[{index}]"
                    if isinstance(member, dict | list):
                        found.extend(_walk_ids(member, at))
                    else:
                        found.append((at, member))
```

Directly after `test_every_id_in_a_fixture_is_synthetic`, add:

```python
@pytest.mark.parametrize(
    "key",
    [
        "asked",
        "ItemsAdded",
        "ItemsRemoved",
        "ItemsUpdated",
        "FoldersAddedTo",
        "FoldersRemovedFrom",
        "CollectionFolders",
        "genre_ids",
    ],
)
def test_every_member_of_an_id_list_is_walked(key: str) -> None:
    """A list under an id-list key is walked member by member, nested entries included.

    Without this, dropping a key from `_ID_LIST_KEYS`, or the branch that reads it,
    leaves every fixture case green: the committed lists hold synthetic ids, so the
    guard passes whether it reads them or not. Parametrized rather than a set
    comparison, as `test_the_guard_reads_what_it_claims_to_read` is.
    """
    walked = _walk_ids({key: ["a", {"Id": "b"}]}, "f.json")
    assert walked == [(f"f.json.{key}[0]", "a"), (f"f.json.{key}[1].Id", "b")]
```

The guard now has three cases that fail if the scans stop scanning, so the two
documents that count them change:

- In `tests/fixtures/README.md`, replace `Two more fail if the scans stop
  scanning. Mutation-verified: eleven mutations, eleven killed.` with
  `Three more fail if the scans stop scanning. Mutation-verified when the guard
  had two of them: eleven mutations, eleven killed. The third was added with the
  walk over id lists and verified by planting against it.`
- In `.claude/rules/fixtures-and-fakes.md`, change `**Keep the two cases that
  fail if the scans stop scanning**` to `**Keep the three cases that fail if the
  scans stop scanning**`, and in the mutation list after it, insert `an id
  list's members left unwalked,` before `each matcher made to match nothing`.

- [ ] **Step 7: Run the guard, plant the walk away, then the gate**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_no_third_party_data.py -p no:randomly`

Expected: PASS.

Plant the new branch away once:
1. Back up: `cp tests/unit/test_no_third_party_data.py /var/tmp/confirmed-retraction-plant.bak`.
2. With Edit, delete the `elif key in _ID_LIST_KEYS` branch.
3. Rerun the file. Expected: all eight parameters of
   `test_every_member_of_an_id_list_is_walked` fail, and nothing else does.
4. Restore and read the restore back:
   `cp /var/tmp/confirmed-retraction-plant.bak tests/unit/test_no_third_party_data.py && diff /var/tmp/confirmed-retraction-plant.bak tests/unit/test_no_third_party_data.py`.

Then run the full gate (Global Constraints). Expected: green.

- [ ] **Step 8: Commit**

```bash
git add tests/fixtures/emby/ids_version_group.json tests/fixtures/emby/README.md tests/unit/test_no_third_party_data.py tests/fixtures/README.md .claude/rules/fixtures-and-fakes.md
git commit -m "tests: record how an Ids listing answers two versions of one item"
```

---

### Task 2: the fake Emby server learns version groups

**Files:**
- Modify: `tests/fakes/emby_server.py`: `__init__` (line 144), a new
  `group_versions` beside `add_alternate_version` (line 208), `_ordered` (lines
  431–470) and a new `_listed_for`
- Test: `tests/unit/test_fakes_emby_server.py`, directly after
  `test_ids_lists_only_the_named_items` (line 983)

**Interfaces:**
- Consumes: `tests/fixtures/emby/ids_version_group.json` from Task 1.
- Produces: `FakeEmbyServer.group_versions(self, *external_ids: str) -> None`.
  Task 3's Emby harness calls it.

- [ ] **Step 1: Write the failing tests**

Add these four cases to `tests/unit/test_fakes_emby_server.py`, directly after
`test_ids_lists_only_the_named_items`. Every name they use is already imported
or defined in that module: `replace`, `load_emby_fixture`, `MOVIE`, `ADDED_AT`,
`_Driver`, `_listed` and the `driver` fixture.

```python
async def test_an_ids_listing_replays_the_recorded_version_group(driver: _Driver) -> None:
    """Both versions share a name, so only the group decides which one is listed."""
    recorded = load_emby_fixture("ids_version_group")
    versions = [entry["Id"] for entry in recorded["versions"]]
    for version in versions:
        driver.server.add_item(replace(MOVIE, external_id=version), ADDED_AT)
    driver.server.group_versions(*versions)
    listings = recorded["listings"]
    assert len(listings) == 4, "the premise: both orders, then each version alone"
    assert {tuple(listing["asked"]) for listing in listings} == {
        tuple(versions),
        tuple(reversed(versions)),
        (versions[0],),
        (versions[1],),
    }, "the premise: both orders, then each version alone"
    for listing in listings:
        asked = listing["asked"]
        assert await _listed(driver, Ids=",".join(asked), Limit=str(len(asked))) == [
            entry["Id"] for entry in listing["Items"]
        ]


async def test_a_version_group_lists_its_next_member_once_the_first_is_gone(
    driver: _Driver,
) -> None:
    for version in ("v0", "v1"):
        driver.server.add_item(replace(MOVIE, external_id=version), ADDED_AT)
    driver.server.group_versions("v0", "v1")
    driver.server.remove_item("v0")
    assert await _listed(driver, Ids="v0,v1", Limit="2") == ["v1"]


async def test_a_version_group_changes_no_listing_but_one_by_ids(driver: _Driver) -> None:
    for version in ("v0", "v1"):
        driver.server.add_item(replace(MOVIE, external_id=version), ADDED_AT)
    driver.server.group_versions("v0", "v1")
    assert sorted(await _listed(driver)) == ["v0", "v1"]


async def test_a_version_group_lists_by_its_own_order_not_the_ids(driver: _Driver) -> None:
    for version in ("v0", "v1"):
        driver.server.add_item(replace(MOVIE, external_id=version), ADDED_AT)
    driver.server.group_versions("v1", "v0")
    assert await _listed(driver, Ids="v0,v1", Limit="2") == ["v1"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_fakes_emby_server.py -p no:randomly -k "version_group"`

Expected: the first two and the fourth FAIL with `AttributeError:
'FakeEmbyServer' object has no attribute 'group_versions'`. The third fails the
same way, because it calls `group_versions` before it lists.

- [ ] **Step 3: Implement the groups**

In `FakeEmbyServer.__init__`, beside the other item stores, add:

```python
        # External id -> its version group, in the group's own order.
        self._groups: dict[str, tuple[str, ...]] = {}
```

Add this method directly after `add_alternate_version`:

```python
    def group_versions(self, *external_ids: str) -> None:
        """Hold `external_ids` as versions of one item, in this order.

        An `Ids` listing then names one member of the group per request: the first, in
        this order, of those the rest of the request matches, before paging. That is
        what `ids_version_group.json` records Emby doing. Emby's full listing shows one
        version per group too; this fake does not model that, and no walk test groups
        versions.
        """
        group = tuple(external_ids)
        for external_id in group:
            self._groups[external_id] = group
```

At the end of `_ordered`, replace the final `return ordered` with:

```python
        if ids:
            # One member of each version group per `Ids` request: see `group_versions`.
            present = set(ordered)
            ordered = [one for one in ordered if self._listed_for(one, present)]
        return ordered
```

Add this method directly after `_ordered`:

```python
    def _listed_for(self, external_id: str, present: set[str]) -> bool:
        """Whether `external_id` is its group's first member among `present`."""
        group = self._groups.get(external_id, (external_id,))
        return next(member for member in group if member in present) == external_id
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_fakes_emby_server.py -p no:randomly`

Expected: PASS, every case in the file.

- [ ] **Step 5: Plant the rule false, once**

The replay case and the own-order case are the tests of which member is listed.
Prove the replay can fail against the recording.

1. Back up the file:
   `cp tests/fakes/emby_server.py /var/tmp/confirmed-retraction-plant.bak`
2. With Edit, change `_listed_for`'s return to
   `return next(member for member in reversed(group) if member in present) == external_id`.
3. Run the replay case and confirm it fails on its `assert await _listed(...)` line:
   `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_fakes_emby_server.py -p no:randomly -k replays 2>&1 | grep -E '^E '`
4. Restore the file and read the restore back:
   `cp /var/tmp/confirmed-retraction-plant.bak tests/fakes/emby_server.py && diff /var/tmp/confirmed-retraction-plant.bak tests/fakes/emby_server.py`

Expected: the run in step 3 fails, and the `diff` in step 4 prints nothing.

- [ ] **Step 6: Run the gate, then commit**

Run the full gate. Expected: green.

```bash
git add tests/fakes/emby_server.py tests/unit/test_fakes_emby_server.py
git commit -m "tests: the fake Emby server lists one version of a group per Ids request"
```

---

### Task 3: `confirm_gone` on the port and on both adapters

**Files:**
- Modify: `src/usher/ports/source.py`: the imports (line 6), `get_item`'s
  docstring, and a new abstract method after `get_item`
- Modify: `src/usher/adapters/emby/adapter.py`: the imports (line 7), the comment
  above `IDS_PER_REQUEST` (line 86), `_page`'s docstring (line 507 on), and two
  new methods after `get_item` (line 868)
- Modify: `tests/fakes/source_adapter.py`: the imports (line 5), `__init__`,
  `clear_failure` (line 169), a new `fail_confirm`, a new `confirm_gone`, and
  `FakeSourceHarness.given_versions`
- Modify: `tests/contract/source_harness.py` (an abstract `given_versions`) and
  `tests/fakes/emby_harness.py` (its `given_versions`)
- Modify: `tests/contract/source_adapter_contract.py`: five cases
- Modify: `tests/unit/test_adapters_emby_contract.py`: the case count goes from 55 to 60
- Create: `tests/unit/test_adapters_emby_confirm.py`
- Modify: `tests/unit/test_ports_source.py`

**Interfaces:**
- Consumes: `FakeEmbyServer.group_versions(*external_ids: str)` from Task 2.
- Produces:
  - `SourceAdapter.confirm_gone(self, external_ids: Collection[str]) -> frozenset[str]`,
    abstract.
  - `FakeSourceAdapter.confirm_calls: list[frozenset[str]]`, which records every
    question in order, an empty one included.
  - `FakeSourceAdapter.fail_confirm() -> None`. After it, a non-empty question
    raises `PortUnavailable("source went away while confirming removals")`.
    `clear_failure()` undoes it.
  - `SourceHarness.given_versions(items: Sequence[SourceItem], *, changed_at: AwareDatetime) -> None`,
    async.
  - Tasks 5 and 7 call `confirm_gone` only through
    `usher.services.retraction.confirmed_gone`.

- [ ] **Step 1: Give the harnesses `given_versions`**

This is test infrastructure that the failing cases need. In
`tests/contract/source_harness.py`, after `given_item_in_libraries`, add:

```python
    @abstractmethod
    async def given_versions(
        self, items: Sequence[SourceItem], *, changed_at: AwareDatetime
    ) -> None:
        """`given_item` for each of `items`, held as versions of one item.

        Emby groups a film's files into one item with several versions, and its `Ids`
        listing names one version of a group per request.
        """
```

In `tests/fakes/source_adapter.py`, in `FakeSourceHarness`, after
`given_item_in_libraries`, add:

```python
    async def given_versions(
        self, items: Sequence[SourceItem], *, changed_at: AwareDatetime
    ) -> None:
        # The fake answers by id alone, so versions are just items.
        for item in items:
            self._adapter.seed(item, changed_at)
```

In `tests/fakes/emby_harness.py`, after `given_item_in_libraries`, add:

```python
    async def given_versions(
        self, items: Sequence[SourceItem], *, changed_at: AwareDatetime
    ) -> None:
        for item in items:
            self._server.add_item(item, changed_at)
        self._server.group_versions(*(item.external_id for item in items))
```

- [ ] **Step 2: Write the shared contract cases**

In `tests/contract/source_adapter_contract.py`, add `from dataclasses import
replace` to the imports. Then, directly after
`test_get_item_raises_when_the_source_is_unreachable` and before
`# --- authentication ---`, add:

```python
    # --- confirming removals -------------------------------------------

    async def test_confirm_gone_returns_what_the_source_no_longer_serves(
        self, harness: SourceHarness
    ) -> None:
        """A removed id and one the source never had are gone; a served one is not."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.given_item(_filler(0), changed_at=T0)
        await harness.remove_item("movie-1")
        gone = await harness.adapter.confirm_gone(["movie-1", "filler-0", "never-existed"])
        assert gone == {"movie-1", "never-existed"}

    async def test_confirm_gone_returns_none_of_a_served_items_versions(
        self, harness: SourceHarness
    ) -> None:
        """Three versions asked together: a listing that names one must not condemn two."""
        versions = [replace(MOVIE, external_id=f"movie-1-v{index}") for index in range(3)]
        await harness.given_versions(versions, changed_at=T0)
        gone = await harness.adapter.confirm_gone([version.external_id for version in versions])
        assert gone == frozenset()

    async def test_confirm_gone_returns_a_removed_version_beside_a_served_one(
        self, harness: SourceHarness
    ) -> None:
        versions = [replace(MOVIE, external_id=f"movie-1-v{index}") for index in range(2)]
        await harness.given_versions(versions, changed_at=T0)
        await harness.remove_item("movie-1-v0")
        gone = await harness.adapter.confirm_gone(["movie-1-v0", "movie-1-v1"])
        assert gone == {"movie-1-v0"}

    async def test_confirm_gone_of_nothing_asks_nothing(self, harness: SourceHarness) -> None:
        """Offline first: an implementation that asked anyway would raise, not answer."""
        await harness.go_offline()
        assert await harness.adapter.confirm_gone([]) == frozenset()

    async def test_confirm_gone_raises_when_the_source_is_unreachable(
        self, harness: SourceHarness
    ) -> None:
        """Never "nothing came back, so all gone": a transport failure must raise."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.go_offline()
        with pytest.raises(PortUnavailable):
            await harness.adapter.confirm_gone(["movie-1"])
```

In `tests/unit/test_adapters_emby_contract.py`, change both `55`s in
`TestEmbyAdapter`'s docstring to `60`, and change `assert len(cases) == 55` to
`assert len(cases) == 60`.

- [ ] **Step 3: Write the Emby wire cases**

Create `tests/unit/test_adapters_emby_confirm.py`:

```python
"""`EmbyAdapter.confirm_gone` on the wire: its rounds, its chunks, and what it will not read."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from tests.fakes.emby_server import FakeEmbyServer
from usher.adapters.emby.adapter import IDS_PER_REQUEST, EmbyAdapter
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortDataMalformed
from usher.ports.source import SourceItem, SourceItemKind

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
SOURCE = Source(
    id=new_id(),
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-1",
    device_id=str(new_id()),
)


def _movie(external_id: str) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=f"Movie {external_id}",
        kind=SourceItemKind.MOVIE,
        year=2021,
        container="mkv",
    )


def _adapter(
    server: FakeEmbyServer, answer: dict[str, object] | None = None
) -> tuple[EmbyAdapter, list[httpx.Request]]:
    """An adapter over `server`, every request it sends, and an optional canned `Ids` answer."""
    seen: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        # A turn of the loop per request, as a real one gives, so a deadline can fire.
        await asyncio.sleep(0)
        seen.append(request)
        if answer is not None and "Ids" in request.url.params:
            return httpx.Response(200, json=answer)
        return server.handle(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url=SOURCE.base_url)
    return EmbyAdapter(SOURCE, CREDENTIALS, client=client), seen


def _asked(seen: list[httpx.Request]) -> list[str]:
    """Each `Ids` request's ids, in the order the requests were sent."""
    return [request.url.params["Ids"] for request in seen if "Ids" in request.url.params]


async def test_a_served_version_its_sibling_hid_is_asked_again_and_kept() -> None:
    """One `Ids` request lists one version of a group, so the next round asks for the other."""
    server = FakeEmbyServer()
    for version in ("v0", "v1"):
        server.add_item(_movie(version), T0)
    server.group_versions("v0", "v1")
    adapter, seen = _adapter(server)
    try:
        gone = await adapter.confirm_gone(["v1", "v0"])
    finally:
        await adapter.aclose()
    assert gone == frozenset()
    assert _asked(seen) == ["v0,v1", "v1"]


async def test_a_listing_of_only_ids_nobody_asked_for_raises_rather_than_reading_as_gone() -> None:
    """A correct server never lists an id it was not asked for, so this one cannot decide.

    It is what a server ignoring `Ids` sends. Read as "none listed", it would condemn every
    asked id; counted as listed, it would keep every round non-empty, and the rounds would
    never end: hence the deadline.
    """
    stray = {"Items": [{"Id": "stray-1"}], "TotalRecordCount": 1}
    adapter, seen = _adapter(FakeEmbyServer(), answer=stray)
    try:
        async with asyncio.timeout(5):
            with pytest.raises(PortDataMalformed, match="none of the ids it was asked for"):
                await adapter.confirm_gone(["gone-1"])
    finally:
        await adapter.aclose()
    assert _asked(seen) == ["gone-1"]


async def test_a_stray_id_beside_an_asked_one_is_skipped() -> None:
    """The listing names an asked id, so it decided; the stray beside it is not counted."""
    answer = {"Items": [{"Id": "kept-1"}, {"Id": "stray-1"}], "TotalRecordCount": 2}
    adapter, seen = _adapter(FakeEmbyServer(), answer=answer)
    try:
        async with asyncio.timeout(5):
            gone = await adapter.confirm_gone(["kept-1"])
    finally:
        await adapter.aclose()
    assert gone == frozenset()
    assert _asked(seen) == ["kept-1"]


async def test_a_hundred_and_one_ids_go_as_two_requests_each_limited_to_its_chunk() -> None:
    """`Limit` is each chunk's own length, so a server that ignores `Ids` sends back no more."""
    asked = [f"gone-{index:03d}" for index in range(IDS_PER_REQUEST + 1)]
    adapter, seen = _adapter(FakeEmbyServer())
    try:
        gone = await adapter.confirm_gone(asked)
    finally:
        await adapter.aclose()
    assert gone == frozenset(asked)
    limits = [request.url.params.get("Limit") for request in seen if "Ids" in request.url.params]
    assert limits == [str(IDS_PER_REQUEST), "1"]


async def test_a_body_without_items_raises_rather_than_reading_as_gone() -> None:
    adapter, _ = _adapter(FakeEmbyServer(), answer={"TotalRecordCount": 0})
    try:
        with pytest.raises(PortDataMalformed, match="no Items array"):
            await adapter.confirm_gone(["kept-1"])
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "entry",
    [{"Name": "Movie kept-1"}, {"id": "kept-1"}, {"Id": 1}, "kept-1"],
    ids=["no-id", "lowercase-id", "integer-id", "bare-string"],
)
async def test_an_entry_without_a_string_id_raises_rather_than_reading_as_gone(
    entry: object,
) -> None:
    """An entry with no readable `Id` may be the asked one, so the round cannot decide."""
    adapter, _ = _adapter(FakeEmbyServer(), answer={"Items": [entry], "TotalRecordCount": 1})
    try:
        with pytest.raises(PortDataMalformed, match="no string Id"):
            await adapter.confirm_gone(["kept-1"])
    finally:
        await adapter.aclose()


async def test_a_confirmation_is_timed_as_confirm_and_spanned_with_its_counts() -> None:
    """Timed as `list`, a hundred-id read would share a bucket with pages of a thousand."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    server = FakeEmbyServer()
    server.add_item(_movie("kept-1"), T0)
    adapter, seen = _adapter(server)
    try:
        gone = await adapter.confirm_gone(["kept-1", "gone-1"])
    finally:
        await adapter.aclose()
    assert gone == {"gone-1"}
    reads = [request for request in seen if request.url.path != "/Users/AuthenticateByName"]
    assert len(reads) == 2, "the premise: one round listed kept-1, the next listed nothing"
    spans = exporter.get_finished_spans()
    labels = [
        span.attributes["usher.op"]
        for span in spans
        if span.name == "source.request" and span.attributes is not None
    ]
    assert labels == ["confirm", "confirm"]
    [confirming] = [span for span in spans if span.name == "source.confirm_gone"]
    assert confirming.attributes is not None
    counts = (
        confirming.attributes["usher.confirm.asked"],
        confirming.attributes["usher.confirm.gone"],
    )
    assert counts == (2, 1)
```

- [ ] **Step 4: Pin the port method as abstract**

In `tests/unit/test_ports_source.py`, add the case below, and import
`SourceAdapter` from `usher.ports.source` if the module does not already:

```python
def test_confirm_gone_is_abstract_so_no_adapter_inherits_a_silent_default() -> None:
    """A default answering "none gone" would switch retraction off for a new adapter."""
    assert "confirm_gone" in SourceAdapter.__abstractmethods__
```

- [ ] **Step 5: Run the new cases to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_confirm.py tests/unit/test_ports_source.py tests/unit/test_source_adapter_contract.py tests/unit/test_adapters_emby_contract.py -p no:randomly`

Expected:
- `test_confirm_gone_is_abstract...` FAILS its assertion;
- every confirm case FAILS with `AttributeError: ... has no attribute 'confirm_gone'`;
- the contract-count case already PASSES, because it counts the contract's
  `test_` names and Step 2 added five.

- [ ] **Step 6: Add the abstract port method**

In `src/usher/ports/source.py`, change line 6 to
`from collections.abc import AsyncGenerator, AsyncIterator, Collection`.

Replace `get_item`'s docstring with:

```python
        """Fetch one item.

        `None` means the source no longer serves it. A transient failure to reach the
        source must raise (`PortUnavailable`), never return `None`: conflating the two
        would read a flaky network as a deletion.
        """
```

Directly after `get_item`, add:

```python
    @abstractmethod
    async def confirm_gone(self, external_ids: Collection[str]) -> frozenset[str]:
        """The subset of `external_ids` this source no longer serves.

        "Serves" means the account can read the item by its id. Never returns an id the
        source still serves, and raises a `UsherPortError` when it cannot decide: a
        partial answer is never returned as a whole one. Only asked ids come back, and an
        empty input returns an empty set without a request.

        Abstract on purpose: a default answering "none gone" would quietly switch
        retraction off for a new adapter, and one built on `get_item` would read an item
        the mapper cannot model as gone.
        """
```

- [ ] **Step 7: Implement it on the fake adapter**

In `tests/fakes/source_adapter.py`, change line 5 to
`from collections.abc import AsyncGenerator, AsyncIterator, Collection, Sequence`.

At the end of `FakeSourceAdapter.__init__`, add:

```python
        #: Every `confirm_gone` question, in order, an empty one included.
        self.confirm_calls: list[frozenset[str]] = []
        self._confirm_fails = False
```

Directly after `clear_failure`, add:

```python
    def fail_confirm(self) -> None:
        """Make every non-empty `confirm_gone` raise, as a source lost mid-question would."""
        self._confirm_fails = True
```

In `clear_failure`, change the docstring's first line to
`"""Undo `fail_after`, `fail_unit_after` and `fail_confirm`.`, and add
`self._confirm_fails = False` as the body's last line.

Directly after `get_item`, add:

```python
    async def confirm_gone(self, external_ids: Collection[str]) -> frozenset[str]:
        asked = frozenset(external_ids)
        self.confirm_calls.append(asked)
        if not asked:
            return frozenset()
        await self._ready()
        if self._confirm_fails:
            raise PortUnavailable("source went away while confirming removals")
        # A turn of the loop, as a request gives, so a caller's heartbeat can run.
        await asyncio.sleep(0)
        return frozenset(external_id for external_id in asked if external_id not in self._items)
```

- [ ] **Step 8: Implement it on the Emby adapter**

In `src/usher/adapters/emby/adapter.py`:

- Change line 7 to the `collections.abc` import below, which `ruff format`
  wraps because one line would pass 100 columns:

  ```python
  from collections.abc import (
      AsyncGenerator,
      AsyncIterator,
      Awaitable,
      Callable,
      Collection,
      Mapping,
      Sequence,
  )
  ```

- Change the comment above `IDS_PER_REQUEST = 100` to
  `# Items asked for by `Ids` in one request: a hundred ids keep the URL short.`
- In `_page`'s docstring, change `so a count, a views read or a read of series by
  `Ids` is not timed as a listing page.` to `so a count, a views read, a read of
  series by `Ids` or a confirmation is not timed as a listing page.`

Directly after `get_item`, add:

```python
    async def confirm_gone(self, external_ids: Collection[str]) -> frozenset[str]:
        """The asked ids Emby no longer lists for this account, read by `Ids` in rounds.

        An `Ids` listing names one member of a version group per request, so an id a round
        does not list is asked again in the next. A round whose listings are all empty
        proves the rest gone: it would have listed a member of every group with one still
        served. Only an id that was asked for counts.
        """
        with _tracer.start_as_current_span("source.confirm_gone") as span:
            remaining = set(external_ids)
            span.set_attribute("usher.source", self._source.name)
            span.set_attribute("usher.confirm.asked", len(remaining))
            while remaining:
                listed: set[str] = set()
                for chunk in batched(sorted(remaining), IDS_PER_REQUEST, strict=False):
                    listed |= await self._listed_of(chunk)
                if not listed:
                    break
                remaining -= listed
            span.set_attribute("usher.confirm.gone", len(remaining))
            return frozenset(remaining)

    async def _listed_of(self, chunk: Sequence[str]) -> set[str]:
        """Which of `chunk` one `Ids` listing names; `Limit` is the chunk's length.

        An entry with no string `Id` raises, because it may be one of `chunk`. A string
        `Id` nobody asked for is skipped beside one that was asked for. A listing of only
        such ids raises: a server that answers `Ids` never sends one, so a server that
        does cannot say what is gone.
        """
        params = {"Ids": ",".join(chunk), "Limit": str(len(chunk))}
        body = await self._page(await self._items_path(), params, 0, op="confirm")
        entries = body.get("Items")
        if not isinstance(entries, list):
            raise PortDataMalformed("Emby's Ids listing carried no Items array")
        asked = set(chunk)
        listed: set[str] = set()
        for entry in entries:
            listed_id = entry.get("Id") if isinstance(entry, dict) else None
            if not isinstance(listed_id, str):
                raise PortDataMalformed("Emby's Ids listing carried an entry with no string Id")
            if listed_id in asked:
                listed.add(listed_id)
        if entries and not listed:
            raise PortDataMalformed("Emby's Ids listing named none of the ids it was asked for")
        return listed
```

`_tracer` is the module's existing tracer (`trace.get_tracer("usher.source.emby")`,
line 75), the one `get_item`'s `source.get_item` span uses.

- [ ] **Step 9: Run the cases to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_confirm.py tests/unit/test_ports_source.py tests/unit/test_source_adapter_contract.py tests/unit/test_adapters_emby_contract.py tests/unit/test_fakes_emby_server.py -p no:randomly`

Expected: PASS.

- [ ] **Step 10: Plant the Review Focus guards and the `_listed_of` guards**

For each plant below:
- back up the adapter first:
  `cp src/usher/adapters/emby/adapter.py /var/tmp/confirmed-retraction-plant.bak`;
- run the named case and confirm it fails on its own assertion:
  `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_adapters_emby_confirm.py -p no:randomly -k <case> 2>&1 | grep -E '^E '`;
- restore the file and read the restore back:
  `cp /var/tmp/confirmed-retraction-plant.bak src/usher/adapters/emby/adapter.py && diff /var/tmp/confirmed-retraction-plant.bak src/usher/adapters/emby/adapter.py`.

The plants:
1. In `_listed_of`, replace the `isinstance(entries, list)` check with
   `entries = entries if isinstance(entries, list) else []`. Expected:
   `test_a_body_without_items_raises_rather_than_reading_as_gone` fails.
2. In `_listed_of`, drop `"Limit"` from `params`. Expected:
   `test_a_hundred_and_one_ids_go_as_two_requests_each_limited_to_its_chunk`
   fails on `assert [None, None] == ['100', '1']`.
3. In `_listed_of`, replace the `raise` for an entry with no string `Id` with
   `continue`. Expected: all four parameters of
   `test_an_entry_without_a_string_id_raises_rather_than_reading_as_gone` fail
   with `DID NOT RAISE`.
4. In `_listed_of`, add every string `Id` to `listed`, dropping
   `if listed_id in asked:`. Expected:
   `test_a_listing_of_only_ids_nobody_asked_for_raises_rather_than_reading_as_gone`
   fails on its `TimeoutError` after five seconds. Bound the run from outside
   with `timeout -s KILL 60`.
5. In `_listed_of`, change `if entries and not listed:` to `if False:`.
   Expected: the same case fails with `DID NOT RAISE`.
6. In `_listed_of`, raise for any `Id` nobody asked for, in place of skipping
   it. Expected: `test_a_stray_id_beside_an_asked_one_is_skipped` fails.

- [ ] **Step 11: Run the gate, then commit**

Run the full gate. Expected: green. Integration tests do not call
`confirm_gone` until Task 5, so its two duck adapters need nothing yet.

```bash
git add src/usher/ports/source.py src/usher/adapters/emby/adapter.py tests/fakes/source_adapter.py tests/contract/source_harness.py tests/fakes/emby_harness.py tests/contract/source_adapter_contract.py tests/unit/test_adapters_emby_contract.py tests/unit/test_adapters_emby_confirm.py tests/unit/test_ports_source.py
git commit -m "emby: confirm_gone asks a source which ids it no longer serves"
```

---

### Task 4: the repository's four new methods, added beside the old one

The old `mark_unseen_unavailable` stays until Task 6, so this task changes no
caller.

**Files:**
- Modify: `src/usher/ports/ingest.py`: `UnseenRows` and `Retraction`, directly
  after `SweepResult` (line 164)
- Modify: `src/usher/ports/repository/media_item.py`: the imports, and four
  abstract methods directly after `mark_unseen_unavailable` (lines 66–70)
- Modify: `src/usher/db/repositories/media_item.py`: the imports, five
  statements after `_SWEEP` (line 114), and four methods after
  `mark_unseen_unavailable` (lines 275–295)
- Modify: `tests/fakes/media_item_repository.py`: the imports, and four methods
  after `mark_unseen_unavailable` (line 129)
- Test: `tests/contract/media_item_repository_contract.py`, which both arms run:
  `TestFakeMediaItemRepository` in `tests/unit/test_media_item_repository_contract.py`
  and `TestPostgresMediaItemRepository` in
  `tests/integration/test_media_item_repository.py`
- Test: `tests/integration/test_media_item_repository.py`
- Test: `tests/unit/test_ports_repository_ingest.py`

**Interfaces:**
- Produces, in `usher.ports.ingest`:
  - `UnseenRows(external_ids: frozenset[str], total: int)`, frozen with slots;
  - `Retraction(retracted: int, targets: frozenset[MediaItemTarget])`, frozen with slots.
- Produces, on `MediaItemRepository` (all async, none commits):
  - `list_unseen(source_id: uuid.UUID, *, seen_since: AwareDatetime, max_retract_fraction: float) -> UnseenRows`.
    It raises `AvailabilitySweepRefused` over the ceiling, having read no id.
  - `available_among(source_id: uuid.UUID, external_ids: Collection[str]) -> frozenset[str]`
  - `retract(source_id: uuid.UUID, external_ids: Collection[str], *, unseen_since: AwareDatetime) -> Retraction`
  - `mark_seen(source_id: uuid.UUID, external_ids: Collection[str], *, seen_at: AwareDatetime) -> int`

- [ ] **Step 1: Write the failing contract cases**

In `tests/contract/media_item_repository_contract.py`, change the
`usher.ports.ingest` import to:

```python
from usher.ports.ingest import (
    AvailabilitySweepRefused,
    MediaItemTarget,
    MediaItemUpsert,
    Retraction,
    UnseenRows,
)
```

Then insert these thirteen cases into `MediaItemRepositoryContract`, directly
before `test_marking_unseen_unavailable_spares_items_seen_during_the_run`:

```python
    async def test_list_unseen_counts_the_source_and_names_its_available_unseen_rows(
        self, repository: MediaItemRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """Seen, retracted and another source's rows are not candidates; this source's all count."""
        await repository.upsert_many(
            [
                item(source_id, "seen", last_seen_at=RUN_AT),
                item(source_id, "unseen-1", last_seen_at=EARLIER),
                item(source_id, "unseen-2", last_seen_at=EARLIER),
                item(source_id, "retracted", last_seen_at=EARLIER),
                item(other_source_id, "theirs", last_seen_at=EARLIER),
            ]
        )
        premise = await repository.retract(source_id, ["retracted"], unseen_since=RUN_AT)
        assert premise.retracted == 1
        unseen = await repository.list_unseen(
            source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )
        assert unseen == UnseenRows(external_ids=frozenset({"unseen-1", "unseen-2"}), total=4)

    async def test_list_unseen_refuses_past_the_ceiling_and_changes_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """A walk that completes and lists almost nothing is not a deletion.

        An unmounted drive, or a library removed by accident, looks the same to the
        adapter as a mass deletion, so the sweep declines before asking the source.
        """
        await repository.upsert_many(
            [item(source_id, f"movie-{index}", last_seen_at=EARLIER) for index in range(10)]
        )
        with pytest.raises(AvailabilitySweepRefused) as caught:
            await repository.list_unseen(source_id, seen_since=RUN_AT, max_retract_fraction=0.25)
        assert (caught.value.would_retract, caught.value.total) == (10, 10)
        still = await repository.get_by_external_id(source_id, "movie-0")
        assert still is not None and still.available is True

    async def test_list_unseen_allows_exactly_the_ceiling(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """One unseen of four at 0.25 stands on the boundary, and the boundary is allowed."""
        await repository.upsert_many(
            [item(source_id, f"seen-{index}", last_seen_at=RUN_AT) for index in range(3)]
            + [item(source_id, "unseen", last_seen_at=EARLIER)]
        )
        unseen = await repository.list_unseen(
            source_id, seen_since=RUN_AT, max_retract_fraction=0.25
        )
        assert unseen == UnseenRows(external_ids=frozenset({"unseen"}), total=4)

    async def test_list_unseen_of_an_empty_source_consults_no_ceiling(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """A ratio divides by zero here; the count comparison never runs, so 0.0 passes."""
        unseen = await repository.list_unseen(
            source_id, seen_since=RUN_AT, max_retract_fraction=0.0
        )
        assert unseen == UnseenRows(external_ids=frozenset(), total=0)

    async def test_an_accepted_retraction_does_not_count_against_the_next_walk(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """The ceiling counts what this walk left unlisted, not what is already unavailable.

        Counted the other way, every walk after an accepted mass retraction is refused.
        """
        names = [f"movie-{index}" for index in range(10)]
        await repository.upsert_many(
            [item(source_id, name, last_seen_at=EARLIER) for name in names]
        )
        premise = await repository.retract(source_id, names, unseen_since=RUN_AT)
        assert premise.retracted == 10
        unseen = await repository.list_unseen(
            source_id, seen_since=RUN_AT, max_retract_fraction=0.25
        )
        assert unseen == UnseenRows(external_ids=frozenset(), total=10)

    async def test_available_among_names_only_this_sources_available_rows(
        self, repository: MediaItemRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        await repository.upsert_many(
            [
                item(source_id, "held"),
                item(source_id, "unasked"),
                item(source_id, "retracted", last_seen_at=EARLIER),
                item(other_source_id, "theirs"),
            ]
        )
        premise = await repository.retract(source_id, ["retracted"], unseen_since=RUN_AT)
        assert premise.retracted == 1
        among = await repository.available_among(
            source_id, ["held", "held", "retracted", "theirs", "never-stored"]
        )
        assert among == {"held"}

    async def test_available_among_nothing_is_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        await repository.upsert_many([item(source_id, "held")])
        assert await repository.available_among(source_id, []) == frozenset()

    async def test_retract_changes_only_available_rows_unseen_since_the_instant(
        self, repository: MediaItemRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """A row read since the instant is spared, and a retracted one is not counted again."""
        await repository.upsert_many(
            [
                item(source_id, "gone", last_seen_at=EARLIER),
                item(source_id, "read-since", last_seen_at=RUN_AT),
                item(source_id, "already", last_seen_at=EARLIER),
                item(other_source_id, "theirs", last_seen_at=EARLIER),
            ]
        )
        premise = await repository.retract(source_id, ["already"], unseen_since=RUN_AT)
        assert premise.retracted == 1
        retraction = await repository.retract(
            source_id, ["gone", "read-since", "already", "theirs"], unseen_since=RUN_AT
        )
        assert retraction.retracted == 1
        for external_id, available in (("gone", False), ("read-since", True)):
            stored = await repository.get_by_external_id(source_id, external_id)
            assert stored is not None and stored.available is available, external_id
        theirs = await repository.get_by_external_id(other_source_id, "theirs")
        assert theirs is not None and theirs.available is True

    async def test_retract_names_the_distinct_titles_and_episodes_it_changed(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """Two copies of one film are one target; an unmatched row counts but names nothing."""
        await repository.upsert_many(
            [
                item(source_id, "copy-a", title_id=title_id, last_seen_at=EARLIER),
                item(source_id, "copy-b", title_id=title_id, last_seen_at=EARLIER),
                item(
                    source_id,
                    "episode-1",
                    title_id=title_id,
                    episode_id=episode_id,
                    last_seen_at=EARLIER,
                ),
                item(source_id, "unmatched", last_seen_at=EARLIER),
            ]
        )
        retraction = await repository.retract(
            source_id, ["copy-a", "copy-b", "episode-1", "unmatched"], unseen_since=RUN_AT
        )
        assert retraction == Retraction(
            retracted=4,
            targets=frozenset(
                {
                    MediaItemTarget(title_id=title_id, episode_id=None),
                    MediaItemTarget(title_id=title_id, episode_id=episode_id),
                }
            ),
        )

    async def test_retract_of_nothing_changes_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        await repository.upsert_many([item(source_id, "held", last_seen_at=EARLIER)])
        retraction = await repository.retract(source_id, [], unseen_since=RUN_AT)
        assert retraction == Retraction(retracted=0, targets=frozenset())
        stored = await repository.get_by_external_id(source_id, "held")
        assert stored is not None and stored.available is True

    async def test_a_retracted_row_that_comes_back_is_available_again(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """PRD 02: an item that comes back must come back, or a re-added file stays hidden."""
        await repository.upsert_many([item(source_id, "movie-1", last_seen_at=EARLIER)])
        premise = await repository.retract(source_id, ["movie-1"], unseen_since=RUN_AT)
        assert premise.retracted == 1
        await repository.upsert_many([item(source_id, "movie-1", last_seen_at=RUN_AT)])
        stored = await repository.get_by_external_id(source_id, "movie-1")
        assert stored is not None and stored.available is True

    async def test_mark_seen_moves_an_available_rows_stamp_forward_only(
        self, repository: MediaItemRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """Never back, never onto a retracted row, and never onto another source's row."""
        later = RUN_AT + timedelta(days=1)
        await repository.upsert_many(
            [
                item(source_id, "older", last_seen_at=EARLIER),
                item(source_id, "newer", last_seen_at=later),
                item(source_id, "unasked", last_seen_at=EARLIER),
                item(source_id, "retracted", last_seen_at=EARLIER),
                item(other_source_id, "theirs", last_seen_at=EARLIER),
            ]
        )
        premise = await repository.retract(source_id, ["retracted"], unseen_since=RUN_AT)
        assert premise.retracted == 1
        moved = await repository.mark_seen(
            source_id, ["older", "newer", "retracted", "theirs"], seen_at=RUN_AT
        )
        assert moved == 1
        expected = {
            "older": (True, RUN_AT),
            "newer": (True, later),
            "retracted": (False, EARLIER),
            "unasked": (True, EARLIER),
        }
        for external_id, (available, seen) in expected.items():
            stored = await repository.get_by_external_id(source_id, external_id)
            assert stored is not None
            assert (stored.available, stored.last_seen_at) == (available, seen), external_id
        theirs = await repository.get_by_external_id(other_source_id, "theirs")
        assert theirs is not None and theirs.last_seen_at == EARLIER

    async def test_mark_seen_of_nothing_moves_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        await repository.upsert_many([item(source_id, "held", last_seen_at=EARLIER)])
        assert await repository.mark_seen(source_id, [], seen_at=RUN_AT) == 0
        stored = await repository.get_by_external_id(source_id, "held")
        assert stored is not None and stored.last_seen_at == EARLIER
```

- [ ] **Step 2: Write the statement-count cases**

In `tests/integration/test_media_item_repository.py`, change
`from usher.ports.ingest import AvailabilitySweepRefused` to
`from usher.ports.ingest import AvailabilitySweepRefused, UnseenRows`, and add
these four cases directly after `test_a_refused_sweep_issues_no_update_at_all`:

```python
async def test_list_unseen_costs_two_statements_however_many_rows_are_unseen(
    repository: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    other_source_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """A count, then the ids: never a statement per row."""
    await repository.upsert_many(
        [item(source_id, f"m-{index}", last_seen_at=EARLIER) for index in range(4)]
    )
    await repository.upsert_many(
        [item(other_source_id, f"m-{index}", last_seen_at=EARLIER) for index in range(400)]
    )

    statement_counter.clear()
    small = await repository.list_unseen(source_id, seen_since=RUN_AT, max_retract_fraction=1.0)
    small_statements = len(statement_counter)

    statement_counter.clear()
    large = await repository.list_unseen(
        other_source_id, seen_since=RUN_AT, max_retract_fraction=1.0
    )
    large_statements = len(statement_counter)

    assert (len(small.external_ids), len(large.external_ids)) == (4, 400), (
        "the premise: every row of both sources is unseen"
    )
    assert (small_statements, large_statements) == (2, 2)


async def test_a_refused_list_unseen_reads_no_id(
    repository: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """Over the ceiling, the count is the whole cost: the ids are never read."""
    await repository.upsert_many(
        [item(source_id, f"m-{index}", last_seen_at=EARLIER) for index in range(4)]
    )
    statement_counter.clear()
    with pytest.raises(AvailabilitySweepRefused):
        await repository.list_unseen(source_id, seen_since=RUN_AT, max_retract_fraction=0.25)
    [statement] = statement_counter
    assert "count(*)" in statement


async def test_list_unseen_with_nothing_unseen_reads_no_id(
    repository: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """With every row seen, the count is the whole cost: there is no id to read."""
    await repository.upsert_many(
        [item(source_id, f"m-{index}", last_seen_at=RUN_AT) for index in range(4)]
    )
    statement_counter.clear()
    unseen = await repository.list_unseen(source_id, seen_since=RUN_AT, max_retract_fraction=1.0)
    statements = len(statement_counter)
    assert unseen == UnseenRows(external_ids=frozenset(), total=4), (
        "the premise: every row of the source was stamped at the instant"
    )
    assert statements == 1


async def test_the_three_keyed_methods_cost_one_statement_each_and_none_for_nothing(
    repository: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    await repository.upsert_many(
        [item(source_id, f"m-{index}", last_seen_at=EARLIER) for index in range(4)]
    )
    statement_counter.clear()
    among = await repository.available_among(source_id, ["m-0", "m-1"])
    among_statements = len(statement_counter)
    statement_counter.clear()
    retraction = await repository.retract(source_id, ["m-0", "m-2"], unseen_since=RUN_AT)
    retract_statements = len(statement_counter)
    statement_counter.clear()
    moved = await repository.mark_seen(source_id, ["m-1", "m-3"], seen_at=RUN_AT)
    mark_statements = len(statement_counter)
    assert (among, retraction.retracted, moved) == (frozenset({"m-0", "m-1"}), 2, 2), (
        "the premise: each call did its work"
    )

    statement_counter.clear()
    await repository.available_among(source_id, [])
    await repository.retract(source_id, [], unseen_since=RUN_AT)
    await repository.mark_seen(source_id, [], seen_at=RUN_AT)
    nothing_statements = len(statement_counter)

    assert (among_statements, retract_statements, mark_statements, nothing_statements) == (
        1,
        1,
        1,
        0,
    )
```

In `tests/unit/test_ports_repository_ingest.py`, add the four names to the set
in `test_media_item_repository_surface`, directly after
`"mark_unseen_unavailable",`:

```python
            "list_unseen",
            "available_among",
            "retract",
            "mark_seen",
```

- [ ] **Step 3: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_media_item_repository_contract.py tests/unit/test_ports_repository_ingest.py -p no:randomly`

Expected: an `ImportError` for `Retraction` and `UnseenRows`. Once Step 4 lands,
the contract cases fail on `AttributeError: ... 'list_unseen'` and the surface
case fails its set comparison.

- [ ] **Step 4: Add the two result types**

In `src/usher/ports/ingest.py`, directly after `SweepResult`, add:

```python
@dataclass(frozen=True, slots=True)
class UnseenRows:
    """A source's rows a walk did not list, and how many rows the source has."""

    external_ids: frozenset[str]
    total: int


@dataclass(frozen=True, slots=True)
class Retraction:
    """How many rows a retraction changed, and the distinct titles and episodes they hold."""

    retracted: int
    targets: frozenset[MediaItemTarget]
```

- [ ] **Step 5: Add the four abstract methods**

In `src/usher/ports/repository/media_item.py`:
- change `from collections.abc import Sequence` to `from collections.abc import Collection, Sequence`;
- change the `usher.ports.ingest` import to
  `from usher.ports.ingest import MediaItemTarget, MediaItemUpsert, Retraction, SweepResult, UnseenRows`.

Directly after the abstract `mark_unseen_unavailable`, add:

```python
    @abstractmethod
    async def list_unseen(
        self, source_id: uuid.UUID, *, seen_since: AwareDatetime, max_retract_fraction: float
    ) -> UnseenRows:
        """This source's row count, and its available rows not seen since `seen_since`.

        Raises `AvailabilitySweepRefused`, having read no id, when the unseen rows are
        more than `max_retract_fraction` of the source's rows. A source with none unseen
        never consults the ceiling, so `0.0` still permits it.
        """

    @abstractmethod
    async def available_among(
        self, source_id: uuid.UUID, external_ids: Collection[str]
    ) -> frozenset[str]:
        """The ids among `external_ids` this source holds available.

        An empty input asks nothing.
        """

    @abstractmethod
    async def retract(
        self, source_id: uuid.UUID, external_ids: Collection[str], *, unseen_since: AwareDatetime
    ) -> Retraction:
        """Mark these rows unavailable, where available and not seen since `unseen_since`.

        The guard spares a row any read has stamped since the caller's instant. The
        result counts the rows this call changed and names the distinct titles and
        episodes they belong to; an unmatched row is counted but names nothing. An empty
        input writes nothing.
        """

    @abstractmethod
    async def mark_seen(
        self, source_id: uuid.UUID, external_ids: Collection[str], *, seen_at: AwareDatetime
    ) -> int:
        """Move `last_seen_at` forward to `seen_at` on these available rows; how many moved.

        Never moves a stamp back, and never makes a row available. An empty input writes
        nothing.
        """
```

- [ ] **Step 6: Implement them on Postgres**

In `src/usher/db/repositories/media_item.py`:
- change `from collections.abc import Sequence` to `from collections.abc import Collection, Sequence`;
- add `Retraction` and `UnseenRows` to the `from usher.ports.ingest import (...)` list.

Directly after `_SWEEP`, add:

```python
# Two statements under READ COMMITTED: a row committed between them can reach the id read uncounted;
# the source's confirmation is what makes that safe.
_UNSEEN_COUNTS = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE available AND last_seen_at < :seen_since) AS unseen
FROM media_items WHERE source_id = :source_id
"""

_UNSEEN = """
SELECT external_id FROM media_items
WHERE source_id = :source_id AND available AND last_seen_at < :seen_since
"""

_AVAILABLE_AMONG = """
SELECT external_id FROM media_items
WHERE source_id = :source_id AND external_id = ANY(:external_ids) AND available
"""

# The guard is the caller's instant: a row any read has stamped since then is spared.
_RETRACT = """
UPDATE media_items SET available = false
WHERE source_id = :source_id
  AND external_id = ANY(:external_ids)
  AND available
  AND last_seen_at < :unseen_since
RETURNING title_id, episode_id
"""

# Forward only, and never onto a retracted row.
_MARK_SEEN = """
UPDATE media_items SET last_seen_at = :seen_at
WHERE source_id = :source_id
  AND external_id = ANY(:external_ids)
  AND available
  AND last_seen_at < :seen_at
"""
```

Directly after `mark_unseen_unavailable`, add:

```python
    async def list_unseen(
        self, source_id: uuid.UUID, *, seen_since: AwareDatetime, max_retract_fraction: float
    ) -> UnseenRows:
        parameters = {"source_id": source_id, "seen_since": seen_since}
        with self._session.no_autoflush:
            counts = (await self._session.execute(text(_UNSEEN_COUNTS), parameters)).one()
            total, unseen = int(counts.total), int(counts.unseen)
            # A count comparison rather than a division: an empty source divides by
            # zero, and `unseen and` in front of it means a run with nothing unseen
            # never consults the ceiling at all -- so a ceiling of 0.0 still permits a
            # walk that missed nothing.
            if unseen and unseen > total * max_retract_fraction:
                raise AvailabilitySweepRefused(
                    would_retract=unseen, total=total, ceiling=max_retract_fraction
                )
            if not unseen:
                return UnseenRows(external_ids=frozenset(), total=total)
            rows = (await self._session.execute(text(_UNSEEN), parameters)).scalars().all()
        return UnseenRows(external_ids=frozenset(rows), total=total)

    async def available_among(
        self, source_id: uuid.UUID, external_ids: Collection[str]
    ) -> frozenset[str]:
        if not external_ids:
            return frozenset()
        with self._session.no_autoflush:
            rows = (
                (
                    await self._session.execute(
                        text(_AVAILABLE_AMONG),
                        {"source_id": source_id, "external_ids": list(set(external_ids))},
                    )
                )
                .scalars()
                .all()
            )
        return frozenset(rows)

    async def retract(
        self, source_id: uuid.UUID, external_ids: Collection[str], *, unseen_since: AwareDatetime
    ) -> Retraction:
        if not external_ids:
            return Retraction(retracted=0, targets=frozenset())
        rows = (
            await self._session.execute(
                text(_RETRACT),
                {
                    "source_id": source_id,
                    "external_ids": list(set(external_ids)),
                    "unseen_since": unseen_since,
                },
            )
        ).all()
        return Retraction(
            retracted=len(rows),
            targets=frozenset(
                MediaItemTarget(title_id=row.title_id, episode_id=row.episode_id)
                for row in rows
                if row.title_id is not None or row.episode_id is not None
            ),
        )

    async def mark_seen(
        self, source_id: uuid.UUID, external_ids: Collection[str], *, seen_at: AwareDatetime
    ) -> int:
        if not external_ids:
            return 0
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                text(_MARK_SEEN),
                {
                    "source_id": source_id,
                    "external_ids": list(set(external_ids)),
                    "seen_at": seen_at,
                },
            ),
        )
        return result.rowcount
```

- [ ] **Step 7: Implement them on the fake**

In `tests/fakes/media_item_repository.py`:
- change `from collections.abc import Sequence` to `from collections.abc import Collection, Sequence`;
- add `Retraction` and `UnseenRows` to the `usher.ports.ingest` import.

Directly after `mark_unseen_unavailable`, add the methods below. Like
`mark_unseen_unavailable`, none of them counts in `self.calls`.

```python
    async def list_unseen(
        self, source_id: uuid.UUID, *, seen_since: AwareDatetime, max_retract_fraction: float
    ) -> UnseenRows:
        mine = [entry for entry in self._items.values() if entry.source_id == source_id]
        unseen = [entry for entry in mine if entry.available and entry.last_seen_at < seen_since]
        # A count comparison, never `len(unseen) / len(mine)`: an empty source
        # divides by zero, and `mine` is empty on the very first sync of a
        # source that turned out to hold nothing.
        if unseen and len(unseen) > len(mine) * max_retract_fraction:
            raise AvailabilitySweepRefused(
                would_retract=len(unseen), total=len(mine), ceiling=max_retract_fraction
            )
        return UnseenRows(
            external_ids=frozenset(entry.external_id for entry in unseen), total=len(mine)
        )

    async def available_among(
        self, source_id: uuid.UUID, external_ids: Collection[str]
    ) -> frozenset[str]:
        return frozenset(
            external_id
            for external_id in external_ids
            if (entry := self._items.get((source_id, external_id))) is not None and entry.available
        )

    async def retract(
        self, source_id: uuid.UUID, external_ids: Collection[str], *, unseen_since: AwareDatetime
    ) -> Retraction:
        retracted = 0
        targets: set[MediaItemTarget] = set()
        for external_id in set(external_ids):
            entry = self._items.get((source_id, external_id))
            if entry is None or not entry.available or entry.last_seen_at >= unseen_since:
                continue
            self._items[(source_id, external_id)] = entry.evolve(available=False)
            retracted += 1
            if entry.title_id is not None or entry.episode_id is not None:
                targets.add(MediaItemTarget(title_id=entry.title_id, episode_id=entry.episode_id))
        return Retraction(retracted=retracted, targets=frozenset(targets))

    async def mark_seen(
        self, source_id: uuid.UUID, external_ids: Collection[str], *, seen_at: AwareDatetime
    ) -> int:
        moved = 0
        for external_id in set(external_ids):
            entry = self._items.get((source_id, external_id))
            if entry is None or not entry.available or entry.last_seen_at >= seen_at:
                continue
            self._items[(source_id, external_id)] = entry.evolve(last_seen_at=seen_at)
            moved += 1
        return moved
```

- [ ] **Step 8: Run both arms to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_media_item_repository_contract.py tests/unit/test_ports_repository_ingest.py tests/integration/test_media_item_repository.py -p no:randomly`

Expected: PASS. The integration file needs Docker.

- [ ] **Step 9: Plant six guards**

Back up both arms first:

```bash
cp src/usher/db/repositories/media_item.py /var/tmp/confirmed-retraction-plant.bak
cp tests/fakes/media_item_repository.py /var/tmp/confirmed-retraction-plant-fake.bak
```

Plant each line below in **both arms**. For each plant:
- run Step 8's command and confirm the named case fails on an `E ` line;
- restore both files and read each restore back with `diff` against its backup.

The plants:
1. In `_RETRACT`, delete `AND last_seen_at < :unseen_since`; in the fake's
   `retract`, delete `or entry.last_seen_at >= unseen_since`. Expected:
   `test_retract_changes_only_available_rows_unseen_since_the_instant` fails.
2. In `_MARK_SEEN`, delete `AND available`; in the fake's `mark_seen`, delete
   `or not entry.available`. Expected:
   `test_mark_seen_moves_an_available_rows_stamp_forward_only` fails.
3. In both `list_unseen`s, change `>` to `>=` in the ceiling comparison.
   Expected: `test_list_unseen_allows_exactly_the_ceiling` fails.
4. In `_AVAILABLE_AMONG`, delete `AND external_id = ANY(:external_ids)`; make the
   fake's `available_among` read every row of the source behind an
   `if not external_ids: return frozenset()` guard. Expected:
   `test_available_among_names_only_this_sources_available_rows` fails on its
   `unasked` row.
5. In `_MARK_SEEN`, delete `AND external_id = ANY(:external_ids)`; make the
   fake's `mark_seen` move every row of the source behind an
   `if not external_ids: return 0` guard. Expected:
   `test_mark_seen_moves_an_available_rows_stamp_forward_only` fails on
   `assert moved == 1`.

Then plant the Postgres arm alone: delete `list_unseen`'s
`if not unseen: return UnseenRows(...)`. Expected:
`test_list_unseen_with_nothing_unseen_reads_no_id` fails on
`assert statements == 1`.

- [ ] **Step 10: Run the gate, then commit**

Run the full gate. Expected: green.

```bash
git add src/usher/ports/ingest.py src/usher/ports/repository/media_item.py src/usher/db/repositories/media_item.py tests/fakes/media_item_repository.py tests/contract/media_item_repository_contract.py tests/integration/test_media_item_repository.py tests/unit/test_ports_repository_ingest.py
git commit -m "db: the four media item methods confirmed retraction needs"
```

---

### Task 5: a full walk's sweep retracts only what the source confirms gone

**Files:**
- Create: `src/usher/services/retraction.py`
- Modify: `src/usher/services/reconcile.py`:
  - the imports (lines 1–37);
  - a new `_beating` beside `_beat` (line 761);
  - `_walk_plan`'s planning wait (lines 567–577);
  - `_sweep` (lines 837–876);
  - its call site (line 345).
- Modify: `tests/fakes/source_adapter.py`: `unlist`, `_unlisted`, `forget`,
  `_walk_items` (line 301), `plan_walk` (lines 312–330), `_walk_library`
- Modify: `src/usher/ports/ingest.py`: the docstrings of `MediaItemUpsert` and
  `AvailabilitySweepRefused`
- Test: `tests/unit/test_services_reconcile.py`
- Create: `tests/unit/test_telemetry_retraction.py`
- Test: `tests/integration/test_services_reconcile.py` and
  `tests/integration/test_pipeline_spans.py`
- Docs:
  - `docs/prd/03-sources-and-sync.md`, `docs/prd/08-operations.md`,
    `docs/guide/command-line.md`, `docs/prd/10-telemetry-and-dashboards.md`;
  - `CHANGELOG.md`;
  - `.claude/rules/emby-push-and-ingest.md` and `CLAUDE.md`.
- Pins: `tests/unit/test_telemetry_metric_names.py` and
  `tests/unit/test_dashboards.py`

**Interfaces:**
- Consumes:
  - `SourceAdapter.confirm_gone` and `FakeSourceAdapter.confirm_calls` and
    `fail_confirm()` (Task 3);
  - `list_unseen`, `retract`, `mark_seen`, `UnseenRows` and `Retraction` (Task 4).
- Produces:
  - `usher.services.retraction.confirmed_gone(adapter: SourceAdapter, external_ids: Collection[str], *, source: str, path: Literal["sweep", "push"]) -> frozenset[str]`.
    Task 7 calls it with `path="push"`.
  - `FakeSourceAdapter.unlist(external_id: str) -> None`.
  - `tests.unit.test_telemetry_retraction.retraction_checks(reader: InMemoryMetricReader) -> dict[tuple[str, str, str], float]`,
    the counter as `{(source, path, outcome): value}`.

- [ ] **Step 1: Teach the fake adapter to serve an item it does not list**

In `tests/fakes/source_adapter.py`, at the end of `FakeSourceAdapter.__init__`,
add:

```python
        # Served by id but in no item listing, as Emby's full listing leaves out an
        # alternate version.
        self._unlisted: set[str] = set()
```

Directly after `forget`, add:

```python
    def unlist(self, external_id: str) -> None:
        """Keep serving `external_id` by id, but leave it out of every item listing.

        Emby's full listing shows one version of a film while a read by id serves each,
        so a full walk never lists an alternate it can still read. A later `place` does
        not list it again. `watch_state` still lists it. An item delta does not, though Emby's
        delta listing returns alternates too.
        """
        self._unlisted.add(external_id)
```

`unlist` only records the id: every item listing consults `_unlisted`, so it
does not matter which libraries hold it. Make four more changes:
- In `forget`, add `self._unlisted.discard(external_id)` after its
  `self._changed_at.pop(external_id, None)`.
- In `_walk_items`, open the loop with
  `if external_id in self._unlisted:` / `continue`, so the single walk does not
  list it.
- In `_walk_library`, change `if item is None:` to
  `if item is None or external_id in self._unlisted:`, so a library unit does
  not list it either, even after a later `place`.
- In `plan_walk`, compute
  `listed = [one for one in self._items if one not in self._unlisted]` before
  `in_a_library`. Use `listed` in place of `self._items` in the
  `in_a_library.issuperset(...)` check and in both `expected_total=len(...)`
  arguments, and count each unit as the walk will list it:

  ```python
                None if name in self._uncounted else len(set(placed) - self._unlisted),
  ```

Without the `plan_walk` change, an unlisted item fails the superset check, so a
placed source falls back to one whole-library unit and the planned-walk cases
stop exercising a plan. Without the unit count, a unit holding an unlisted id
plans one more item than it walks.

- [ ] **Step 2: Write the failing sweep cases**

In `tests/unit/test_services_reconcile.py`:
- add `Collection` to the `from collections.abc import ...` line;
- add `from tests.unit.test_telemetry_retraction import retraction_checks` after
  the `tests.fakes.title_repository` import. Step 3 writes that reader, and the
  sweep's counter case reads the counter through it rather than through a copy.

In `test_a_refused_sweep_fails_the_run_and_changes_nothing`, add as its last
line:

```python
    assert fixture.adapter.confirm_calls == [], "the source was asked before the ceiling refused"
```

In `test_a_delta_walk_never_sweeps`, add as its last line:

```python
    assert fixture.adapter.confirm_calls == []
```

In `test_the_sweep_window_is_the_runs_own_start_instant`, change both mentions
of `mark_unseen_unavailable` to `list_unseen`. Keep its `-> object` and its
`type: ignore` comments.

Directly after `test_a_refused_sweep_keeps_the_upserts_the_walk_made`, add:

```python
# -- the sweep asks before it retracts ---------------------------------------


async def test_a_sweep_retracts_what_the_source_confirms_gone_and_stamps_what_it_serves(
    fixture: _Fixture,
) -> None:
    """m9 is gone. m8 is still served by id but in no full listing, as an alternate version is."""
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.forget("m9")
    fixture.adapter.unlist("m8")
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        second = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    finally:
        logger.remove(sink)
    assert fixture.adapter.confirm_calls == [frozenset({"m8", "m9"})]
    assert second.status is SyncRunStatus.COMPLETED
    assert second.items_retracted == 1
    gone = await fixture.media_items.get_by_external_id(fixture.source.id, "m9")
    assert gone is not None and gone.available is False
    served = await fixture.media_items.get_by_external_id(fixture.source.id, "m8")
    assert served is not None
    assert (served.available, served.last_seen_at) == (True, second.started_at)
    assert [line.rstrip("\n") for line in lines if "sweep found" in line] == [
        "Living Room Emby's sweep found 2 rows the walk did not list: 1 gone, 1 still served"
    ]


async def test_an_unlisted_item_placed_again_stays_out_of_its_library_unit() -> None:
    """`place` after `unlist` lists m8 in neither the unit nor its count, so the sweep asks."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(10))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.unlist("m8")
    fixture.adapter.place("m8", "Films")
    fixture.adapter.unit_starts.clear()
    second = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert fixture.adapter.unit_starts == [("library:Films", 0)], (
        "the premise: Films walked as a unit of its own"
    )
    units = await fixture.runs.units_for(second.id)
    assert [unit.expected_items for unit in units] == [9]
    assert second.items_seen == 9
    assert fixture.adapter.confirm_calls == [frozenset({"m8"})]
    served = await fixture.media_items.get_by_external_id(fixture.source.id, "m8")
    assert served is not None
    assert (served.available, served.last_seen_at) == (True, second.started_at)


async def test_a_sweep_with_nothing_unseen_asks_nothing_and_says_so(fixture: _Fixture) -> None:
    for index in range(4):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        run = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    finally:
        logger.remove(sink)
    assert run.status is SyncRunStatus.COMPLETED
    assert fixture.adapter.confirm_calls == []
    assert [line.rstrip("\n") for line in lines if "sweep found" in line] == [
        "Living Room Emby's sweep found 0 rows the walk did not list: 0 gone, 0 still served"
    ]


async def test_a_sweep_the_source_cannot_answer_fails_and_the_next_walk_only_sweeps() -> None:
    """Nothing retracted or stamped, a resumable failure, and a resume that walks no unit."""
    fixture = _Fixture()
    _shelve(fixture, "Films", range(10))
    first = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert first.status is SyncRunStatus.COMPLETED, "the premise: the first walk completed"
    fixture.adapter.forget("m9")
    fixture.adapter.unlist("m8")
    fixture.adapter.fail_confirm()

    failed = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert failed.status is SyncRunStatus.FAILED
    assert (failed.error_code, failed.items_retracted) == (None, 0)
    assert "while confirming removals" in (failed.error or "")
    for external_id in ("m8", "m9"):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, external_id)
        assert stored is not None
        assert (stored.available, stored.last_seen_at) == (True, first.started_at), external_id

    fixture.adapter.clear_failure()
    fixture.adapter.unit_starts.clear()
    resumed = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)

    assert (resumed.id, resumed.started_at) == (failed.id, failed.started_at)
    assert resumed.status is SyncRunStatus.COMPLETED
    assert fixture.adapter.unit_starts == [], "the resume walked a unit again"
    assert resumed.items_retracted == 1
    gone = await fixture.media_items.get_by_external_id(fixture.source.id, "m9")
    assert gone is not None and gone.available is False
    served = await fixture.media_items.get_by_external_id(fixture.source.id, "m8")
    assert served is not None
    assert (served.available, served.last_seen_at) == (True, resumed.started_at)


async def test_a_row_read_since_the_walk_began_survives_a_gone_answer(fixture: _Fixture) -> None:
    """The guard is the run's `started_at`, so a read after the walk began wins over it."""
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.forget("m9")
    original = fixture.adapter.confirm_gone

    async def _read_back_first(external_ids: Collection[str]) -> frozenset[str]:
        await fixture.ingest.ingest_batch(
            fixture.source.id, [_item("m9")], observed_at=datetime.now(UTC)
        )
        return await original(external_ids)

    fixture.adapter.confirm_gone = _read_back_first  # type: ignore[method-assign]
    second = await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    stored = await fixture.media_items.get_by_external_id(fixture.source.id, "m9")
    assert stored is not None
    assert stored.last_seen_at > second.started_at, "the premise: read after the walk began"
    assert stored.available is True
    assert second.items_retracted == 0


async def test_the_heartbeat_beats_while_the_source_confirms_removals() -> None:
    """Confirming a long unseen list can take as long as a plan, so the sweep beats too."""
    fixture = _Fixture(heartbeat_seconds=0.01)
    _shelve(fixture, "Films", range(10))
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.forget("m9")
    asked, release = asyncio.Event(), asyncio.Event()
    original = fixture.adapter.confirm_gone

    async def _held(external_ids: Collection[str]) -> frozenset[str]:
        asked.set()
        await release.wait()
        return await original(external_ids)

    fixture.adapter.confirm_gone = _held  # type: ignore[method-assign]
    walk = asyncio.create_task(
        fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    )
    try:
        async with asyncio.timeout(5):
            await asked.wait()
        start = len(fixture.checkpoints)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(5):
                while len(fixture.checkpoints) < start + 3:
                    await asyncio.sleep(0.01)
        beats = [beat for _, beat in fixture.checkpoints[start : start + 3] if beat is not None]
        assert len(beats) == 3, "fewer than three beats while the source was asked"
        assert beats == sorted(set(beats)), "a beat that did not move the heartbeat"
        held = await fixture.media_items.get_by_external_id(fixture.source.id, "m9")
        assert held is not None and held.available is True, "retracted before the answer"
    finally:
        release.set()
        run = await walk
    assert run.status is SyncRunStatus.COMPLETED
    assert run.items_retracted == 1
    assert run.heartbeat_at is not None and run.heartbeat_at >= beats[-1], (
        "the completing row moved the heartbeat back past a beat"
    )
```

Directly after `_unit_durations`, near the `meter_reader` fixture, add:

```python
async def test_a_sweep_counts_each_id_it_asked_about_on_the_sweep_path(
    meter_reader: InMemoryMetricReader,
) -> None:
    fixture = _Fixture()
    for index in range(10):
        fixture.adapter.seed(_item(f"m{index}"), T0)
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    fixture.adapter.forget("m9")
    fixture.adapter.unlist("m8")
    await fixture.service.reconcile(fixture.source, SyncRunKind.FULL, fixture.adapter)
    assert retraction_checks(meter_reader) == {
        ("Living Room Emby", "sweep", "gone"): 1.0,
        ("Living Room Emby", "sweep", "served"): 1.0,
    }
```

- [ ] **Step 3: Write the counter's own cases**

Create `tests/unit/test_telemetry_retraction.py`:

```python
"""`usher.sync.retraction.checks`: every id asked of a source before it is retracted."""

from collections.abc import Collection, Iterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from tests.fakes.source_adapter import FakeSourceAdapter
from usher.domain.enums import SourceKind
from usher.domain.source import Source
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.retraction import confirmed_gone

T0 = datetime(2026, 7, 1, tzinfo=UTC)
SOURCE = Source(
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-1",
    device_id="device-1",
)


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A provider of this test's own; `tests/conftest.py` resets the set-once global."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


def retraction_checks(reader: InMemoryMetricReader) -> dict[tuple[str, str, str], float]:
    """`usher.sync.retraction.checks` as `{(source, path, outcome): value}`.

    Exported because `tests/unit/test_services_reconcile.py` reads the same counter
    off a sweep, and a second copy of this reader would stop tracking the first.
    """
    found: dict[tuple[str, str, str], float] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data is not None else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != "usher.sync.retraction.checks":
                    continue
                for point in metric.data.data_points:
                    assert isinstance(point, NumberDataPoint)
                    labels = dict(point.attributes or {})
                    key = (str(labels["source"]), str(labels["path"]), str(labels["outcome"]))
                    found[key] = float(point.value)
    return found


def _serving(*external_ids: str) -> FakeSourceAdapter:
    adapter = FakeSourceAdapter(SOURCE)
    for external_id in external_ids:
        adapter.seed(
            SourceItem(
                external_id=external_id,
                name=f"Movie {external_id}",
                kind=SourceItemKind.MOVIE,
                year=2021,
            ),
            T0,
        )
    return adapter


class _StrayAdapter(FakeSourceAdapter):
    """Answers with an id nobody asked about, as a misbehaving source might."""

    async def confirm_gone(self, external_ids: Collection[str]) -> frozenset[str]:
        return await super().confirm_gone(external_ids) | {"stray-1"}


@pytest.mark.parametrize("path", ["sweep", "push"])
async def test_each_asked_id_is_counted_once_by_path_and_outcome(
    meter_reader: InMemoryMetricReader, path: Literal["sweep", "push"]
) -> None:
    adapter = _serving("kept-1")
    gone = await confirmed_gone(
        adapter, ["kept-1", "gone-1", "gone-2"], source=SOURCE.name, path=path
    )
    assert gone == {"gone-1", "gone-2"}
    assert retraction_checks(meter_reader) == {
        (SOURCE.name, path, "gone"): 2.0,
        (SOURCE.name, path, "served"): 1.0,
    }


async def test_an_id_nobody_asked_about_is_neither_returned_nor_counted(
    meter_reader: InMemoryMetricReader,
) -> None:
    """And an outcome with no id adds nothing: no `served` series at all, not a zero."""
    gone = await confirmed_gone(_StrayAdapter(SOURCE), ["gone-1"], source=SOURCE.name, path="sweep")
    assert gone == {"gone-1"}
    assert retraction_checks(meter_reader) == {(SOURCE.name, "sweep", "gone"): 1.0}


async def test_an_empty_question_asks_nothing_and_counts_nothing(
    meter_reader: InMemoryMetricReader,
) -> None:
    premise = _serving()
    await premise.confirm_gone([])
    assert premise.confirm_calls == [frozenset()], "the premise: the fake records an empty question"
    adapter = _serving()
    assert await confirmed_gone(adapter, [], source=SOURCE.name, path="push") == frozenset()
    assert adapter.confirm_calls == []
    assert retraction_checks(meter_reader) == {}
```

- [ ] **Step 4: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_services_reconcile.py tests/unit/test_telemetry_retraction.py -p no:randomly`

Expected: FAIL at collection, `ModuleNotFoundError: No module named
'usher.services.retraction'`, and nothing runs: `test_services_reconcile.py`
imports `retraction_checks` from `test_telemetry_retraction.py`, which imports
`confirmed_gone`. Once Step 5 lands, the new sweep cases FAIL because the old
sweep never asks and retracts m8, and the re-pointed window case FAILS because
`list_unseen` is never called.

- [ ] **Step 5: Write `confirmed_gone`**

Create `src/usher/services/retraction.py`:

```python
"""Asking a source which ids it no longer serves, counted for both retraction paths."""

from collections.abc import Collection
from typing import Literal

from opentelemetry import metrics

from usher.ports.source import SourceAdapter

_meter = metrics.get_meter("usher.retraction")
_retraction_checks = _meter.create_counter(
    "usher.sync.retraction.checks",
    unit="1",
    description="Ids asked of a source before retracting them, by path and outcome",
)


async def confirmed_gone(
    adapter: SourceAdapter,
    external_ids: Collection[str],
    *,
    source: str,
    path: Literal["sweep", "push"],
) -> frozenset[str]:
    """The ids among `external_ids` the source confirms gone, each counted by outcome.

    Only an asked id is returned or counted, whatever the adapter answers. An empty
    question asks nothing and counts nothing, and an outcome with no id adds nothing.
    `source` is the source's name, the label `usher.sync.run.duration` carries too.
    """
    asked = frozenset(external_ids)
    if not asked:
        return frozenset()
    gone = asked & await adapter.confirm_gone(asked)
    for outcome, count in (("gone", len(gone)), ("served", len(asked) - len(gone))):
        if count:
            _retraction_checks.add(count, {"source": source, "path": path, "outcome": outcome})
    return gone
```

- [ ] **Step 6: Beat while waiting, and ask before retracting**

In `src/usher/services/reconcile.py`:

- Change `from collections.abc import Awaitable, Callable, Mapping, Sequence` to
  `from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence`.
- Add `from typing import Any` after the `datetime` import.
- Add `from usher.services.retraction import confirmed_gone` after the
  `usher.services.ingest` import.

Directly after `_beat`, add:

```python
    async def _beating[T](self, progress: _Progress, work: Coroutine[Any, Any, T]) -> T:
        """Await `work`, moving the run's heartbeat every `heartbeat_seconds` meanwhile.

        `work` must not touch the session, because a beat commits beside it. However the
        wait ends -- `work` raising, or a beat -- `work` is not left running.
        """
        task = asyncio.create_task(work)
        try:
            pending: set[asyncio.Task[T]] = {task}
            while pending:
                _, pending = await asyncio.wait(pending, timeout=self._heartbeat_seconds)
                if pending:
                    await self._beat(progress)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return task.result()
```

In `_walk_plan`, replace the block from `planning = asyncio.create_task(adapter.plan_walk())`
through `plan = planning.result()` with:

```python
            plan = await self._beating(progress, adapter.plan_walk())
```

Replace the whole of `_sweep` with:

```python
    async def _sweep(
        self, progress: _Progress, kind: SyncRunKind, source_name: str, adapter: SourceAdapter
    ) -> SyncRun:
        """Retract what the source confirms gone -- full walks only, after one finished.

        The rows the walk did not list are the candidates, and the ceiling is applied to
        them before the source is asked anything. Of the rest, those the source no longer
        serves are retracted, and those it still serves -- alternate versions, which
        Emby's full listing never shows -- are stamped as seen at the run's `started_at`. Both
        writes commit with the run's completing row. A source that cannot answer raises
        before either, so nothing changes and the run fails resumable.

        `source_name` is carried in for the metric's label rather than read off the run,
        which holds only a `source_id`: `usher.sync.run.duration` beside it is already
        labelled by name, and a second per-source identity in telemetry is one identity
        too many.
        """
        run = progress.run
        if kind is not SyncRunKind.FULL:
            return run
        try:
            unseen = await self._media_items.list_unseen(
                run.source_id,
                seen_since=run.started_at,
                max_retract_fraction=self._max_retract_fraction,
            )
        except AvailabilitySweepRefused as exc:
            # The refusal's own numerator, never a retraction count -- which is
            # what a refused sweep did, i.e. nothing. See the instrument's own
            # comment: the two differ exactly when the guard fires, which is the
            # one state this series exists for.
            _retraction_fraction.record(
                _fraction(exc.would_retract, exc.total),
                {"source": source_name, "outcome": "refused"},
            )
            logger.error(
                "availability sweep refused for source {source_id}: {error}",
                source_id=run.source_id,
                error=str(exc),
            )
            # Re-raised so `reconcile`'s own handler records it as a failed
            # run -- a refusal is not a successful reconcile with a footnote.
            # `AvailabilitySweepRefused` is a `UsherPortError`, so it lands
            # in the same branch a transport failure does.
            raise
        gone = await self._beating(
            progress,
            confirmed_gone(adapter, unseen.external_ids, source=source_name, path="sweep"),
        )
        retraction = await self._media_items.retract(
            run.source_id, gone, unseen_since=run.started_at
        )
        await self._media_items.mark_seen(
            run.source_id, unseen.external_ids - gone, seen_at=run.started_at
        )
        logger.info(
            "{source}'s sweep found {candidates} rows the walk did not list: "
            "{gone} gone, {served} still served",
            source=source_name,
            candidates=len(unseen.external_ids),
            gone=len(gone),
            served=len(unseen.external_ids) - len(gone),
        )
        _retraction_fraction.record(
            _fraction(retraction.retracted, unseen.total),
            {"source": source_name, "outcome": "swept"},
        )
        # `progress.run`, not `run`: a beat while the source was asked moved its heartbeat.
        return progress.run.evolve(items_retracted=retraction.retracted)
```

At the call site (line 345), change `run = await self._sweep(progress.run, kind,
source.name)` to:

```python
                        run = await self._sweep(progress, kind, source.name, adapter)
```

- [ ] **Step 7: Run the unit cases to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_services_reconcile.py tests/unit/test_telemetry_retraction.py -p no:randomly`

Expected: PASS, the existing heartbeat-while-planning cases included.

- [ ] **Step 8: Give the integration duck adapters `confirm_gone`, and add one case**

In `tests/integration/test_services_reconcile.py`:
- add `Collection` to the `collections.abc` import;
- change `_Adapter`'s docstring first line to
  ``"""The smallest `list_items`, `plan_walk`, `list_unit`, `confirm_gone` for `ReconcileService`.``;
- at the end of `_Adapter.__init__`, add:

```python
        # Served by id but in no item listing, as Emby's full listing leaves out an
        # alternate version.
        self.unlisted: set[str] = set()
```

- in `_walk`, change the loop's head to:

```python
        for index, item in enumerate(
            [item for item in self.items.values() if item.external_id not in self.unlisted]
        ):
```

- after `plan_walk`, add:

```python
    async def confirm_gone(self, external_ids: Collection[str]) -> frozenset[str]:
        return frozenset(external_ids).difference(self.items)
```

In the case whose docstring opens `The sweep's own SQL, driven by the service
that decides when it runs.`, change `` `mark_unseen_unavailable`'s `last_seen_at <
:seen_since` `` to `` `list_unseen`'s `last_seen_at < :seen_since` ``.

Directly after that case, add:

```python
async def test_a_walk_retracts_only_what_the_source_confirms_gone(
    service: ReconcileService,
    media_items: PostgresMediaItemRepository,
    source: Source,
    adapter: _Adapter,
) -> None:
    """Against the real table: m6 is gone, m7 is served but in no listing."""
    for index in range(10):
        adapter.items[f"m{index}"] = _item(f"m{index}")
    await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    del adapter.items["m6"]
    adapter.unlisted.add("m7")
    second = await service.reconcile(source, SyncRunKind.FULL, adapter)  # type: ignore[arg-type]
    assert second.items_seen == 8, "the premise: the walk listed neither m6 nor m7"
    assert second.status is SyncRunStatus.COMPLETED
    assert second.items_retracted == 1
    gone = await media_items.get_by_external_id(source.id, "m6")
    assert gone is not None and gone.available is False
    served = await media_items.get_by_external_id(source.id, "m7")
    assert served is not None
    assert (served.available, served.last_seen_at) == (True, second.started_at)
```

`tests/integration/test_pipeline_spans.py` has a duck of the same shape, which a
full walk now asks too:
- add `Collection` to its `collections.abc` import;
- replace its `_Adapter` docstring with:

```python
    """The smallest `list_items`, `plan_walk`, `list_unit`, `confirm_gone` for `ReconcileService`.

    No network. `tests/integration/test_services_reconcile.py` uses the same shape
    and for the same reason: `FakeSourceAdapter` carries a session model and a
    watch-state store, none of which is under test here.
    """
```

- after `list_unit`, add:

```python
    async def confirm_gone(self, external_ids: Collection[str]) -> frozenset[str]:
        return frozenset(external_ids).difference(item.external_id for item in self._items)
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/integration/test_services_reconcile.py tests/integration/test_pipeline_spans.py -p no:randomly`

Expected: PASS (Docker).

- [ ] **Step 9: Plant four guards**

Back up first: `cp src/usher/services/reconcile.py /var/tmp/confirmed-retraction-plant.bak`.
For each plant, run Step 7's command and confirm the named case fails on an `E `
line. Then restore the file and read the restore back with `diff`.

1. Pass `unseen.external_ids` to `retract` in place of `gone`. Expected:
   `test_a_sweep_retracts_what_the_source_confirms_gone_and_stamps_what_it_serves`
   fails.
2. Delete the `mark_seen` call. Expected: the same case fails, on m8's
   `last_seen_at`.
3. Call `confirmed_gone` directly instead of through `self._beating`. Expected:
   `test_the_heartbeat_beats_while_the_source_confirms_removals` fails.
4. Return `run.evolve(...)` in place of `progress.run.evolve(...)`. Expected:
   `test_the_heartbeat_beats_while_the_source_confirms_removals` fails, on its
   last assertion.

- [ ] **Step 10: Update the docstrings that state the old rule**

In `src/usher/ports/ingest.py`, replace the first paragraph of
`MediaItemUpsert`'s docstring body (it begins `` `last_seen_at` has no default.``)
with:

```python
    `last_seen_at` has no default. It decides which rows the availability
    sweep asks the source about, and it must be the *run's* start instant
    rather than each row's own write instant -- a per-row `now()` would make
    the sweep's `last_seen_at < run.started_at` comparison race against the
    batch it is sweeping over.
```

Replace `AvailabilitySweepRefused`'s docstring from its first line up to (not
including) the paragraph that begins `Carries the numbers` with:

```python
    """A walk left more of a source unlisted than the ceiling permits, so its sweep stopped.

    `SourceAdapter.list_items` already guarantees a walk raises rather than truncating,
    and `ReconcileService` refuses to sweep after a run that raised. This covers the
    residual: a walk that *completes* and lists far less than the library holds -- an
    unmounted drive, a library removed by accident, a permissions change on the
    source's account. Confirmation cannot tell those from a genuine mass deletion
    either -- the source no longer serves the items in every one of them -- so above
    the ceiling the sweep asks nothing, retracts nothing, and says so.

```

- [ ] **Step 11: Update the PRD, the guide, the metric table and the changelog**

`docs/prd/03-sources-and-sync.md`:

1. In the lanes table, change the **Full reconcile** row's last cell from
   `Walk the source; upsert everything; mark unseen items `available = false``
   to:
   `Walk the source; upsert everything; ask the source about the rows the walk did not list, and mark those it confirms gone `available = false``
2. Replace the paragraph that begins `**Retraction is a separate step, and it can
   decline.**` (it ends `not of the source itself.`) with:

   ```markdown
   **Retraction is a separate step, and it can decline.** It runs only after a
   walk returns normally — after a whole-library walk, once every unit has
   completed. The rows the walk did not list are its candidates. If they are more
   than `sync_max_retract_fraction` (default `0.25`) of a source's rows, it
   refuses, raising and changing nothing, before asking the source anything.
   `1.0` disables the ceiling. The ceiling is a fraction of what *Usher* holds
   for that source, not of the source itself. Otherwise it asks the source about
   each candidate. It marks unavailable those the source confirms gone, and
   records the rest as seen at the walk's start, since the source still serves
   them. If the source cannot answer, the sweep changes nothing and the run
   fails; the next full walk resumes it and only sweeps.
   ```

3. In the paragraph that begins `**A source is either a library this deployment
   owns`, change `the retraction ceiling treats removals as the operator's to
   authorise` to `the retraction ceiling treats a walk that left many of a
   source's rows unlisted as the operator's to authorise`.

`docs/prd/08-operations.md`: directly after the row that begins `| A refused
availability sweep |`, add:

```markdown
| A source cannot confirm a removal | A full walk's sweep retracts **nothing** and stamps nothing, and the run records `FAILED` with the error. It is not the ceiling's `availability_ceiling`, so the next full walk resumes the run, walks no unit again, and only sweeps. The catalog is unaffected |
```

`docs/guide/command-line.md`: replace the bullet that begins `- **The retraction
guard** refuses` with:

```markdown
- **The retraction guard** refuses a walk that left more than
  `USHER_SYNC_MAX_RETRACT_FRACTION` (25%) of a source's items unlisted, and
  changes nothing. If you really did remove that much, run the walk with
  `--allow-full-retraction`. A walk marks an item unavailable only once the
  server confirms it is gone. An item the server still serves but did not
  list, such as a film's second version, stays available.
```

`docs/prd/10-telemetry-and-dashboards.md`:
- change `44 rows: 43 instruments Usher declares` to `45 rows: 44 instruments Usher declares`;
- directly after the `` `usher.sync.retraction.fraction` `` row, add:

```markdown
| `usher.sync.retraction.checks` | counter | source, path, outcome | ✅ confirmed retraction |
```

Pins:
- In `tests/unit/test_telemetry_metric_names.py`, change
  `assert len(catalogue) == 44` to `assert len(catalogue) == 45`.
- In `tests/unit/test_dashboards.py`, change `assert len(catalogue) == 44` to
  `== 45`, and `not the 44 its own header` to `not the 45 its own header`.

`CHANGELOG.md`, under `## [Unreleased]`:
- Add as the first bullet of `### Fixed`:

  ```markdown
  - **A full walk no longer marks items unavailable that the server still
    serves.** Emby's full listing shows one version of a film with several, so
    every full walk retracted the alternate versions a delta or a push event had
    stored. The sweep now asks the server about the items the walk did not list,
    retracts only those it confirms gone, and records the rest as seen. If the
    server cannot answer, nothing is retracted, and the next full walk resumes
    the run and only sweeps.
  ```

- Add as the first bullet of `### Added`:

  ```markdown
  - The counter `usher.sync.retraction.checks` counts the items a sweep or a
    push removal asked the server about, by `path` (`sweep` or `push`) and
    `outcome` (`gone` or `served`).
  ```

`.claude/rules/emby-push-and-ingest.md`:
- In the frontmatter's `paths:`, add `  - "src/usher/services/retraction.py"` after
  the `reconcile.py` line.
- In the bullet that begins `- **A resumed whole-library walk keeps its run's
  `started_at`**`, change `and a fresh one retracts them all.` to `and a fresh
  one makes every one of them a sweep candidate.`

`CLAUDE.md`: in the rules table's `emby-push-and-ingest.md` row, change
`services/{push,ingest,matching,reconcile,watch_sync,watch_write}.py` to
`services/{push,ingest,matching,reconcile,retraction,watch_sync,watch_write}.py`.

- [ ] **Step 12: Run the docs tests, then the gate, then commit**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_telemetry_metric_names.py tests/unit/test_dashboards.py tests/unit/test_docs_pointers.py tests/unit/test_prd_10_panel_backing.py -p no:randomly`

Expected: PASS. Then run the full gate. Expected: green.

```bash
git add src/usher/services/retraction.py src/usher/services/reconcile.py src/usher/ports/ingest.py tests/fakes/source_adapter.py tests/unit/test_services_reconcile.py tests/unit/test_telemetry_retraction.py tests/integration/test_services_reconcile.py tests/integration/test_pipeline_spans.py docs/prd/03-sources-and-sync.md docs/prd/08-operations.md docs/guide/command-line.md docs/prd/10-telemetry-and-dashboards.md tests/unit/test_telemetry_metric_names.py tests/unit/test_dashboards.py CHANGELOG.md .claude/rules/emby-push-and-ingest.md CLAUDE.md
git commit -m "sync: a full walk's sweep retracts only what the source confirms gone"
```

---

### Task 6: retire `mark_unseen_unavailable`

Nothing in `src/` calls the old method after Task 5. This task removes it and
moves every remaining caller to the new methods.

**Files:**
- Modify: `src/usher/ports/repository/media_item.py`,
  `src/usher/db/repositories/media_item.py`, `tests/fakes/media_item_repository.py`,
  `src/usher/ports/ingest.py`
- Modify: every caller in the table below, plus `scripts/measure_ingest.py`,
  `scripts/measure_rows.py` and `src/usher/db/models/source.py`

**Interfaces:**
- Consumes: Task 4's `list_unseen`, `retract`, `UnseenRows` and `Retraction`.
- Produces: no `mark_unseen_unavailable`, `SweepResult`, `_SWEEP` or
  `_SWEEP_COUNTS` anywhere in `src/`, `tests/` or `scripts/`.

- [ ] **Step 1: Remove the old method and its types**

- In `src/usher/ports/repository/media_item.py`, delete the abstract
  `mark_unseen_unavailable` and drop `SweepResult` from the
  `usher.ports.ingest` import.
- In `src/usher/db/repositories/media_item.py`, delete `mark_unseen_unavailable`,
  delete `_SWEEP_COUNTS` with its comment, delete `_SWEEP`, and drop
  `SweepResult` from the import.
- In `tests/fakes/media_item_repository.py`, delete `mark_unseen_unavailable` and
  drop `SweepResult` from the import.
- In `src/usher/ports/ingest.py`, delete `SweepResult`.

Then replace the second paragraph of `MediaItemRepository`'s class docstring (it
begins `Availability is retracted by exactly one method`) with:

```python
    Availability is retracted only by `retract`, for rows the source confirms gone. A
    full walk's sweep asks about the rows it did not list, after the walk has returned
    normally -- `SourceAdapter.list_items` guarantees a walk raises rather than
    truncating, and that guarantee is worth nothing if the sweep runs either way -- so
    it is a separate call, never a side effect of `upsert_many`.
```

In the same class, `count_for_source`'s docstring names the sweep's denominator,
which nothing reads it for. Replace it with the one line
`"""How many items this source has, available or not."""`.

- [ ] **Step 2: Move the callers**

Each old call made rows unavailable by sweeping everything unseen before an
instant. Each replacement names those same rows.

| Site | Change |
|---|---|
| `tests/contract/media_item_repository_contract.py`, `test_an_older_sighting_never_rewinds_a_newer_one` (lines 141–144) | Replace the `swept = ...` call and its assert with `unseen = await repository.list_unseen(source_id, seen_since=RUN_AT, max_retract_fraction=1.0)` and `assert unseen.external_ids == frozenset()` |
| the same file, lines 187–331 | Delete the eight cases from `test_marking_unseen_unavailable_spares_items_seen_during_the_run` through `test_a_sweep_after_an_accepted_retraction_is_not_refused_forever`. Task 4's cases cover each of them |
| the same file, the call near line 831 | `await repository.retract(other_source_id, ["theirs"], unseen_since=RUN_AT)` |
| the same file, the calls near lines 854 and 1001 | `await repository.retract(source_id, ["stale", "fresh"], unseen_since=RUN_AT + timedelta(days=1))` |
| the same file, the call near line 1277 | `await repository.retract(source_id, ["gone"], unseen_since=RUN_AT)` |
| `tests/contract/title_repository_contract.py`, line 832 | In the docstring, change `` `mark_unseen_unavailable` sets it false for an item a walk stopped seeing `` to `` `retract` sets it false for an item its source confirmed gone `` |
| `tests/integration/test_media_item_repository.py`, lines 269–319 | Delete `test_the_sweep_costs_the_same_number_of_statements_however_big_it_is` and `test_a_refused_sweep_issues_no_update_at_all`. Task 4's four cases replace them. In `TestPostgresMediaItemRepository`'s docstring, change `the four Postgres-only tests below share them` to `the Postgres-only tests below share them` |
| `tests/unit/test_ports_repository_ingest.py` | In `test_media_item_repository_surface`, delete `"mark_unseen_unavailable",` from the set |
| `tests/integration/test_services_titles.py`, line 302 | `await PostgresMediaItemRepository(session).retract(other.id, ["theirs"], unseen_since=datetime(2026, 8, 2, tzinfo=UTC))` |
| `tests/integration/test_titles_route.py`, line 250 | `await PostgresMediaItemRepository(session).retract(theirs.id, ["theirs-1"], unseen_since=SWEPT_AFTER)`, keeping the `await session.commit()` after it |
| `tests/unit/test_api_titles.py`, line 983 | `await media_items.retract(source.id, [EXTERNAL_ID], unseen_since=datetime(2026, 8, 2, tzinfo=UTC))` |
| `tests/unit/test_services_titles.py`, line 374 | `await media_items.retract(source.id, ["e1"], unseen_since=datetime(2026, 8, 2, tzinfo=UTC))` |
| `tests/unit/test_services_watch_write.py`, line 611 | `await household.media_items.retract(source_id, ["gone-42"], unseen_since=datetime.now(UTC))` |
| `tests/unit/test_services_watch_sync.py`, lines 474–480 | In `test_a_walk_never_retracts_a_watch_state`, spy on `retract` in place of `mark_unseen_unavailable`: change both mentions |
| `tests/unit/test_services_reconcile.py` | Nothing: Task 5 re-pointed the window case |
| `tests/integration/test_services_reconcile.py` | Nothing: Task 5 updated the docstring |

Before each replacement in the table, read the case's seeding. The ids above
must be every row of that source whose `last_seen_at` is before the old call's
`seen_since`. If a case seeds one more, add it to the list.

In `tests/unit/test_services_playback.py`:
- change the comment above `AFTER_EVERYTHING` (lines 51–52) to
  `# Past every `last_seen_at` any case seeds, so `retract` retracts whatever it` /
  `# is pointed at.`;
- in `_Household.__init__`, add `self._copies: dict[uuid.UUID, list[str]] = {}`
  (import `uuid` if the module does not already);
- at the end of `add_copy`, add
  `self._copies.setdefault(source.id, []).append(external_id)`;
- in `_Household.retract`, replace the call with
  `await self.media_items.retract(source.id, self._copies.get(source.id, []), unseen_since=AFTER_EVERYTHING)`;
- in the docstring of
  `test_a_household_holding_only_unavailable_copies_still_gets_targets`, change
  `` `mark_unseen_unavailable` sets `available = false` `` to
  `` `retract` sets `available = false` ``.

In `tests/unit/test_ports_ingest.py`:
- drop `SweepResult` from the `usher.ports.ingest` import;
- delete `test_a_sweep_result_reports_the_denominator_as_well_as_the_count`. A
  frozen dataclass built and read back is a case no plant can fail, and the
  contract's whole-value `UnseenRows` equalities already pin both fields on both
  arms;
- in `test_the_sweep_refusal_survives_a_zero_denominator`'s docstring, change
  `` (`stale > 0` implies `total > 0`) `` to `` (`unseen > 0` implies `total > 0`) ``.

In `tests/integration/test_ingest_end_to_end.py`:
- drop `SweepResult` from the line-42 import;
- rename `test_the_availability_sweeps_update_uses_its_index` to
  `test_the_availability_sweeps_unseen_read_uses_its_index`, with the docstring
  ``"""`ix_media_items_sweep` exists for the sweep's unseen read; the numbers say exactly that."""``;
- change the capture to:

```python
    captured, stop = _capture(
        lambda statement: "SELECT external_id" in statement and "last_seen_at <" in statement
    )
```

- replace the call and its premise with:

```python
        unseen = await media_items.list_unseen(
            source.id, seen_since=seen_since, max_retract_fraction=1.0
        )
    finally:
        stop()
    assert (len(unseen.external_ids), unseen.total) == (_STALE_ON_IT, _SWEPT_SOURCE), (
        "the premise: a mostly-fresh source with a stale tail, not a source that vanished"
    )
    assert captured, "list_unseen's id read was not issued"
```

Leave the rest of that case, `A_DECISIVE_MARGIN` included, as it is. **If the
margin assertion fails, do not lower it:** stop and report the two plans it
prints.

In `scripts/measure_ingest.py` (lines 339–352):
- change the capture predicate to
  `lambda one: "count(*) FILTER" in one or "SELECT external_id" in one`;
- change the call to
  `await media_items.list_unseen(source.id, seen_since=datetime.now(UTC), max_retract_fraction=1.0)`.

In `scripts/measure_rows.py`, the probe moves to the id read, and its
`seen_since` must split the seed's stale rows from its fresh ones whenever the
seed ran, at every scale the script accepts:
- change `from datetime import UTC, datetime` to
  `from datetime import UTC, datetime, timedelta`, and add
  `from dataclasses import dataclass` before it;
- change line 14's `_SWEEP` to `_UNSEEN`;
- directly after `_NARROW`, add the seed's arithmetic, shared by `_seed` and the
  scale check:

```python
# How many rows the seed stamps stale. A seed must hold more, or none is left fresh and
# the unseen read selects nothing.
_STALE_ROWS = 200


@dataclass(frozen=True, slots=True)
class _Sizes:
    """One seed's populations, scaled linearly off one real library's proportions."""

    movies: int
    series: int
    regular: int
    pathological: int

    @property
    def episodes(self) -> int:
        """How many episodes the seed inserts.

        Series 0 carries the pathological run and every other series `regular` episodes, so
        with no series there are none.
        """
        return self.pathological + self.regular * (self.series - 1) if self.series else 0

    @property
    def media_items(self) -> int:
        """One per title, movies and series alike, and one per episode."""
        return self.movies + self.series + self.episodes


def _sizes(scale: int) -> _Sizes:
    factor = scale / 1_126_674
    # No population goes below zero: argparse takes a negative --scale, and `generate_series`
    # yields no rows for a negative bound, so zero is what the seed would insert.
    return _Sizes(
        movies=max(0, int(94_448 * factor)),
        series=max(0, int(32_409 * factor)),
        regular=max(1, int(30 * factor)),
        pathological=max(0, int(20_000 * factor)),
    )


def _smallest_scale() -> int:
    """The smallest `--scale` whose seed leaves a row fresh beside the stale ones."""
    scale = 1
    while _sizes(scale).media_items <= _STALE_ROWS:
        scale += 1
    return scale
```

- in `_seed`, replace the block from `factor = scale / 1_126_674` through the
  `print(...)` with:

```python
    sizes = _sizes(scale)
    print(f"seeding {sizes.movies} movies, {sizes.series} series, {sizes.episodes} episodes")
```

  and read `movies`, `series`, `pathological` and `regular` off `sizes` in the
  four statements that bind them (`sizes.movies`, `sizes.series - 1`,
  `sizes.pathological`, `sizes.regular`);
- replace the stale-row comment and `UPDATE` with:

```python
    # `_STALE_ROWS` stale rows, which is the nightly shape `f1a7d3c9e824`'s sweep index
    # exists for. Without them the sweep's unseen read plans against nothing and this
    # script's whole reason to run is vacuous.
    await session.execute(
        text(
            "UPDATE media_items SET last_seen_at = now() - interval '3 days' "
            "WHERE id IN (SELECT id FROM media_items LIMIT :stale)"
        ),
        {"stale": _STALE_ROWS},
    )
```

- in `_statements`, directly after the `source = ...` line, add:

```python
    # The seed stamps every row with one instant and `_STALE_ROWS` of them three days
    # earlier, so a day before the newest stamp splits the two however long ago it ran.
    seeded_at = (
        await session.execute(
            text("SELECT max(last_seen_at) FROM media_items WHERE source_id = :source_id"),
            {"source_id": source},
        )
    ).scalar_one()
    if seeded_at is None:
        raise SystemExit(
            "the measured source holds no media items, so list_unseen's id read would plan "
            "against nothing: run the seed first, which this script does on an empty database"
        )
```

- change the sweep's tuple to:

```python
        (
            "list_unseen's id read",
            _UNSEEN,
            {"source_id": source, "seen_since": seeded_at - timedelta(days=1)},
        ),
```

- at the top of `measure`, before `settings = get_settings()`, add:

```python
    planned = _sizes(scale).media_items
    if planned <= _STALE_ROWS:
        raise SystemExit(
            f"--scale {scale} seeds {planned} media items and the stale stamp takes "
            f"{_STALE_ROWS}, so none would be fresh: use --scale {_smallest_scale()} or more"
        )
```

In `src/usher/db/models/source.py`, line 114, replace
`# The availability sweep's `UPDATE`, and nothing else.` with:

```python
        # Both of `list_unseen`'s reads: the id read seeks on all three columns, and the
        # count is answered from the index alone.
```

- [ ] **Step 3: Check nothing still names the old method**

```bash
grep -rnE "mark_unseen_unavailable|SweepResult|\b_SWEEP\b|_SWEEP_COUNTS" src tests scripts
```

Expected: no output. The leading `\b` keeps the unrelated `_PADDING_SWEEP` out.

- [ ] **Step 4: Run the gate, then commit**

Run the full gate. Expected: green, the index case in
`test_ingest_end_to_end.py` included. That case needs Docker.

```bash
git add -A src tests scripts
git status --short
git commit -m "db: retire mark_unseen_unavailable"
```

`git status --short` must list only files named in this task.

---

### Task 7: a push removal retracts what the source confirms gone

**Files:**
- Modify: `src/usher/services/push.py`:
  - the imports (lines 1–28);
  - `PushOutcome` (lines 37–50);
  - `PushApplyService.__init__` (lines 54–73);
  - `apply`'s removal branch (line 93);
  - `_ignore_removal` (lines 226–246), which becomes `_apply_removal`.
- Modify: `src/usher/composition.py`, the `PushApplyService(...)` call (line 570)
- Modify: `src/usher/ports/repository/media_item.py`, the class docstring and
  a new abstract `available_episodes_of` after `available_among`
- Modify: `src/usher/db/repositories/media_item.py` and
  `tests/fakes/media_item_repository.py`: `available_episodes_of`
- Test: `tests/contract/media_item_repository_contract.py` (three cases),
  `tests/integration/test_media_item_repository.py` (one statement-count case)
  and `tests/unit/test_ports_repository_ingest.py` (the surface set)
- Test: `tests/unit/test_services_push.py`
- Test: `tests/unit/test_telemetry_push.py`
- Test: `tests/integration/test_services_push.py`
- Test: `tests/integration/test_push_lane_end_to_end.py` (line 90) and
  `tests/unit/test_services_watch_sync.py` (line 1172), construction sites only
- Docs:
  - `docs/prd/03-sources-and-sync.md`, `docs/prd/08-operations.md`,
    `docs/guide/command-line.md`;
  - `CHANGELOG.md`;
  - `.claude/rules/emby-push-and-ingest.md` and `tests/fixtures/emby/README.md`.

**Interfaces:**
- Consumes:
  - `confirmed_gone(..., path="push")` (Task 5);
  - `MediaItemRepository.available_among` and `retract`, and `Retraction` (Task 4);
  - `FakeSourceAdapter.confirm_calls` and `fail_confirm()` (Task 3).
- Produces:
  - `MediaItemRepository.available_episodes_of(source_id: uuid.UUID, external_ids: Collection[str]) -> frozenset[str]`,
    abstract: the available episode rows of the series among `external_ids`.
  - `PushApplyService(ingest, watch, events, commit, *, media_items: MediaItemRepository, cache: RowCache | None = None, max_items_per_event: int = 50)`.
    `media_items` is required.
  - `PushOutcome.items_retracted: int = 0`, which replaces `ignored`.

- [ ] **Step 1: Write the failing removal cases**

In `tests/unit/test_services_push.py`, make these import changes:
- `from collections.abc import AsyncIterator, Callable, Iterator` becomes
  `from collections.abc import AsyncIterator, Callable, Collection, Iterator`;
- add `from loguru import logger` after `import pytest`;
- `from usher.ports.events import ClientEventKind` becomes
  `from usher.ports.events import ClientEvent, ClientEventKind`;
- `from usher.ports.ingest import MediaItemUpsert` becomes
  `from usher.ports.ingest import MediaItemUpsert, Retraction`.

In `_Fixture.__init__`, add `media_items=self.media_items,` to the
`PushApplyService(...)` call, directly after `self._commit,`.

Replace `given_matched` with these two methods:

```python
    async def given_row(
        self,
        external_id: str,
        *,
        title_id: uuid.UUID | None,
        episode_id: uuid.UUID | None = None,
        changed_at: AwareDatetime = T0,
    ) -> None:
        """A stored, available row for `external_id`, which the source also serves."""
        self.adapter.seed(_item(external_id), changed_at)
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=self.source.id,
                    external_id=external_id,
                    title_id=title_id,
                    episode_id=episode_id,
                    container="mkv",
                    video_codec=None,
                    audio_codec=None,
                    width=None,
                    height=None,
                    hdr_format=None,
                    audio_channels=None,
                    file_size_bytes=None,
                    runtime_seconds=None,
                    added_at=None,
                    last_seen_at=T0,
                )
            ]
        )

    async def given_matched(self, external_id: str, *, changed_at: AwareDatetime = T0) -> uuid.UUID:
        title_id = new_id()
        await self.given_row(external_id, title_id=title_id, changed_at=changed_at)
        self.title_ids[external_id] = title_id
        return title_id
```

Replace `test_a_removed_item_retracts_nothing` with:

```python
async def test_a_confirmed_removal_retracts_and_publishes_after_its_commit(
    fixture: _Fixture,
) -> None:
    """Two copies of one film are one `title.updated`; an unmatched row is retracted silently."""
    film = new_id()
    await fixture.given_row("copy-a", title_id=film)
    await fixture.given_row("copy-b", title_id=film)
    other = await fixture.given_matched("other-1")
    await fixture.given_row("unmatched-1", title_id=None)
    removed = ("copy-a", "copy-b", "other-1", "unmatched-1")
    for external_id in removed:
        fixture.adapter.forget(external_id)
    commits_at_publish: list[int] = []
    publish = fixture.events.publish

    async def _recording(event: ClientEvent) -> None:
        commits_at_publish.append(fixture.commits)
        await publish(event)

    fixture.events.publish = _recording  # type: ignore[method-assign]
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=removed)
    )
    assert outcome == PushOutcome(items_retracted=4)
    assert fixture.commits == 2, "the read's commit before asking, then the retraction's"
    assert commits_at_publish == [2, 2], "a client heard of a retraction before its commit"
    published = fixture.events.published
    assert len(published) == 2
    assert {(event.kind, event.title_id, event.episode_id) for event in published} == {
        (ClientEventKind.TITLE_UPDATED, film, None),
        (ClientEventKind.TITLE_UPDATED, other, None),
    }
    assert all(event.data == {"fields": ["availability"]} for event in published)
    for external_id in removed:
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, external_id)
        assert stored is not None and stored.available is False, external_id


async def test_a_removal_publishes_in_sorted_order_whatever_order_its_targets_iterate(
    fixture: _Fixture,
) -> None:
    """Sorted by title id, so the order a client hears does not follow set iteration.

    Literal ids, because a `UUID`'s hash and `None`'s, unlike a `str`'s, are the same on
    every run. That is not enough on its own: the fake adds the targets in the order of a
    set of `str` ids, which moves with the seed, and where two targets share a slot of the
    set's table, the one added first takes it. These three take three of the eight slots a
    three-entry set has (`hash(target) % 8` is 7, 3 and 0 for `first`, `second` and
    `third`), so they iterate in one order whatever order they arrive in, and the premise
    shows it is not the sorted one. New ids need distinct slots too: with ids ending in 0, 1
    and 6, two share slot 7 and the premise fails under some seeds. The rows are stored, and
    the event names them, in two orders that are not sorted either, so neither can stand in
    for the sort.
    """
    first, second, third = (uuid.UUID(f"00000000-0000-7000-8000-00000000000{n}") for n in "124")
    rows = {"gone-a": second, "gone-b": third, "gone-c": first}
    for external_id, title_id in rows.items():
        await fixture.given_row(external_id, title_id=title_id)
        fixture.adapter.forget(external_id)
    retract = fixture.media_items.retract
    returned: list[Retraction] = []

    async def _spying(
        source_id: uuid.UUID, external_ids: Collection[str], *, unseen_since: AwareDatetime
    ) -> Retraction:
        retraction = await retract(source_id, external_ids, unseen_since=unseen_since)
        returned.append(retraction)
        return retraction

    fixture.media_items.retract = _spying  # type: ignore[method-assign]
    await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-b", "gone-c", "gone-a"))
    )
    (retraction,) = returned
    assert [target.title_id for target in retraction.targets] != [first, second, third], (
        "the premise: the targets iterate out of sorted order"
    )
    assert [event.title_id for event in fixture.events.published] == [first, second, third]


async def test_a_removal_the_source_still_serves_changes_nothing(fixture: _Fixture) -> None:
    """What an Emby library refresh sends: `ItemsRemoved` for a file that is still there."""
    await fixture.given_matched("kept-1")
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("kept-1",))
    )
    assert fixture.adapter.confirm_calls == [frozenset({"kept-1"})]
    assert outcome == PushOutcome()
    assert fixture.commits == 1, "only the read's transaction, ended before asking"
    assert fixture.events.published == []
    stored = await fixture.media_items.get_by_external_id(fixture.source.id, "kept-1")
    assert stored is not None and stored.available is True


async def test_a_removal_of_ids_usher_does_not_hold_asks_nothing(fixture: _Fixture) -> None:
    """Folders, seasons and extras are removed too, and Usher stores no row for them."""
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("folder-1", "season-1"))
    )
    assert fixture.adapter.confirm_calls == []
    assert outcome == PushOutcome()
    assert fixture.commits == 0


async def test_a_removal_larger_than_the_item_limit_is_applied_not_deferred() -> None:
    """The source is asked a hundred ids a request, so a removal never costs a delta walk."""
    fixture = _Fixture(max_items_per_event=1)
    for external_id in ("gone-1", "gone-2"):
        await fixture.given_matched(external_id)
        fixture.adapter.forget(external_id)
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1", "gone-2"))
    )
    assert outcome == PushOutcome(items_retracted=2)


async def test_a_removal_the_source_cannot_confirm_raises_and_changes_nothing(
    fixture: _Fixture,
) -> None:
    """As any failed read in the lane does."""
    await fixture.given_matched("gone-1")
    fixture.adapter.forget("gone-1")
    fixture.adapter.fail_confirm()
    with pytest.raises(PortUnavailable):
        await fixture.apply(
            SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1",))
        )
    stored = await fixture.media_items.get_by_external_id(fixture.source.id, "gone-1")
    assert stored is not None and stored.available is True
    assert fixture.commits == 1, "only the read's transaction, ended before asking"
    assert fixture.events.published == []


async def test_a_removal_naming_an_id_twice_asks_and_publishes_once(fixture: _Fixture) -> None:
    title_id = await fixture.given_matched("gone-1")
    fixture.adapter.forget("gone-1")
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1", "gone-1"))
    )
    assert fixture.adapter.confirm_calls == [frozenset({"gone-1"})]
    assert outcome == PushOutcome(items_retracted=1)
    assert [event.title_id for event in fixture.events.published] == [title_id]


async def test_a_removal_of_an_already_retracted_row_asks_nothing(fixture: _Fixture) -> None:
    await fixture.given_matched("gone-1")
    fixture.adapter.forget("gone-1")
    premise = await fixture.media_items.retract(
        fixture.source.id, ["gone-1"], unseen_since=datetime.now(UTC)
    )
    assert premise.retracted == 1
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1",))
    )
    assert fixture.adapter.confirm_calls == []
    assert outcome == PushOutcome()
    assert fixture.events.published == []


async def test_a_removal_says_what_it_asked_and_what_it_retracted(fixture: _Fixture) -> None:
    await fixture.given_matched("gone-1")
    await fixture.given_matched("kept-1")
    fixture.adapter.forget("gone-1")
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        await fixture.apply(
            SourceEvent(
                kind=SourceEventKind.ITEM_REMOVED, external_ids=("gone-1", "kept-1", "never-1")
            )
        )
    finally:
        logger.remove(sink)
    assert [line.rstrip("\n") for line in lines if "items removed" in line] == [
        "Living Room Emby reported 3 items removed: 2 held, 1 gone, 1 retracted, 1 still served"
    ]


async def test_a_row_read_since_the_removal_arrived_survives_a_gone_answer(
    fixture: _Fixture,
) -> None:
    """The guard is the instant the removal began, so a read after it wins over the answer.

    The log line counts the row gone and not retracted, so its numbers still add up.
    """
    await fixture.given_matched("back-1")
    fixture.adapter.forget("back-1")
    original = fixture.adapter.confirm_gone

    async def _read_back_first(external_ids: Collection[str]) -> frozenset[str]:
        await fixture.ingest.ingest_batch(
            fixture.source.id, [_item("back-1")], observed_at=datetime.now(UTC)
        )
        return await original(external_ids)

    fixture.adapter.confirm_gone = _read_back_first  # type: ignore[method-assign]
    before = datetime.now(UTC)
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        outcome = await fixture.apply(
            SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("back-1",))
        )
    finally:
        logger.remove(sink)
    stored = await fixture.media_items.get_by_external_id(fixture.source.id, "back-1")
    assert stored is not None
    assert stored.last_seen_at >= before, "the premise: read after the removal began"
    assert stored.available is True
    assert outcome == PushOutcome()
    assert fixture.events.published == []
    assert [line.rstrip("\n") for line in lines if "items removed" in line] == [
        "Living Room Emby reported 1 items removed: 1 held, 1 gone, 0 retracted, 0 still served"
    ]


async def _given_a_series(fixture: _Fixture) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """`series-1` with episodes `ep-1` to `ep-3`, plus `ep-other` of another series.

    The source no longer serves `series-1`, `ep-1` or `ep-2`; it still serves `ep-3`.
    """
    series = new_id()
    await fixture.given_row("series-1", title_id=series)
    episodes = {f"ep-{number}": new_id() for number in (1, 2, 3)}
    for external_id, episode_id in episodes.items():
        await fixture.given_row(external_id, title_id=series, episode_id=episode_id)
    await fixture.given_row("ep-other", title_id=new_id(), episode_id=new_id())
    for external_id in ("series-1", "ep-1", "ep-2"):
        fixture.adapter.forget(external_id)
    return series, episodes


async def test_a_removed_series_retracts_its_episodes_asking_about_each_once(
    fixture: _Fixture,
) -> None:
    """Emby named `ep-1` beside the series and not `ep-2`; both go, and `ep-3` stays.

    `ep-1` was asked about with the series, so it is not asked again or counted twice.
    """
    series, episodes = await _given_a_series(fixture)
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        outcome = await fixture.apply(
            SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("series-1", "ep-1"))
        )
    finally:
        logger.remove(sink)
    assert fixture.adapter.confirm_calls == [
        frozenset({"series-1", "ep-1"}),
        frozenset({"ep-2", "ep-3"}),
    ]
    assert outcome == PushOutcome(items_retracted=3)
    expected = sorted(
        [(series, None), (series, episodes["ep-1"]), (series, episodes["ep-2"])],
        key=lambda pair: (str(pair[0]), str(pair[1])),
    )
    assert [(event.title_id, event.episode_id) for event in fixture.events.published] == expected
    for external_id, available in (
        ("series-1", False),
        ("ep-1", False),
        ("ep-2", False),
        ("ep-3", True),
        ("ep-other", True),
    ):
        stored = await fixture.media_items.get_by_external_id(fixture.source.id, external_id)
        assert stored is not None and stored.available is available, external_id
    assert [line.rstrip("\n") for line in lines if "items removed" in line] == [
        "Living Room Emby reported 2 items removed: 4 held, 3 gone, 3 retracted, 1 still served"
    ]


async def test_a_removal_ends_each_reads_transaction_before_asking_the_source(
    fixture: _Fixture,
) -> None:
    """Nothing is written before an answer, so no connection waits on one inside a transaction.

    The named rows are read, then the series' episodes; each read is committed before its
    question goes out, and the retraction after both is committed once more.
    """
    await _given_a_series(fixture)
    commits_at_ask: list[int] = []
    original = fixture.adapter.confirm_gone

    async def _recording(external_ids: Collection[str]) -> frozenset[str]:
        commits_at_ask.append(fixture.commits)
        return await original(external_ids)

    fixture.adapter.confirm_gone = _recording  # type: ignore[method-assign]
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("series-1",))
    )
    assert outcome == PushOutcome(items_retracted=3), "the premise: both questions were asked"
    assert commits_at_ask == [1, 2]
    assert fixture.commits == 3


async def test_a_served_series_does_not_ask_about_its_episodes(fixture: _Fixture) -> None:
    """Only a series the source confirms gone takes its episodes with it, even a gone one."""
    series = new_id()
    await fixture.given_row("series-1", title_id=series)
    await fixture.given_row("ep-1", title_id=series, episode_id=new_id())
    fixture.adapter.forget("ep-1")
    outcome = await fixture.apply(
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("series-1",))
    )
    assert fixture.adapter.confirm_calls == [frozenset({"series-1"})]
    assert outcome == PushOutcome()
```

- [ ] **Step 2: Write the failing counter case**

In `tests/unit/test_telemetry_push.py`:
- add `from datetime import UTC, datetime` after `import uuid`;
- add `from usher.ports.ingest import MediaItemUpsert` before the
  `usher.ports.source` import;
- change that import to
  `from usher.ports.source import SourceEvent, SourceEventKind, SourceItem, SourceItemKind`;
- add `T0 = datetime(2026, 7, 1, tzinfo=UTC)` directly above `SOURCE = Source(`.

Change `_applier`:
- its signature becomes:

```python
def _applier(
    events: FakeEventPublisher, media_items: FakeMediaItemRepository | None = None
) -> PushApplyService:
```

- `media_items = FakeMediaItemRepository()` becomes
  `if media_items is None:` / `media_items = FakeMediaItemRepository()`;
- add `media_items=media_items,` as the last argument of `PushApplyService(...)`,
  after `_commit,`.

In `test_every_applied_event_is_counted_by_source_and_kind`, replace the
docstring's first paragraph after the summary line with:

```python
    Labelled by kind because an event that costs a confirmation (`item_removed`, which
    asks the source before retracting) and one that costs a merge look identical on an
    unlabelled series -- and "is this lane doing anything" is the question dashboard 3's
    push panel exists to answer alongside uptime.
```

Directly after that test, add:

```python
def _row(external_id: str) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=SOURCE.id,
        external_id=external_id,
        title_id=None,
        episode_id=None,
        container="mkv",
        video_codec=None,
        audio_codec=None,
        width=None,
        height=None,
        hdr_format=None,
        audio_channels=None,
        file_size_bytes=None,
        runtime_seconds=None,
        added_at=None,
        last_seen_at=T0,
    )


async def test_a_removal_counts_each_held_id_it_asked_about_once(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Named twice, an id is asked about once; not held at all, it is never asked about."""
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many([_row("gone-1"), _row("kept-1")])
    adapter = FakeSourceAdapter(SOURCE)
    adapter.seed(
        SourceItem(external_id="kept-1", name="Movie kept-1", kind=SourceItemKind.MOVIE, year=2021),
        T0,
    )
    applier = _applier(FakeEventPublisher(), media_items=media_items)
    await applier.apply(
        SOURCE,
        adapter,
        SourceEvent(
            kind=SourceEventKind.ITEM_REMOVED,
            external_ids=("gone-1", "gone-1", "kept-1", "never-1"),
        ),
        user_id=uuid.uuid4(),
    )
    checks = _recorded(meter_reader)["usher.sync.retraction.checks"]
    assert sorted(
        (str(labels["source"]), str(labels["path"]), str(labels["outcome"]), value)
        for labels, value in checks
    ) == [("Living Room Emby", "push", "gone", 1.0), ("Living Room Emby", "push", "served", 1.0)]
```

- [ ] **Step 3: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_services_push.py tests/unit/test_telemetry_push.py -p no:randomly`

Expected: every case that builds an applier errors with
`TypeError: PushApplyService.__init__() got an unexpected keyword argument 'media_items'`.

- [ ] **Step 4: Apply removals in the service**

In `src/usher/services/push.py`:
- add `from usher.ports.repository import MediaItemRepository` after the
  `usher.ports.events` import;
- add `from usher.services.retraction import confirmed_gone` after the
  `usher.services.ingest` import.

In `PushOutcome`, replace `ignored: int = 0` with `items_retracted: int = 0`.

In `__init__`, add `media_items: MediaItemRepository,` directly after `*,`, and
add `self._media_items = media_items` directly after `self._commit = commit`.

In `apply`, change `outcome = self._ignore_removal(source, event)` to:

```python
                outcome = await self._apply_removal(source, adapter, event)
```

Replace the whole of `_ignore_removal` with:

```python
    async def _apply_removal(
        self, source: Source, adapter: SourceAdapter, event: SourceEvent
    ) -> PushOutcome:
        """Retract the named rows the source confirms gone, and a gone series' episodes.

        Emby sends `ItemsRemoved` during an ordinary library refresh for items that have
        not gone anywhere, so only an available row the event names is asked about, and
        only one the source no longer serves is retracted. A named series confirmed gone
        brings its available episodes into the same removal, each asked about once. No
        ceiling and no `max_items_per_event` deferral: the source is asked a hundred ids
        a request. Ids Usher holds no row for -- seasons, folders, libraries -- change
        nothing here; the next full walk's sweep retracts what they held.

        The guard is `asked_at`, taken before anything is read, so a row a read has
        stamped since -- the item came back -- is spared. A failed confirmation raises,
        as any failed read in the lane does.
        """
        asked_at = datetime.now(UTC)
        named = frozenset(event.external_ids)
        held = await self._media_items.available_among(source.id, named)
        gone = await self._confirm(source, adapter, held)
        episodes = await self._media_items.available_episodes_of(source.id, gone) - held
        gone |= await self._confirm(source, adapter, episodes)
        held |= episodes
        retraction = await self._media_items.retract(source.id, gone, unseen_since=asked_at)
        logger.info(
            "{source} reported {count} items removed: {held} held, {gone} gone, "
            "{retracted} retracted, {served} still served",
            source=source.name,
            count=len(named),
            held=len(held),
            gone=len(gone),
            retracted=retraction.retracted,
            served=len(held) - len(gone),
        )
        if not retraction.retracted:
            return PushOutcome()
        await self._commit()
        # Sorted, so the order a client hears them in does not depend on set iteration.
        for target in sorted(
            retraction.targets, key=lambda one: (str(one.title_id), str(one.episode_id))
        ):
            if target.title_id is not None:
                await self._events.publish(
                    ClientEvent(
                        kind=ClientEventKind.TITLE_UPDATED,
                        title_id=target.title_id,
                        episode_id=target.episode_id,
                        data={"fields": ["availability"]},
                    )
                )
        return PushOutcome(items_retracted=retraction.retracted)

    async def _confirm(
        self, source: Source, adapter: SourceAdapter, external_ids: frozenset[str]
    ) -> frozenset[str]:
        """The ids among `external_ids` the source confirms gone, asked outside a transaction.

        The commit ends the transaction the caller's read opened. Nothing has been written
        yet, so it commits nothing; it keeps the connection from sitting idle in a
        transaction for as long as the source takes to answer.
        """
        if not external_ids:
            return frozenset()
        await self._commit()
        return await confirmed_gone(adapter, external_ids, source=source.name, path="push")
```

`_confirm` commits before it asks: `available_among` and
`available_episodes_of` each open a transaction, nothing has been written when
the question goes out, and the commit keeps the connection from idling in a
transaction while the source answers. The INFO line counts `gone` beside
`retracted`, so a row the `asked_at` guard spared still adds up.

In `src/usher/composition.py`, add `media_items=pipeline.media_items,` to the
`PushApplyService(...)` call, directly after `pipeline.commit,`.

In `src/usher/ports/repository/media_item.py`, add this sentence to the end of
the class docstring's second paragraph (the one Task 6 wrote):
`A push removal asks about the removed rows it holds available, and about a removed series' episodes.`
Directly after `available_among`, add:

```python
    @abstractmethod
    async def available_episodes_of(
        self, source_id: uuid.UUID, external_ids: Collection[str]
    ) -> frozenset[str]:
        """The available episode rows this source holds for the series among `external_ids`.

        A series' own row carries its `title_id` and no `episode_id`, and each episode's
        row carries that `title_id` beside its own `episode_id`. So a named episode, a
        film and a row matched to nothing name no episodes. A named row need not be
        available. An empty input asks nothing.
        """
```

In `src/usher/db/repositories/media_item.py`, add this statement after
`_AVAILABLE_AMONG`:

```python
# A series' own row is the one with no `episode_id`; its episodes share its `title_id`.
_AVAILABLE_EPISODES_OF = """
SELECT episode.external_id FROM media_items AS series
JOIN media_items AS episode
  ON episode.source_id = series.source_id AND episode.title_id = series.title_id
WHERE series.source_id = :source_id
  AND series.external_id = ANY(:external_ids)
  AND series.episode_id IS NULL
  AND episode.episode_id IS NOT NULL
  AND episode.available
"""
```

and this method after `available_among`:

```python
    async def available_episodes_of(
        self, source_id: uuid.UUID, external_ids: Collection[str]
    ) -> frozenset[str]:
        if not external_ids:
            return frozenset()
        with self._session.no_autoflush:
            rows = (
                (
                    await self._session.execute(
                        text(_AVAILABLE_EPISODES_OF),
                        {"source_id": source_id, "external_ids": list(set(external_ids))},
                    )
                )
                .scalars()
                .all()
            )
        return frozenset(rows)
```

In `tests/fakes/media_item_repository.py`, after `available_among`, add:

```python
    async def available_episodes_of(
        self, source_id: uuid.UUID, external_ids: Collection[str]
    ) -> frozenset[str]:
        series = {
            entry.title_id
            for external_id in set(external_ids)
            if (entry := self._items.get((source_id, external_id))) is not None
            and entry.title_id is not None
            and entry.episode_id is None
        }
        return frozenset(
            entry.external_id
            for entry in self._items.values()
            if entry.source_id == source_id
            and entry.available
            and entry.episode_id is not None
            and entry.title_id in series
        )
```

In `tests/unit/test_ports_repository_ingest.py`, add `"available_episodes_of"`
to the surface set after `"available_among"`, under the comment
`# The push lane's expansion from a removed series to its episodes.`

In `tests/contract/media_item_repository_contract.py`, add before
`test_retract_changes_only_available_rows_unseen_since_the_instant`:

```python
    async def test_available_episodes_of_names_a_series_available_episodes_on_this_source(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        other_source_id: uuid.UUID,
        title_id: uuid.UUID,
        series_title_id: uuid.UUID,
        episode_ids: list[uuid.UUID],
    ) -> None:
        """A film, an episode and an unmatched row name no episodes; a series names its own."""
        series, film = series_title_id, title_id
        await repository.upsert_many(
            [
                item(source_id, "series", title_id=series),
                item(source_id, "ep-0", title_id=series, episode_id=episode_ids[0]),
                item(source_id, "ep-1", title_id=series, episode_id=episode_ids[1]),
                item(
                    source_id,
                    "ep-retracted",
                    title_id=series,
                    episode_id=episode_ids[2],
                    last_seen_at=EARLIER,
                ),
                item(other_source_id, "their-ep", title_id=series, episode_id=episode_ids[3]),
                item(source_id, "film", title_id=film),
                item(source_id, "unmatched"),
            ]
        )
        premise = await repository.retract(source_id, ["ep-retracted"], unseen_since=RUN_AT)
        assert premise.retracted == 1
        assert await repository.available_episodes_of(
            source_id, ["series", "series", "film", "unmatched", "never-stored"]
        ) == {"ep-0", "ep-1"}
        assert await repository.available_episodes_of(source_id, ["ep-0"]) == frozenset()
        assert await repository.available_episodes_of(other_source_id, ["series"]) == frozenset()

    async def test_available_episodes_of_reads_a_retracted_series_row(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        series_title_id: uuid.UUID,
        episode_ids: list[uuid.UUID],
    ) -> None:
        """The named row need not be available: its availability is not what is asked."""
        await repository.upsert_many(
            [
                item(source_id, "series", title_id=series_title_id, last_seen_at=EARLIER),
                item(source_id, "ep-0", title_id=series_title_id, episode_id=episode_ids[0]),
            ]
        )
        premise = await repository.retract(source_id, ["series"], unseen_since=RUN_AT)
        assert premise.retracted == 1
        assert await repository.available_episodes_of(source_id, ["series"]) == {"ep-0"}

    async def test_available_episodes_of_nothing_is_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        await repository.upsert_many([item(source_id, "held")])
        assert await repository.available_episodes_of(source_id, []) == frozenset()
```

In `tests/integration/test_media_item_repository.py`, add before
`_seed_a_series_with_episodes`:

```python
async def test_available_episodes_of_costs_one_statement_and_none_for_nothing(
    repository: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    series_title_id: uuid.UUID,
    episode_ids: list[uuid.UUID],
    statement_counter: list[str],
) -> None:
    """One join, however many series are named; never one read per series."""
    await repository.upsert_many(
        [item(source_id, "series", title_id=series_title_id)]
        + [
            item(source_id, f"ep-{index}", title_id=series_title_id, episode_id=episode_id)
            for index, episode_id in enumerate(episode_ids)
        ]
    )
    statement_counter.clear()
    episodes = await repository.available_episodes_of(source_id, ["series", "never-stored"])
    statements = len(statement_counter)
    statement_counter.clear()
    await repository.available_episodes_of(source_id, [])
    nothing_statements = len(statement_counter)
    assert episodes == {f"ep-{index}" for index in range(len(episode_ids))}, (
        "the premise: every episode was named"
    )
    assert (statements, nothing_statements) == (1, 0)
```

- [ ] **Step 5: Update the other construction sites**

- In `tests/integration/test_push_lane_end_to_end.py`'s `applier` fixture, add
  `media_items=media_items,` after the final `session.flush,`.
- In `tests/integration/test_services_push.py`'s `applier` fixture, add
  `media_items=media_items,` after the final `session.flush,`, below its comment.
- In `tests/unit/test_services_watch_sync.py`, line 1172, add
  `media_items=fixture.media_items,` after `commit=fixture._commit,`.

- [ ] **Step 6: Flip the integration removal case, and add its served twin and the series case**

In `tests/integration/test_services_push.py`, replace
`test_a_removal_leaves_every_row_available` with:

```python
async def test_a_removal_the_source_confirms_retracts_the_row(
    session: AsyncSession,
    applier: PushApplyService,
    events: FakeEventPublisher,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """Against the table that holds the flag: the source no longer serves movie-1."""
    title_id = await _given_matched_movie(session, source, "movie-1")
    outcome = await applier.apply(
        source,
        FakeSourceAdapter(source),
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("movie-1",)),
        user_id=user_id,
    )
    assert outcome.items_retracted == 1
    stored = await PostgresMediaItemRepository(session).get_by_external_id(source.id, "movie-1")
    assert stored is not None and stored.available is False
    assert [(event.kind, event.title_id) for event in events.published] == [
        (ClientEventKind.TITLE_UPDATED, title_id)
    ]


async def test_a_removal_the_source_still_serves_leaves_the_row_available(
    session: AsyncSession,
    applier: PushApplyService,
    events: FakeEventPublisher,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """What an Emby library refresh sends: `ItemsRemoved` for a file that is still there."""
    await _given_matched_movie(session, source, "movie-1")
    adapter = FakeSourceAdapter(source)
    adapter.seed(
        SourceItem(
            external_id="movie-1",
            name="Film movie-1",
            kind=SourceItemKind.MOVIE,
            year=2021,
            container="mkv",
        ),
        SEEN_AT,
    )
    outcome = await applier.apply(
        source,
        adapter,
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("movie-1",)),
        user_id=user_id,
    )
    assert outcome.items_retracted == 0
    stored = await PostgresMediaItemRepository(session).get_by_external_id(source.id, "movie-1")
    assert stored is not None and stored.available is True
    assert events.published == []
```

Append the series case, which repeats the unit series case against real
episodes and the real join:

```python
async def _given_a_series_with_episodes(
    session: AsyncSession, source: Source, episodes: int
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """`series-1`, one season, and `ep-0` onward, each with its `media_items` row."""
    series = Title(kind=TitleKind.SERIES, name="Pushed Series", sort_name="Pushed Series")
    await PostgresTitleRepository(session).add(series)
    season, episode_ids = new_id(), [new_id() for _ in range(episodes)]
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": season, "title_id": series.id},
    )
    for number, episode_id in enumerate(episode_ids, start=1):
        await session.execute(
            text(
                "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
                "VALUES (:id, :title_id, :season_id, 1, :number)"
            ),
            {"id": episode_id, "title_id": series.id, "season_id": season, "number": number},
        )
    rows = [("series-1", None)] + [(f"ep-{index}", one) for index, one in enumerate(episode_ids)]
    await PostgresMediaItemRepository(session).upsert_many(
        [
            MediaItemUpsert(
                source_id=source.id,
                external_id=external_id,
                title_id=series.id,
                episode_id=episode_id,
                container=None if episode_id is None else "mkv",
                video_codec=None,
                audio_codec=None,
                width=None,
                height=None,
                hdr_format=None,
                audio_channels=None,
                file_size_bytes=None,
                runtime_seconds=None,
                added_at=None,
                last_seen_at=SEEN_AT,
            )
            for external_id, episode_id in rows
        ]
    )
    return series.id, episode_ids


async def test_a_removed_series_retracts_its_episodes_the_source_confirms_gone(
    session: AsyncSession,
    applier: PushApplyService,
    events: FakeEventPublisher,
    source: Source,
    user_id: uuid.UUID,
) -> None:
    """Emby names the series alone; `ep-0` and `ep-1` go with it, and the served `ep-2` stays."""
    series, episode_ids = await _given_a_series_with_episodes(session, source, episodes=3)
    adapter = FakeSourceAdapter(source)
    adapter.seed(
        SourceItem(external_id="ep-2", name="Episode 3", kind=SourceItemKind.EPISODE),
        SEEN_AT,
    )
    outcome = await applier.apply(
        source,
        adapter,
        SourceEvent(kind=SourceEventKind.ITEM_REMOVED, external_ids=("series-1",)),
        user_id=user_id,
    )
    assert adapter.confirm_calls == [frozenset({"series-1"}), frozenset({"ep-0", "ep-1", "ep-2"})]
    assert outcome.items_retracted == 3
    repository = PostgresMediaItemRepository(session)
    for external_id, available in (
        ("series-1", False),
        ("ep-0", False),
        ("ep-1", False),
        ("ep-2", True),
    ):
        stored = await repository.get_by_external_id(source.id, external_id)
        assert stored is not None and stored.available is available, external_id
    assert {(event.title_id, event.episode_id) for event in events.published} == {
        (series, None),
        (series, episode_ids[0]),
        (series, episode_ids[1]),
    }
```

The file now holds six cases, so its two descriptions of them change. Replace
the module docstring with:

```python
"""`PushApplyService` against real Postgres, where three cases need what no port fake can express.

The three removal cases repeat unit cases against the table that holds the `available` flag.
"""
```

and the comment above `from tests.fakes.job_queue import FakeJobQueue` with:

```python
# The queue is a fake here deliberately: `PostgresJobQueue` has its own
# contract run against real Postgres, and no case reads what is queued. Each
# is about what the real tables do: the `watch_states` merge and its trigger,
# the foreign keys a pushed item's rows must satisfy, or the `available` flag
# a removal may clear.
```

- [ ] **Step 7: Run them to see them pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_services_push.py tests/unit/test_telemetry_push.py tests/unit/test_services_watch_sync.py -p no:randomly`

Expected: PASS. Then run
`PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_media_item_repository_contract.py tests/unit/test_ports_repository_ingest.py tests/integration/test_media_item_repository.py tests/integration/test_services_push.py tests/integration/test_push_lane_end_to_end.py -p no:randomly`.
Expected: PASS (Docker).

- [ ] **Step 8: Plant seven guards**

Back up first: `cp src/usher/services/push.py /var/tmp/confirmed-retraction-plant.bak`.
For each plant, run Step 7's unit command and confirm the named case fails on
an `E ` line. Then restore the file and read the restore back with `diff`.

1. Pass `named` to the first `_confirm` in place of `held`. Expected:
   `test_a_removal_of_ids_usher_does_not_hold_asks_nothing` fails.
2. Move `_apply_removal`'s own `await self._commit()` below the publish loop. Expected:
   `test_a_confirmed_removal_retracts_and_publishes_after_its_commit` fails, on
   `commits_at_publish`.
3. Pass `unseen_since=datetime.now(UTC)` to `retract` in place of `asked_at`.
   Expected: `test_a_row_read_since_the_removal_arrived_survives_a_gone_answer`
   fails.
4. Iterate `retraction.targets` directly in place of `sorted(...)`. Expected:
   `test_a_removal_publishes_in_sorted_order_whatever_order_its_targets_iterate`
   fails on its last assertion.
5. Pass `source="push"` to `confirmed_gone` in place of `source.name`. Expected:
   `test_a_removal_counts_each_held_id_it_asked_about_once` fails.
6. Replace the `available_episodes_of` line with
   `episodes: frozenset[str] = frozenset()`. Expected:
   `test_a_removed_series_retracts_its_episodes_asking_about_each_once` fails on
   `confirm_calls`, its second question missing.
7. Delete `await self._commit()` from `_confirm`. Expected:
   `test_a_removal_ends_each_reads_transaction_before_asking_the_source` fails
   on `assert [0, 0] == [1, 2]`.

- [ ] **Step 9: Update the PRD, the guide, the rules and the changelog**

`docs/prd/03-sources-and-sync.md`:

1. In the push events table's `LibraryChanged` row, change `A removal
   **retracts nothing**` to `A removal retracts only what the source confirms
   gone`.
2. In the lanes table, change the **Push** row's last cell to:
   `Apply it inline when it is small; defer to a delta when it is large. A removal is always applied inline, asking the source a hundred items a request. A `WATCH_STATE_CHANGED` carrying its own payload merges with no request at all; one naming more than `push_max_items_per_event` items with no payload becomes a delta walk`
3. In the paragraph that begins `**An unfinished whole-library walk is resumed
   in place.**`, change `stays available until the next full walk, which
   starts afresh.` to `stays available until the next full walk, which starts
   afresh, unless a push removal confirms it gone first.`
4. Replace the paragraph `**A push `ITEM_REMOVED` retracts nothing.** The event
   is counted and logged; the row stays available until a walk sweeps it.` with:

   ```markdown
   **A push `ITEM_REMOVED` retracts only what the source confirms gone.** Emby
   sends `ItemsRemoved` during an ordinary library refresh for items that are
   still there. So the lane asks the source about each removed item it holds
   available, and retracts those the source no longer serves. When one of those
   is a series, the lane also asks about the series' available episodes, and
   retracts in the same removal those the source no longer serves. It then
   publishes one `title.updated` for each title or episode it retracted. A
   confirmed removal is applied at once, whatever its size: there is no
   retraction ceiling and no `push_max_items_per_event` deferral. Items Usher
   does not hold are not asked about, so a removed season, folder or library
   changes nothing here: the next full walk's sweep retracts what it held. If
   the source cannot answer, the event fails like any failed read in the lane.
   The gap-closing delta after the reconnect does not retract the items; the
   next full walk's sweep does.
   ```
5. At the end of the paragraph that begins `**Retraction is a separate step**`,
   after `the next full walk resumes it and only sweeps.`, add
   `On either path, an Emby listing that names only items Usher did not ask about counts as no answer.`

`docs/prd/08-operations.md`: in the last cell of the
`| A source cannot confirm a removal |` row Task 5 added, end
`The catalog is unaffected` with a period, then append this sentence, with no
period of its own, as no cell in that table ends with one:
` A push removal the source cannot confirm fails its event like any failed read in the lane and changes nothing; the gap-closing delta after the reconnect never retracts, so the removal waits for the next full walk's sweep`

`docs/guide/command-line.md`: directly after the `- **The retraction guard**`
bullet, add:

```markdown
- **Removed films, series and episodes reach Usher as they happen.** When the
  server reports items removed, Usher asks it about each one it holds, and
  marks unavailable those it confirms gone, however many there are. A removed
  series takes its episodes with it, once the server confirms each gone. A
  library refresh also reports removals for files that are still there; those
  are left alone. A removed season, folder or library is caught by the next
  full sync instead.
```

`.claude/rules/emby-push-and-ingest.md`: replace the bullet that begins
`- **`ItemsRemoved` fires on a library from which nothing was removed**` (two
lines) with:

```markdown
- **`ItemsRemoved` fires on a library from which nothing was removed**, so
  retract only what `confirm_gone` returns, or one refresh marks a present file
  unavailable. **One `Ids` listing names one version of a version group**
  (`ids_version_group.json`), which is why `confirm_gone` asks in rounds.
```

`tests/fixtures/emby/README.md`: in the `LibraryChanged.Data` row, change `why a
removal event retracts nothing, observed` to `why a removal is confirmed before
anything is retracted, observed`.

`CHANGELOG.md`, under `## [Unreleased]`, add as the first bullet of `### Changed`:

```markdown
- **A push removal now retracts what the server confirms gone.** When Emby
  reports items removed, Usher asks it about each one it holds, and marks
  unavailable those it no longer serves, publishing `title.updated` for each
  title or episode. A removed series' episodes are asked about and retracted
  with it. `USHER_SYNC_MAX_RETRACT_FRACTION` does not limit it: a removal the
  server confirms is applied however many items it names. Before, a removal
  changed nothing until a full walk. Items Emby still serves, which a library
  refresh also reports removed, are left alone. A removed season, folder or
  library still waits for the next full walk's sweep.
```

- [ ] **Step 10: Run the gate, then commit**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_docs_pointers.py -p no:randomly`.
Expected: PASS. Then run the full gate. Expected: green.

```bash
git add src/usher/services/push.py src/usher/composition.py src/usher/ports/repository/media_item.py src/usher/db/repositories/media_item.py tests/fakes/media_item_repository.py tests/contract/media_item_repository_contract.py tests/integration/test_media_item_repository.py tests/unit/test_ports_repository_ingest.py tests/unit/test_services_push.py tests/unit/test_telemetry_push.py tests/unit/test_services_watch_sync.py tests/integration/test_services_push.py tests/integration/test_push_lane_end_to_end.py docs/prd/03-sources-and-sync.md docs/prd/08-operations.md docs/guide/command-line.md CHANGELOG.md .claude/rules/emby-push-and-ingest.md tests/fixtures/emby/README.md
git commit -m "push: a removal retracts what the source confirms gone"
```

---

### Task 8: dashboard 3 draws the retraction checks

**Files:**
- Modify: `dashboards/03-pipeline.json`, which gains panel 12; its description
  changes from `Eleven panels` to `Twelve panels`
- Modify: `dashboards/README.md`: the count (line 789), and a section 12 placed
  before the heading `## ⚠️ Three Dashboard 3 panels plot a mean` (line 1253)
- Modify: `docs/prd/10-telemetry-and-dashboards.md`, Dashboard 3 (lines 341–354)
- Modify: `CHANGELOG.md`
- Test: `tests/unit/test_dashboards.py` and `tests/unit/test_alerts.py`

**Interfaces:**
- Consumes: `usher.sync.retraction.checks`, exported as
  `usher_sync_retraction_checks_total` with labels `source`, `path` and
  `outcome` (Task 5), and listed in PRD 10's metric table (Task 5).
- Produces: panel id 12, titled `Retraction checks, by path and outcome`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_dashboards.py`, rename `_assert_dashboard_3_has_eleven_panels`
to `_assert_dashboard_3_has_twelve_panels` at its definition and both call
sites. Its body becomes:

```python
def _assert_dashboard_3_has_twelve_panels(panels: list[dict[str, Any]]) -> None:
    """Dashboard 3's panel count: PRD 10's eleven items, with queue depth drawn twice."""
    titles = [str(panel["title"]) for panel in panels]
    assert len(panels) == 12, (
        "dashboard 3 is twelve panels -- PRD 10's eleven items, with queue depth drawn "
        f"twice -- not {len(panels)}: {titles}"
    )
```

In `test_the_dashboard_3_prose_claims_are_falsifiable`, change the last check to:

```python
    with pytest.raises(AssertionError, match=r"twelve panels .* not 11:"):
        _assert_dashboard_3_has_twelve_panels([{"title": f"panel {n}"} for n in range(11)])
```

Directly after `test_the_walk_panel_draws_the_listing_limit_and_a_mean_unit_duration_by_stage`,
add:

```python
def test_the_retraction_panel_draws_checks_by_path_and_outcome() -> None:
    """The two labels a check carries; summed over source, as panel 9 sums push events."""
    retraction = [
        panel
        for panel in _live_panels(_PIPELINE)
        if str(panel["title"]).startswith("Retraction checks")
    ]
    assert len(retraction) == 1, f"dashboard 3 has {len(retraction)} retraction panels, not one"
    [expr] = _exprs(retraction[0])
    assert _metric_tokens(expr) == {"usher_sync_retraction_checks_total"}, expr
    assert [labels for labels, _operand in _aggregations(expr)] == [{"path", "outcome"}], expr
```

In `tests/unit/test_alerts.py`, in
`test_every_rule_carries_a_window_a_severity_and_a_description_naming_its_series_and_panel`:
- change `assert len(_dashboard_three_panel_titles()) == 11` to `== 12`;
- change `"Dashboard 3 has eleven panels; this scan found "` to
  `"Dashboard 3 has twelve panels; this scan found "`.

- [ ] **Step 2: Run them to see them fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_dashboards.py tests/unit/test_alerts.py -p no:randomly`

Expected: three FAILs:
- `test_the_queue_depth_panels_are_two_panels_over_two_datasources`, with
  "not 11";
- the new retraction panel case, with "0 retraction panels";
- the alerts case, on its count.

- [ ] **Step 3: Add the panel**

Run this one-off from the worktree root. It copies panel 9, which is the push
events panel on the same datasource, so the options match:

```bash
cp dashboards/03-pipeline.json /var/tmp/confirmed-retraction-dashboard.bak
python3 - <<'EOF'
import copy
import json
import pathlib

path = pathlib.Path("dashboards/03-pipeline.json")
dashboard = json.loads(path.read_text(encoding="utf-8"))
[push] = [panel for panel in dashboard["panels"] if panel["id"] == 9]
panel = copy.deepcopy(push)
panel["id"] = 12
panel["title"] = "Retraction checks, by path and outcome"
panel["description"] = (
    "usher.sync.retraction.checks, as usher_sync_retraction_checks_total, labelled source, "
    "path and outcome: every item a full walk's sweep (path sweep) or a push removal (path "
    "push) asked the source about before retracting it. Outcome gone is what the source "
    "confirmed gone; Usher then retracts it unless the row was seen since the sweep's run "
    "began, or since Usher took up the push removal. A sweep resumed after a failed commit "
    "or a cancel asks again and counts again, so gone can exceed what was retracted. "
    "Outcome served is what the source still serves and Usher left available -- on the "
    "sweep path, the rows a sweep that did not ask would have retracted. The cumulative "
    "counter is plotted rather than its rate, so a flat line means nothing was asked of any "
    "source. It restarts from zero when Usher restarts.\n\n**There is no series until a "
    "sweep or a push removal has asked a source something**, so an empty panel on a fresh "
    "deployment means nothing has been asked yet."
)
panel["gridPos"] = {"h": 8, "w": 24, "x": 0, "y": 40}
[target] = panel["targets"]
target["expr"] = "sum by (path, outcome) (usher_sync_retraction_checks_total)"
target["legendFormat"] = "{{path}} · {{outcome}}"
dashboard["panels"].append(panel)
assert dashboard["description"].startswith("Eleven panels")
dashboard["description"] = "Twelve panels" + dashboard["description"][len("Eleven panels") :]
path.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
EOF
git diff --stat dashboards/03-pipeline.json
```

Expected: one file changed, and `git diff` shows only the description's first
two words and the appended panel. If any other line moved, the round trip was
not exact. Restore the file with
`cp /var/tmp/confirmed-retraction-dashboard.bak dashboards/03-pipeline.json`,
then report it rather than committing.

- [ ] **Step 4: Document the panel**

`dashboards/README.md`:
- change `Eleven panels, and the mixed one` (line 789) to `Twelve panels, and the
  mixed one`;
- directly before the heading `## ⚠️ Three Dashboard 3 panels plot a mean, not a
  quantile`, add:

````markdown
### 12 — Retraction checks, by path and outcome

`timeseries`, Prometheus.

```
sum by (path, outcome) (usher_sync_retraction_checks_total)
```

**Not yet observed.** The counter is first written when a full walk's sweep or
a push removal asks a source about an item. Neither has run against a real
source since this panel was committed, so there is no reading to record.

````

`docs/prd/10-telemetry-and-dashboards.md`, under `### 3 — Pipeline`:
- change the end of the item list from `· **whole-library walks: the listing
  limit and the mean unit duration, by stage**.` to `· **whole-library walks:
  the listing limit and the mean unit duration, by stage** · **retraction
  checks, by path and outcome**.`;
- change the bolded backing claim to `✅ **Every panel here is backed by real
  data as of M9, and the whole-library walk panel as of the fast first sync's
  acceptance walk, except the retraction-checks panel, which is unbacked until a
  sweep or a push removal asks a real source.**`. Leave the `⚠️` sentence after
  it unchanged.

`CHANGELOG.md`: append ` Dashboard 3 draws it.` to the end of the
`usher.sync.retraction.checks` bullet Task 5 added under `### Added`.

- [ ] **Step 5: Run them to see them pass, then plant one guard**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_dashboards.py tests/unit/test_alerts.py tests/unit/test_prd_10_panel_backing.py -p no:randomly`

Expected: PASS.

Plant the guard:
1. Back up: `cp dashboards/03-pipeline.json /var/tmp/confirmed-retraction-plant.bak`.
2. Change the new panel's `sum by (path, outcome)` to `sum by (outcome)`.
3. Rerun and confirm `test_the_retraction_panel_draws_checks_by_path_and_outcome`
   fails on an `E ` line.
4. Restore the file and read the restore back with `diff`.

- [ ] **Step 6: Run the gate, then commit**

Run the full gate. Expected: green.

```bash
git add dashboards/03-pipeline.json dashboards/README.md docs/prd/10-telemetry-and-dashboards.md CHANGELOG.md tests/unit/test_dashboards.py tests/unit/test_alerts.py
git commit -m "dashboards: retraction checks on dashboard 3"
```

---

### Task 9 (controller only): the status rows, once the PR is open

Pushing and opening the PR are the owner's call. This task runs only if they
choose it.

**Files:**
- Modify: `docs/plans/progress.md`, the `## Confirmed retraction` table
- Modify: `docs/prd/README.md`, this plan's row

- [ ] **Step 1: Flip the rows**

Change every `📋 planned` in the `## Confirmed retraction` table to `🔨 in review
(PR #N)`, except row 10, which stays `📋 planned` until its acceptance runs. Use
the PR's real number. Make the same change in this plan's row of
`docs/prd/README.md`.

- [ ] **Step 2: Run the currency test, then commit and push**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/unit/test_docs_currency.py -p no:randomly`

Expected: PASS.

```bash
git add docs/plans/progress.md docs/prd/README.md
git commit -m "docs(plan): confirmed retraction in review"
git push -u origin feat/confirmed-retraction
```

---

### Task 10 (controller only, owner-gated): live acceptance

Every step needs the owner's go. The merge is the owner's, and it deploys
`usher-prod`.

- [ ] **Step 1: Before the merge, confirm no walk is running**

A deploy restarts the container and kills any job inside it. Run this read-only
check, and tell the owner if it lists anything:

```bash
docker ps --filter name=usher-prod-sync --format '{{.Names}} {{.Status}}'
```

- [ ] **Step 2: Snapshot what is available, then run the first full walk**

Snapshot first:
1. Write a throwaway read-only script under `/var/tmp/sync-speed/` that saves
   the source's available `external_id`s to a file. It must print no
   credential, token, user id or host.
2. Run it. If the permission classifier refuses a read of production, ask the
   owner. Do not find another route.

On the owner's go, start the walk from `~/code/usher-deploy`:
1. Move the old `data/bulk/sync.log` aside.
2. Run:

```bash
docker compose run -d --rm --no-deps --name usher-prod-sync usher sh -c 'usher sync --source "Shared Emby" > /data/bulk/sync.log 2>&1; echo "exit=$?" >> /data/bulk/sync.log'
```

3. Wait on it with one background until-loop on `^exit=` in the log, not a
   foreground sleep.

- [ ] **Step 3: Check every retraction was real**

Collect:
- the sweep's line (`grep "sweep found" data/bulk/sync.log`);
- the run's `items_retracted`;
- `usher_sync_retraction_checks_total` from Prometheus.

The rows the walk retracted are the snapshot minus what is still available
afterwards. Ask Emby for each by id with the same throwaway script. **Every one
must be absent**: a single served id is a false retraction, and the slice has
failed.

Expect some `served` checks, because the alternate versions the old sweep
retracted on every full walk are now stamped instead.

- [ ] **Step 4: A day of push traffic**

After a day, collect two things:
- the `items removed` lines from `docker logs usher-prod-usher-1 2>&1`;
- `usher_sync_retraction_checks_total{path="push"}`.

Every id a push removal retracted must be absent on Emby. Check that with the
same script.

- [ ] **Step 5: Post the raw output**

Post the actual output of Steps 2–4 on the PR: the lines, the numbers and the
script's verdicts. A summary is not enough. Then, in a docs PR of their own,
flip every row of the `## Confirmed retraction` table and this plan's PRD
README row to `✅ landed (PR #N)`.
