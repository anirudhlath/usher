# Usher

A self-hosted media catalog backend. Usher maintains its own canonical database
of film and television, treats media servers (Emby first) as interchangeable
*sources* that answer "where can this be played?", and exposes an API rich
enough to build a full media browser against.

Design documentation lives in [`docs/prd/`](docs/prd/README.md).

## Status

Pre-release. Milestones M1 (foundation), M2 (catalog bootstrap), M3 (Emby
adapter), M4 (ingest pipeline), M5 (push and read-through), M6 (search),
M7 (rows and recommendations), M8 (LLM curation) and M9 (the API surface) are
complete — see [`docs/plans/`](docs/plans/) for the task breakdowns and
[`docs/prd/09-roadmap.md`](docs/prd/09-roadmap.md) for what's next.

M3, M4 and M5 are each verified against a live Emby server, and M4's metadata
half against the live TMDb API. M5's run is the first in this repository to
have parsed a real `/embywebsocket` message. **M6's typo-tolerance gate ran on
2026-08-03 against a real 1,271,138-title catalog and failed** — short names
are the weak band and no configuration comes close to an as-you-type latency
budget. The result is recorded with its numbers, and the follow-up it obliged —
a two-tier suggest — **shipped in M9**
([ADR-0002](docs/prd/decisions/0002-postgres-first-search.md),
[ADR-0031](docs/prd/decisions/0031-the-two-tier-suggest.md)).

**M9's own live verification of playback and watch write-back ran on
2026-08-12, and both halves passed.** `POST /titles/{id}/play` → a minted ticket
→ `GET /stream/{ticket}` → `302` → a **real `206` with real bytes** from the
source, with the play body's leak check proven by a positive control before its
absence was believed; and the watch write-back driven through the shipped routes
and a real worker pass, read back **from Emby**, then restored **byte-for-byte**.
Twenty-three bounded requests, no walk. ⚠️ It ran *after* the milestone closed:
M9 had recorded it as an unrunnable gap on the strength of checking one `.env`
file, and the credentials were in a secrets file one directory over.
[`docs/prd/09-roadmap.md`](docs/prd/09-roadmap.md) carries the result. It
matters because M3, M4 and M5's live runs each found something their fakes
agreed with and reality did not — including Emby's watch-state write-back route
being simply wrong.

**M7 composes a home screen**: nine row providers, scored and diversified
server-side, plus the taste centroid, the MovieLens tag genome as a third
similarity signal, and `Person`/`Credit`/`Collection` re-derived from the
payload cache with no second network call. **M8 adds the tenth provider** —
`CuratedProvider`, hydrating rows a language model chose from a candidate pool
this server built, validated against that pool before anything reaches a
screen.

**M8's live verification refuted two things this project had written down, and
both are recorded rather than quietly fixed.** Query expansion — named in the
PRD as the cheaper, better-evidenced lever for mood queries since M1 — measured
*worse* (MRR 0.733 → 0.373), so it ships behind its own setting, off by
default. And **88% of the row headings one live run generated (52 of 59) were
the genre labels the prompt explicitly forbids**, which means that on the model
tested, a curated shelf is substantively what the free genre-affinity row
already produces. One model, one evening — but the design consequence is
general: **the prompt's grouping instruction is not self-enforcing and nothing
in the system checks it.** Curated rows are additive, so the home screen is
correct without them.

**The HTTP surface was deliberately small for eight milestones, and M9 is the
one that finished it** — 38 operations across
[PRD 07](docs/prd/07-client-api.md)'s five tables, all of them answering through
one error envelope:

- **Screens** — `GET /home`, `GET /search`, `GET /search/suggest?tier=`,
  `GET /browse`.
- **Resources** — `GET /titles/{id}` (now carrying `cast`, `crew` and `images`),
  `GET /titles/{id}/similar`, `GET /episodes/{id}`,
  `GET /series/{id}/seasons`, `GET /seasons/{id}/episodes`,
  `GET /people/{id}`, `GET /collections/{id}`, `GET /images/{id}`.
- **Actions** — `POST /titles/{id}/play` and `POST /episodes/{id}/play`,
  `GET /stream/{ticket}`, `PUT /watch/titles/{id}`, `PUT /watch/episodes/{id}`,
  `POST`/`DELETE /watch/titles/{id}/played`.
