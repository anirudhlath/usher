# Runbook — restoring a backup

**Audience:** the operator of a running Usher deployment.
**Written from a drill run on 2026-08-25**, not from the design: every command
below was executed and every block quoted is real output. The synthetic arm ran
against a scratch `pgvector/pgvector:pg17`; the real arm read a live 1,272,891-title
catalog. The transcript is `/var/tmp/m10-K5/`. Identifiers in the examples are
from this repository's reserved `tt99` synthetic band — a real one has never been
committed to this repository and must not be.

Companion: [`disaster-recovery.md`](disaster-recovery.md), which is the *whole*
sequence and its clock. This file is the restore step in detail.

---

## 0. Three things to know before you type anything

### The artifact holds no key, and it says so on every run

`source_credentials.ciphertext` travels as the ciphertext it is stored as.
`usher backup` never calls `build_cipher` and holds no key, so **an artifact
restored into a deployment with a different `USHER_SECRET_KEY` restores
credentials nothing can decrypt.** The command prints the sentence every time:

```
source credentials travel as ciphertext and this file holds no key: keep
USHER_SECRET_KEY with it, or the restored credentials will be undecryptable and
every source will ask to be re-entered
```

The degradation is diagnosable rather than silent — Fernet's authentication tag
makes a wrong key an `InvalidToken`, which surfaces as `PortDataMalformed` and
renders on `GET /admin/sources/{id}/status` as *re-enter your credentials* — but
that is a recovery you have to perform by hand, per source. **Store the key with
the artifact, or you are restoring six of seven precious tables.**

### 🔴 `.env` points at your real database, and `alembic` reads it

Every checkout of this project carries a `.env` whose `USHER_DATABASE_URL` names
the deployment's own database, and `alembic/env.py` reads `get_settings()` — so
`uv run alembic upgrade head` typed in a worktree targets production-shared state
by default, with no flag and no prompt. That has already taken this deployment
down for ~3.5 hours once (`.claude/rules/db-and-sql.md`, 2026-08-19). **Every
command in this runbook that touches a database other than the live one exports
an overriding `USHER_DATABASE_URL` first, and the drill asserted the resolved
port before proceeding.**

### Restore is not "load a database"

`watch_states.title_id` is `ON DELETE RESTRICT` (ADR-0010), so **into an empty
catalog every row of the one table PRD 08 calls load-bearing fails its foreign
key.** Measured: the real artifact restored into a freshly migrated, empty
database refuses **14,166 of 14,259 rows** and commits nothing.

```
14,166 rows could not be restored, so nothing was: enrich or import what the
lines above name and run it again
```

The order is **rebuild the catalog first, then restore on top of it.**

---

## 1. Take the backup

```console
$ uv run usher backup --output /var/tmp/nightly.jsonl.gz
  users                           1 row
  sources                         1 row
  source_credentials              1 row
  watch_states                3,347 rows
  llm_calls                       0 rows
  row_provider_settings           1 row
  search_queries                 89 rows
  media_items                10,819 rows
wrote 14,259 rows from 8 tables to /var/tmp/nightly.jsonl.gz (443,902 bytes), schema m10a
```

**1.03 s and 434 kB** on the deployment this project measures, 2026-08-25. It
reads and never writes, so it is safe against a live production database — the
drill ran it against one and the before/after snapshot was identical on all
sixteen counted values.

Two things worth checking on the file itself, both one line:

```console
$ zcat /var/tmp/nightly.jsonl.gz | head -1 | python3 -m json.tool
{
    "manifest_version": 1,
    "usher_version": "0.1.0",
    "schema_revision": "m10a",
    "generated_at": "2026-08-25T20:06:28.037279+00:00",
    "rows": { "users": 1, "sources": 1, ..., "media_items": 10819 }
}
```

⚠️ **`rows` is provenance, not a gate.** `usher restore` reads exactly one key
out of this header — `schema_revision` — and never compares `rows` against the
body. A short artifact restores without complaint; the drill proved it by
restoring a file whose header claimed 10,819 `media_items` over a body holding
10,515, with 0 refusals and exit 0. If you want a truncation check, `zcat … |
wc -l` against the header's own total is it (the file is header + one line per
row).

---

## 2. Rebuild the catalog

Nothing here is optional and nothing here comes out of the artifact — the
artifact deliberately carries none of it, because all of it is reproducible.

```bash
uv run usher bootstrap --phase all
uv run usher sync --kind full        # needs the source, which the restore brings — see §3
uv run usher work --once
```

Timings and the reason `sync` is listed *after* the restore are in
[`disaster-recovery.md`](disaster-recovery.md).

