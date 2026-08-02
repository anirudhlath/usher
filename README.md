# Usher

A self-hosted media catalog backend. Usher maintains its own canonical database
of film and television, treats media servers (Emby first) as interchangeable
*sources* that answer "where can this be played?", and exposes an API rich
enough to build a full media browser against.

Design documentation lives in [`docs/prd/`](docs/prd/README.md).

## Status

Pre-release. Milestones M1 (foundation), M2 (catalog bootstrap), M3 (Emby
adapter), M4 (ingest pipeline) and M5 (push and read-through) are complete —
see [`docs/plans/`](docs/plans/) for the task breakdowns and
[`docs/prd/09-roadmap.md`](docs/prd/09-roadmap.md) for what's next.

M3, M4 and M5 are each verified against a live Emby server, and M4's metadata
half against the live TMDb API. M5's run is the first in this repository to
have parsed a real `/embywebsocket` message.

**The HTTP surface is deliberately small so far**: `/health`,
`/health/ready`, `/titles/{id}`, `/events` (SSE) and the `/admin/sources`
routes. Everything the ingest pipeline does is driven from the command line
until M9 adds the admin API — see below.

## Requirements

- Docker and Docker Compose
- A [TMDb API key](https://www.themoviedb.org/settings/api) (free, non-commercial)

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