- **Admin** — the `/admin/sources` routes plus `POST /admin/sources/{id}/sync`,
  `GET /admin/unmatched` and `POST /admin/unmatched/{id}/resolve`,
  `GET /admin/bootstrap/status` and `POST /admin/bootstrap/{phase}`,
  `GET`/`PUT /admin/rows/providers`, `POST /admin/rows/regenerate`.
- **Meta** — `/health`, `/health/ready`, `GET /meta/attribution`, `/events`
  (SSE), and `/openapi.json`.

**Every failure is an RFC 9457 problem document** —
`application/problem+json`, with a `code` from a **closed seven-member
vocabulary** that [ADR-0030](docs/prd/decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md)
encodes and a test parses back out of the ADR, so a route cannot invent an
eighth. `/events` and `/health/ready` are the two exemptions, and both are
asserted rather than skipped. Paging is **keyset only, never an offset**
([ADR-0034](docs/prd/decisions/0034-the-cursor-carries-a-position.md)):
`GET /home` still returns the whole screen in one response with no cursor at
all, which is what
[ADR-0006](docs/prd/decisions/0006-server-composed-home.md) specifies.
Playback hands back a short-lived opaque ticket that `302`s to the real target
([ADR-0029](docs/prd/decisions/0029-the-playback-ticket-changes-the-artifact-not-the-grant.md)),
so the shareable artifact is opaque rather than a URL with somebody's session
token in it. Everything the API does is also driven from the command line —
see below.

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
mkdir -p data/images && sudo chown 1000:1000 data/images
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

The `chown` is the one line that is not obvious, and it is the image proxy's.
`./data/images` is bind-mounted to `/data/images` and is where
`GET /images/{id}` caches artwork it has fetched. **Docker creates a missing
bind-mount source as `root`**, the container runs as uid 1000, and a bind
mount's host-side ownership wins over the `chown` in the Dockerfile — so
without this the proxy answers 500 on every cold image and nothing else in the
stack notices. Skip it if `data/images` already exists and you own it. There is
no eviction and none is wanted: the width ladder bounds the cache at four
entries per image, and reclaiming space is `rm -rf data/images`, which costs a
re-fetch and nothing else.

