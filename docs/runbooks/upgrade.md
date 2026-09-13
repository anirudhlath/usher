# Runbook — upgrading a running deployment

**Audience:** the operator of a running Usher deployment.
**Written 2026-08-26 against this repository's own `compose.yml` and
`Dockerfile`.** Every command below was run and every block quoted is real
output; where a figure comes from an earlier run it carries that run's date.
The measurements were taken on Docker 29.6.2 / Docker Compose 5.3.1, against
scratch containers created and removed by the run — never against the
deployment's own database, except for two reads (`/health/ready` and
`alembic current`) that write nothing.

Companions: [`restore.md`](restore.md) — the artifact step 1 tells you to take,
in detail; [`disaster-recovery.md`](disaster-recovery.md) — when the upgrade is
not the problem; [`rotation.md`](rotation.md) — the other operator command that
writes to a precious table.

---

## 0. Four facts about this deployment, and they are the whole procedure

### The container runs the migration itself

`Dockerfile:133`:

```dockerfile
CMD ["sh", "-c", "alembic upgrade head && exec python -m usher"]
```

So **"upgrade" means "replace the container"**, and there is no separate
migrate step to forget. Two consequences, both stated by the Dockerfile's own
comment immediately above that line (`Dockerfile:124`):

> `alembic upgrade head` here has no distributed lock. Two containers starting
> at once would both race to apply the same pending migration.

and (`Dockerfile:130`):

> `/health/ready`'s migration-mismatch check would at least surface a lost race
> as a 503 rather than silently serving against the wrong schema, but it does
> not prevent the race itself.

🔴 **Read that as: exactly one Usher process may start at a time, and the check
downstream is a report rather than a guard.**

✅ **The single-service case is better than the caveat sounds, and it was
measured rather than assumed.** `docker compose up -d` over an already-running
service is a **stop-then-start recreate**, not an overlap: a scratch two-run
project on Compose 5.3.1 logged `START …759 / STOP …765 / START …766 / STOP
…772`, the replacement's first line after the outgoing container's last.

**So the exposure is a *second* Usher process, not this one.** A
`docker compose run`, a `--scale usher=2`, or a `uv run usher work` left running
in another shell. `docker compose down` in step 2 is what makes *"one process
ran the migration"* true rather than probable.

### `docker compose pull` does not fetch Usher, and that is not a mistake

`compose.yml`'s `usher` service has `build: .` and **no `image:`** — this
project publishes no image anywhere. Measured:

```console
$ docker compose --dry-run pull
 usher Skipped No image to be pulled
 Image pgvector/pgvector:pg17 Pulling
 Image pgvector/pgvector:pg17 Pulled
```

**The upgrade is therefore `git pull` plus `--build`.** `docker compose pull` is
still worth a line, for `pgvector/pgvector:pg17`.

### `docker compose down` does not delete the database

`compose.yml` declares **no top-level `volumes:` key**, so this project owns no
named volume at all. Postgres' data is the bind mount
`./data/postgres:/var/lib/postgresql/data` and the image proxy's cache is
`./data/images` — both are host directories, so `down`, and even `down -v`,
leave them where they are. The `observability` network survives too: it is
`external: true` precisely so that a `down` here cannot remove a network
Grafana, Prometheus, Loki, Tempo and the collector are all on.

⚠️ **What `down` does remove is anything you wrote inside the container's own
filesystem.** That is the trap in step 1.

### `postgres` publishes no host port

The `postgres` service has no `ports:` mapping — `docker ps` shows it as bare
`5432/tcp` — so its only route is the compose network, and `docker compose down`
removes that network. **This is why the backup is step 1 and cannot be step 3.**

---

## 1. Back up first, and it is a command rather than a `pg_dump`

```fish
uv run usher backup --output /var/tmp/usher-pre-upgrade.jsonl.gz
```

