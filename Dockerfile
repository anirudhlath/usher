# syntax=docker/dockerfile:1

# The console, built here and served by the app at /console. One process serving
# both needs no proxy rewriting paths, which is what pointed playback-ticket URLs
# minted from the Host header at the wrong port. Only `web/dist` leaves this stage.
FROM node:26-alpine AS console

WORKDIR /web

# Dependencies first, so editing a component does not re-resolve the tree.
# `npm ci` installs exactly the lockfile and fails if the two disagree, which is
# what makes the image reproducible.
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./

# `npm run build` is `tsc -b && vite build`, so a type error fails the image
# build rather than shipping a bundle that only fails in a browser.
RUN npm run build

# The builder has uv; the runtime stage copies only the finished venv and source,
# so uv and any build machinery never reach the final image.

FROM python:3.13-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Pinned to the uv the lockfile was produced with, not `:latest`, which would leave
# the resolver itself unpinned.
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /usr/local/bin/uv

WORKDIR /app

# Third-party dependencies only, so this layer invalidates when pyproject.toml or
# uv.lock change and not on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now the project itself. README.md must be here: pyproject.toml declares
# `readme = "README.md"` and hatchling reads it while building usher's wheel.
# Neither sync passes `--extra`, so the image installs no optional dependency.
COPY src/ ./src/
COPY alembic.ini README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root, with a fixed uid/gid so it is stable across rebuilds and a deployer can
# chown a bind-mounted host directory to it.
RUN groupadd --system --gid 1000 usher \
    && useradd --system --uid 1000 --gid usher --no-create-home --home-dir /app usher

WORKDIR /app

COPY --from=builder --chown=usher:usher /app/.venv ./.venv
COPY --from=builder --chown=usher:usher /app/src ./src
COPY --chown=usher:usher alembic.ini ./

# The same path a checkout has: `Settings.console_dist_dir` defaults to `web/dist`
# and neither deployment overrides it.
COPY --from=console --chown=usher:usher /web/dist ./web/dist

# Owned by uid 1000 for a container run without compose's bind mount. With the
# mount, the host side's ownership wins, which is why README.md has the `chown`.
RUN mkdir -p /data/images && chown -R usher:usher /data

USER usher

EXPOSE 8000

# The `exec` replaces the shell, so `docker stop`'s SIGTERM reaches uvicorn.
# `alembic upgrade head` here has no distributed lock. Two containers starting
# at once would both race to apply the same pending migration. Past one replica,
# migrate in a one-shot step; `/health/ready` reports a lost race as a 503 but
# does not prevent it.
CMD ["sh", "-c", "alembic upgrade head && exec python -m usher"]