**Every key in `.env` reaches the container**, because compose hands it the
whole file (`env_file:`). The five exceptions are marked `[compose-owned]` in
`.env.example` and listed in `compose.yml`'s `environment:` block with the
reason each belongs to the container topology rather than to you:
`USHER_DATABASE_URL` (the hostname on the compose network),
`USHER_HOST`/`USHER_PORT` (what the published port, the `EXPOSE` and the
healthcheck all assume), `USHER_SECRET_KEY` (substituted so a missing one
fails at `docker compose up` rather than in a container log) and
`USHER_IMAGE_CACHE_DIR` (the container side of the bind mount above — the
`.env` value is a relative path, which inside the container would put the
cache in the image's own writable layer).

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
uv run usher --traceback <command>   # the full stack, when one line is not enough
```

When something an operator can fix goes wrong — the database is not up, a
`.env` value is wrong, a source or the LLM endpoint is unreachable, a
credential is rejected, an upstream asks to be backed off — every command
exits 1 with one line rather than a stack:

```
usher bootstrap-status: ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 5432)
(the stack is one flag away: `usher --traceback bootstrap-status`)
```

A *bug* still gets its full traceback, deliberately. And a rejected setting is
reported by name without its value, because a setting may be a credential.

**Populate the catalog** — pulls IMDb's `title.basics`/`title.ratings` dumps,
TMDb's *public daily id export* files (no API key needed for these) and
Wikidata's SPARQL endpoint. About three minutes and 1.27M titles on a
reasonable machine for those three; the two IMDb expansion phases below read
another 1.49 GiB and take rather longer. Resumable: kill any phase and re-run,
and it continues from its own checkpoint. See
[`docs/prd/04-catalog-bootstrap.md`](docs/prd/04-catalog-bootstrap.md).

```bash
uv run usher bootstrap                       # every phase, in the order below
uv run usher bootstrap --phase imdb          # one at a time: imdb | credit-names | aliases
uv run usher bootstrap --phase credit-names  #     | tmdb-ids | crosswalk | movielens | ratings | all
uv run usher bootstrap --phase aliases       # IMDb title.akas -> searchable aliases, ~487 MB
uv run usher bootstrap --phase movielens     # the MovieLens tag genome (M7), ~335 MB, ~10 min
uv run usher bootstrap --phase ratings       # IMDb ratings alone, ~8 MiB -- see below
uv run usher bootstrap-status                # progress per dataset, and catalog size
```

**`credit-names` and `aliases` are the two IMDb phases M9 added, and they cost
no API call at all.** `credit-names` joins `name.basics` to
`title.principals` in the importer and fills `titles.credit_names`, which is
weight class B of the search document — **1,192,217 of 1,271,138 titles
(93.8%)** gain a mean of 9.11 names. `aliases` reads `title.akas` and fills
the `alias` half of `title_search_names` — **1,663,364** aliases over
**399,046 titles (31.4%)**, each with its `region` and `language`. Both refuse
an empty catalog and say which phase to run first, both are resumable from
their own checkpoint, and together they add **1.49 GiB** to what a
`--phase all` downloads. Neither writes a `people` or a `credits` row: that
design measured 2.702 GB against a 2.0 GB ceiling and was refused
([`04`](docs/prd/04-catalog-bootstrap.md)).

⚠️ **Run `credit-names` before the TMDb enrichment crawl, not after.** It
writes only where `enrichment_state = 'skeleton'`, so TMDb keeps every title
it has already reached — which means a title you crawl first is one this fill
declines for good, on that run and on every later one. Measured: **203,969 of
the 204,335 titles with ≥100 votes (99.82%)** gain a `credit_names` when the
fill runs first, and none of them when it runs last. It **stales no
embedding** in either order — only non-skeletons are embedded, and only
skeletons are filled — so the cost of getting the order wrong is missing
names, not a re-index.

**After either phase, re-index — and here is the measured number of
embeddings it invalidates.** `usher index --backfill`, then `usher similar
--rebuild` once the index jobs have run; this is the same freshness gap
`usher similar` documents below, arriving at a much larger population. On a
freshly bootstrapped catalog the count of newly-stale embeddings is
**zero** — every title `credit-names` can touch is a `skeleton`, and skeletons
are never embedded — so the obligation is real but the bill is nil until the
catalog has been enriched. `usher index` prints the stale count either way, so
the number is checkable rather than taken on trust.

**`ratings` is an alias rather than a step, and `--phase all` never dispatches
it.** A full run already imports `title.ratings.tsv.gz` inside its IMDb arm;
this member exists for the *refresh*, against a catalog that is already
serving. `--phase imdb` would pull **214.4 MiB** of `title.basics.tsv.gz`
first and rewrite every name and year, and a changed name stales that title's
embedding — so refreshing ratings that way buys a re-index nobody asked for.
`--phase ratings` reads **8.2 MiB** and touches two columns nothing embeds. It
checkpoints against the same `imdb.title.ratings` row `--phase imdb` uses, so
the two cannot disagree about which revision this catalog holds — which also
means a completed run at an unchanged upstream revision resumes at the end and
writes nothing (delete the checkpoint first if that is not what you wanted).

`movielens` runs **last** under `--phase all`, and refuses outright against an
empty catalog: the genome joins `titles` on `imdb_id`, so there is nothing to
join to until the IMDb phase has run. It downloads `ml-latest.zip` and reads
three of its seven members — the only archive that has a genome *and* a
licence permitting redistribution, which is why the phase names it. It stores
one dense 1,128-lane vector per matched title **and** the 1,128 tag names that
say what those lanes mean, both stamped with the archive revision they came
from, so a vocabulary can never label a vector from a different release.

A catalog bootstrapped before the vocabulary shipped just needs the phase
re-run — it resumes from its completed checkpoint, writes no vector, and loads
the words. `usher bootstrap-status` says which state a deployment is in:

```text
titles in catalog: 1271570
genome vectors: 15565
genome vocabulary: 1128 tags
```

All three lines are **one** catalog — the 1,271,570-title bootstrap the genome
phase was measured against on 2026-08-04. (The 1,271,138 quoted for search
further up is a different, earlier catalog; both figures carry their date
wherever they appear, because a terminal block mixing two is a reading no
single run produces.)

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

**Four of the six job kinds are claimed only by a process configured for
them**, and a worker that is not simply leaves them for one that is rather
than failing them: `enrich` and `derive` need `USHER_TMDB_API_KEY`, `index`
needs the `embedding` extra, and `curate` needs `USHER_LLM_ENABLED=true`.
Only `match` and `watch_history` are registered in every build.
The last one is the one to know about before you turn it on — a `curate` job
is where this project spends money, one completion per household per run,
against whatever `USHER_LLM_BASE_URL` names.

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

`usher search` and `usher suggest` are the read side, and M6 added no HTTP
route — the CLI delivered the whole capability, exactly as `bootstrap` and the
ingest commands do. **M9 shipped the routers** (`GET /search`,
`GET /search/suggest?tier=`) and the CLI is still the second composition root
rather than a thin client of them: both build the same `SearchService`.

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

**Query expansion is built, is off by default, and the default is a
measurement.** With `USHER_QUERY_EXPANSION_ENABLED=true` (which also needs
`USHER_LLM_ENABLED=true`, and is refused at startup without it), a semantic or
fused search first spends one completion rewriting the query into the language
a synopsis is written in, and prints what it embedded:

```
$ uv run usher search "movies about isolation in space"
expanded: a lone crew adrift, silence, deep-space confinement, psychological drift
  1   0.7000  ...
