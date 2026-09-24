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

⚠️ **`USHER_SECRET_KEY` is as precious as any table, and no artifact carries
it.** `usher backup` never calls `build_cipher` and holds no key, so
`source_credentials` travels as the ciphertext it is stored as. An artifact
restored into a deployment holding a different key restores credentials nothing
can decrypt. **Store the key with the artifact.**

⚠️ **Nothing schedules `usher similar --rebuild` unless the scheduler is on**
(`USHER_SCHEDULER_ENABLED`, off by default). It is the standing freshness
gap in this project rather than anything specific to recovery — a title's
neighbours go stale when some *other* title gets an embedding, which no per-row
predicate can decide. Three of these four runbooks end up owing it.
