# Usher M1 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a running Usher service with a migrated database schema, canonical domain models, port ABCs, telemetry, health endpoints, and CI that enforces the architecture.

**Architecture:** Modular monolith with a hexagonal core. `domain/` holds pure Pydantic models; `ports/` holds ABCs; `db/` holds SQLAlchemy models and repositories that translate to and from domain models; `api/` holds FastAPI routers. Layering is enforced by `import-linter` in CI, not by convention. See [PRD 01](../prd/01-architecture.md).

**Tech Stack:** Python 3.13 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) · Alembic · PostgreSQL 17 + pgvector · loguru · OpenTelemetry · pytest · testcontainers · ruff · mypy · import-linter

**Scope note:** M1 lands the *core* schema (titles, sources, media_items, users, watch_states) to prove the model→ORM→migration→repository pattern end to end. Seasons, episodes, people, credits, images, and embeddings arrive with the milestones that consume them.

---

## File structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Deps, tool config, import-linter contracts |
| `.gitignore`, `LICENSE`, `README.md` | Repo hygiene, MIT license |
| `.env.example` | Documented environment surface |
| `compose.yml`, `Dockerfile` | Deployment |
| `alembic.ini` | Migration config |
| `src/usher/config.py` | Settings from env, single source of config |
| `src/usher/telemetry.py` | loguru + OTel bootstrap, trace-context injection |
| `src/usher/domain/enums.py` | Shared enumerations |
| `src/usher/domain/ids.py` | UUIDv7 generation |
| `src/usher/domain/title.py` | `Title` |
| `src/usher/domain/source.py` | `Source`, `MediaItem` |
| `src/usher/domain/watch.py` | `User`, `WatchState` |
| `src/usher/ports/source.py` | `SourceAdapter` ABC + its DTOs |
| `src/usher/ports/metadata.py` | `MetadataProvider` ABC |
| `src/usher/ports/search.py` | `SearchIndex` ABC |
| `src/usher/ports/embedding.py` | `Embedder` ABC |
| `src/usher/ports/llm.py` | `LLMClient` ABC |
| `src/usher/db/base.py` | Declarative base, engine, session factory |
| `src/usher/db/models/*.py` | SQLAlchemy tables |
| `src/usher/db/repositories/title.py` | `TitleRepository` |
| `src/usher/db/migrations/` | Alembic environment + versions |
| `src/usher/api/app.py` | App factory |
| `src/usher/api/deps.py` | Request-scoped dependencies |
| `src/usher/api/routers/health.py` | `/health`, `/health/ready` |
| `tests/unit/`, `tests/integration/` | Test suites |

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `LICENSE`, `README.md`, `.env.example`
- Create: `src/usher/__init__.py` and package `__init__.py` files

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "usher"
version = "0.1.0"
description = "A self-hosted media catalog backend that abstracts media servers behind a canonical database"
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30",
    "alembic>=1.14",
    "pgvector>=0.3.6",
    "uuid6>=2024.7.10",
    "loguru>=0.7",
    "httpx[http2]>=0.27",
    "opentelemetry-api>=1.28",
    "opentelemetry-sdk>=1.28",
    "opentelemetry-exporter-otlp>=1.28",
    "opentelemetry-instrumentation-fastapi>=0.49b0",
    "opentelemetry-instrumentation-sqlalchemy>=0.49b0",
    "opentelemetry-instrumentation-httpx>=0.49b0",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "ruff>=0.8",
    "mypy>=1.13",
    "import-linter>=2.1",
    "testcontainers[postgres]>=4.9",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/usher"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
target-version = "py313"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]

[tool.mypy]
python_version = "3.13"
packages = ["usher"]
mypy_path = "src"
strict = true
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = ["uuid6", "testcontainers.*"]
ignore_missing_imports = true

[tool.importlinter]
root_packages = ["usher"]

[[tool.importlinter.contracts]]
name = "domain is pure"
type = "forbidden"
source_modules = ["usher.domain"]
forbidden_modules = ["usher.adapters", "usher.db", "usher.api", "usher.services"]

[[tool.importlinter.contracts]]
name = "ports depend only on domain"
type = "forbidden"
source_modules = ["usher.ports"]
forbidden_modules = ["usher.adapters", "usher.db", "usher.api", "usher.services"]

[[tool.importlinter.contracts]]
name = "services never import adapters"
type = "forbidden"
source_modules = ["usher.services"]
forbidden_modules = ["usher.adapters"]
```

- [ ] **Step 2: Create `.gitignore`**

```gitignore
__pycache__/
*.py[cod]
.venv/
.env
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
data/
```

- [ ] **Step 3: Create `LICENSE` (MIT)**

```text
MIT License

Copyright (c) 2026 Anirudh Lath

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 4: Create `README.md`**

```markdown
# Usher

A self-hosted media catalog backend. Usher maintains its own canonical database
of film and television, treats media servers (Emby first) as interchangeable
*sources* that answer "where can this be played?", and exposes an API rich
enough to build a full media browser against.

Design documentation lives in [`docs/prd/`](docs/prd/README.md).

## Status

Pre-release. Milestone M1 (foundation) in progress — see
[`docs/plans/`](docs/plans/).

## Requirements

- Docker and Docker Compose
- A [TMDb API key](https://www.themoviedb.org/settings/api) (free, non-commercial)

## Attribution

This project ships importers, never data. Each deployment downloads its own
datasets and holds its own API keys.

- Information courtesy of IMDb (https://www.imdb.com). Used with permission.
- This product uses the TMDB API but is not endorsed or certified by TMDB.

## License

MIT
```

- [ ] **Step 5: Create `.env.example`**

```bash
# Database
USHER_DATABASE_URL=postgresql+asyncpg://usher:usher@localhost:5432/usher

# Server
USHER_HOST=0.0.0.0
USHER_PORT=8000
USHER_LOG_LEVEL=INFO
USHER_LOG_JSON=true

# Secrets (required in production; used to encrypt source credentials)
USHER_SECRET_KEY=change-me-to-a-long-random-string

# Providers
USHER_TMDB_API_KEY=

# Telemetry (optional — exporters become no-ops when unset)
OTEL_EXPORTER_OTLP_ENDPOINT=
OTEL_SERVICE_NAME=usher
```