**1.03 s and 443,902 bytes** for **14,259 rows from 8 tables at schema `m10a`**
on the deployment this project measures (K5's drill, 2026-08-25). It reads and
never writes, so it is safe against a live database — that drill ran it against
one and the before/after snapshot was identical on all sixteen counted values.
[`restore.md`](restore.md) §1 is the same command with its output in full.

**Why not `pg_dump`.** The runtime image is `python:3.13-slim` and carries
neither `pg_dump` nor `psql`, so the documented alternative **cannot be run from
the container this project ships**; an operator following it has to reach the
binary inside the *Postgres* container, which Usher does not own. Adding
`postgresql-client` to the runtime stage costs **+62.1 MiB, +18% on a 359 MB
image** (PRD 08, measured 2026-08-13).

⚠️ **Keep `USHER_SECRET_KEY` with the artifact.** `usher backup` never calls
`build_cipher` and holds no key, so `source_credentials` travels as the
ciphertext it is stored as. An upgrade does not change the key — but a restore
taken *for* an upgrade is a restore, and [`restore.md`](restore.md) §0 is the
page that matters if you ever use it.

⚠️ **If you take the backup with `docker compose exec`, get the file off the
container before step 2.** The artifact lands in the container's own writable
layer, which `docker compose down` deletes, and a backup that the first command
of the upgrade destroys is worse than no backup because you will believe you
have one.

---

## 2. 🔴 What the backup does not cover, with `m09e` as the worked example

**Back up first is necessary and it is not sufficient, and the migration in
this project's own history that proves it is `m09e`.**

`m09e` (`src/usher/db/migrations/versions/m09e_embedding_width.py`, 2026-08-13)
moved `title_embeddings.embedding` and `user_taste.centroid` from
`halfvec(384)` to `halfvec(1024)`, because `BAAI/bge-m3` is 1024 wide and a
`halfvec` typmod is DDL. There is no honest conversion — a 384-lane vector
padded or projected to 1024 is not what the new model would have produced — and
the type change is a runtime dimension error the moment one row exists. So the
revision empties three tables before the `ALTER`:

```sql
DELETE FROM title_neighbors;
DELETE FROM user_taste;
DELETE FROM title_embeddings;
```

**Every embedding, every taste centroid and every neighbour row in the
deployment, deleted, by design** — and deletion is the *correct* end state
rather than merely the reachable one, because a `title_embeddings` row with a
`NULL` embedding is a written refusal the backfill would never claim again.

🔴 **The backup would not have helped**, and that is why this section is here
rather than a footnote. All three tables are `REBUILDABLE` in
`src/usher/db/backup_manifest.py`, so `usher backup` carries none of them — the
artifact is 443,902 bytes *precisely because* it holds no vectors. Restoring it
after `m09e` returns the household, the source, its credential, the watch
history, the ledger and the search log, every one of which `m09e` never
touched, and **zero embeddings**.

**What would have helped is knowing what you were about to lose, which is two
numbers:**

| after `m09e`, run | cost | measured |
|---|---|---|
| `uv run usher index --backfill`, then `uv run usher work` until the queue drains | **105.9 min**, 130,720 titles | 2026-08-13 |
| `uv run usher similar --rebuild` | **3.33 h** — 130,720 seeds, 3,268,000 rows, 11,981 s at 91.7 ms/seed | 2026-08-13 |

⚠️ **3.33 h, and not 21.6 h.** 21.6 h is `m09e`'s own figure, taken while
1024-lane `halfvec` columns had crossed `TOAST_TUPLE_THRESHOLD` and every exact
scan cost 594.7 ms/seed; `m09f` moved every `halfvec` column to `PLAIN` storage
and took that back to 91.7 ms/seed. The *conclusion* survived the correction —
plan the neighbour walk as an overnight job — and the number that motivated it
did not. Both figures and their provenance are in `backup_manifest.py`'s
`title_embeddings` and `title_neighbors` entries.

⚠️ **Nothing runs `usher similar --rebuild` for you.** It is not scheduled, not
triggered by the backfill, and not implied by `docker compose up`. Until it
runs, `title_neighbors` is empty and "more like this" is an empty row rather
than a wrong one.

### Reading what you are about to apply, before you apply it

```console
$ uv run alembic current                       # what the database is at
$ uv run alembic history -r <that revision>:head
m09f -> m10a (head), Three columns had two writers each; now five columns each name their source.
m09e -> m09f, Every `halfvec` column stores PLAIN, because 1024 lanes crossed the TOAST line.
m09d -> m09e, The embedding width moves 384 -> 1024, and the fingerprint scheme cannot help.
```

Each revision's one-line summary is its docstring's first line, and every
revision in this project explains in its module docstring what it deletes and
what has to run afterwards — `m09e`'s carries the three commands above verbatim.
**Read them. That is the step this runbook exists to insist on**, and it costs
a minute against the 5.1 hours `m09e` costs unprepared.

🔴 **`alembic` typed in a checkout targets the deployment, with no flag and no
prompt.** Every checkout of this project carries a `.env` whose
`USHER_DATABASE_URL` names the deployment's own database and `alembic/env.py`
reads `get_settings()`; that has taken this deployment down for ~3.5 hours once
(`.claude/rules/db-and-sql.md`, 2026-08-19). `current` and `history` are reads.
`upgrade` is not, and the container is what should be running it.

---

## 3. The upgrade

```bash
uv run usher backup --output /var/tmp/usher-pre-upgrade.jsonl.gz   # §1, first
docker compose down                # one process, and no leftover worker
git pull                           # the "pull" that matters; see §0
docker compose pull                # pgvector only -- `usher` is Skipped
docker compose up -d --build       # migrations run here, inside the container
```

Then watch it come up, because on this deployment the interesting failure is
visible here and nowhere else:

```bash
docker compose logs -f usher
docker compose ps                  # `usher` reaches (healthy) or it does not
```

`compose.yml`'s healthcheck is `/health/ready` on a 5 s interval with a 20 s
start period and 10 retries, so `(healthy)` in `docker compose ps` already means
*database reachable and migration state agreed* — it is the same check as §4,
polled for you.

---

## 4. Check `/health/ready`, and know which failure it can see

```console
$ curl -sf -o /dev/null -w "%{http_code}\n" http://localhost:8100/health/ready
200
$ curl -s http://localhost:8100/health/ready | python3 -m json.tool
{
    "status": "ready",
    "checks": { "database": true, "migrations": true },
    "lanes": { "push": ["<your source>"], "worker": true }
}
```

Read on this deployment 2026-08-26. `8100` is `USHER_COMPOSE_HOST_PORT`'s
default; the container listens on 8000.

**A migration that did not run is a 503, and it names both revisions.**
Measured against a scratch database held one revision behind the code:

```
readiness check failed: migration mismatch (database at 'm09f', code expects 'm10a')
```

```json
{ "status": "degraded", "checks": { "database": true, "migrations": false } }
```

HTTP **503**, with the body still a `ReadinessResponse` so `checks` says which
half failed. That is the shape the Dockerfile's comment promises.

🔴 **But it is not the failure a bad upgrade on this deployment actually
produces, and expecting it will send you to the wrong place.** `/health/ready`
can only answer once the app is serving, and the app only serves if
`alembic upgrade head` succeeded — the `CMD` is one `&&`. Measured on a scratch
database stamped at a revision the code does not carry:

```console
$ uv run alembic upgrade head
ERROR [alembic.util.messaging] Can't locate revision identified by 'm10z'
FAILED: Can't locate revision identified by 'm10z'
```

**The exit code is 255** — read unpiped, because a pipeline reports the last
command's status and not alembic's. **So the `&&` short-circuits, `exec python
-m usher` never runs, the
container exits — and `restart: unless-stopped` starts it again.** What you have
is a crash loop, not a 503, and `curl` gets a connection refused rather than a
degraded body. **`docker compose logs usher` is the only place that error
appears.**

| what you see | what happened | §  |
|---|---|---|
| `200`, `migrations: true` | the upgrade landed | done — §2's backfills may still be owed |
| `503`, `migrations: false`, both revisions named | the app is serving against a schema it does not expect — a second process, or an app started outside the container | §5 |
| connection refused, `docker compose ps` restarting | `alembic upgrade head` failed; read the logs | §5 |

---

## 5. Rolling back

**The code rolls back with `git`. The database does not roll back with it**, and
that asymmetry is the whole of this section.

Checking out the previous revision and running `docker compose up -d --build`
puts a container carrying revision *N-1* in front of a database at revision *N*.
Alembic cannot resolve a revision its script directory does not contain, so the
container fails at its first command with the exit 255 above and never serves.
**A rollback that only rolls back the code produces a crash loop.**

So there are two honest options, and the first is almost always right:

1. **Roll forward.** Read the error in `docker compose logs usher`, fix it, and
   deploy again. A migration that failed part-way has already rolled back — this
   project's migrations run under `Will assume transactional DDL`, which is what
   `alembic` reports against Postgres on every run above — so the database is at
   the last revision that completed, not half-way through one.

2. **Downgrade the database first, then the code.** `alembic downgrade
   <the older revision>` from a checkout at the *newer* code — it is the newer
   tree that holds the revision's `downgrade()`, so the order is forced.

   🔴 **A `downgrade()` is not an undo.** `m09e`'s is symmetric and *equally
   destructive*: it restores the width and not the data, because the data it
   would restore was 1024 wide. Downgrading past `m09e` empties
   `title_embeddings`, `user_taste` and `title_neighbors` a second time, and the
   two costs in §2's table are owed again. Read the revision's docstring before
   typing this, for the same reason §2 says to read it before upgrading.

**Where the step-1 artifact is the answer instead:** when the database is the
thing that is wrong rather than the code — a migration that corrupted data, or a
rollback whose `downgrade()` you are unwilling to run. Provision a database, take
it to the artifact's revision, restore, and point the deployment at it.
[`restore.md`](restore.md) §4 is the refusal table and
[`disaster-recovery.md`](disaster-recovery.md) §2 is the clock. ⚠️ `usher
restore` compares the artifact's `schema_revision` against **this database's**
`alembic_version` and refuses a mismatch naming both, so an artifact taken
before the upgrade restores into a database at the *old* revision and not into
the upgraded one.

---

## 6. Checklist

- [ ] `uv run usher backup --output …` ran **before** anything was stopped, the
      file is on the host and not inside the container, and `USHER_SECRET_KEY`
      is stored with it.
- [ ] `uv run alembic current` and `uv run alembic history -r <it>:head` were
      read, and the docstring of every revision about to run was read with them.
      A revision that deletes a derived table says so in its first paragraph.
- [ ] If one of them is a `m09e`-shaped revision: **105.9 min** of embedding
      backfill and **3.33 h** of neighbour walk are budgeted, and
      `uv run usher similar --rebuild` is on somebody's list, because nothing
      schedules it.
- [ ] `docker compose down` ran, and no `uv run usher work` is alive in another
      shell.
- [ ] `git pull`, then `docker compose up -d --build`. `docker compose pull`
      fetches Postgres only.
- [ ] `docker compose ps` reports `usher` **(healthy)**, or
      `docker compose logs usher` was read to find out why not.
- [ ] `/health/ready` answers **200** with `migrations: true`. A 503 names both
      revisions; a connection refused means the container never started and the
      answer is in the logs.
- [ ] The rollback plan is *roll forward*, unless you have read the
      `downgrade()` you intend to run.