mode=fused results=12 semantic_coverage=0.884
```

🔴 **Measured on 2026-08-07, it made retrieval worse, which is why it is a
second switch rather than part of the first.** Against a local
`gemma-4-26b-a4b`, over five mood queries and the 150 most-voted titles' real
overviews, expansion moved MRR **0.733 → 0.373** and recall@10 **0.800 →
0.533**; the typed query won four of the five queries and tied the fifth. The
rewrites drift toward generic critic prose, which sits near the middle of a
corpus of synopses — measured directly, the five queries became *more like each
other* after rewriting (mean pairwise cosine 0.5417 → 0.5975). One model, one
150-document corpus, five queries: thin, and it is the only measurement there
is. Turn it on to try it against your own model; `llm_calls` grouped by
`purpose` is what it cost.

The `expanded:` line is not optional decoration — a viewer who searched for one
thing and got results for another cannot tell a good rewrite from a bad one
without seeing it. It appears only when a completion actually produced one, so
on the default deployment the output is unchanged and no completion is bought.
Neither is one bought by `--mode full_text`, by a blank query, by a deployment
with no embedding model, or by `usher suggest` — type-ahead has no semantic
lane, which is what keeps this off the path a client drives per keystroke.
Every attempt lands in `llm_calls` with `purpose = 'query_expansion'`,
including the ones that failed; an unreachable endpoint or an unusable answer
leaves the search to run on the words you typed **and is still billed**, so an
absent `expanded:` line says nothing about whether money was spent.

⚠️ **Run `usher index --backfill` before turning it on.** The guard in front of
the completion is *"this deployment has an embedding model"*, not *"anything is
actually embedded"* — so with a model installed and nothing indexed yet, every
fused search buys a rewrite and then reports `semantic_coverage=0.000`.

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
0/0. Against an empty household it exits 0 and prints ten providers that
proposed nothing — ten since M8 registered `CuratedProvider`, which on an empty
database is a household whose nightly generation has never run.

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

`usher curate` runs one LLM generation for the default household and prints
what it bought.

```bash
uv run usher curate                # one completion, one generation, one report
```

```
generation: 019fdbeb-6858-79b6-9c6e-1d5654baef71
pool: 200 candidates
kept: 2 rows, 11 cards
  curated-1     Slow-burn sci-fi for a rainy night                5 cards
  curated-2     Quietly devastating, quietly funny                6 cards
dropped (all five reasons, zeros included -- an absent line and a
         reason nobody counts read the same):
  not_in_pool        1 card
  unparseable        0 cards
  duplicate          0 cards
  row_unusable       0 rows
  row_too_short      0 rows