- [ ] **Step 6: Create the package tree**

```bash
cd ~/code/usher
mkdir -p src/usher/{domain,ports,db/{models,repositories},api/routers,services,adapters}
mkdir -p tests/{unit,integration}
for d in src/usher src/usher/domain src/usher/ports src/usher/db src/usher/db/models \
         src/usher/db/repositories src/usher/api src/usher/api/routers \
         src/usher/services src/usher/adapters tests tests/unit tests/integration; do
  touch "$d/__init__.py"
done
```

- [ ] **Step 7: Install and verify**

Run: `uv sync`
Expected: resolves and installs; creates `.venv/` and `uv.lock`.

Run: `uv run python -c "import usher; print('ok')"`
Expected: `ok`

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: project scaffold with uv, tooling config, and package layout"
```

---

## Task 2: Configuration

**Files:**
- Create: `src/usher/config.py`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_config.py
import pytest
from pydantic import ValidationError

from usher.config import Settings


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    settings = Settings()
    assert settings.database_url.get_secret_value() == "postgresql+asyncpg://u:p@db:5432/usher"
    assert settings.port == 8000


def test_settings_reject_short_secret_key(monkeypatch):
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "short")
    with pytest.raises(ValidationError):
        Settings()


def test_telemetry_disabled_when_no_endpoint(monkeypatch):
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert Settings().telemetry_enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.config'`

- [ ] **Step 3: Write the implementation**

```python
# src/usher/config.py
"""Application configuration, read from the environment."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Infrastructure comes from the environment; sources
    are configured at runtime and live in the database."""

    model_config = SettingsConfigDict(
        env_prefix="USHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr
    secret_key: SecretStr = Field(min_length=32)

    host: str = "0.0.0.0"
    port: int = 8000

    log_level: str = "INFO"
    log_json: bool = True

    tmdb_api_key: SecretStr | None = None

    otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    service_name: str = Field(default="usher", alias="OTEL_SERVICE_NAME")

    @property
    def telemetry_enabled(self) -> bool:
        """Telemetry is optional: with no endpoint configured, exporters are no-ops."""
        return bool(self.otlp_endpoint)


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

Note for whoever next touches this file: the real `src/usher/config.py` has grown
past this minimal walkthrough — `extra="forbid"`, a placeholder-secret-key
rejection validator, `Literal[...]` bounds on `log_level`, `Field(ge=1, le=65535)`
on `port`, an asyncpg-driver validator on `database_url`, and an empty-string→`None`
coercion for the optional fields. Read the file directly rather than trusting this
block for anything beyond the two facts that matter to other tasks: `database_url`/
`secret_key`/`tmdb_api_key` are `SecretStr` (unwrap with `.get_secret_value()` at
the point of use), and `get_settings()` is cached (tests call `get_settings.cache_clear()`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/usher/config.py tests/unit/test_config.py
git commit -m "feat: settings loaded from environment with validation"
```

---

## Task 3: Identifiers and enums

**Files:**
- Create: `src/usher/domain/ids.py`, `src/usher/domain/enums.py`
- Test: `tests/unit/test_ids.py`

- [ ] **Step 1: Write the failing test**

The first assertion deliberately pins an assumption: that the `uuid6` package returns a value compatible with the standard library, which SQLAlchemy and Pydantic both require.

```python
# tests/unit/test_ids.py
import uuid

from usher.domain.ids import new_id


def test_new_id_is_a_stdlib_uuid():
    value = new_id()
    assert isinstance(value, uuid.UUID)


def test_new_id_is_version_7():
    assert new_id().version == 7


def test_new_ids_are_time_ordered():
    ids = [new_id() for _ in range(100)]
    assert ids == sorted(ids, key=lambda u: u.hex)


def test_new_ids_are_unique():
    assert len({new_id() for _ in range(1000)}) == 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_ids.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.domain.ids'`

- [ ] **Step 3: Write the implementation**

```python
# src/usher/domain/ids.py
"""Usher-owned identifiers.

UUIDv7 rather than v4: time-ordered, so index locality stays good during the
bulk imports that insert millions of rows. See ADR-0003.
"""

import uuid

from uuid6 import uuid7


def new_id() -> uuid.UUID:
    """Generate a fresh time-ordered identifier."""
    return uuid7()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_ids.py -v`
Expected: 4 passed

If `test_new_id_is_a_stdlib_uuid` fails, `uuid6` is not returning a `uuid.UUID` subclass. Fix by converting: `return uuid.UUID(bytes=uuid7().bytes)`, then re-run.

- [ ] **Step 5: Write the enums**

```python
# src/usher/domain/enums.py
"""Shared enumerations. Values are stable wire and storage identifiers."""

from enum import StrEnum


class TitleKind(StrEnum):
    MOVIE = "movie"
    SERIES = "series"


class EnrichmentState(StrEnum):
    """How complete a Title's metadata is. Always exposed to clients so they
    render deliberately rather than inferring from nulls."""

    SKELETON = "skeleton"  # from a bulk dataset; no overview or artwork
    STUB = "stub"          # seen on a source; source metadata only
    ENRICHED = "enriched"  # full provider metadata
    FAILED = "failed"      # enrichment attempted and failed


class SourceKind(StrEnum):
    EMBY = "emby"


class WatchStateOrigin(StrEnum):
    SOURCE = "source"
    API = "api"


class ProductionStatus(StrEnum):
    RELEASED = "released"
    IN_PRODUCTION = "in_production"
    POST_PRODUCTION = "post_production"
    PLANNED = "planned"
    CANCELED = "canceled"
    ENDED = "ended"
    RETURNING = "returning"
```

- [ ] **Step 6: Verify enums import**

Run: `uv run python -c "from usher.domain.enums import TitleKind; print(TitleKind.MOVIE)"`
Expected: `movie`

