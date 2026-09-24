# Usher

A self-hosted media catalog backend. Usher maintains its own canonical database
of film and television, treats media servers (Emby first) as interchangeable
*sources* that answer "where can this be played?", and exposes an API rich
enough to build a full media browser against.

Design documentation lives in [`docs/prd/`](docs/prd/README.md).

## Status

Beta. Milestones M1 (foundation), M2 (catalog bootstrap), M3 (Emby
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
a two-tier suggest — **shipped in M9**.

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
vocabulary** that `ProblemCode` holds and a test closes against every code the
routes emit, so a route cannot invent an eighth. `/events` and `/health/ready`
are the two exemptions, and both are asserted rather than skipped. Paging is
**keyset only, never an offset**: `GET /home` still returns the whole screen in
one response with no cursor at all. Playback hands back a short-lived opaque
ticket that `302`s to the real target, so the shareable artifact is opaque
rather than a URL with somebody's session token in it. Everything the API does
is also driven from the command line — see below.

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

### ⚠️ Nothing here requires authentication

**No route in this API checks a credential, and twelve of them are `/admin`.**
There is no auth module, no token check and no `current_user` anywhere in the
tree. Anyone who can reach the port can list your sources, resolve unmatched
items, and start work.

Two of those twelve are expensive rather than merely readable.
`POST /admin/bootstrap/{phase}` and `POST /admin/sources/{id}/sync` put the two
longest units of work in the system onto the single sequential worker lane —
enrichment, indexing, derivation and curation are unavailable for the duration,
which is hours in the sync case. `POST /admin/rows/regenerate` enqueues a
curation job, which is where this project spends money on an LLM.

**This is a posture, not an oversight.** Usher is a self-hosted backend whose
threat model is a home network, and authorization designed against routes
landing in the same milestone is a guess at a client that does not exist yet.
It is a recorded boundary call, and
[#18](https://github.com/anirudhlath/usher/issues/18) is where an auth mode for
`POST /admin/sources` is tracked.

**So: do not publish this port to the internet.** Bind it to your LAN, or put
it behind whatever already fronts your other services — a reverse proxy with
authentication, a VPN, or an SSH tunnel. That decision belongs before the
`docker compose up` below, which is why this paragraph is above it.

## Quickstart

Seven steps from clone to the first rows on `/home`. The `usher` commands run
inside the container through `docker compose exec`; the rest (`cp`, `openssl`,
`mkdir`, `curl`) are plain host commands. None of them is shell-specific, so
every line works as written in bash, zsh or fish.

🔴 **Read this first: it is not a five-minute path, and the long poles are
steps 5 and 6.** The times below were measured on 2026-09-23 and 2026-09-24.
Steps 1 to 3 take about ten minutes, most of it the crosswalk.
Syncing and enriching a real library take **hours**, because both are paced
by somebody else's server rather than by yours. That run never reached an
enriched home screen, so nothing below claims one.

**1. Configure and start.** See [Running it](#running-it) for what each line is
for — especially the `chown`, which has the best paragraph in this file.

```
cp .env.example .env
openssl rand -hex 32          # paste into USHER_SECRET_KEY= in .env
```

⚠️ **Put your TMDb key in `.env` now, before `docker compose up`, as
`USHER_TMDB_API_KEY=`.** Compose reads `.env` when it creates the container, so
a key added after `up` never reaches it — re-run `docker compose up -d` if you
add or change it later. It is listed under [Requirements](#requirements) and it
is easy to skip, because nothing fails without it. The server logs
`no TMDb API key configured; enrich and derive jobs will not be claimed` once at
startup, and step 6's `enrich` queue then never moves.

⚠️ **Already running Usher on this host? Separate the second stack before its
first `up`.** Put these three lines in the second checkout's `.env`, with names
of your own:

```dotenv
COMPOSE_PROJECT_NAME=usher-scratch
USHER_COMPOSE_NETWORK=usher-scratch_default
USHER_COMPOSE_HOST_PORT=8101
```

and read `8100` below as `8101`. All three matter. **The project name is the
one that destroys things**: compose names a project after its directory, a
clone left at `git clone`'s default is called `usher`, and an `up` from a
second checkout with the same project name recreates the first stack's
containers with the second one's config. The network name is pinned
(`usher_default`), so a second stack that keeps it joins the first one's
network, **both `postgres` containers answer to the alias `postgres`**, and the
CLI reaches whichever DNS picks. The symptom is `database "usher" does not
exist` from `usher bootstrap` while `psql -d usher` works fine. `-p <name>`
separates the project too, but not the network, and it has to be on every
compose command; `.env` is read by all of them.

```
mkdir -p data/images data/bulk && sudo chown 1000:1000 data/images data/bulk
docker compose up -d --build  # 26 s the first time, 8 s the second
```

**2. Check it is up.**

```
curl -sf http://localhost:8100/health/ready
```

**3. Load a catalog — three phases, and the third is not optional.**

```
docker compose exec usher usher bootstrap --phase imdb       # 93 s, 1,279,749 titles
docker compose exec usher usher bootstrap --phase tmdb-ids   # 17 s
docker compose exec usher usher bootstrap --phase crosswalk  # 491 s, four retries included
```

🔴 **`--phase imdb` alone is not enough, and the failure is silent until step
6.** IMDb gives you titles with no TMDb id, and enrichment has nothing to
enrich *from*: every job parks with `title carries no tmdb id to enrich from`,
and `/home` never gets past the rows that need no enrichment (step 7).
`tmdb-ids` and `crosswalk` are what make the catalog enrichable. A complete
crosswalk on 2026-09-24 took 491 s and gave 293,665 titles a TMDb id, 55,590 of
them series.

⚠️ **Check that every phase finished.** The crosswalk reads Wikidata's public
SPARQL endpoint page by page, and that endpoint times out and answers `502`
often. A page that fails that way is retried from its checkpoint with backoff,
up to five attempts and 15 minutes; each retry prints a line and the command
carries on, and the 2026-09-24 run above retried four times. A phase that still
fails, or is skipped because an import it reads failed, makes the command exit
1, and its line ends with the command that resumes it. Check that every row
reads `completed`:

```
docker compose exec usher usher bootstrap-status
```

Re-run a phase whose row reads `failed` **before step 5**. If you run it
afterwards, the enrichment jobs that already parked for want of an id stay
parked, because nothing un-parks a job yet
([#87](https://github.com/anirudhlath/usher/issues/87)).

This still skips the IMDb expansion phases and MovieLens — see
[Command line](#command-line) for `--phase all`, which is **3–5 hours**, mostly
the TMDb crawl.

**4. Register a source.** There is no CLI subcommand for this — it is the admin
API, and the credentials are encrypted at rest with `USHER_SECRET_KEY`.

```
curl -sf -X POST http://localhost:8100/admin/sources \
  -H 'content-type: application/json' \
  -d '{"kind":"emby","name":"Living Room","base_url":"https://emby.example.com","username":"YOUR_USER","password":"YOUR_PASSWORD"}'
```

⚠️ That route requires no authentication, like every route here — read the
posture under [Requirements](#requirements) before exposing this port.

**5. Walk the source. This is the long pole.**

```
docker compose exec usher usher sync --source "Living Room"
```

🔴 **Budget hours, not minutes, and there is no bound flag.** Usher is a polite
guest: outbound requests are rate-limited on purpose, so the walk is paced by
your media server. Measured on 2026-09-23: about **25,000 items in the first
10.5 minutes** and **81,000 in 35 minutes**, when the run was stopped, so it
never timed a whole library. A small library is proportionally quicker. Run it
in a terminal you can leave. If it is interrupted, it resumes rather than
restarting.

⚠️ **It prints nothing while it runs.** Watch it from a second terminal:

```
docker compose exec usher usher sync-status
```

`seen=` and `matched=` climb on the run marked `running`.

**6. Let the server enrich what you ingested.** There is nothing to start. The
server already runs a worker lane (`USHER_WORKER_ENABLED=true` is the default),
and with the key from step 1 it has been matching and enriching since step 5
began. Watch the queue drain:

```
docker compose exec usher usher sync-status
```

The `enrich` line's `pending=` falls as titles are enriched, at **one
rate-limited TMDb call per title**, so this is hours on a large library too.
Watch `parked jobs:` as well. A parked `enrich` whose error is
`title carries no tmdb id to enrich from` belongs to a title step 3 did not
link, and it stays parked (#87).

⚠️ **Do not start `usher work` beside the server.** A second worker spends
the per-process TMDb budget twice against a limit that is per client (see
[Command line](#command-line)). `usher work --once` does not drain the queue
either: it claims one batch and exits. On 2026-09-23 that was 20 jobs in 47 s.

**7. Ask for a screen.**

```
curl -sf http://localhost:8100/home
```

**The first row back proves the source walk, not the whole path.** It can be
**Recently Added**, which needs only a sync, a match and library items added in
the last 30 days. It needs no TMDb key, no crosswalk and no
enrichment, and its cards can be bare skeleton titles. On 2026-09-23 it was
the only row, first seen 10½ minutes into step 5, with a skeleton series
on it.

**A working path looks like this:**

- `bootstrap-status` shows every row `completed`.
- `sync-status` shows the walk `completed`, and the `enrich` line's `pending=`
  falling while `parked jobs:` stays small.
- `/home`'s cards move from `"enrichment_state": "skeleton"` (a bulk-dataset
  title with no overview or artwork) to `"enriched"`, which is step 6's work.

The history-driven rows (Continue Watching, Next Up, Because You Watched) wait
for the watch-state walk, which runs only after the item walk completes.

An empty `rows` array with a `200` is not an error: no row has anything to
show yet. Early on, the walk has usually matched nothing added in the last 30
days and the watch-state walk has not run. Rows built from enriched metadata
also wait on the TMDb key (step 1), a completed crosswalk (step 3) and step 6.
The console is at <http://localhost:8100/console>.

## Running it

Tagged releases publish a container image to
`ghcr.io/anirudhlath/usher`, built and attested by
[`.github/workflows/release.yml`](.github/workflows/release.yml) on any `v*`
tag. **`linux/amd64` only** — there is no arm64 machine to test on here, and
publishing an emulated image nobody has ever started would be a guess.

```bash
cp .env.example .env
openssl rand -hex 32          # paste this into USHER_SECRET_KEY= in .env
mkdir -p data/images data/bulk && sudo chown 1000:1000 data/images data/bulk
docker compose up -d --build

curl -sf http://localhost:8100/health        # {"status":"ok","version":"0.1.0"}
curl -sf http://localhost:8100/health/ready  # adds database + migration state,
                                             # and reports the background lanes
```

Then open **<http://localhost:8100/>**, which redirects to the console.

## Usher Console

The web client ships in this repository, in `web/`, and is served by the same
process as the API — one container, one origin, no reverse proxy. It has two
halves for the same person: a **viewer** for browsing, searching and playing,
and an **operator** console for connecting a media server, running imports,
draining the review queue and reading metrics.

It lives at **`/console`** rather than at `/`, and that is not a preference.
All seventeen routers are mounted with no prefix, so the API already owns
`/titles`, `/search`, `/home`, `/browse`, `/admin`, `/stream` and thirteen more
root segments — a client-side route for a title detail page would collide with
the operation that answers it. Giving the API an `/api` prefix instead would be
a breaking change to a public contract for the benefit of the client generated
from it. Plex and Emby both put their web app on a subpath for the same reason.
`GET /` redirects, so the bare host still works.

**What serving it in-process buys, beyond one fewer container.** Usher mints
playback ticket URLs from the incoming `Host` header and ships no CORS
middleware. The previous client ran behind its own nginx rewriting `/api/*` to
`/*`, and a proxy that dropped the port from that header produced ticket URLs
pointing at a different service — invisibly, because a browser re-issues them
same-origin and only an external player following the `deep_link` ever noticed.
With no proxy there is no header to get wrong and no origin to allow.

Two settings, both optional, both `null` by default and both rendered as
*absent* rather than as a dead link when unset: `USHER_GRAFANA_URL` for the
Insights screen's "Open in Grafana", and `USHER_TEMPO_URL` for the "Open trace"
link on a failed request. Neither is proxied through Usher; the browser follows
them directly. `USHER_CONSOLE_ENABLED=false` turns the whole thing off, which is
the right answer for a worker-only container or a household running its own
client.

### Developing it

```bash
cd web
npm ci
npm run dev          # Vite on :5173, proxying the API to $USHER_ORIGIN (default :8100)
npm run verify       # typecheck, lint, format, unit tests, production build
npm run e2e          # Playwright at 1440 / 834 / 390, with an axe sweep at each
```

The design system it implements is a handoff bundle: tokens, 28 components in
ten groups, and 18 screens. `web/CONVENTIONS.md` is the contract between that
bundle and this codebase, and `web/docs/patterns.md` is the behavioural
authority — fifteen sections of redlines covering loading, the four absent
states, the seven-code error taxonomy, keyset pagination, confirms, 202
receipts, live data, cursor progress, keyboard, density, responsive,
accessibility and security. Read it before changing a screen.

Three of its rules are correctness rules rather than style, and are the ones
most likely to be lost: **the UI never lies about what it knows** ("we have
never computed this" is drawn differently from "we computed this and it is
empty"); **no number ships without its denominator**, which is why bootstrap
progress is a cursor and a throughput and never a percentage; and **a playback
ticket URL is a secret** — never displayed, copied, shared or logged.

A backend checkout with no `npm run build` is a normal state. The app logs the
missing bundle once and serves the API alone, so `uv run pytest` never needs
node.

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
application never sees it. `USHER_COMPOSE_NETWORK` (default `usher_default`)
is the same kind of variable, for the network's name. `USHER_COMPOSE_*` is the
one namespace reserved for variables like that; every other `USHER_*` key is a
real setting, and an unknown one is refused at startup rather than ignored, so
a typo is loud. Compose's own `COMPOSE_*` variables, such as
`COMPOSE_PROJECT_NAME`, are ignored the same way.

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
`.env.example`, and `compose.yml`'s `environment:` block replaces each with the
container topology's own value, saying why: `USHER_DATABASE_URL` (the hostname
on the compose network), `USHER_HOST`/`USHER_PORT` (what the published port,
the `EXPOSE` and the healthcheck all assume), and
`USHER_IMAGE_CACHE_DIR`/`USHER_BULK_DATA_DIR` (the container side of the two
bind mounts — the `.env` values are relative paths, which inside the container
resolve under the root-owned `/app`). `USHER_SECRET_KEY` sits in that block too
but is not an exception: it carries your own `.env` value, substituted there
only so a missing one fails at `docker compose up` rather than in a container
log.

Migrations run automatically on container start.

### Telemetry

Usher exports traces and metrics over OTLP/gRPC when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set, and builds no exporter at all when it is
empty, which is the shipped default. **Nothing in `compose.yml` needs a
telemetry stack**, so a host without one starts with a plain `docker compose
up`.

A collector in another compose stack that publishes on `127.0.0.1` alone is
unreachable from inside this container except over a shared docker network.
`compose.observability.yml` joins the `usher` service to an existing network
called `observability`. Opt in from `.env`:

```
COMPOSE_FILE=compose.yml:compose.observability.yml
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

The endpoint's host is the collector's service name on that network, and the
network must already exist (`docker network create observability`, or the
telemetry stack's own `up`). **Put `COMPOSE_FILE` in `.env` rather than
passing `-f compose.yml -f compose.observability.yml`.** Every compose command
reads `.env`, but an `up` that forgets `-f` recreates the container without
the network, and nothing fails at startup to tell you. `Settings` ignores `COMPOSE_*`
keys, so `.env` can carry them. The stack itself and the dashboards are in
[PRD 10](docs/prd/10-telemetry-and-dashboards.md#where-the-stack-lives).

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
Wikidata's SPARQL endpoint. About ten minutes and 1.28M titles for those
three, most of it the crosswalk; the two IMDb expansion phases below read
another 1.49 GiB and take rather longer. Resumable: kill any phase and re-run,
and it continues from its own checkpoint. **A phase that fails exits 1**, and
so does one skipped because an import it reads is `failed` or `running`; each
prints a line ending with the command that resumes it. See
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
whole library unavailable in one run.
Only use it for a library the operator really did remove.

`usher sync` **exits non-zero if any run it performed recorded `FAILED`**, so a
cron entry or a CI step sees a sync that did not work. Every source is still
walked first — one source's failure does not skip the others.

**Pointing Usher at a server you do not administer is an intended deployment,
not a misuse** — it is the one this project is developed against. Two defaults
assume otherwise and are documented rather than changed: the retraction ceiling
above assumes removals are yours to authorise, and marking a title played writes
to your account on that server. See
[PRD 03](docs/prd/03-sources-and-sync.md) for the distinction between a library
you own and a *view* of somebody else's.

⚠️ **A new source needs this command once, and that is deliberate.** With the
push lane on (the default) the running server holds a channel per source and
closes the gap after a reconnect with a *delta* walk — but a delta resumes from
your last completed walk, and a source that has never had one has nothing to
resume from, so that "delta" would read **every item your server has**. Usher
will not do that to a media server on its own: it logs a warning naming the
source and pointing back here, and waits for you to run `usher sync`. Once one
walk has completed, every later reconnect takes the small delta it was designed
to take. `USHER_PUSH_GAP_CLOSE` is the switch — `always` restores the old
behaviour (still warning first), `never` turns gap-closing walks off entirely,
at the cost of not seeing what changed while a socket was down until your next
sync. Full reasoning in
[08 — Operations](docs/prd/08-operations.md#starting-the-app-is-not-a-command-to-walk-your-library).

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
payloads M4 already cached — **with no second network call**. Its
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

`usher genres` normalises `titles.genres` into Usher's own vocabulary.
The column is written by two importers that share no alphabet — IMDb's bulk
phase writes `Sci-Fi`, TMDb's enrichment writes `Science Fiction` — and
`usher.domain.genres` is the map between them. `/browse` expands the two at
read time; this rewrites the column, which is what also fixes
`search_document`'s weight class D, the embedded documents and every row
provider that reads the raw array.

**It is a command rather than a migration because the vocabulary is data.** It
will grow, `canonicalise_genres` is idempotent, and re-running is free: the
write is guarded by `IS DISTINCT FROM`, so a second sweep over a normalised
catalog reports zero rows. The bare form only reads.

```bash
uv run usher genres                            # rows scanned / to rewrite / already canonical
uv run usher genres --backfill                 # rewrite, batched; prints a resume cursor
uv run usher genres --backfill --batch-size 5000
uv run usher genres --backfill --limit 100000  # bounded; resume with --after <id>
uv run usher genres --backfill --after 01a01b35-3380-77e4-909a-9588c7b1056d
```

Rewriting a genre changes segment 6 of the embedding document, so
`_FINGERPRINT_SQL` restales exactly the affected titles and the report says how
many. **Expect that number to be far smaller than the rewrite count**: the
embedded population is the enriched tier and the source spellings are almost
entirely on skeletons. Measured on a 1,272,869-title catalog: **79,913 rows
rewritten, 304 embeddings staled.** Follow it with `usher index --backfill`,
`usher work`, and — if you keep `title_neighbors` — `usher similar --rebuild`.

**Order matters after a fresh upgrade**: `alembic upgrade head` → `usher derive
--backfill` → `usher genres --backfill` → `usher index --backfill` → `usher
work`. Indexing before deriving embeds every title with an empty weight class B
and then re-claims all of them once `credit_names` is populated, which is the
wasted pass twice over; normalising after indexing re-claims the affected
titles a second time for the same reason.

Embedding is optional and off by default. The model lives behind an extra
(`uv sync --extra embedding`, 167 MiB, no torch) and `USHER_EMBEDDING_ENABLED`
gates it; without it a worker simply never claims `index` jobs, and full-text
and trigram still serve the whole catalog. `usher index` itself loads no model
— staleness is a question about a recorded model *name*.

`usher search` and `usher suggest` are the read side, and M6 added no HTTP
route — the CLI delivered the whole capability, exactly as `bootstrap` and the
ingest commands do. **M9 shipped the routers** (`GET /search`,
`GET /search/suggest?tier=`) and the CLI is still the second composition root
rather than a thin client of them: both build the same `SearchService`. **They
also now build it the same way** — the server keeps one embedding model per
process and `GET /search?mode=semantic` uses it, so the route and the command
answer the same question with the same lanes (issue #31). Until that landed
the HTTP surface was lexical-only on every deployment however it was
configured, and only `usher search` could reach the vector lane.

```bash
uv run usher search "the quiet vacuum"                 # hybrid by default
uv run usher search "vacuum" --mode full_text --limit 5
uv run usher search "vacuum" --kind movie --year-from 1990 --year-to 2030 \
                             --genre drama --owned-only --min-enrichment enriched
uv run usher suggest "the quie" --limit 5              # type-ahead, typo-tolerant
```

**`usher search` prints `semantic_coverage` on every run, not only when it is
low**, and that line is the reason the command has a human-readable mode at
all. ⚠️ **Read it as *"has the backfill drained?"*, not as *"how much of my
catalog can the vector lane see?"*** — its denominator is the enriched tier,
which excludes the skeleton titles nothing ever embeds, while the lexical lane
searches them anyway. On the catalog this project measures, 130,720 vectors
over ~130,647 enriched titles prints `1.000` against 1,271,138 rows in
`titles`. A `--mode fused` search against a catalog with no embeddings degrades to
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

✅ **The guard in front of the completion asks whether the semantic lane can
*answer*, not merely whether a model exists** (issue #16). It used to be
*"this deployment has an embedding model"*, so with a model installed and
nothing indexed yet every fused search bought a rewrite and *then* reported
`semantic_coverage=0.000` — the warning arriving after the money. It is now
the coverage of the request's own filtered population, measured before the
embed, so a not-yet-backfilled deployment spends nothing. Running `usher index
--backfill` first is still the right order; it is no longer something you are
billed for forgetting.

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
reverse of `usher search`: the claim that one request paints a screen is
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

## Backup

**Most of this database is rebuildable and a little of it is not.** The short
list is the one that matters.

```bash
uv run usher backup                       # writes usher-backup-<UTC>.jsonl.gz
uv run usher restore <artifact>           # refuses in one transaction, or applies
uv run usher restore <artifact> --dry-run # resolve everything, commit nothing
```

**Precious — nothing recomputes these:** `users`, `watch_states`, `sources`,
`source_credentials`, `row_provider_settings`, `search_queries` and
`llm_calls`. `usher backup` writes exactly this set, read from the manifest in
`usher.db.backup_manifest` rather than from a list anybody maintains by hand.

🔴 **`llm_calls` is the one to care about**, because it is the first thing in
this project that is not rebuildable from anything, at any price. It is a spend
ledger. It cannot be recomputed from the catalog, from `curated_rows` (replaced
nightly), or from the provider — no OpenAI-compatible endpoint offers a per-key
call history, and the price applied was a setting at the time of the call. It
is also small and append-only, which makes it the cheapest thing here to keep
and the most complete loss if you don't.

**Rebuildable — but read the two footnotes.** The catalog, embeddings, the
search index, neighbour tables, cached images and curated rows all come back
from `usher bootstrap`, `usher index --backfill`, `usher work` and `usher
similar --rebuild`. Two qualifications a reader would otherwise get wrong:

- `genome_scores` and `genome_tags` rebuild **only from upstream** — re-download
  `ml-latest.zip` and re-run the bootstrap — so they depend on GroupLens still
  serving that file.
- `curated_rows` rebuilds cheaply but **not to the same rows**. A curated row
  has no oracle and is not deterministic above `temperature 0`, so
  "rebuildable" there means *a screen appears*, not *your screen comes back*.

**And one table is neither:** `media_items` is classified `partial`, because a
sync re-derives the rows but not the manual unmatched resolutions somebody made
by hand.

Full procedures — including the restore drill this was verified against — are
in [`docs/runbooks/`](docs/runbooks/README.md).

## Building a client

Three obligations a client takes on, and neither of the first two is obvious
from an API that answers.

### Playback hands you a token you must treat as a secret

`POST /titles/{id}/play` returns an **opaque, short-lived ticket URL**
(`/stream/{ticket}`) rather than a source URL. The ticket is stateless — a
Fernet token over an HKDF-SHA256 subkey of `USHER_SECRET_KEY` — with a
**300-second** TTL.

**Redeeming it is a `302`, and the `Location` it sends you to carries the
source's session token.** A client reads `Location` by definition, so that
token reaches you. What the ticket changed is the **artifact, not the grant** —
and that distinction matters, because three documents in this repository have
claimed the opposite at one time or another and all three were wrong.

So: that URL is the whole capability grant for whatever the configured source
account can do. It is not minted per request, nothing about the response
ending ends it, and there is no revocation before expiry — the coarse
revocation that exists is rotating `USHER_SECRET_KEY`, which invalidates every
outstanding ticket at once. **Never log it, never render it, never put it in a
URL bar you screenshot.**

⚠️ **The `deep_link` target is asymmetric and it is the case you will actually
hit.** A deep link hands the ticket to a third-party player, which follows the
redirect and then holds the real URL exactly as before — for that target the
reduction is close to nil.

### You must render the attribution, and the logo

`GET /meta/attribution` returns four strings. Render all of them.

**TMDb also requires their logo**, which is an image this project does not ship
and cannot ship for you — a string cannot carry it. That obligation is yours,
not Usher's, and it is a licensing condition rather than a courtesy. See
[`docs/prd/04-catalog-bootstrap.md`](docs/prd/04-catalog-bootstrap.md) for the
full table and the four hard rules, and [Attribution](#attribution) below for
what the strings are and why the endpoint does not filter them.

### Pin the minor version

See [Versioning](#versioning). `0.x` means the wire contract may still move.

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

## Versioning

**Releases are `0.x`, and the first is `v0.1.0`.** The roadmap's *"v1 — the
abstraction works end to end"* names a **scope** milestone, which is met;
`1.0.0` in semver would name a **compatibility promise**, which is not. There
is no authentication anywhere, there is exactly one source adapter — so the
port's shape has never been tested against a second media server — and six open
feature issues each move a wire contract.

**Pin the minor version.** `0.x` is where the wire contract may still move, and
`1.0.0` stays available for the day it is meant.

## License

MIT
