# Usher

A self-hosted media catalog backend. Usher maintains its own canonical database
of film and television, treats media servers (Emby first) as interchangeable
*sources* that answer "where can this be played?", and exposes an API rich
enough to build a full media browser against.

Design documentation lives in [`docs/prd/`](docs/prd/README.md).

## Status

Pre-release. Milestones M1 (foundation), M2 (catalog bootstrap), M3 (Emby
adapter), M4 (ingest pipeline), M5 (push and read-through) and M6 (search)
are complete — see [`docs/plans/`](docs/plans/) for the task breakdowns and
[`docs/prd/09-roadmap.md`](docs/prd/09-roadmap.md) for what's next.

M3, M4 and M5 are each verified against a live Emby server, and M4's metadata
half against the live TMDb API. M5's run is the first in this repository to
have parsed a real `/embywebsocket` message. **M6's one outstanding item is
its own typo-tolerance gate**, which is built but has not yet been run
against a real 1.27M-title catalog
([ADR-0002](docs/prd/decisions/0002-postgres-first-search.md)).

**The HTTP surface is deliberately small so far**: `/health`,
`/health/ready`, `/titles/{id}`, `/events` (SSE) and the `/admin/sources`
routes. **M6 adds none** — search, suggest, similarity and indexing are all
command-line, and M9 owns the routers. Everything the ingest pipeline does is
driven from the command line until then — see below.

## Requirements