- [ ] **Step 7: Commit**

```bash
git add src/usher/domain/ids.py src/usher/domain/enums.py tests/unit/test_ids.py
git commit -m "feat: UUIDv7 identifiers and shared domain enums"
```

---

## Task 4: Title domain model

**Files:**
- Create: `src/usher/domain/title.py`
- Test: `tests/unit/test_domain_title.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_domain_title.py
import pytest
from pydantic import ValidationError

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title


def test_title_requires_only_kind_and_name():
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    assert title.name == "Dune"
    assert title.enrichment_state is EnrichmentState.SKELETON
    assert title.tmdb_id is None
    assert title.genres == []


def test_title_generates_its_own_identity():
    a = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    b = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    assert a.id != b.id
    assert a.id.version == 7


def test_title_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        Title(kind="documentary", name="X", sort_name="X")


def test_title_accepts_provider_ids_as_attributes():
    title = Title(
        kind=TitleKind.MOVIE,
        name="Dune",
        sort_name="Dune",
        tmdb_id=438631,
        imdb_id="tt1160419",
    )
    assert title.tmdb_id == 438631
    assert title.imdb_id == "tt1160419"


def test_title_is_immutable():
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    with pytest.raises(ValidationError):
        title.name = "Other"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_domain_title.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.domain.title'`

- [ ] **Step 3: Write the implementation**

```python
# src/usher/domain/title.py
"""The canonical production: one film, or one series."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from usher.domain.enums import EnrichmentState, ProductionStatus, TitleKind
from usher.domain.ids import new_id


class Title(BaseModel):
    """A canonical production.

    Identity is Usher's own UUIDv7. Provider identifiers are nullable,
    indexed *attributes* — never identity. See ADR-0003.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    kind: TitleKind

    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None

    name: str
    original_name: str | None = None
    sort_name: str
    year: int | None = None
    release_date: date | None = None
    end_year: int | None = None

    overview: str | None = None
    tagline: str | None = None
    runtime_minutes: int | None = None
    status: ProductionStatus | None = None

    genres: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    original_language: str | None = None
    spoken_languages: list[str] = Field(default_factory=list)
    origin_countries: list[str] = Field(default_factory=list)
    content_rating: str | None = None

    community_rating: float | None = None
    vote_count: int | None = None
    popularity: float | None = None

    collection_id: uuid.UUID | None = None

    enrichment_state: EnrichmentState = EnrichmentState.SKELETON
    enriched_at: datetime | None = None
    field_provenance: dict[str, str] = Field(default_factory=dict)

    created_at: datetime | None = None
    updated_at: datetime | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_domain_title.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/usher/domain/title.py tests/unit/test_domain_title.py
git commit -m "feat: Title domain model with Usher-owned identity"
```

---

## Task 5: Source, MediaItem, User, and WatchState models

**Files:**
- Create: `src/usher/domain/source.py`, `src/usher/domain/watch.py`
- Test: `tests/unit/test_domain_source.py`, `tests/unit/test_domain_watch.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_domain_source.py
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import MediaItem, Source


def test_source_defaults_to_enabled_without_push():
    source = Source(
        kind=SourceKind.EMBY,
        name="Living room Emby",
        base_url="https://emby.example.com",
        credentials_ref="cred-1",
        device_id="device-1",
    )
    assert source.enabled is True
    assert source.supports_push is False


def test_media_item_may_be_unmatched():
    item = MediaItem(source_id=new_id(), external_id="12345")
    assert item.title_id is None
    assert item.available is True
```

```python
# tests/unit/test_domain_watch.py
from usher.domain.enums import WatchStateOrigin
from usher.domain.ids import new_id
from usher.domain.watch import User, WatchState


def test_user_has_identity_and_name():
    user = User(name="default")
    assert user.name == "default"
    assert user.id.version == 7


def test_watch_state_attaches_to_a_title():
    state = WatchState(
        user_id=new_id(),
        title_id=new_id(),
        position_seconds=1840,
        updated_by=WatchStateOrigin.SOURCE,
    )
    assert state.played is False
    assert state.play_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_domain_source.py tests/unit/test_domain_watch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.domain.source'`

- [ ] **Step 3: Write `source.py`**

```python
# src/usher/domain/source.py
"""Sources and availability — the only place a media server is represented."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from usher.domain.enums import SourceKind
from usher.domain.ids import new_id


class Source(BaseModel):
    """A configured backend that holds playable media."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    kind: SourceKind
    name: str
    base_url: str
    credentials_ref: str
    device_id: str
    enabled: bool = True
    supports_push: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MediaItem(BaseModel):
    """'This title is available on that source', plus the quality facts of
    that particular copy. A Title may have many, across sources."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    source_id: uuid.UUID
    title_id: uuid.UUID | None = None
    episode_id: uuid.UUID | None = None
    external_id: str

    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    hdr_format: str | None = None
    audio_channels: int | None = None
    file_size_bytes: int | None = None
    runtime_seconds: int | None = None

    added_at: datetime | None = None
    last_seen_at: datetime | None = None
    available: bool = True
```

- [ ] **Step 4: Write `watch.py`**

```python
# src/usher/domain/watch.py
"""Users and watch state.

Watch state attaches to the canonical Title, not to a MediaItem, so it
survives adding, changing, or losing a source.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from usher.domain.enums import WatchStateOrigin
from usher.domain.ids import new_id


class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    name: str
    is_default: bool = False
    created_at: datetime | None = None


class WatchState(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    user_id: uuid.UUID
    title_id: uuid.UUID | None = None
    episode_id: uuid.UUID | None = None

    position_seconds: int = 0
    runtime_seconds: int | None = None
    played: bool = False
    play_count: int = 0
    last_played_at: datetime | None = None

    updated_at: datetime | None = None
    updated_by: WatchStateOrigin = WatchStateOrigin.API
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_domain_source.py tests/unit/test_domain_watch.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add src/usher/domain/source.py src/usher/domain/watch.py \
        tests/unit/test_domain_source.py tests/unit/test_domain_watch.py
git commit -m "feat: Source, MediaItem, User, and WatchState domain models"
```