**Do not re-add your source through the admin route first.** `sources` is a
precious table and the artifact carries it; `_merge_sources` refuses an artifact
row whose *name* a **different** source id already holds, because two sources
pointing at one server is a state an operator has to resolve rather than one a
restore may create. A hand-added source therefore refuses the whole file:

```
  refused sources    name=<your source>, id=<the artifact's> -- a different source
  (<the one you just added>) already has this name, and two sources pointing at one
  server is a state an operator has to resolve rather than one a restore may create
```

---

## 3. Restore, then walk, then restore again

This is the sequence, and it is three commands rather than one for a structural
reason: `media_items` is the manifest's one `PARTIAL` entry, so restore writes
its two link columns **onto rows that must already exist** — and those rows come
from `usher sync`, which needs the `sources` row that the artifact itself
carries.

```bash
uv run usher restore /var/tmp/nightly.jsonl.gz --dry-run   # look first
uv run usher restore /var/tmp/nightly.jsonl.gz            # 1: household, source, credential, history
uv run usher sync --kind full                             # 2: the walk recreates media_items
uv run usher restore /var/tmp/nightly.jsonl.gz            # 3: the operator's links land on them
```

Run 1, real output from the drill's synthetic arm (every identifier below is
synthetic):

```console
$ uv run usher restore /var/tmp/m10-K5/arm1.jsonl.gz
  llm_calls                       1 written         0 already present
  media_items                     0 written         2 already present
  row_provider_settings           1 written         0 already present
  search_queries                  1 written         0 already present
  source_credentials              1 written         0 already present
  sources                         1 written         0 already present
  users                           1 written         0 already present
  watch_states                    4 written         0 already present
10 rows written, 2 already present, 0 refused, from /var/tmp/m10-K5/arm1.jsonl.gz at schema m10a: committed
```

Run 3, after the walk:

```console
  media_items                     2 written         0 already present
  watch_states                    0 written         4 already present
2 rows written, 10 already present, 0 refused, … : committed
```

⚠️ **"already present" is the wrong word for `media_items` on run 1, and the
number is right.** `skipped` covers two states — a row the target has already
linked, and *a row the walk has not created yet* — and the report renders both
as "already present". On the real artifact into a correctly rebuilt catalog the
drill measured **`media_items 0 written / 10,515 already present` with zero rows
in the table.** Read it as *"not written"*, and check `SELECT count(*) FROM
media_items` rather than the word.

`--dry-run` prints the identical report and commits nothing: in the drill the
two outputs differed by exactly one character sequence, the trailing
`(--dry-run)`. It still exits non-zero if it found refusals, because that is the
same answer the real run would have given.

| exit code | meaning |
|---|---|
| `0` | committed (or a clean `--dry-run`) |
| `1` | refused, or an unreadable artifact, or the database is not up. **Nothing was written.** |

---

## 4. Reading a refusal report

There are **four** refusals and only the last one is a *report*. The first three
raise, because no row was attempted and three counts of zero would be a table of
nothing under a headline.

| # | condition | what to do |
|---|---|---|
| 1 | the artifact is unreadable or truncated — not gzip, ends mid-member, a line that is not JSON, a row missing a column | you have a damaged copy. Take another backup, or restore an older artifact. |
| 2 | **schema mismatch** — the header's `schema_revision` against **this database's** `alembic_version`, with both named | artifact newer: `uv run alembic upgrade head` here first. Artifact older: `alembic downgrade` a scratch database to that revision, or take a newer backup. Restore does not guess across a schema change. |
| 3 | a `table` key `usher.db.backup_manifest` does not classify | the artifact was written against a later schema than this code knows. Upgrade Usher. |
| 4 | **unresolved references**, collected across the whole file and reported together | §5. |

Refusal 4 is the one you will actually see, and the whole file is refused for it
— **one transaction, one commit at the end**, so an unresolved reference in the
last row rolls back the first. The drill confirmed it directly: after a refused
run, every precious table read **0** on a second connection.

⚠️ **The report names every refusal and summarises nothing.** That is right at
41 and unusable at scale: restoring the real artifact into an empty catalog
printed a **14,176-line** report. Pipe it.

```bash
uv run usher restore /var/tmp/nightly.jsonl.gz --dry-run 2>&1 | tee /var/tmp/restore-report.txt
awk '/^  refused /{print $2}' /var/tmp/restore-report.txt | sort | uniq -c
```

---

## 5. What an unresolved reference actually means