tokens: 4812 in, 391 out   cost: $0.00042100   latency: 2314 ms   model: served/qwen3-30b-a3b
```

**Illustrative, not a measurement**: the layout is a real run's, captured from
`tests/integration/test_cli_pipeline.py`'s fixtures, with the pool at the
shipped `USHER_CURATION_POOL_SIZE` default of 200 and a scripted completion
standing in for a model's. Only the usage line is that fixture's verbatim —
**the two rows, the eleven cards, the `not_in_pool 1` and the model name are
invented**, so read the shape and not the numbers.

**The real ones, from the live verification on 2026-08-07** against a local
vLLM serving `gemma-4-26b-a4b` over a real 1,271,138-title catalog: a pool-200
prompt is **4,304 tokens cold** and **4,359** with three lines of watch history
(**~20.4 tokens a candidate**, ~18 a history line), output runs **192–277**
tokens (median 219.5), latency **1,230–1,787 ms** (median 1,420), and over 20
generations **not one of 405 identifiers fell outside the pool**. Only
`row_too_short` ever fired of the five drop reasons — the other four are close
to unreachable when the endpoint honours the JSON schema, so a report of
zeros like the one above is the system working. ⚠️ One model, one evening;
none of those numbers is a property of "an LLM".

It takes no arguments at all. The household is the singleton default user that
stands in for authentication — **still, after M9, which deliberately did not
build it** (its boundary call 1: designing authorization against routes landing
in the same milestone is the mistake the error envelope was deferred four times
to avoid). So a `--user` flag would be an id nobody can look up on a deployment
that has exactly one.

**It is one of three surfaces onto the same `CurationService`** — the other
two are `POST /admin/rows/regenerate`, which enqueues a `curate` job and
answers 202, and `usher work`, which claims it. This is the only one that
reports the answer, which is what a command that spends money owes: a 202
says nothing about what the completion returned.

**All five drop reasons print every time, zeros included.** A reason absent
from a report is indistinguishable from a reason nobody counts, and at a
terminal there is no second export to compare against. Two of them count
*rows* (`row_unusable`, `row_too_short` — the `row_` prefix says so) and
three count *cards*, so summing across them means nothing. `not_in_pool` and
`unparseable` produce the same empty screen and have opposite fixes: the
first is the model inventing a candidate (look at the prompt, the
temperature, the pool size) and the second is a shape the reader could not
use at all (look at `response_format` and the schema).

Three things make it exit 1 with a sentence instead of a report, and none of
them is a stack:

- **`USHER_LLM_ENABLED=false`** — there is no client, so there is no service
  to build. Unlike `GET /home` (a shorter screen) and `usher work` (five
  other job kinds), this command has exactly one job, so it says so rather
  than exiting 0 having done nothing.
- **An empty candidate pool** — which in practice means an empty catalog.
  ⚠️ *"A household that has watched everything"* is the other reading and it is
  a far smaller door than it sounds: the pool is
  `SELECT … FROM titles WHERE NOT watched`, with no enrichment, ownership or
  availability filter, so "everything" is every row in `titles` — after
  `usher bootstrap --phase all`, 1.27M of them. Measured 2026-08-07 against a
  migrated but empty database: `usher curate` refuses; insert **one** unwatched
  title and the same command reaches the model instead. Nothing is attempted
  and **nothing is billed**: this is the one path in the whole milestone that
  writes no `llm_calls` row.
- **A generation that validated to nothing** — the call worked, the money is
  spent, and the message is the tally (`not_in_pool=5, row_too_short=1`).
  Numbers and label names only; nothing the model wrote reaches the screen.

In all three the household's previous rows still stand, and the two that reach
the service say so — a curated screen that has not changed since last night
otherwise looks identical to one that was just replaced. The disabled
deployment carries no such clause, because it never had a generation to
replace them with.

**The message does not say "nothing was written", and that is deliberate.**
Only the empty pool writes nothing at all; the other two are billed, and a
sentence claiming otherwise would tell an operator they were not charged on
the one path where they were. What was or was not written to `llm_calls` is a
question `llm_calls` answers.

An endpoint that is down, rate-limiting or refusing the key is a fourth
sentence and not a stack, but it comes from the CLI-wide boundary above rather
than from this command, so it names the endpoint instead of the screen —
**and it is still billed**, exactly like the other two that reach the model.

### Configuring the model, and running it nightly

Every one of these is in `.env.example` with its own reason. `USHER_LLM_ENABLED`
is `false` by default, so none of the rest does anything until it is `true`.

```bash
USHER_LLM_ENABLED=true
USHER_LLM_BASE_URL=http://localhost:8000/v1    # any OpenAI-compatible endpoint
USHER_LLM_MODEL=gpt-4o-mini                    # recorded on every llm_calls row
USHER_LLM_API_KEY=...                          # omit for a local endpoint
USHER_LLM_MAX_OUTPUT_TOKENS=2048               # a correctness ceiling, not a cost one
USHER_LLM_TIMEOUT_SECONDS=120
USHER_LLM_PRICE_IN_PER_MTOK=0                  # dollars per million tokens
USHER_LLM_PRICE_OUT_PER_MTOK=0                 # 0 is honest for a local model
USHER_CURATION_POOL_SIZE=200                   # candidates in one prompt
```

**The two price settings are the ones that silently do the wrong thing.** No
OpenAI-compatible endpoint reports cost — `usage` carries token counts and
nothing else — so `cost_usd` is computed from these two and written onto the
row, which means a later price change cannot rewrite history and an unset price
gives you a cost dashboard reading zero. The mitigation is that `tokens_in` and
`tokens_out` are recorded exactly, so spend is recomputable from `llm_calls`
after the fact.

⚠️ **`USHER_CURATION_POOL_SIZE` and `USHER_LLM_MAX_OUTPUT_TOKENS` spend one
budget and nothing couples them.** The endpoint's constraint is
`prompt_tokens + max_output_tokens ≤ its context window`, so raising the
output ceiling lowers the pool you can actually send — and the failure is an
HTTP 400 that **parks** the job rather than a warning at startup. Measured
against a 16k-context model at the shipped defaults: **600 candidates works,
700 and 1,000 both fail.** The setting's ceiling of 1,000 is a bound on
arithmetic no endpoint can satisfy, not a promise that your endpoint will serve
it.

**Nothing schedules the nightly generation.** There is no scheduler in Usher —
deliberately, the same call `usher similar --rebuild` gets — so it is a cron
entry:

```cron
# One curation generation a night, after the queue has drained.
30 4 * * *  cd /srv/usher && /usr/local/bin/usher curate >> /var/log/usher-curate.log 2>&1
```

Run it in a process that has the settings above. A generation costs one
completion per household; `llm_calls` is what it cost and `curated_rows` is
what you got.

⚠️ **A night that produced no rows exits 1, so cron will mail you about it**,
and that is deliberate rather than overlooked. Measured 2026-08-07 against a
migrated but empty database: `home`, `bootstrap-status`, `sync-status`, `derive`
and `index` all exit **0**, and `curate` exits **1**. The five that exit 0 are
*reporting* commands in their bare form — `derive` and `index` say so in their
own docstrings, "the bare form only reads" — and a report of nothing is still a
report. `curate`'s bare form is the one that writes, and a write command that
wrote nothing has not succeeded: exiting 0 would tell this cron entry that
curation is running on a deployment whose catalog was never bootstrapped, which
is by far the likeliest way to reach an empty pool (see above). If you would
rather hear only about the failures you intend to act on, that belongs in the
cron entry — append `|| true`, or filter the log — rather than in the command,
since the exit code is the only thing a generation that did not happen has to
say for itself.

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
splitting the lanes across containers.

Running `usher work` beside a server with `USHER_WORKER_ENABLED=true` used to
be a **correctness** rule — a worker requeued everything left `running` at
startup, so at two workers each stole the other's live claims. It is not any
more: recovery takes back only claims nobody has heartbeated for
`USHER_JOB_LEASE_SECONDS`, so two workers coexist safely. What two workers
still do is spend the same upstream budget twice: `USHER_JOB_CONCURRENCY` and
`USHER_TMDB_REQUESTS_PER_SECOND` are both **per process**, so N processes are N
times the configured limit against a rate limit that is per client. Set
`USHER_WORKER_ENABLED=false` on the server, or divide the two settings by the
number of processes.

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

**`GET /meta/attribution` is where a client gets these strings**, and it is the
surface a client should render from rather than this section — PRD 04's hard
rule 4 is that the API exposes them so every client can display them, and until
M9 that route was named in three documents and served by nothing. It answers a
list of four `{source, text}` entries, one per dataset this project can import,
in PRD 04's own licensing-table order — pinned by a test, because a licensing
surface's response bytes should be deterministic:

```bash
curl -s http://localhost:8100/meta/attribution
```

It is **static and deliberately not filtered by what this deployment has
actually imported**. `import_runs` could answer that, and the answer would be
wrong in the direction that matters: on a fresh install it is empty, so a
licence string would be withheld from exactly the deployment most likely to be
rendering freshly imported data. Over-display costs a client one citation too
many; under-display is a licence breach. TMDb's table row also asks for a logo,
which a string cannot carry and this project does not ship — that half stays a
client obligation.

The four, reproduced here for a reader who is not running the service:

- **IMDb** — Information courtesy of IMDb (https://www.imdb.com). Used with
  permission.
- **TMDb** — This product uses the TMDB API but is not endorsed or certified by
  TMDB. Data from The Movie Database (https://www.themoviedb.org).
- **Wikidata** — ID crosswalk from Wikidata (https://www.wikidata.org),
  available under CC0 1.0.
- **MovieLens** — F. Maxwell Harper and Joseph A. Konstan. 2015. The MovieLens
  Datasets: History and Context. ACM Transactions on Interactive Intelligent
  Systems (TiiS) 5, 4: 19:1-19:19. https://doi.org/10.1145/2827872

## License

MIT