---

## Task 6: Port ABCs

**Files:**
- Create: `src/usher/ports/{source,metadata,search,embedding,llm}.py`
- Test: `tests/unit/test_ports.py`

- [ ] **Step 1: Write the failing test**

This test verifies the property ADR-0001 chose ABCs *for*: an incomplete implementation fails at instantiation rather than at the call site.

```python
# tests/unit/test_ports.py
import pytest

from usher.ports.embedding import Embedder
from usher.ports.llm import LLMClient
from usher.ports.metadata import MetadataProvider
from usher.ports.search import SearchIndex
from usher.ports.source import SourceAdapter

ALL_PORTS = [SourceAdapter, MetadataProvider, SearchIndex, Embedder, LLMClient]


@pytest.mark.parametrize("port", ALL_PORTS)
def test_port_cannot_be_instantiated_directly(port):
    with pytest.raises(TypeError):
        port()


@pytest.mark.parametrize("port", ALL_PORTS)
def test_port_declares_abstract_methods(port):
    assert port.__abstractmethods__


def test_incomplete_implementation_fails_at_instantiation():
    class Incomplete(Embedder):
        pass

    with pytest.raises(TypeError, match="abstract"):
        Incomplete()


def test_complete_implementation_instantiates():
    class Fake(Embedder):
        @property
        def model_name(self) -> str:
            return "fake"

        @property
        def dimension(self) -> int:
            return 3

        async def embed(self, texts):
            return [[0.0, 0.0, 0.0] for _ in texts]

    assert Fake().dimension == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_ports.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.ports.embedding'`

- [ ] **Step 3: Write `embedding.py` and `llm.py`**

```python
# src/usher/ports/embedding.py
"""Port for computing text embeddings."""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class Embedder(ABC):
    """Turns text into vectors. Implementations are expected to batch."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Stored alongside vectors so a model change is detectable."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector width, must match the database column."""

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, returning one vector per input in order."""
```

```python
# src/usher/ports/llm.py
"""Port for large language model completions."""

from abc import ABC, abstractmethod
from typing import Any


class LLMClient(ABC):
    """Provider-agnostic completion interface."""

    @abstractmethod
    async def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        purpose: str,
    ) -> tuple[dict[str, Any], "LLMUsage"]:
        """Return a JSON object conforming to `schema`, plus usage for cost
        accounting. `purpose` is recorded against the call."""


class LLMUsage:
    """Token counts and cost for a single completion."""

    def __init__(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        latency_ms: int,
    ) -> None:
        self.model = model
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_usd = cost_usd
        self.latency_ms = latency_ms
```

- [ ] **Step 4: Write `metadata.py` and `search.py`**

```python
# src/usher/ports/metadata.py
"""Port for external metadata providers."""

import uuid
from abc import ABC, abstractmethod
from typing import Any

from usher.domain.title import Title


class MetadataProvider(ABC):
    """Supplies high-quality metadata for a canonical Title."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier, recorded in field provenance."""

    @abstractmethod
    async def search(self, name: str, year: int | None) -> list[dict[str, Any]]:
        """Candidate matches for a name and optional year."""

    @abstractmethod
    async def fetch(self, provider_id: int, kind: str) -> dict[str, Any]:
        """Full raw payload for one item. Stored before normalisation."""

    @abstractmethod
    def to_title(self, payload: dict[str, Any], title_id: uuid.UUID) -> Title:
        """Normalise a raw payload into a canonical Title."""

    @abstractmethod
    async def changed_since(self, days: int) -> list[int]:
        """Provider ids mutated in the window, for incremental refresh."""
```

```python
# src/usher/ports/search.py
"""Port for the search index."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchHit:
    title_id: uuid.UUID
    score: float


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int = 20
    semantic: bool = False
    filters: dict[str, Any] = field(default_factory=dict)


class SearchIndex(ABC):
    """Candidate generation. Ranking blends happen in application code, so
    this returns hits and scores, not final ordering."""

    @abstractmethod
    async def index(self, title_id: uuid.UUID) -> None:
        """Insert or update one title's document."""

    @abstractmethod
    async def remove(self, title_id: uuid.UUID) -> None:
        """Drop a title from the index."""

    @abstractmethod
    async def search(self, request: SearchRequest) -> list[SearchHit]:
        """Full-text, semantic, or fused search."""

    @abstractmethod
    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        """Typo-tolerant type-ahead over names."""
```

- [ ] **Step 5: Write `source.py`**

```python
# src/usher/ports/source.py
"""Port for media sources, and the DTOs that cross that boundary."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class SourceEventKind(StrEnum):
    ITEM_ADDED = "item_added"
    ITEM_UPDATED = "item_updated"
    ITEM_REMOVED = "item_removed"
    WATCH_STATE_CHANGED = "watch_state_changed"


@dataclass(frozen=True)
class SourceItem:
    """One playable item as the source describes it, already normalised."""

    external_id: str
    name: str
    kind: str
    year: int | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    hdr_format: str | None = None
    audio_channels: int | None = None
    file_size_bytes: int | None = None
    runtime_seconds: int | None = None
    added_at: datetime | None = None
    series_external_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceWatchState:
    external_id: str
    position_seconds: int
    played: bool
    play_count: int = 0
    last_played_at: datetime | None = None


@dataclass(frozen=True)
class WatchStateUpdate:
    position_seconds: int
    played: bool


@dataclass(frozen=True)
class SourceEvent:
    kind: SourceEventKind
    external_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StreamTarget:
    """How to play an item. Clients choose between the returned targets."""

    kind: str
    url: str
    container: str | None = None
    video_codec: str | None = None
    hdr_format: str | None = None
    resolution: str | None = None
    runtime_seconds: int | None = None
    resume_position_seconds: int | None = None


class SourceNotSupported(Exception):
    """Raised by adapters for capabilities they do not have."""


class SourceAdapter(ABC):
    """A backend that holds playable media.

    Nothing source-specific may escape an implementation of this port.
    """

    @property
    @abstractmethod
    def source_id(self) -> uuid.UUID:
        """The configured Source this adapter serves."""

    @abstractmethod
    async def verify(self) -> bool:
        """Authenticate and confirm reachability."""

    @abstractmethod
    def list_items(self, since: datetime | None = None) -> AsyncIterator[SourceItem]:
        """Walk the library, optionally limited to changes since a cursor."""

    @abstractmethod
    async def get_item(self, external_id: str) -> SourceItem | None:
        """Fetch one item, or None if it is gone."""

    @abstractmethod
    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        """Ranked ways to play an item."""

    @abstractmethod
    def watch_state(
        self, since: datetime | None = None
    ) -> AsyncIterator[SourceWatchState]:
        """Watch state from the source, optionally since a cursor."""

    @abstractmethod
    async def push_watch_state(
        self, external_id: str, state: WatchStateUpdate
    ) -> None:
        """Write watch state back. Best-effort; may raise."""

    @abstractmethod
    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        """Push channel. Adapters without one raise SourceNotSupported; the
        reconciler covers them."""
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_ports.py -v`
Expected: 12 passed

