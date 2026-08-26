# Runbooks

Four operator-facing procedures, for the four things this deployment can ask an
operator to do under pressure. PRD [08 — Operations](../prd/08-operations.md)
is the design; these are the commands, in order, with real output.

**Every one of them was written from a drill rather than from the design.**
Each says at the top which day it ran and against what, every block is output
somebody actually saw, and every figure carries its date. Where a drill refuted
a design claim, the refutation is in the runbook and marked — that is the point
of writing them this way, and it has already happened four times.

| runbook | read it when | the one thing it exists to stop |
|---|---|---|
| [`restore.md`](restore.md) | you have an artifact and want the rows back | restoring into an **empty** catalog. `watch_states.title_id` is `ON DELETE RESTRICT`, so the load-bearing table's every row fails its foreign key — measured at **14,166 of 14,259 rows refused**, nothing committed. Rebuild first, restore on top. |
| [`disaster-recovery.md`](disaster-recovery.md) | the database is gone | despair at the clock. The restore is **seconds** and the rebuild is **hours**, in that order, and Usher serves in between — you lose an afternoon of indexing, not a household's history. |
| [`upgrade.md`](upgrade.md) | you are about to deploy a newer Usher | applying a migration you have not read. `m09e` deleted **every embedding, centroid and neighbour row** in the deployment, and a backup would not have helped — those tables are rebuildable and the artifact deliberately carries none of them. |
| [`rotation.md`](rotation.md) | you are changing `USHER_SECRET_KEY` | changing `.env` **first**. That leaves a deployment that cannot read its own credentials and a rotation command that can decrypt nothing — the one mistake that looks like data loss when it is not. |

**Read them in that order if you are reading them cold.** `restore.md` is the
longest and the other three point into it: `disaster-recovery.md` is its clock,
`upgrade.md` §1 takes the artifact it describes, and `rotation.md` §4 is the
reason an artifact taken before a rotation is not restorable after one.

---

## Three things every one of these pages assumes

🔴 **`.env` points at your real database, and `alembic` reads it.** Every
checkout of this project carries a `.env` whose `USHER_DATABASE_URL` names the
deployment's own database, and `alembic/env.py` reads `get_settings()` — so a
command typed in a worktree targets production-shared state by default, with no
flag and no prompt. That has taken this deployment down for ~3.5 hours once
(`.claude/rules/db-and-sql.md`, 2026-08-19). Every command in these runbooks
that touches a database other than the live one exports an overriding
`USHER_DATABASE_URL` first, and reads the *resolved* host and port back before
proceeding — the variable being set is not the same claim as it having won.

⚠️ **`USHER_SECRET_KEY` is the seventh precious table.** `usher backup` never
calls `build_cipher` and holds no key, so `source_credentials` travels as the
ciphertext it is stored as. An artifact restored into a deployment holding a
different key restores credentials nothing can decrypt. **Store the key with
the artifact**, or you are backing up six of seven precious tables.

⚠️ **Nothing schedules `usher similar --rebuild`.** It is the standing freshness
gap in this project rather than anything specific to recovery — a title's
neighbours go stale when some *other* title gets an embedding, which no per-row
predicate can decide. Three of these four runbooks end up owing it.

---

## Draft for `README.md` — not yet landed

PRD [08](../prd/08-operations.md)'s backup section asks for one thing this
directory cannot deliver on its own:

> Disaster recovery becomes a short restore plus a background rebuild instead
> of a crisis. **State this loudly in the README** — it is the difference
> between "lost everything" and "lost an afternoon of indexing".

**The paragraph below is that statement, drafted and deliberately not yet added
to `README.md`.** It is parked here rather than landed because the repository's
README pass belongs to a later phase of this milestone, and two groups editing
`README.md` in one phase is a merge conflict for no benefit. Whoever does that
pass moves it; nothing else in this directory depends on it.

> ### Backing it up
>
> **`usher backup` writes one gzipped JSON Lines file, and on the household
> this project measures it is 434 kB and takes a second.** That is not a
> database dump — it is the seven tables nothing can rebuild: the household,
> the source and its credential, the watch history, the LLM spend ledger, the
> row-provider settings, the search log, and the manual match decisions on
> `media_items`. Everything else in the database — the 1.27 M-title catalog,
> the embeddings, the neighbour graph, the payload cache, the artwork — is
> rebuilt by the importers, so it is deliberately not carried.
>
> **That asymmetry is the whole design, and it is what makes recovery
> survivable: the restore is seconds and the rebuild is hours, they happen in
> that order, and Usher serves in between.** Losing the database means losing
> an afternoon of indexing rather than a household's history.
>
> ```bash
> uv run usher backup --output /var/tmp/usher-backup.jsonl.gz
> ```
>
> 🔴 **Keep `USHER_SECRET_KEY` with the artifact.** Source credentials travel
> as the ciphertext they are stored as, and the file holds no key — restored
> under a different one, every source has to be re-entered by hand. The command
> says so on every run.
>
> ⚠️ **A backup is not restorable into an empty database.** Rebuild the catalog
> first, then restore on top of it; `usher restore --dry-run` tells you which
> it is before anything is written. The full sequence, with real output, is
> `docs/runbooks/restore.md`.

*(One thing to change when this moves: the path in the last line is written
relative to the repository root, so it is deliberately **not** a markdown link
while it lives here — from `docs/runbooks/` it would resolve to nothing. Make it
a link once it is in `README.md`, where the path is correct.)*
