# Runbook — disaster recovery

**The database is gone. You have `usher-backup-<UTC>.jsonl.gz` and you have
`USHER_SECRET_KEY`.** This file is the whole sequence and its clock;
[`restore.md`](restore.md) is the restore step in detail.

**The headline, and it is the reason the artifact is 434 kB rather than 6 GB:
the restore is seconds and the rebuild is hours, they happen in that order, and
Usher serves in between.** You lose an afternoon of indexing, not a household's
history.

Every number below carries its date and its source. The ones this drill
measured itself are marked ✅ 2026-08-25; the rest are earlier runs already
recorded in `docs/prd/08-operations.md`, `src/usher/db/backup_manifest.py` and
`.claude/rules/`, cited rather than re-derived.

---

## 1. What is lost, and what is not

| not in the artifact — **rebuilt** | in the artifact — **restored** |
|---|---|
| `titles` (1,272,891), `seasons`, `episodes` | `users` — 1 row. Nothing recreates a household |
| `title_embeddings`, `title_neighbors` (3,311,050), `title_search_names` | `sources`, `source_credentials` — a URL, a name, and ciphertext |
| `people`, `credits`, `collections`, `images` | `watch_states` — **3,347 rows.** The load-bearing one |
| `raw_payloads` (132,462), `tmdb_ids`, `id_crosswalk` | `llm_calls` — the spend ledger. No endpoint sells it back |
| `genome_scores`, `genome_tags` | `row_provider_settings` — your enable/disable choices |
| `curated_rows`, `user_taste`, `jobs`, `sync_runs`, `import_runs` | `search_queries` — 89 rows: what was typed, and what was played |
| `media_items`, **except its two link columns** | `media_items.title_id` / `.episode_id` — 10,819 links |

**14,259 rows, 443,902 bytes, 1.03 s to write.** ✅ 2026-08-25, counted
read-only against the live catalog.

⚠️ **`curated_rows` is rebuildable but not restorable, and the words differ.**
One `usher curate` puts a shelf back on the screen; no re-run reproduces the
shelf you had, and at `temperature > 0` it is not even deterministic.

⚠️ **`genome_scores` / `genome_tags` are rebuildable only from upstream.** They
are deliberately not carried, because a backup holding 15,565 MovieLens vectors
is a redistribution of MovieLens data. If GroupLens withdraws `ml-latest.zip`,
they are not rebuildable at all — an accepted risk, recorded in
`backup_manifest.py`.

---

## 2. The honest clock

| step | cost | measured |
|---|---|---|
| provision Postgres, `alembic upgrade head` | **~4 s** into an empty database | ✅ 2026-08-25 |
| `usher bootstrap --phase imdb` | **114.9 s**, 12.7 M lines → 1,272,367 titles | 2026-08-11 (M9 T8) |
| `usher bootstrap --phase credit-names` | **826.1 s** (13.8 min) | 2026-08-11 |
| `usher bootstrap --phase aliases` | **520.1 s** (8.7 min), 1,663,455 aliases | 2026-08-11 |
| `--phase tmdb-ids` + `--phase crosswalk` + `--phase movielens` | the remainder of a **2 min 59 s** end-to-end `--phase all` measured before the two expansion phases existed | 2026-07-31 |
| ⇒ **`usher bootstrap --phase all`** | **≈ 25 min**, summed from the phase timings above | ⚠️ **no single run of the current phase list has been timed end to end** |
| `usher restore <artifact>` | **5.06 s** for 3,440 rows written; **13.46 s** when the 10,515 links land too | ✅ 2026-08-25 |
| `usher sync` — the source walk | per-request, not per-run: `verify` 0.1253 s, `get_item` 0.1495 s, a 200-item page **5.0954 s median** | 2026-08-15 (M10 S1) |
| `usher work` — TMDb enrichment, which refills `raw_payloads` | **130,334 requests over 1.98 h** | 2026-08-12 (M9 S3) |
| `usher derive --backfill` — people, credits, collections, images | no network call **once `raw_payloads` is back**; it derives from the cache | — |
| `usher index --backfill` then `usher work` | **105.9 min** for 130,720 titles | 2026-08-13 |
| `usher similar --rebuild` | **3.33 h** — 130,720 seeds, 3,268,000 rows, 11,981 s at 91.7 ms/seed | 2026-08-13 |

🔴 **The neighbour walk is 3.33 h and not 21.6 h.** 21.6 h is `m09e`'s figure,
taken while 1024-lane `halfvec` columns had crossed `TOAST_TUPLE_THRESHOLD` and
every exact scan cost 594.7 ms/seed. `m09f` moved every `halfvec` column to
`PLAIN` storage and took that back to 91.7 ms/seed. The *conclusion* survived
the correction — plan it as an overnight job, not a follow-on step — and the
number that motivated it did not. (`src/usher/db/backup_manifest.py`'s
`title_neighbors` entry carries the storage class.)

⚠️ **A complete walk of this library has never once been run**, so the `sync`
row above is a per-request cost and not a total. `.claude/rules/emby-push-and-ingest.md`
holds what Phase 1 did and did not establish about that server.