- [ ] **Step 7: Verify layering contracts hold**

Run: `uv run lint-imports`
Expected: `Contracts: 3 kept, 0 broken.`

- [ ] **Step 8: Commit**

```bash
git add src/usher/ports tests/unit/test_ports.py
git commit -m "feat: port ABCs for sources, metadata, search, embedding, and LLM"
```

---

## Task 7: Database engine and session factory

**Files:**
- Create: `src/usher/db/base.py`
- Test: `tests/unit/test_db_base.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_db_base.py
from usher.db.base import Base, build_engine, build_session_factory


def test_base_has_no_tables_until_models_imported():
    assert hasattr(Base, "metadata")


def test_engine_is_async():
    engine = build_engine("postgresql+asyncpg://u:p@localhost:5432/usher")
    assert engine.dialect.is_async is True


def test_session_factory_produces_async_sessions():
    engine = build_engine("postgresql+asyncpg://u:p@localhost:5432/usher")
    factory = build_session_factory(engine)
    session = factory()
    assert hasattr(session, "execute")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_db_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.db.base'`

- [ ] **Step 3: Write the implementation**

```python
# src/usher/db/base.py
"""Declarative base, engine, and session factory."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all Usher tables."""


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=5,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_db_base.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/usher/db/base.py tests/unit/test_db_base.py
git commit -m "feat: async SQLAlchemy engine and session factory"
```

---

## Task 8: SQLAlchemy models for the core schema

**Files:**
- Create: `src/usher/db/models/{title,source,watch}.py`
- Modify: `src/usher/db/models/__init__.py`
- Test: `tests/unit/test_db_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_db_models.py
from usher.db.base import Base
from usher.db.models import MediaItemRow, SourceRow, TitleRow, UserRow, WatchStateRow


def test_all_core_tables_registered():
    names = set(Base.metadata.tables)
    assert {"titles", "sources", "media_items", "users", "watch_states"} <= names


def test_title_provider_ids_are_indexed_not_primary():
    table = TitleRow.__table__
    assert list(table.primary_key.columns)[0].name == "id"
    indexed = {c.name for idx in table.indexes for c in idx.columns}
    assert {"tmdb_id", "imdb_id"} <= indexed


def test_media_item_is_unique_per_source_and_external_id():
    constraints = {
        tuple(c.name for c in con.columns)
        for con in MediaItemRow.__table__.constraints
        if hasattr(con, "columns") and len(con.columns) == 2
    }
    assert ("source_id", "external_id") in constraints


def test_media_item_title_is_nullable_for_unmatched():
    assert MediaItemRow.__table__.c.title_id.nullable is True


def test_source_and_user_tables_exist():
    assert SourceRow.__tablename__ == "sources"
    assert UserRow.__tablename__ == "users"
    assert WatchStateRow.__tablename__ == "watch_states"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_db_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'MediaItemRow'`

- [ ] **Step 3: Write `title.py`**

```python
# src/usher/db/models/title.py
"""Catalog tables."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base
from usher.domain.enums import EnrichmentState, TitleKind


class TitleRow(Base):
    __tablename__ = "titles"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    kind: Mapped[TitleKind] = mapped_column(String(16), nullable=False)

    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    imdb_id: Mapped[str | None] = mapped_column(String(16))
    tvdb_id: Mapped[int | None] = mapped_column(Integer)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    original_name: Mapped[str | None] = mapped_column(Text)
    sort_name: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    release_date: Mapped[date | None] = mapped_column(Date)
    end_year: Mapped[int | None] = mapped_column(Integer)

    overview: Mapped[str | None] = mapped_column(Text)
    tagline: Mapped[str | None] = mapped_column(Text)
    runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(32))

    genres: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    original_language: Mapped[str | None] = mapped_column(String(16))
    spoken_languages: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    origin_countries: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    content_rating: Mapped[str | None] = mapped_column(String(32))

    community_rating: Mapped[float | None] = mapped_column(Float)
    vote_count: Mapped[int | None] = mapped_column(Integer)
    popularity: Mapped[float | None] = mapped_column(Float)

    collection_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    enrichment_state: Mapped[EnrichmentState] = mapped_column(
        String(16), nullable=False, default=EnrichmentState.SKELETON
    )
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    field_provenance: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_titles_tmdb_id",
            "tmdb_id",
            unique=True,
            postgresql_where=text("tmdb_id IS NOT NULL"),
        ),
        Index(
            "ix_titles_imdb_id",
            "imdb_id",
            unique=True,
            postgresql_where=text("imdb_id IS NOT NULL"),
        ),
        Index("ix_titles_sort_name", "sort_name"),
        Index("ix_titles_enrichment_state", "enrichment_state"),
        Index("ix_titles_popularity", "popularity"),
    )
```

- [ ] **Step 4: Write `source.py`**

