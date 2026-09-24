# Command line

Most operator work can also be driven from the `usher` command. Managing
sources and row-provider settings, playback, and watch writes are API-only. In
the compose stack, run it inside the container:

```sh
docker compose exec usher usher <command>
```

`usher --help` lists every command, and `usher <command> --help` lists a
command's flags.

From a source checkout, use `uv run usher <command>`. It needs
`USHER_DATABASE_URL` and `USHER_SECRET_KEY` in the environment, the same two
settings the server needs. The compose stack doesn't publish its database, so
to reach it from a checkout, publish it on the host's loopback in a
`compose.override.yml` beside `compose.yml`, and run `docker compose up -d`:

```yaml
services:
  postgres:
    ports:
      - "127.0.0.1:5432:5432"
```

The checkout's `USHER_DATABASE_URL` is then
`postgresql+asyncpg://usher:usher@localhost:5432/usher`. If port 5432 is taken
on the host, publish a different host port.

| Task | Commands |
|---|---|
| Run the server | `serve`, which is also the default with no command |
| Load the catalog | `bootstrap`, `bootstrap-status` |
| Sync a media server | `sync`, `sync-status`, `unmatched`, `push` |
| Run background work | `work`, `schedule` |
| Search and similarity | `search`, `suggest`, `index`, `similar`, `derive`, `genres` |
| Home screen and curation | `home`, `curate` |
| Backup and secrets | `backup`, `restore`, `rotate-secret` |
| Measure quality | `eval` |

**Errors are short.** When something you can fix goes wrong, a command exits 1
with a short message and no stack trace. That covers the database not being up,
a bad `.env` value, an unreachable source or LLM endpoint, a rejected
credential, or an upstream asking to back off:

```text
usher bootstrap-status: ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 5432)
(the stack is one flag away: `usher --traceback bootstrap-status`)
```

A bug still prints its full traceback. Exit codes are `0` for success, `1` for
an error, `2` for a bad argument, and `130` when you interrupt a command with
Ctrl-C.

## Loading the catalog

`usher bootstrap` imports the public datasets. With no `--phase` it runs all six
phases in this order:

| Phase | What it adds | Download |
|---|---|---|
| `imdb` | Every title, with IMDb ratings | ~220 MiB |
| `credit-names` | Cast and crew names, searchable | ~1 GiB |
| `aliases` | Alternate and regional titles, searchable | ~490 MiB |
| `tmdb-ids` | TMDb's list of ids | small |
| `crosswalk` | IMDb-to-TMDb links from Wikidata's query service. It takes 8–21 minutes, depending on Wikidata. | — |
| `movielens` | The MovieLens tag genome, a similarity signal for the home screen | ~335 MiB |

`imdb`, `tmdb-ids` and `crosswalk` are the minimum: without the last two, no
title has a TMDb id to enrich from. `credit-names`, `aliases` and `movielens`
refuse an empty catalog, so run `imdb` first.

```sh
usher bootstrap --phase imdb
usher bootstrap-status        # one row per dataset; each should read `completed`
```

- **Every phase resumes** from its own checkpoint if you stop it and run it
  again.
- **A phase that fails exits 1**, and so does one skipped because a dataset it
  reads is `failed` or still `running`. Its line ends with the command that
  resumes it. The crosswalk retries Wikidata's frequent timeouts by itself: up
  to five attempts and 15 minutes per page.
- **A `completed` row that also shows `error=`** means a later attempt failed,
  but the import it had completed still stands.
- **Run `credit-names` before the TMDb crawl below.** It fills in only titles
  TMDb has not enriched yet, so a title enriched first never gets IMDb's names.
- **Refresh ratings with `--phase ratings`.** It downloads 8 MiB and touches
  only the two rating columns. `--phase imdb` also rewrites every title's name
  and year, and a changed name has to be re-embedded.

### Enriching titles you don't own

Your library's titles are enriched from TMDb as they are synced. To enrich the
rest of the popular catalog too, queue one enrichment job for every movie with
at least 100 IMDb votes and a TMDb id:

```sh
docker compose exec -T usher python - < scripts/enqueue_tier_enrichment.py
```

These jobs run below your library's in priority, at
`USHER_TMDB_REQUESTS_PER_SECOND`. The script stops at 200,000 jobs; pass
`--limit N` after the `-` to change that.

## Syncing a media server