**End to end: on the order of eight hours of unattended machine time**, of
which about 25 minutes is before Usher can serve anything and **13 seconds is
the part that carries anything you cannot recreate.**

---

## 3. The sequence

**Restore → serve → backfill.** Not rebuild-everything-then-restore: the point
of the order is that Usher is up and useful long before the vector work
finishes.

```bash
# 0. a database at the artifact's revision, and nothing else
#    ⚠️ export USHER_DATABASE_URL explicitly. `.env` names your live database
#    and `alembic/env.py` reads it — see restore.md §0.
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@127.0.0.1:5432/usher"
export USHER_SECRET_KEY="<the key that was stored with the artifact>"
uv run alembic upgrade head
uv run usher restore /var/tmp/nightly.jsonl.gz --dry-run     # expect: refused, every title missing

# 1. the catalog, ~25 min
uv run usher bootstrap --phase all

# 2. the precious rows, seconds. Brings back the household, the source and
#    its credential, the history, the ledger and the search log.
#    ⚠️ --skip-unresolvable, because on this deployment the artifact is refused
#    whole without it — see §5. --dry-run first, and read WHICH tables the
#    skipped rows are in before committing.
uv run usher restore /var/tmp/nightly.jsonl.gz --dry-run --skip-unresolvable
uv run usher restore /var/tmp/nightly.jsonl.gz --skip-unresolvable

# 3. SERVE. Everything below this line is a background job.
uv run uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000

# 4. the source walk — needs the `sources` row step 2 restored
uv run usher sync --kind full
uv run usher work

# 5. the operator's link decisions, onto the rows step 4 recreated
uv run usher restore /var/tmp/nightly.jsonl.gz --skip-unresolvable

# 6. the derived read surface, from the payload cache step 4 refilled
uv run usher derive --backfill

# 7. search, in the order the surfaces come back
uv run usher index --backfill && uv run usher work      # ~105.9 min
uv run usher similar --rebuild                          # ~3.33 h — overnight
```

**Step 5 is not a typo.** `media_items` is the manifest's one `PARTIAL` entry:
restore writes its two link columns onto rows that must already exist, and those
rows come from the walk in step 4 — which needs the `sources` row that step 2
restored. The first restore therefore reports every link under **nothing to
write onto** and the second one writes them. ✅ Measured 2026-08-25: **0 written
/ 10,515 with nothing to write onto**, then **10,515 written**. Restoring the
same artifact twice is a no-op on everything else, by construction.

⚠️ **That column was headed *already present* when the drill ran**, against a
`media_items` table holding **zero** rows — the same number under the opposite
instruction. The two states were split into separate columns on 2026-08-25
([`restore.md`](restore.md) §8). The counts above are the drill's; the heading
is the current one, and **`skipped` now means something else entirely** — see
the flag below.

⚠️ **`--skip-unresolvable` is on both restore steps because this deployment's
artifact is refused without it**, not as a precaution. §5 has the measurement
and the judgement it asks of you.

---

## 4. Search is degraded, not absent

This is what step 3 buys, and it is worth knowing which surface returns when.

| surface | works after | why |
|---|---|---|
| `GET /titles/{id}`, browse, the series hierarchy | **step 1** | plain reads over `titles`/`seasons`/`episodes` |
| **keyword search** (the lexical lane) | **step 1** | `titles.search_document` is `GENERATED ALWAYS AS (…) STORED`, so the bootstrap's own writes build it and the GIN index comes with the migration. No job, no backfill |
| watch state, resume points, played/unplayed | **step 2** | restored, not rebuilt |
| the home screen's history-driven rows | **step 2** | they read `watch_states` |
| type-ahead / `usher suggest` | **step 1** for the alias half (`bootstrap --phase aliases`), **step 6** for the person half | `title_search_names` has two writers |
| artwork, cast and crew, collections | **step 6**, which needs step 4's payload cache | `derive --backfill` makes no network call — but only because `raw_payloads` is back |
| **semantic search** (the vector lane) | **step 7** | `usher search` prints `semantic_coverage`; before the backfill it reads 0 and the hybrid answer is the lexical lane alone |
| **"more like this"** | **after `similar --rebuild`** | `title_neighbors` is empty until then, so the row is empty rather than wrong |
| curated shelves | one `usher curate` | a shelf appears; it is not the shelf you had |

**Nothing runs `usher similar --rebuild` for you**, and that is the standing
freshness gap in this project rather than something specific to recovery: a
title's neighbours go stale when some *other* title gets an embedding, which no
per-row predicate can decide. It is an operator's command or a cron entry after
`usher index --backfill`.

---

## 5. Two things that stop the restore dead

### The schema stamp

`usher restore` compares the artifact's header against **this database's**
`alembic_version` and refuses a mismatch, naming both values. It never compares
against the code's head — a container whose code is ahead of its database is a
different problem, and `/health/ready` is what reports that one.

So **`alembic upgrade head` in step 0 must reach the revision the artifact was
written at.** If the artifact is older than this code, upgrade a *scratch*
database to the artifact's revision, restore there, upgrade it, and back it up
again. Restore does not guess across a schema change.