```python
# src/usher/db/models/source.py
"""Source and availability tables."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base
from usher.domain.enums import SourceKind


class SourceRow(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    kind: Mapped[SourceKind] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    credentials_ref: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_push: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MediaItemRow(Base):
    __tablename__ = "media_items"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    title_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="SET NULL")
    )
    episode_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    external_id: Mapped[str] = mapped_column(Text, nullable=False)

    container: Mapped[str | None] = mapped_column(String(32))
    video_codec: Mapped[str | None] = mapped_column(String(32))
    audio_codec: Mapped[str | None] = mapped_column(String(32))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    hdr_format: Mapped[str | None] = mapped_column(String(16))
    audio_channels: Mapped[int | None] = mapped_column(Integer)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    runtime_seconds: Mapped[int | None] = mapped_column(Integer)

    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_media_items_source_external"),
        Index("ix_media_items_title_id", "title_id"),
        Index(
            "ix_media_items_unmatched",
            "source_id",
            postgresql_where=text("title_id IS NULL"),
        ),
    )
```

- [ ] **Step 5: Write `watch.py`**

```python
# src/usher/db/models/watch.py
"""User and watch-state tables."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base
from usher.domain.enums import WatchStateOrigin


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WatchStateRow(Base):
    __tablename__ = "watch_states"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE")
    )
    episode_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    position_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runtime_seconds: Mapped[int | None] = mapped_column(Integer)
    played: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    play_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    updated_by: Mapped[WatchStateOrigin] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "title_id", name="uq_watch_states_user_title"),
        UniqueConstraint("user_id", "episode_id", name="uq_watch_states_user_episode"),
        Index("ix_watch_states_user_played", "user_id", "played"),
    )
```

- [ ] **Step 6: Export from the package**

```python
# src/usher/db/models/__init__.py
"""SQLAlchemy tables. Importing this module registers all metadata."""

from usher.db.models.source import MediaItemRow, SourceRow
from usher.db.models.title import TitleRow
from usher.db.models.watch import UserRow, WatchStateRow

__all__ = ["MediaItemRow", "SourceRow", "TitleRow", "UserRow", "WatchStateRow"]
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_db_models.py -v`
Expected: 5 passed

- [ ] **Step 8: Commit**

```bash
git add src/usher/db/models tests/unit/test_db_models.py
git commit -m "feat: SQLAlchemy models for titles, sources, media items, and watch state"
```

---

## Task 9: Alembic migrations

**Files:**
- Create: `alembic.ini`, `src/usher/db/migrations/env.py`, `src/usher/db/migrations/script.py.mako`
- Create: `src/usher/db/migrations/versions/0001_core_schema.py` (generated)

- [ ] **Step 1: Initialise the async template**

```bash
cd ~/code/usher
uv run alembic init -t async src/usher/db/migrations
```

Expected: creates `alembic.ini` and the `migrations/` tree.

- [ ] **Step 2: Point `alembic.ini` at the migrations package**

Edit `alembic.ini` and set exactly these two keys (leave the rest as generated):

```ini
script_location = src/usher/db/migrations
sqlalchemy.url =
```

- [ ] **Step 3: Replace `src/usher/db/migrations/env.py`**

```python
"""Alembic environment. Reads the URL from Usher settings, not alembic.ini."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from usher.config import get_settings
from usher.db.base import Base
from usher.db import models  # noqa: F401  — registers all tables

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: Start Postgres for migration generation**

```bash
docker run -d --name usher-pg-tmp \
  -e POSTGRES_USER=usher -e POSTGRES_PASSWORD=usher -e POSTGRES_DB=usher \
  -p 5432:5432 pgvector/pgvector:pg17
sleep 5
```

Expected: container id printed, then the sleep returns.

- [ ] **Step 5: Generate the initial migration**

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="0123456789abcdef0123456789abcdef"
uv run alembic revision --autogenerate -m "core schema"
```

Expected: `Generating .../versions/<hash>_core_schema.py ... done`

- [ ] **Step 6: Apply and verify**

```bash
uv run alembic upgrade head
uv run alembic current
```

Expected: `<hash> (head)`

Verify tables exist:

```bash
docker exec usher-pg-tmp psql -U usher -d usher -c '\dt'
```

Expected: rows for `alembic_version`, `media_items`, `sources`, `titles`, `users`, `watch_states`

- [ ] **Step 7: Tear down the temporary database**

```bash
docker rm -f usher-pg-tmp
```

- [ ] **Step 8: Commit**

```bash
git add alembic.ini src/usher/db/migrations
git commit -m "feat: alembic async migrations and initial core schema"
```

---

## Task 10: Title repository

**Files:**
- Create: `src/usher/db/repositories/title.py`
- Create: `tests/integration/conftest.py`
- Test: `tests/integration/test_title_repository.py`

- [ ] **Step 1: Write the integration fixtures**

```python
# tests/integration/conftest.py
"""Integration fixtures backed by a real PostgreSQL with pgvector."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.postgres import PostgresContainer

from usher.db.base import Base, build_engine, build_session_factory
from usher.db import models  # noqa: F401  — registers all tables


@pytest.fixture(scope="session")
def postgres_url() -> str:
    with PostgresContainer(
        "pgvector/pgvector:pg17", username="usher", password="usher", dbname="usher"
    ) as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(postgres_url: str) -> AsyncSession:
    engine = build_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = build_session_factory(engine)
    async with factory() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
```

- [ ] **Step 2: Write the failing test**

