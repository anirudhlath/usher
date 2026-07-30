# syntax=docker/dockerfile:1

# Multi-stage: the builder stage has uv (and would have a C toolchain, if
# any dependency here needed one to compile from sdist -- verified directly
# that none currently do; every dependency in uv.lock resolves to a
# prebuilt manylinux wheel for cp313, so this image never installs one).
# The runtime stage copies only the finished venv and source across, so
# uv itself and pip's build machinery never reach the final image.

FROM python:3.13-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Pinned to the version this project's own lockfile was produced with
# (`uv --version` in dev), not `:latest` -- a floating tag would make the
# image's dependency-resolution tool itself unpinned even though every
# dependency it resolves is pinned by uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, isolated from application source: this layer's cache
# only invalidates when pyproject.toml/uv.lock change, not on every source
# edit -- `--no-install-project` deliberately skips building/installing
# usher itself here, so this RUN is pure third-party dependency resolution.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now the project itself. README.md has to be present here -- pyproject.toml
# declares `readme = "README.md"` and hatchling (the configured build
# backend) reads that file while building usher's own wheel; this second
# `uv sync` (no --no-install-project this time) does that build. Omitting
# the COPY of README.md makes this step fail -- verified directly, and it
# is exactly the kind of gap Task 13's own plan text warned might be
# stale ("verify rather than assume").
COPY src/ ./src/
COPY alembic.ini README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root: a fixed uid/gid (rather than useradd's own default allocation)
# so it is stable across rebuilds and matches whatever a deployer chooses
# to chown a bind-mounted host directory to.
RUN groupadd --system --gid 1000 usher \
    && useradd --system --uid 1000 --gid usher --no-create-home --home-dir /app usher

WORKDIR /app

COPY --from=builder --chown=usher:usher /app/.venv ./.venv
COPY --from=builder --chown=usher:usher /app/src ./src
COPY --chown=usher:usher alembic.ini ./

# /data/images is where compose.yml bind-mounts a host directory for the
# image cache a later milestone's proxy will write to -- nothing in M1
# writes here yet. Pre-creating it owned by the non-root user only helps
# when the container runs *without* that bind mount (a bind mount's host-
# side ownership wins once mounted); if the host directory doesn't exist
# yet, Docker will create it as root on first `docker compose up`, which a
# future milestone's writer will need `chown 1000:1000 data/images` (or
# equivalent) to use. Documented, not solved here -- there is no writer to
# test against yet.
RUN mkdir -p /data/images && chown -R usher:usher /data

USER usher

EXPOSE 8000

# Runs migrations, then execs uvicorn as PID 1's replacement (via `exec`)
# rather than leaving it as a child of the `sh -c` shell, so `docker stop`'s
# SIGTERM reaches uvicorn directly instead of being swallowed by the shell
# for the full stop-grace-period.
#
# Multiple-replica caveat (noted, not solved -- M1 runs exactly one usher
# container): `alembic upgrade head` here has no distributed lock. Two
# containers starting at once would both race to apply the same pending
# migration. Fine today; a real problem the moment this service is ever
# scaled past one replica, at which point migrations belong in a separate
# one-shot step (`docker compose run --rm usher alembic upgrade head`, a
# CI/CD release step, or a Kubernetes Job/initContainer) instead of every
# replica's own startup. `/health/ready`'s migration-mismatch check would
# at least surface a lost race as a 503 rather than silently serving
# against the wrong schema, but it does not prevent the race itself.
CMD ["sh", "-c", "alembic upgrade head && exec python -m usher"]