Register a source through the admin API (see the
[README](../../README.md#quickstart)), then walk it:

```sh
usher sync                              # every enabled source, full walk
usher sync --source "Living Room"       # one source, by name
usher sync --kind delta                 # only what changed since the last completed walk
usher sync-status                       # recent walks, queue depth, parked jobs
```

- **A new source needs one `usher sync`.** Once the server is running, it keeps
  sources current over their push channels, and after a reconnect it walks only
  what changed. A source that has never completed a walk has nothing to resume
  from, so the server logs a warning and waits for you rather than walking the
  whole library on its own. `USHER_PUSH_GAP_CLOSE` changes that behaviour.
- **The watch-state walk follows the item walk.** Each watch state has to match
  an item first.
- **`usher sync` exits non-zero if any walk failed**, after it has tried every
  source, so cron can notice.
- **The retraction guard** refuses a walk that would mark more than
  `USHER_SYNC_MAX_RETRACT_FRACTION` (25%) of a source's items unavailable, and
  changes nothing. If you really did remove that much, run the walk with
  `--allow-full-retraction`.
- **You don't have to own the server.** Pointing Usher at a server you don't
  administer is supported. Marking a title played writes to your account on
  that server.

Items Usher could not match to a catalog title wait in the review queue:

```sh
usher unmatched --limit 50                                # list them
usher unmatched --resolve <media_item_id> --title <title_id>
```

`usher push --probe` opens each source's push channel and reports the messages
that arrived. Receiving messages is the health signal, because a handshake can
succeed against the wrong path.

## Background work

The server already runs a worker lane, which drains the job queue (matching,
enrichment, indexing, curation), and a push lane for each source. A normal
deployment needs neither `usher work` nor `usher push`. They exist for
splitting the lanes across containers.

- **Don't run `usher work` beside a server whose worker lane is on.**
  `USHER_JOB_CONCURRENCY` and `USHER_TMDB_REQUESTS_PER_SECOND` apply per
  process, so a second worker spends TMDb's per-client rate limit twice. Set
  `USHER_WORKER_ENABLED=false` on the server first.
- **A worker claims only the jobs it is configured for.** `enrich` and `derive`
  need `USHER_TMDB_API_KEY`, `index` needs an embedding model, and `curate` needs
  `USHER_LLM_ENABLED=true`. Other workers leave those jobs queued rather than
  failing them.
- **`usher work --once`** claims one batch and exits. It does not drain the
  queue.
- **`usher schedule --once`** runs whatever scheduled jobs are due, for cron.
  The alternative is `USHER_SCHEDULER_ENABLED` in exactly one process. See the
  [configuration guide](configuration.md#the-scheduler).

## Search and similarity

```sh
usher search "the quiet vacuum"                        # full-text and semantic, fused
usher search "vacuum" --mode full_text --kind movie --year-from 1990 --owned-only
usher suggest "the quie" --limit 5                     # type-ahead, typo-tolerant
usher similar <title_id>                               # precomputed neighbours
```

`usher search` ends with a `semantic_coverage` line: the share of enriched
titles that have an embedding. Without a model, a fused search quietly narrows
to full-text and says `fused was served as full_text`. `semantic_coverage=0.000`
means a model is configured but nothing has been embedded yet.

Four commands keep derived data fresh. Each one only reports when run bare, and
writes when given `--backfill` or `--rebuild`:

| Command | Bare form reports | Writing form |
|---|---|---|
| `usher index` | stale embeddings | `--backfill` queues one `index` job per stale title |
| `usher similar` | the neighbour table's age | `--rebuild` recomputes it from stored vectors; `--resume` continues one |
| `usher derive` | people, credits and collections coverage | `--backfill` re-derives them from cached TMDb payloads |
| `usher genres` | titles using a non-canonical genre name | `--backfill` rewrites them into Usher's vocabulary |

The neighbour table goes stale whenever *other* titles gain embeddings, so
`usher similar --rebuild` needs rerunning after a backfill. Either run it from
cron or turn on the [scheduler](configuration.md#the-scheduler).

`usher genres --backfill` changes the text that titles are embedded from, so it
marks their embeddings stale. Follow it with `usher index --backfill`, and then
`usher similar --rebuild` once the index has drained.

## Home screen

`usher home` composes the home screen `GET /home` returns, and prints one line
per row provider, including providers that proposed nothing. `--repeat 5` times
five cold compositions.

## LLM curation

`usher curate` runs one curation generation for the household and prints what
it kept, what it dropped and why, and what it cost. Abbreviated:

```text
pool: 200 candidates
kept: 2 rows, 11 cards
  curated-1     Slow-burn sci-fi for a rainy night                5 cards
  curated-2     Quietly devastating, quietly funny                6 cards
dropped (all five reasons, zeros included -- an absent line and a
         reason nobody counts read the same):
  not_in_pool        1 card
  ...
tokens: 4812 in, 391 out   cost: $0.00042100   latency: 2314 ms   model: ...
```

It exits 1 when LLM curation is off, when the candidate pool is empty (usually
an empty catalog), or when the model's answer validated to nothing. The
household's previous rows stay in place. Only the empty pool is free: every
attempt that reaches the model is billed and recorded in `llm_calls`, including
failed ones.

Nothing schedules a generation, so use cron:

```cron
30 4 * * *  cd /srv/usher && docker compose exec -T usher usher curate >> /var/log/usher-curate.log 2>&1
```

`POST /admin/rows/regenerate` queues the same generation for the worker, and
answers 202 without the report.

## Backup, restore and secrets

```sh
usher backup --output /data/bulk/usher-backup.jsonl.gz
usher restore --dry-run /data/bulk/usher-backup.jsonl.gz
```

In the container, `usher backup` needs `--output`, because its default location,
the working directory `/app`, isn't writable by the container's user. The
container's `/data/bulk` is the host's `data/bulk`; copy the backup off the host
from there. The README's
[Backup section](../../README.md#backup) covers what a backup holds. The
[runbooks](../runbooks/README.md) have the full procedures, including why a
restore needs a rebuilt catalog first.

`usher rotate-secret --new-key-env VAR` re-encrypts stored credentials under a
new `USHER_SECRET_KEY`. It takes the *name* of the variable holding the new key,
never the key itself. In the compose stack, export the new key in your shell and
forward it with `-e`, which passes the variable's value without putting it on a
command line:

```sh
docker compose exec -e USHER_NEW_SECRET_KEY usher usher rotate-secret --new-key-env USHER_NEW_SECRET_KEY
```

Follow the [rotation runbook](../runbooks/rotation.md): changing `.env` first
leaves credentials nothing can decrypt.

## Measuring quality

`usher eval` measures search type-ahead against the bars in
[`docs/evals/`](../evals/). It needs `uv sync --extra eval`, which the container
image doesn't include, so run it from a source checkout against the stack's
database, published as described [above](#command-line).