```python
# tests/integration/test_title_repository.py
import pytest

from usher.db.repositories.title import TitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title


@pytest.fixture
def repo(session) -> TitleRepository:
    return TitleRepository(session)


async def test_add_then_get_round_trips_the_domain_model(repo):
    title = Title(
        kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", year=2021, tmdb_id=438631
    )
    await repo.add(title)
    fetched = await repo.get(title.id)
    assert fetched is not None
    assert fetched.name == "Dune"
    assert fetched.tmdb_id == 438631
    assert fetched.enrichment_state is EnrichmentState.SKELETON


async def test_get_returns_none_for_unknown_id(repo):
    from usher.domain.ids import new_id

    assert await repo.get(new_id()) is None


async def test_get_by_tmdb_id_finds_the_title(repo):
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=438631)
    await repo.add(title)
    found = await repo.get_by_tmdb_id(438631)
    assert found is not None and found.id == title.id


async def test_titles_without_provider_ids_are_allowed(repo):
    title = Title(kind=TitleKind.MOVIE, name="Home Video 1998", sort_name="Home Video 1998")
    await repo.add(title)
    assert (await repo.get(title.id)) is not None


async def test_count_by_state_reports_the_catalog(repo):
    for i in range(3):
        await repo.add(Title(kind=TitleKind.MOVIE, name=f"Film {i}", sort_name=f"Film {i}"))
    counts = await repo.count_by_state()
    assert counts[EnrichmentState.SKELETON] == 3
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_title_repository.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.db.repositories.title'`

- [ ] **Step 4: Write the implementation**

```python
# src/usher/db/repositories/title.py
"""Persistence for canonical titles.

Repositories translate between SQLAlchemy rows and domain models. Nothing
above this layer sees a Row type.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.title import TitleRow
from usher.domain.enums import EnrichmentState
from usher.domain.title import Title


def _to_domain(row: TitleRow) -> Title:
    return Title.model_validate(
        {c.name: getattr(row, c.name) for c in TitleRow.__table__.columns}
    )


def _to_row(title: Title) -> TitleRow:
    return TitleRow(**title.model_dump(exclude={"created_at", "updated_at"}))


class TitleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, title: Title) -> None:
        self._session.add(_to_row(title))
        await self._session.flush()

    async def get(self, title_id: uuid.UUID) -> Title | None:
        row = await self._session.get(TitleRow, title_id)
        return _to_domain(row) if row else None

    async def get_by_tmdb_id(self, tmdb_id: int) -> Title | None:
        result = await self._session.execute(
            select(TitleRow).where(TitleRow.tmdb_id == tmdb_id)
        )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        result = await self._session.execute(
            select(TitleRow).where(TitleRow.imdb_id == imdb_id)
        )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        result = await self._session.execute(
            select(TitleRow.enrichment_state, func.count()).group_by(
                TitleRow.enrichment_state
            )
        )
        return {EnrichmentState(state): count for state, count in result.all()}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_title_repository.py -v`
Expected: 5 passed (first run pulls the `pgvector/pgvector:pg17` image)

- [ ] **Step 6: Commit**

```bash
git add src/usher/db/repositories tests/integration
git commit -m "feat: title repository translating between rows and domain models"
```

---

## Task 11: Telemetry — logging with trace context

**Files:**
- Create: `src/usher/telemetry.py`
- Test: `tests/unit/test_telemetry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_telemetry.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from usher.telemetry import inject_trace_context


def test_no_trace_context_outside_a_span():
    record: dict = {"extra": {}}
    inject_trace_context(record)
    assert "trace_id" not in record["extra"]


def test_trace_context_injected_inside_a_span():
    trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer("test")
    record: dict = {"extra": {}}
    with tracer.start_as_current_span("unit"):
        inject_trace_context(record)
    assert len(record["extra"]["trace_id"]) == 32
    assert len(record["extra"]["span_id"]) == 16
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_telemetry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.telemetry'`

- [ ] **Step 3: Write the implementation**

```python
# src/usher/telemetry.py
"""Logging and tracing setup.

Telemetry is optional: with no OTLP endpoint configured the exporters are
no-ops and Usher runs normally. See PRD 10 and ADR-0007.
"""

import sys
from typing import Any

from loguru import logger
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from usher.config import Settings


def inject_trace_context(record: dict[str, Any]) -> None:
    """Patch the active trace and span ids into every log record, so a line
    in Loki links to its trace and back again."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.is_valid:
        record["extra"]["trace_id"] = format(context.trace_id, "032x")
        record["extra"]["span_id"] = format(context.span_id, "016x")


def configure_logging(settings: Settings) -> None:
    logger.remove()
    logger.configure(patcher=inject_trace_context)
    logger.add(
        sys.stdout,
        level=settings.log_level,
        serialize=settings.log_json,
        backtrace=False,
        diagnose=False,
    )


def configure_tracing(settings: Settings) -> None:
    if not settings.telemetry_enabled:
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
    )
    trace.set_tracer_provider(provider)


def configure_telemetry(settings: Settings) -> None:
    configure_logging(settings)
    configure_tracing(settings)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_telemetry.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/usher/telemetry.py tests/unit/test_telemetry.py
git commit -m "feat: loguru and OpenTelemetry bootstrap with trace-correlated logs"
```

---

## Task 12: FastAPI application and health endpoints

**Files:**
- Create: `src/usher/api/deps.py`, `src/usher/api/routers/health.py`, `src/usher/api/app.py`
- Test: `tests/integration/test_health.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_health.py
import pytest
from httpx import ASGITransport, AsyncClient

from usher.api.app import create_app
from usher.config import Settings


@pytest.fixture
def app(postgres_url: str):
    settings = Settings(
        database_url=postgres_url, secret_key="0123456789abcdef0123456789abcdef"
    )
    return create_app(settings)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_is_liveness_only(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_reports_database_connectivity(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] is True


async def test_openapi_schema_is_served(client):
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Usher"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.api.app'`

- [ ] **Step 3: Write `deps.py`**

```python
# src/usher/api/deps.py
"""Request-scoped dependencies."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
```

- [ ] **Step 4: Write `routers/health.py`**

```python
# src/usher/api/routers/health.py
"""Liveness and readiness.

Readiness is degraded rather than binary, so a dashboard can distinguish
"down" from "running without a source".
"""

from fastapi import APIRouter
from sqlalchemy import text

from usher.api.deps import SessionDep

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Checks nothing external by design."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(session: SessionDep) -> dict[str, object]:
    """Readiness. Reports each dependency separately."""
    checks: dict[str, bool] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False

    return {
        "status": "ready" if all(checks.values()) else "degraded",
        "checks": checks,
    }
```