### 🔴 A reference with no provider id refuses the whole file, and the way through is a flag

Full detail in [`restore.md`](restore.md) §5. The short version, because it is
the failure this drill actually hit and it will hit you:

- Every title reference travels as `imdb_id` → `(kind, tmdb_id)` → its raw UUID.
- A title with **neither** provider id — an unmatched stub the ingest ladder
  created — has only the raw UUID, which a rebuilt catalog will never mint again.
- ✅ **Measured 2026-08-25: 6 such titles of 1,272,891, holding 602 of the
  artifact's 16,819 carried title references — one in 28.** Restoring the real
  artifact into a correctly rebuilt catalog was **refused whole on 304
  `media_items` rows**, while all 3,347 watch states resolved and were rolled
  back with them.
- The way through is **`--skip-unresolvable`**, which is why §3's two restore
  steps carry it. It drops the rows naming a title or episode this catalog
  cannot resolve, counts them in a bucket of their own — never folded into
  *already present*, never written as a null — and commits everything else.

  ```bash
  uv run usher restore /var/tmp/nightly.jsonl.gz --dry-run --skip-unresolvable
  uv run usher restore /var/tmp/nightly.jsonl.gz --skip-unresolvable
  ```

  ⚠️ **Read the per-table counts on the dry run before you accept it, because
  the flag is you accepting a loss and the two losses are not the same.** If the
  skipped rows are `media_items` only — which is what all 304 are here — you lose
  links the next `usher sync` re-derives to the same answer. **If any are
  `watch_states`, stop:** that is history no importer rebuilds, and nothing in
  the command enforces that judgement. [`restore.md`](restore.md) §5 has the
  table.

  ⚠️ **It does not relax the other three refusals.** A household this database
  does not hold, a source colliding on a name, and a credential whose source is
  absent all still refuse the whole file with the flag set — none of them is
  *"the catalog is at a different bootstrap phase"*, and no `usher sync`
  re-derives any of them.

#### 🔴 The old escape was a `grep` over the artifact. Do not use it.

Every version of this page before 2026-08-26 told you to filter the file:

```bash
zcat nightly.jsonl.gz | grep -v '"imdb_id": null, "tmdb_id": null' | gzip > nightly-filtered.jsonl.gz
```

**That command now fails, and the failure looks like artifact corruption**,
which is the worst thing for it to look like in the middle of a recovery. The
header carries a per-table row count and `usher restore` checks the body against
it — so an artifact with 304 lines removed is refused for being *"truncated or
was edited"*, naming two numbers that do not match. The check landed on
2026-08-25 and the filter predates it.

Two further reasons, both of which were true before the check existed: it is a
substring match on one JSON spelling, so it drops whatever happens to match —
including a `watch_states` row, **silently**, which is the one loss the bullet
above tells you to stop for; and `--skip-unresolvable` does the same job with
the counts printed and `--dry-run` to read them first.

⚠️ **The ✅ this page used to carry here was real and no longer refers to
anything runnable.** K5's live drill did run that filter, on 2026-08-25, and it
dropped exactly 304 lines, all `media_items`, and the result restored clean —
which is how the 304 figure above was established. What that drill measured is
*which rows are the problem*, and that survives. What it measured about *the
filter as a procedure* did not survive the truncation check landing the same
day.

**What has and has not been drilled, stated rather than implied.**
`--skip-unresolvable` is built and covered by an integration case against a real
Postgres (`tests/integration/test_restore.py::test_the_flag_skips_the_unresolvable_rows_and_commits_everything_else`,
plus a mutation plant that turns the flag off and is killed by two cases). **The
sequence in §3 has not been re-run end to end with the flag against the real
1,272,891-title catalog** — the live arm of K5's drill used the filter, because
the flag did not exist yet. Treat §3's *order* as drilled and the flag on its
two restore lines as the sanctioned replacement for a step that no longer works.

---

## 6. Checklist

- [ ] The artifact **and** the `USHER_SECRET_KEY` it was taken under are both in
      hand. Without the key you restore six of seven precious tables and
      re-enter every source credential by hand.
- [ ] `USHER_DATABASE_URL` is exported and names the **new** database. Do not
      rely on `.env`.
- [ ] `alembic upgrade head`, then `SELECT version_num FROM alembic_version`
      read back **from the database** and compared against
      `zcat artifact | head -1`'s `schema_revision`.
- [ ] `usher restore --dry-run` before every real run. It prints the identical
      report and commits nothing.
- [ ] `--skip-unresolvable` on both restore steps, **and the dry run's
      per-table counts read before committing**. `media_items` only: take it.
      Any `watch_states`: stop, that is history nothing rebuilds.
- [ ] The artifact is **the file `usher backup` wrote**, unedited. Do not filter
      it — the header's row counts are checked against the body, so a `grep`ped
      or `head`ed file is refused as truncated.
- [ ] Do **not** re-add the source through the admin route first — the artifact
      carries it, and a hand-added source with the same name refuses the file.
- [ ] Restore **twice**, with `usher sync` in between, or the 10,819 link
      decisions never land.
- [ ] `usher similar --rebuild` is scheduled. Nothing runs it for you.
