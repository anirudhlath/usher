# Usher

A self-hosted media catalog backend. Usher maintains its own canonical database
of film and television, treats media servers (Emby first) as interchangeable
*sources* that answer "where can this be played?", and exposes an API rich
enough to build a full media browser against.

Design documentation lives in [`docs/prd/`](docs/prd/README.md).

## Status

Pre-release. Milestones M1 (foundation), M2 (catalog bootstrap) and M3 (Emby
adapter) are complete — see [`docs/plans/`](docs/plans/) for the task
breakdowns and [`docs/prd/09-roadmap.md`](docs/prd/09-roadmap.md) for what's
next.

## Requirements

- Docker and Docker Compose
- A [TMDb API key](https://www.themoviedb.org/settings/api) (free, non-commercial)

## Running it

```bash
cp .env.example .env
echo "USHER_SECRET_KEY=$(openssl rand -hex 32)" >> .env
docker compose up -d --build

curl -sf http://localhost:8100/health        # {"status":"ok"}
curl -sf http://localhost:8100/health/ready  # adds database + migration state
```

`USHER_SECRET_KEY` is required and has no default — compose refuses to start
without it. It encrypts stored source credentials, so changing it later makes
existing ones unreadable (the admin status endpoint reports that state rather
than failing). `USHER_HOST_PORT` defaults to `8100`.

Migrations run automatically on container start. To populate the catalog from
the public datasets, see [`docs/prd/04-catalog-bootstrap.md`](docs/prd/04-catalog-bootstrap.md).

## Attribution

This project ships importers, never data. Each deployment downloads its own
datasets and holds its own API keys.

- Information courtesy of IMDb (https://www.imdb.com). Used with permission.
- This product uses the TMDB API but is not endorsed or certified by TMDB.

## License

MIT