- [ ] **Step 5: Write `app.py`**

```python
# src/usher/api/app.py
"""Application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from usher.api.routers import health
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.telemetry import configure_telemetry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_telemetry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(settings.database_url.get_secret_value())
        app.state.engine = engine
        app.state.session_factory = build_session_factory(engine)
        yield
        await engine.dispose()

    app = FastAPI(
        title="Usher",
        version="0.1.0",
        description="A self-hosted media catalog backend.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(health.router)
    return app
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_health.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add src/usher/api tests/integration/test_health.py
git commit -m "feat: FastAPI application factory with liveness and readiness endpoints"
```

---

## Task 13: Container and compose

**Files:**
- Create: `Dockerfile`, `compose.yml`

- [ ] **Step 1: Write the `Dockerfile`**

```dockerfile
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000"]
```

- [ ] **Step 2: Write `compose.yml`**

```yaml
services:
  usher:
    build: .
    restart: unless-stopped
    environment:
      USHER_DATABASE_URL: postgresql+asyncpg://usher:usher@postgres:5432/usher
      USHER_SECRET_KEY: ${USHER_SECRET_KEY:?set USHER_SECRET_KEY in .env}
      USHER_TMDB_API_KEY: ${USHER_TMDB_API_KEY:-}
      OTEL_EXPORTER_OTLP_ENDPOINT: ${OTEL_EXPORTER_OTLP_ENDPOINT:-}
      OTEL_SERVICE_NAME: usher
    ports:
      - "8000:8000"
    volumes:
      - ./data/images:/data/images
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    image: pgvector/pgvector:pg17
    restart: unless-stopped
    environment:
      POSTGRES_USER: usher
      POSTGRES_PASSWORD: usher
      POSTGRES_DB: usher
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U usher -d usher"]
      interval: 5s
      timeout: 5s
      retries: 10
```

- [ ] **Step 3: Build and start the stack**

```bash
cd ~/code/usher
echo "USHER_SECRET_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --build
```

Expected: both services start; `usher` becomes healthy after migrations run.

- [ ] **Step 4: Verify the running service**

```bash
sleep 15
curl -sf http://localhost:8000/health
curl -sf http://localhost:8000/health/ready
```

Expected:
```
{"status":"ok"}
{"status":"ready","checks":{"database":true}}
```

- [ ] **Step 5: Stop the stack**

```bash
docker compose down
```

- [ ] **Step 6: Commit**

```bash
git add Dockerfile compose.yml
git commit -m "feat: container image and compose stack with postgres"
```

---

## Task 14: Continuous integration

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Install dependencies
        run: uv sync --frozen

      - name: Lint
        run: uv run ruff check .

      - name: Format check
        run: uv run ruff format --check .

      - name: Type check
        run: uv run mypy

      - name: Architecture contracts
        run: uv run lint-imports

      - name: Tests
        run: uv run pytest --cov=usher --cov-report=term-missing
```

- [ ] **Step 2: Run the full check suite locally**

```bash
uv run ruff check . && uv run ruff format --check . && uv run lint-imports
```

Expected: no lint errors; `Contracts: 3 kept, 0 broken.`

Fix any formatting differences with `uv run ruff format .` and re-run.

- [ ] **Step 3: Run type checking**

Run: `uv run mypy`
Expected: `Success: no issues found`

If errors appear in generated Alembic files, add to `pyproject.toml`:

```toml
[[tool.mypy.overrides]]
module = ["usher.db.migrations.*"]
ignore_errors = true
```

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -v`
Expected: all tests pass (unit tests fast, integration tests start a container)

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml pyproject.toml
git commit -m "ci: lint, format, type check, architecture contracts, and tests"
```

---

## Task 15: Milestone verification

**Files:**
- Modify: `CLAUDE.md` (replace the Commands placeholder)
- Modify: `docs/prd/09-roadmap.md` (mark M1 complete)

- [ ] **Step 1: Verify every acceptance point**

```bash
cd ~/code/usher
uv run pytest -v                      # all tests pass
uv run lint-imports                   # 3 contracts kept
uv run mypy                           # no issues
docker compose up -d --build && sleep 15
curl -sf http://localhost:8000/health/ready
docker compose down
```

Expected: tests pass, contracts kept, mypy clean, readiness reports `"database": true`.

- [ ] **Step 2: Replace the Commands section in `CLAUDE.md`**

Replace the final section of `CLAUDE.md` (currently "None yet — no code exists…") with:

```markdown
## Commands

```bash
uv sync                          # install dependencies
uv run pytest                    # all tests (integration tests start a container)
uv run pytest tests/unit         # fast unit tests only
uv run ruff check . && uv run ruff format .
uv run mypy                      # type check
uv run lint-imports              # enforce architecture contracts
uv run alembic revision --autogenerate -m "message"
uv run alembic upgrade head
docker compose up -d --build     # run the stack
```
```

- [ ] **Step 3: Mark M1 complete in the roadmap**

In `docs/prd/09-roadmap.md`, change the M1 row to:

```markdown
| **M1 — Foundation** ✅ | Repo, uv project, compose, Postgres + migrations, domain models, port ABCs, config, health, CI with layering checks, telemetry bootstrap |
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/prd/09-roadmap.md
git commit -m "docs: record M1 completion and real project commands"
```

---

## Definition of done

- [ ] `uv run pytest` passes, unit and integration
- [ ] `uv run lint-imports` reports 3 contracts kept — the architecture is enforced, not just documented
- [ ] `uv run mypy` is clean under strict mode
- [ ] `docker compose up` produces a service whose `/health/ready` reports the database healthy
- [ ] Migrations create the five core tables from a clean database
- [ ] Logs carry `trace_id` when inside a span, and telemetry is a no-op without an OTLP endpoint
- [ ] `CLAUDE.md` documents real commands
- [ ] `docs/prd/09-roadmap.md` marks M1 complete