No title id survives a bootstrap boundary — `db/repositories/bulk.py` mints a
fresh `new_id()` for every row of every import — so every title, episode and user
reference in the artifact travels as a **natural key** and is re-resolved here.
The ladder is `imdb_id` → `(kind, tmdb_id)` → the raw UUID, first hit wins, and
the refusal line shows you exactly which rungs the row had to offer:

```
refused watch_states  imdb_id=tt99010003, id=00000000-0000-7000-8000-00000000000c
                      -- this database holds no title under any of these
refused watch_states  id=00000000-0000-7000-8000-00000000000d
                      -- this database holds no title under any of these
```

| the line shows | rung | what to do |
|---|---|---|
| `imdb_id=…` | 1 | the title is not in your catalog. Finish `usher bootstrap --phase all`, or check the phase failed. |
| `movie+tmdb_id=…` / `series+tmdb_id=…` and no `imdb_id=` | 2 | the crosswalk has not run. `usher bootstrap --phase tmdb-ids` then `--phase crosswalk`. |
| **`id=…` alone** | 3 | 🔴 **nothing to import.** See below. |

### 🔴 The third rung is the one that cannot be fixed by importing

The raw UUID is accepted **if and only if this database already holds a title
with that exact id** — it is a check on the target, not a key, and it exists so
that restoring into the database the backup came from works with no special
mode. A title reaches it only by having **neither** an `imdb_id` nor a
`tmdb_id`, which on this deployment means an unmatched **stub** the ingest ladder
created. Such a title is in no IMDb dump, has no TMDb id to enrich by, and a
rebuild mints it a new UUID — so *"enrich or import what the lines above name"*,
which is what the command prints, **cannot be done**.

**This is not a corner case.** Measured on the live catalog on 2026-08-25: 6
titles of 1,272,891 carry neither provider id — and those 6 account for **602 of
the artifact's 16,819 carried title references, one in 28**, because a series
stub is referenced once per episode file. In the drill, restoring the real
artifact into a *correctly rebuilt* catalog was **refused whole on exactly 304
`media_items` rows, every one of them rung 3**, while all 3,347 watch states
resolved cleanly and were thrown away with them:

```
  media_items                     0 written    10,515 already present
  watch_states                3,347 written         0 already present
3,440 rows written, 10,515 already present, 304 refused, … : nothing was committed
```

### The escape, and why the format allows it

The artifact is gzip'd JSON Lines precisely so an operator can read and edit it.
A reference with no provider id is spelled with both keys null, so the rows are
one filter away:

```bash
zcat /var/tmp/nightly.jsonl.gz \
  | grep -v '"imdb_id": null, "tmdb_id": null' \
  | gzip > /var/tmp/nightly-filtered.jsonl.gz
uv run usher restore /var/tmp/nightly-filtered.jsonl.gz
```

In the drill this dropped **exactly 304 lines, all `media_items`**, and the
result restored with **0 refusals, exit 0, 3,347 watch states and 89 search
queries committed**.

**What you lose by doing that is small and self-repairing**: those links point
at unmatched stubs, which is exactly the population the match ladder re-derives
on the next `usher sync`. What you would lose by *not* doing it is the entire
artifact.

⚠️ **Check what the filter removed before you trust it.** The grep is a substring
match on one JSON spelling; count it, and confirm every dropped line is a table
you meant:

```bash
zcat /var/tmp/nightly.jsonl.gz | grep '"imdb_id": null, "tmdb_id": null' \
  | python3 -c 'import sys,json,collections; print(collections.Counter(json.loads(l)["table"] for l in sys.stdin))'
```

If that reports any `watch_states`, **do not filter** — you would be discarding
watch history rather than a re-derivable link. Fix the catalog instead, or
restore into a database that still holds those ids.

---

## 6. Restoring into the database the backup came from

This is the easy case and it is the same code path: every raw-id lookup
resolves, so nothing refuses. The drill ran it as its control — the *unfiltered*
artifact, all 304 otherwise-unresolvable rows included, into a catalog holding
the original ids:

```
3,440 rows written, 10,819 already present, 0 refused, … : committed
```

5.06 s. **Same file, same code, same rows; the only variable is whether the
target holds the id.**

---

## 7. Restoring twice is safe

Every merge rule is idempotent by construction — `ON CONFLICT DO NOTHING` for
the two append-only tables, an `IS DISTINCT FROM` guard on the two upserts, and
`WHERE title_id IS NULL` on the links — so a second run reports `already
present` rather than claiming to have rewritten rows that did not move. That is
what makes step 3's *"restore, walk, restore"* sequence safe rather than clever.

**The artifact never wins over a link you already have.** `media_items`' merge
writes only where the target's `title_id` is `NULL`, because overwriting a link
the target holds is the one move that can lose information on both sides at once.