- Docker and Docker Compose
- A [TMDb API key](https://www.themoviedb.org/settings/api) (free, non-commercial)
- **Optional:** the embedding extra, for semantic search and "more like
  this". `uv sync --extra embedding` installs `fastembed` — **167 MiB, 28
  packages, no torch** — and downloads a 65 MB model on first use. Without
  it, full-text and typo-tolerant type-ahead still serve the whole catalog;
  the deployment is narrowed, not broken. Leave `USHER_EMBEDDING_OFFLINE=true`
  on: it sets `HF_HUB_OFFLINE=1` before the library loads, and without it a
  host with a warm cache but no network fails with
  `RuntimeError: Cannot send a request, as the client has been closed` — a
  message that names neither the network nor the cache.

## Running it

```bash
cp .env.example .env
openssl rand -hex 32          # paste this into USHER_SECRET_KEY= in .env
docker compose up -d --build

curl -sf http://localhost:8100/health        # {"status":"ok"}
curl -sf http://localhost:8100/health/ready  # adds database + migration state,
                                             # and reports the background lanes
```

`USHER_SECRET_KEY` is the one value you must fill in: `.env.example` ships it
empty, it has no default, and compose refuses to start without it. It encrypts
stored source credentials, so changing it later makes existing ones unreadable
(the admin status endpoint reports that state rather than failing).

Set it *in place* rather than appending a second line. Docker Compose takes
the last definition of a duplicated key, so `echo "USHER_SECRET_KEY=…" >> .env`
does work — but it leaves a file with two `USHER_SECRET_KEY` lines, which
nobody can read confidently and which behaves differently under tools that
take the first.

`USHER_COMPOSE_HOST_PORT` defaults to `8100`. It is the *host*-side publish
port, not a setting — compose substitutes it into the `ports:` mapping and the
application never sees it. `USHER_COMPOSE_*` is the one namespace reserved for
variables like that; every other `USHER_*` key is a real setting, and an
unknown one is refused at startup rather than ignored, so a typo is loud.

**Every key in `.env` reaches the container**, because compose hands it the
whole file (`env_file:`). The four exceptions are marked `[compose-owned]` in
`.env.example` and listed in `compose.yml`'s `environment:` block with the
reason each belongs to the container topology rather than to you:
`USHER_DATABASE_URL` (the hostname on the compose network),
`USHER_HOST`/`USHER_PORT` (what the published port, the `EXPOSE` and the
healthcheck all assume) and `USHER_SECRET_KEY` (substituted so a missing one
fails at `docker compose up` rather than in a container log).

Migrations run automatically on container start.

## Command line

`usher` is installed as a console script by `uv sync`; `python -m usher` is
the identical code path and is what the container's `CMD` runs. With no
arguments, either one starts the HTTP server.

Every command needs `USHER_DATABASE_URL` and `USHER_SECRET_KEY` in the
environment, the same two the server needs. Inside the compose stack, reach
them with `docker compose exec usher python -m usher <command>`.

```bash
uv run usher --help                  # every command and flag
uv run usher serve                   # the HTTP server (also the no-argument default)
```

**Populate the catalog** — pulls IMDb's `title.basics`/`title.ratings` dumps,
TMDb's *public daily id export* files (no API key needed for these) and
Wikidata's SPARQL endpoint. About three minutes and 1.27M titles on a
reasonable machine. Resumable: kill it and re-run, and it continues from its
checkpoint. See
[`docs/prd/04-catalog-bootstrap.md`](docs/prd/04-catalog-bootstrap.md).

```bash
uv run usher bootstrap                     # all three phases
uv run usher bootstrap --phase imdb        # one at a time: imdb | tmdb-ids | crosswalk | all
uv run usher bootstrap-status              # progress per dataset, and catalog size
```

**Sync a source** — walks a registered media server into the catalog
(matching, ingest, availability sweep), then walks its watch state.

```bash
uv run usher sync                             # every enabled source, full walk
uv run usher sync --source "Living Room Emby" # just one, by name
uv run usher sync --kind delta                # only what changed since the last completed run
uv run usher sync --allow-full-retraction     # see below
```

`--kind` offers `full` and `delta` only. Watch state is not an alternative to
the item walk — it always runs *after* it, because each state has to resolve
against a media item.

`--allow-full-retraction` lifts the safety ceiling that refuses to mark a
whole library unavailable in one run
([ADR-0015](docs/prd/decisions/0015-availability-is-retracted-only-by-a-finished-walk.md)).
Only use it for a library the operator really did remove.

**Inspect and repair**

```bash
uv run usher sync-status                                # recent runs, queue depth, parked jobs
uv run usher unmatched --limit 50 --offset 0            # the review queue
uv run usher unmatched --resolve <media_item_id> --title <title_id>
```

`--resolve` and `--title` are used together; either alone is refused, because
`--resolve` on its own would blank a link rather than create one.

**Run queued work** — matching that needs a provider lookup, watch-history
backfill, and TMDb enrichment (the last only when `USHER_TMDB_API_KEY` is
set).

```bash
uv run usher work --once   # one pass over the queue, then exit
uv run usher work          # stay up, polling
```

`usher index` reports how much of the search index is out of date. **The bare
form only reads**, so it is safe to run on a production box while diagnosing
something; `--backfill` is the writing form and enqueues one `index` job per
stale title for a worker to run.

```bash
uv run usher index             # model, stale count, refused count, estimated worker time
uv run usher index --backfill  # enqueue the work; re-running writes zero rows
```

`usher derive` re-derives people, credits and collections out of the provider
payloads M4 already cached (ADR-0016) — **with no second network call**. Its
bare form is five counts and no writes; `--backfill` walks the cache inline,
which is where it deliberately differs from `usher index`: derivation needs no
model, no request and no rate limit, so the queue would buy ordering, retry and
backoff for work that needs none of the three. In steady state each enrichment
enqueues a `derive` job alongside its `index` one, and the backfill exists
because M7 arrives after a catalog is already enriched — nothing will ever
re-enrich those titles, so nothing will ever enqueue a job for them.

```bash
uv run usher derive             # cached payloads, titles with credits, people, collections
uv run usher derive --backfill  # walk the cache and re-derive inline; idempotent
```

**Order matters after a fresh upgrade**: `alembic upgrade head` → `usher derive
--backfill` → `usher index --backfill` → `usher work`. Indexing before deriving
embeds every title with an empty weight class B and then re-claims all of them
once `credit_names` is populated, which is the wasted pass twice over.

Embedding is optional and off by default. The model lives behind an extra
(`uv sync --extra embedding`, 167 MiB, no torch) and `USHER_EMBEDDING_ENABLED`
gates it; without it a worker simply never claims `index` jobs, and full-text
and trigram still serve the whole catalog. `usher index` itself loads no model
— staleness is a question about a recorded model *name*.

`usher search` and `usher suggest` are the read side, and M6 adds no HTTP
route — the CLI delivers the whole capability, exactly as `bootstrap` and the
ingest commands do, and M9 owns the routers.

```bash
uv run usher search "the quiet vacuum"                 # hybrid by default
uv run usher search "vacuum" --mode full_text --limit 5
uv run usher search "vacuum" --kind movie --year-from 1990 --year-to 2030 \
                             --genre drama --owned-only --min-enrichment enriched
uv run usher suggest "the quie" --limit 5              # type-ahead, typo-tolerant
```

**`usher search` prints `semantic_coverage` on every run, not only when it is
low**, and that line is the reason the command has a human-readable mode at
all. A `--mode fused` search against a catalog with no embeddings degrades to
full-text — correctly, because a title with no vector is *absent from the
semantic candidate list* rather than ranked last — and the result looks
exactly like a working hybrid search: no error, no empty result, no log line.
Two things can produce it and they get different sentences, because they have
different fixes: `fused was served as full_text` means this deployment has no
model (install the extra, set `USHER_EMBEDDING_ENABLED=true`), while
`semantic_coverage=0.000` on a search that really did run fused means nothing
has been embedded yet (`usher index --backfill`). `--mode semantic` with no
model refuses outright rather than narrowing — it is the one question
full-text cannot answer, so a plausible answer to a different one is worse
than none.

Every `SearchFilters` field has a flag and no filter has two, which is
deliberate: an engine that cannot express a filter raises rather than ignoring
it, because an ignored filter returns *more* results and reads as working.

`usher similar` has the same two forms `usher index` does, and for the same
reason: a read and the write that refreshes what it reads.

```bash
uv run usher similar <title id>    # the precomputed neighbours, best first
uv run usher similar --rebuild     # recompute title_neighbors for the embedded tier
```

`usher home` composes the screen `GET /home` returns, and times it.

```bash
uv run usher home                  # one composition, with a per-provider table
uv run usher home --repeat 5       # five *cold* compositions; the cache is cleared before each
```

It ships **alongside** the route rather than instead of it, which is the
reverse of `usher search`: ADR-0006's claim — one request paints a screen — is
a property of a request boundary that no command can exhibit, so there the
route is the deliverable. What the command is for is the rule that every
operator command works against an empty database, and the arithmetic that rule
is hunting: the taste centroid is a mean, and the mean of zero embeddings is
0/0. Against an empty household it exits 0 and prints nine providers that
proposed nothing.

**Every registered provider gets a line, including the ones that proposed
nothing** — an absent provider and a silent one are the two states a composed
home screen cannot otherwise tell apart. `proposed 1, built 0` is a row that
was chosen, hydrated and found nothing renderable; `proposed 2` with no build
is the per-family cap. The cold/warm pair is the only measurement of the row
cache this milestone has, because `usher.cache.hits`/`.misses` is M9's.

Measured 2026-08-04 against a real 1,271,570-title catalog with a synthetic
household on top of it: **p50 23.9 ms, p95 35.9 ms cold, 0.0 ms warm**, eight
rows and 115 cards, slowest provider 34% of build time. The rows build
**sequentially** — `AsyncSession` is not safe for concurrent use — and the
command prints the rule for revisiting that (p95 > 400 ms *and* no provider
≥ 50% of build time) beside the numbers, so it is read off the output rather
than recomputed.

**Nothing runs the rebuild for you**, and that is stated rather than implied.
A title's neighbours go stale when *some other* title gets an embedding, which
no per-row predicate can decide — so unlike everything else Usher derives,
`title_neighbors` carries a whole-artefact age instead of a per-row
fingerprint, and refreshing it is an operator's command or a cron entry, run
after `usher index --backfill`. The read form says which of the two empty
answers you are looking at: "no neighbours for this title" and "no neighbours
have ever been computed" have different fixes. Neither form loads a model —
the rebuild reads stored vectors — so both start in about a tenth of a second.

**The server process already runs a worker lane**, and a push lane per
enabled source, so a normal deployment needs neither command. They are for
splitting the lanes across containers, and there is one rule: **do not run
`usher work` against a server that also has `USHER_WORKER_ENABLED=true`.**
A worker requeues everything left `running` at startup — correct at exactly
one worker, and at two it steals the other's live claims. Set
`USHER_WORKER_ENABLED=false` on the server first.

```bash
uv run usher push --probe                    # open each source's channel, report what arrived
uv run usher push --probe --source "Living Room Emby"
uv run usher push                            # run the lanes in the foreground, no HTTP server
```

`--probe` reports **messages received**, never that the handshake succeeded:
a WebSocket handshake against a nonexistent path also upgrades and also
receives traffic, so "it connected" is not a health signal.

Exit codes: `0` success, `1` a malformed id or an unhandled error, `2` any
argument error (including `--resolve` without `--title`).

## Attribution

This project ships importers, never data. Each deployment downloads its own
datasets and holds its own API keys.

- Information courtesy of IMDb (https://www.imdb.com). Used with permission.
- This product uses the TMDB API but is not endorsed or certified by TMDB.

## License

MIT
