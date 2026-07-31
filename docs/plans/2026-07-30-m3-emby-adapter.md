# Usher M3 — Emby Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Emby reachable through `SourceAdapter` — durable-client authentication that re-mints its own token, a streaming library walk, watch state in both directions, and ranked stream targets — and prove the abstraction is real with a contract suite written so a future Jellyfin or Plex adapter passes it unchanged.

**Architecture:** `EmbyAdapter` (`src/usher/adapters/emby/`) implements the shipped `SourceAdapter` ABC over an `EmbySession` that owns one `httpx.AsyncClient`, attaches the `MediaBrowser Client=…, DeviceId=…` identity header to every request, and silently re-authenticates on any 401 under a single-flight lock. Credentials reach it from a new `CredentialStore` port whose Postgres implementation encrypts with Fernet under a key derived from `USHER_SECRET_KEY`; `SourceRepository` persists the `Source` row that holds the stable `DeviceId` and the opaque `credentials_ref`. The contract suite in `tests/contract/` talks only to a `SourceHarness` ABC — never to Emby JSON — so the same ~30 assertions run against a pure in-memory `FakeSourceAdapter` and against the real `EmbyAdapter` driven by an in-memory Emby behind `httpx.MockTransport`.

**Tech Stack:** Python 3.13 · httpx (`MockTransport` for every test; no network, ever) · cryptography (Fernet + HKDF-SHA256) · SQLAlchemy 2.0 async · Alembic · PostgreSQL 17 · FastAPI · loguru · OpenTelemetry · pytest · testcontainers · ruff · mypy strict · import-linter

**Scope note:** M3 is the spec's "durable-client auth, item listing, watch-state read/write, stream targets; **contract test suite**", plus the two things that milestone cannot exist without: somewhere to keep the credentials it authenticates with, and somewhere to persist the `DeviceId` that makes the client durable. Four things are explicitly out of scope, each for a concrete reason:

- **The WebSocket push listener and the reconnect/reconcile loop are M5.** M3 ships `supports_push -> False` and `events()` raising `SourceNotSupported`, which is exactly the fallback PRD 03 specifies for a source whose socket can't be established. Push is *verified working* (ADR-0004) — it is not blocked, it is sequenced.
- **Ingest, match, enrich, and index are M4.** M3 produces `SourceItem`s; nothing in M3 writes a `MediaItem`, a `Title`, or a `raw_payloads` row. The adapter is the thing; the pipeline that drives it is not.
- **`Season` and `Episode` domain models are M4.** `SourceItem` already carries `series_external_id`, `season_number`, and `episode_number`, so M3's adapter emits fully-formed episode items — but there is no `episodes` table and no `TitleKind` episode member to persist them into. `MediaItem.episode_id` is a dangling column with no FK target and stays that way. This is already recorded in M2's own "what M2 deliberately does not do" table.
- **`POST /admin/sources/{id}/sync` is M4/M5.** It triggers a reconcile, and there is no reconciler. The other four admin-source endpoints *are* in scope, because PRD 07's 🔶 on `verify()` says to settle it "when the Emby adapter and this endpoint are built together", and a status endpoint that nothing serves has not been settled.

**What M3 delivers that M4 depends on:** a `SourceAdapter` that can be handed to an ingest loop, and a contract that says exactly what that loop may assume — including the two guarantees a reconciler's correctness rests on: a walk that raises rather than truncating, and a `get_item` that never reports a network failure as a deletion.

---

## File structure

| File | Responsibility |
|---|---|
| `src/usher/__init__.py` | *(modify)* `__version__`, for the `Version="…"` field of the durable-client header |
| `src/usher/ports/source.py` | *(modify)* settles all three 🔶 markers: `StreamTargetKind`, `StreamTarget.scheme`/`.audio`, `verify() -> SourceStatus`, `SourceStatus`, `SourceAdapterFactory`, canonical provider-id keys |
| `src/usher/ports/credentials.py` | `SourceCredentials`, `CredentialStore` ABC — the `credentials_ref` indirection PRD 08 requires |
| `src/usher/ports/repository.py` | *(modify)* adds `SourceRepository` |
| `src/usher/config.py` | *(modify)* `source_page_size`, `source_timeout_seconds`, `source_reauth_cooldown_seconds` |
| `src/usher/db/models/source.py` | *(modify)* `SourceCredentialRow` |
| `src/usher/db/migrations/versions/d4c9b1e37a05_source_credentials.py` | the `source_credentials` table |
| `src/usher/db/repositories/credentials.py` | `PostgresCredentialStore` — Fernet over HKDF-SHA256(`USHER_SECRET_KEY`) |
| `src/usher/db/repositories/source.py` | `PostgresSourceRepository` |
| `src/usher/adapters/http.py` | `retry_after_seconds` — shared 429 parsing, extracted from `bulk/download.py` |
| `src/usher/adapters/bulk/download.py`, `.../wikidata.py` | *(modify)* import it from there instead of from each other |
| `src/usher/adapters/emby/__init__.py` | package marker |
| `src/usher/adapters/emby/session.py` | `EmbySession` — durable-client header, single-flight 401 re-auth, error translation, `usher.source.request.duration` |
| `src/usher/adapters/emby/mapping.py` | Emby JSON → `SourceItem` / `SourceWatchState`; HDR, audio token, ticks, datetimes |
| `src/usher/adapters/emby/playback.py` | `StreamTarget` construction — direct URL and the Infuse deep link |
| `src/usher/adapters/emby/adapter.py` | `EmbyAdapter` — the `SourceAdapter` implementation |
| `src/usher/adapters/factory.py` | `SourceKind` → adapter; implements `SourceAdapterFactory` |
| `src/usher/services/sources.py` | `SourceService` — register / list / status / remove |
| `src/usher/api/dto/source.py` | request and response DTOs; the password is write-only |
| `src/usher/api/routers/sources.py` | `/admin/sources` |
| `src/usher/api/deps.py`, `src/usher/api/app.py` | *(modify)* wire the service and the router |
| `tests/contract/source_harness.py` | `SourceHarness` ABC — the seam that makes the suite source-agnostic |
| `tests/contract/source_adapter_contract.py` | The suite every `SourceAdapter` must pass |
| `tests/contract/credential_store_contract.py`, `tests/contract/source_repository_contract.py` | Shared suites for the two new repository-shaped ports |
| `tests/fakes/source_adapter.py` | `FakeSourceAdapter` + `FakeSourceHarness` |
| `tests/fakes/emby_server.py` | `FakeEmbyServer` — an in-memory Emby behind `httpx.MockTransport` |
| `tests/fakes/emby_harness.py` | `EmbyHarness` — binds a real `EmbyAdapter` to `FakeEmbyServer` |
| `tests/fakes/credential_store.py`, `tests/fakes/source_repository.py` | in-memory doubles |
| `tests/fakes/emby_fixtures.py` | loader for the committed payloads, shared by the mapping tests and the fake server |
| `tests/fixtures/emby/*.json` | shape-recorded, value-synthetic Emby payloads — **never a real library's data** |
| `pyproject.toml` | *(modify)* adds `cryptography`; widens `tests/**` per-file ignores to `S105`/`S106`/`S107` |
| `tests/integration/conftest.py` | *(modify)* drops the `# noqa: S106` those ignores make redundant |
| `scripts/capture_emby_fixture.py` | operator tool: re-derive a scrubbed fixture from a live server. **Not a test** |
| `docs/prd/decisions/0012-playback-urls-carry-a-source-token.md` | ADR — the one place PRD 08's "no credential reaches a client" is knowingly bent |
| `docs/prd/decisions/0013-contract-suite-drives-a-source-harness.md` | ADR — why the suite is harness-driven rather than cassette-driven |

---

## Facts this plan builds on, and does not re-derive

1. **Emby push works, verified end to end 2026-07-29** against the live server with a **non-admin** token: `/embywebsocket?api_key=…&deviceId=…` returns 101, delivers periodic `Sessions`, and pushes `UserDataChanged` within seconds of an out-of-band played/unplayed change. Two earlier negative findings were both wrong. [ADR-0004](../prd/decisions/0004-push-over-polling.md).
2. **A successful WebSocket upgrade is not a health signal.** A handshake against a *nonexistent* path also upgrades and also receives `Sessions`. Any push-health claim must assert on *received messages*. That is M5's problem to implement, and M3's job is only to not design it away: `SourceStatus.push_available` is `bool | None`, M3 always returns `None` ("not probed"), and Task 10's contract case asserts that no implementation may report `True` without evidence.
3. **Emby has no OAuth2.** There is no refresh-token flow to build against. The durable-client pattern below *is* the refresh mechanism.
4. **The durable-client pattern** (PRD 03): `Authorization: MediaBrowser Client="Usher", Device="<source name>", DeviceId="<persisted UUID>", Version="<app version>"`, then `POST /Users/AuthenticateByName` with `{"Username": …, "Pw": …}` → `AccessToken` and `User.Id`. `DeviceId` is generated once and persisted on the `Source` row so Usher is one device, not an accumulating pile of sessions. **Any 401 triggers silent re-authentication** with the stored credentials and the same `DeviceId`. A dead token in a Home Assistant dashboard is the concrete failure that motivated this project; no human pastes a token here.
5. **This deployment holds 94,395 movies across 17 libraries** (measured on the deployment Usher was built for). `list_items` must stream a page at a time and hold no more than one page; it must never build a list.
6. **`SourceAdapter.supports_push` already exists** on the shipped port (`src/usher/ports/source.py`, lines 143–150, from M1). It is not a gap. M3 implements it, returning `False` until M5's socket exists.
7. **`SourceEvent`'s empty payload is correctly deferred to M5** and this plan does not touch it. Its 🔶 says the cost of re-walking `watch_state(since=…)` is only measurable once the push lane exists — still true, because M3 builds no push lane.
8. **Verified while writing this plan, against this checkout:** `uv run pytest` collects **467 tests (372 unit, 95 integration)**; `uv run mypy` reports success over **101 source files**; `uv run lint-imports` reports **5 kept, 0 broken**; `ruff check`/`format --check` are clean over 106 files.

Six smaller facts were checked directly rather than remembered, because a fence that assumes otherwise does not work:

| Checked | Result |
|---|---|
| `httpx.MockTransport(handler)` with `handler: Callable[[httpx.Request], httpx.Response]` | passes mypy strict with **no** `type: ignore` — M2's `tests/unit/test_adapters_bulk_download.py` needed one only because it typed the parameter `object` |
| A closed `httpx.AsyncClient` | raises `RuntimeError`, **not** an `httpx.HTTPError` — so `except httpx.HTTPError` does not catch it and the adapter needs its own closed-flag guard, or a raw `RuntimeError` escapes the port |
| `datetime.fromisoformat` on Python 3.13 | accepts Emby's 7-digit fractional seconds and a trailing `Z` — **but** a value with no offset yields a **naive** datetime, which `SourceItem` (a plain dataclass) will not catch |
| `cryptography` 49.0.0 | installs as a wheel, ships `py.typed`; `HKDF(SHA256).derive()` + `Fernet` + `InvalidToken` all pass mypy strict |
| ruff `S105`/`S106` | fire on `password="…"` keyword arguments and on module constants named `TOKEN`/`PASSWORD` — `tests/**` needs both added to `per-file-ignores`, and `src/` constants must avoid the word "token" in their *names* (`_EMBY_AUTH_HEADER = "X-Emby-Token"` is clean; `_TOKEN_HEADER = …` is not) |
| `importlib.metadata.version("usher")` | returns `"0.1.0"` in this checkout's editable install and in the container's `uv sync`-installed one |

---

## Which Emby routes are guessed, and how the plan makes that safe

Every upstream path this milestone uses is a module constant in `src/usher/adapters/emby/adapter.py` or `session.py`, annotated with whether it has been exercised against the live server. Two are load-bearing and confirmed by ADR-0004's own verification session (`POST /Users/AuthenticateByName`, and a REST played/unplayed toggle). The rest are the well-established Emby 4.9 routes, and **the definition of done requires running the real adapter against the real server before M3 is called complete** — the fake server cannot catch a wrong-but-self-consistent path, and nothing in this plan pretends it can.

Two design choices bound the damage from a wrong guess:

- **`list_items` sends its delta filter as a query parameter, and Emby ignores unknown query parameters.** A wrong parameter name therefore degrades to a *full walk* — a safe superset — not to a silently empty result. The nightly full reconcile (PRD 03) is the same walk with no filter, so the worst case is the behaviour the design already budgets for.
- **`stream_targets` does not call `/Items/{id}/PlaybackInfo`.** That endpoint exists for transcode negotiation, which Usher explicitly does not do (PRD 07: Usher never proxies bytes and never chooses a stream). Everything the direct-play URL needs — container, `MediaSourceId`, resume position — is already on the item, so this is one fewer guessed endpoint *and* one fewer request against an upstream measured at 1–5 s per request.

---

## What the contract suite rules out

The spec calls this "the test that proves the abstraction is real". A suite that only pins method signatures proves nothing — M2 shipped a contract suite that passed against a repository carrying four injected defects before it was tightened. So each case below is written against a specific wrong implementation, and the plan states which.

| Contract case | The wrong implementation it fails |
|---|---|
| `test_list_items_yields_every_seeded_item` | An adapter that stops after the first page. Seven items over a page size of two means four pages. |
| `test_list_items_raises_rather_than_truncating` | An adapter whose generator swallows an upstream error and just stops. The reconciler cannot distinguish that from "the library ended" and would mark the rest of the library `available = false`. |
| `test_list_items_streams_rather_than_materialising` | An adapter that builds the whole library into a list before yielding. At 94,395 movies that is the memory failure; here it fails because the first item must arrive *before* the page that errors. |
| `test_list_items_since_is_inclusive` | An exclusive `>` filter that drops the item that changed exactly at the cursor. |
| `test_list_items_since_does_not_invert_the_window` | A filter sent with the comparison reversed, returning only items *older* than the cursor. |
| `test_provider_ids_use_canonical_lowercase_keys` | An adapter that passes Emby's `"Tmdb"` straight through, so M4's matcher has to know each source's casing. |
| `test_hdr_format_is_the_canonical_enum` | The typing drift PRD 02 names explicitly: Emby's `"DolbyVision"` reaching `MediaItem` as a raw string. |
| `test_added_at_is_timezone_aware` | The verified `fromisoformat` trap — a naive datetime that constructs fine on a plain dataclass and only explodes at the `TIMESTAMPTZ` column. |
| `test_get_item_returns_none_only_for_a_deletion` + `test_get_item_raises_when_the_source_is_unreachable` | The single most dangerous wrong implementation: reporting a transient network failure as `None`, which marks a healthy item unavailable because of a flaky network. |
| `test_operations_recover_from_an_expired_credential` | (a) No re-authentication at all — the original Home Assistant failure. (b) A re-auth storm: four concurrent 401s producing four `AuthenticateByName` calls — **but (b) only against a harness whose transport genuinely overlaps**, which it reports via `SourceHarness.observed_overlap`. `EmbyHarness` does (`SlowTransport`); `FakeSourceHarness` does not and claims only (a). Measured, with the mutations, in Task 9 Step 2. |
| `test_rejected_credentials_do_not_produce_a_request_storm` | An adapter that retries authentication on every call when the password is simply wrong. |
| `test_push_watch_state_is_visible_to_the_source` | A no-op `push_watch_state`. The port's docstring warns that swallowing here means the retry never happens; a test that only asserted "it didn't raise" would pass against a `pass` body. |
| `test_push_watch_state_raises_on_failure` | Swallowing the write-back error, which converts "enqueue a retry" into "lose the write". |
| `test_verify_reports_bad_credentials_without_raising` / `…_an_unreachable_source` | A `verify()` that collapses both into one bool, which is exactly what the 🔶 was about. |
| `test_verify_does_not_claim_push_without_evidence` | An adapter that reports `push_available=True` off a successful handshake — the documented Emby quirk where *any* path upgrades. |
| `test_stream_targets_rank_a_direct_target_first` + `…_carry_the_quality_facts` | A target list a client cannot choose from: no container, no codec, no resolution. |
| `test_stream_targets_are_empty_for_something_unplayable` | An adapter that fabricates a stream URL for a series folder. |
| `test_operations_after_aclose_raise_port_unavailable` | The verified `RuntimeError` leak: httpx's closed-client error is not an `httpx.HTTPError`, so an adapter that only translates `httpx.HTTPError` lets a raw stdlib exception cross the port boundary. |
| `test_events_is_offered_exactly_when_supports_push_says_so` | An adapter that advertises push it does not have (or has push it does not advertise), which would make the reconciler skip a source it must cover. |

**Two implementations run it in M3**, and the pair is the point. `FakeSourceAdapter` is a pure in-memory `SourceAdapter` with no HTTP anywhere — if the suite passes against it, the suite is not accidentally Emby-shaped. `EmbyAdapter` runs against `FakeEmbyServer` — if the suite passes against *that*, the abstraction survives a real wire format. Neither alone would be evidence.

**Where the fake is deliberately weak, and why that is honest.** `FakeSourceAdapter` stores the very `SourceItem`s the harness seeds, so its round-trip cases are close to tautological. They are not there to find bugs in the fake; they are there to prove each assertion is *expressible* without reference to Emby. The round-trip has teeth only in `EmbyHarness`, where a seeded `SourceItem` becomes Emby JSON and has to come back. The fake does model one behaviour for real — a token that can expire and must be silently re-minted — because that is the port's central promise and a fake that no-op'd it would let a broken `expire_credentials()` case pass on both implementations.

---

## Task 1: Settle `usher.ports.source`, and add the credentials port

Three 🔶 markers name M3. This task closes two of them and records why the third stays.

- `StreamTarget` gains `scheme` and `audio` (PRD 07's `/play` response shows both), and `kind` becomes a `StreamTargetKind` rather than a bare `str` — the same fix `SourceItemKind` already got.
- `verify() -> bool` becomes `verify() -> SourceStatus`, because `GET /admin/sources/{id}/status` must report bad credentials, unreachable, and reachable-but-push-blocked as **separate** states and a bool cannot. It returns a value rather than raising: the taxonomy in `usher.ports.errors` governs every *other* method, but a status probe whose entire job is reporting failure kinds should not make its one caller write an exception ladder to render a table.
- `SourceEvent`'s empty payload is **left alone** — its own marker defers it to M5 on the grounds that the cost of re-walking is only measurable once the push lane exists, and M3 builds no push lane.

`SourceAdapterFactory` is added here too, because `services/` may not import `adapters/` (PRD 01, layering rule 2) and `SourceService` in Task 11 still has to end up holding an `EmbyAdapter`.

**Files:**
- Modify: `src/usher/ports/source.py`
- Create: `src/usher/ports/credentials.py`
- Modify: `src/usher/__init__.py`
- Modify: `src/usher/config.py`
- Modify: `tests/unit/test_ports.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/test_ports_source.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ports_source.py
"""The source port's settled shape.

Every 🔶 marker in `usher/ports/source.py` that named M3 has an assertion
here, and each one is written so that reverting the corresponding
production line fails it — not so that it reads as a description of the
code.
"""

import dataclasses
import inspect
from abc import ABC

import pytest
from pydantic import SecretStr

from usher.domain.enums import HdrFormat
from usher.ports.credentials import CredentialStore, SourceCredentials
from usher.ports.source import (
    CANONICAL_PROVIDER_IDS,
    SourceAdapter,
    SourceAdapterFactory,
    SourceStatus,
    StreamTarget,
    StreamTargetKind,
)


def test_stream_target_carries_scheme_and_audio() -> None:
    """PRD 07's `/play` response documents both, and the deep-link
    construction "currently done by hand in the Home Assistant card" cannot
    move here until the DTO can express it."""
    target = StreamTarget(
        kind=StreamTargetKind.DEEP_LINK,
        url="infuse://x-callback-url/play?url=https%3A%2F%2Fexample.invalid%2Fa.mkv",
        scheme="infuse",
    )
    assert target.scheme == "infuse"
    direct = StreamTarget(
        kind=StreamTargetKind.DIRECT,
        url="https://example.invalid/a.mkv",
        container="mkv",
        video_codec="hevc",
        audio="truehd_atmos_7_1",
        hdr_format=HdrFormat.DOLBY_VISION,
        resolution="3840x2160",
        runtime_seconds=9360,
        resume_position_seconds=1840,
    )
    assert direct.audio == "truehd_atmos_7_1"
    assert direct.scheme is None


def test_stream_target_kind_is_an_enum_not_a_string() -> None:
    """Same fix `SourceItemKind` already got: a bare `str` field invites
    `kind="deeplink"` (no underscore) to reach a client, where it silently
    matches nothing."""
    assert StreamTargetKind.DIRECT == "direct"
    assert StreamTargetKind.DEEP_LINK == "deep_link"
    assert set(StreamTargetKind) == {StreamTargetKind.DIRECT, StreamTargetKind.DEEP_LINK}


def test_stream_target_is_frozen() -> None:
    target = StreamTarget(kind=StreamTargetKind.DIRECT, url="https://example.invalid/a.mkv")
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.url = "https://elsewhere.invalid/b.mkv"  # type: ignore[misc]


def test_verify_returns_a_status_not_a_bool() -> None:
    """The 🔶 this settles: `GET /admin/sources/{id}/status` (PRD 07) has to
    report bad credentials, unreachable, and reachable-but-push-blocked as
    distinct states."""
    assert inspect.signature(SourceAdapter.verify).return_annotation == "SourceStatus"


def test_source_status_separates_reachable_from_authenticated() -> None:
    status = SourceStatus(reachable=True, authenticated=False, detail="401 from /System/Info")
    assert status.reachable is True
    assert status.authenticated is False


def test_source_status_rejects_authenticated_but_unreachable() -> None:
    """An invariant, not decoration: a status object that claims both would
    render as a contradiction in the admin UI and there is no upstream
    behaviour that produces it."""
    with pytest.raises(ValueError, match="reachable"):
        SourceStatus(reachable=False, authenticated=True)


def test_source_status_rejects_push_without_authentication() -> None:
    with pytest.raises(ValueError, match="authenticated"):
        SourceStatus(reachable=True, authenticated=False, push_available=True)


def test_push_available_defaults_to_unknown_not_false() -> None:
    """`None` means "not probed". This is the health-check caveat in DTO
    form: a successful upgrade proves nothing (ADR-0004 — a handshake
    against a *nonexistent* path also upgrades and also receives
    `Sessions`), so an adapter with no message-level evidence must be able
    to say "I don't know" rather than being forced to pick a bool."""
    assert SourceStatus(reachable=True, authenticated=True).push_available is None


def test_canonical_provider_ids_are_lowercase() -> None:
    """Cross-source normalisation, not cosmetics: M4's matcher reads
    `provider_ids["tmdb"]` and must not have to know that Emby spells it
    `Tmdb` and something else spells it `TMDB`."""
    assert CANONICAL_PROVIDER_IDS == frozenset({"tmdb", "imdb", "tvdb"})
    assert all(key == key.lower() for key in CANONICAL_PROVIDER_IDS)


def test_source_credentials_password_is_a_secret() -> None:
    """PRD 08's "credentials are never logged" enforced by the type system
    rather than by discipline — the same standard `Settings` already holds
    for `database_url`/`secret_key`/`tmdb_api_key`."""
    credentials = SourceCredentials(username="usher", password=SecretStr("hunter2"))
    assert "hunter2" not in repr(credentials)
    assert "hunter2" not in str(credentials)
    assert credentials.password.get_secret_value() == "hunter2"


def test_credential_store_is_an_abc() -> None:
    assert issubclass(CredentialStore, ABC)
    assert CredentialStore.__abstractmethods__ == frozenset({"put", "get", "delete"})


def test_source_adapter_factory_is_an_abc() -> None:
    """`services/` may depend only on `domain/` and `ports/` (PRD 01,
    layering rule 2), so `SourceService` cannot import `EmbyAdapter`. This
    is the seam that lets it hold one anyway — and the one place a Jellyfin
    adapter would be registered."""
    assert issubclass(SourceAdapterFactory, ABC)
    assert SourceAdapterFactory.__abstractmethods__ == frozenset({"build"})


def test_source_adapter_still_declares_supports_push() -> None:
    """Already shipped in M1 — asserted here so a future edit that "cleans
    up" the unimplemented property is caught. PRD 03 needs it: an adapter
    whose socket cannot be established reports `False` and the reconciler
    covers the gap."""
    assert "supports_push" in SourceAdapter.__abstractmethods__
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_ports_source.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.ports.credentials'`

- [ ] **Step 3: Write `src/usher/ports/credentials.py`**

```python
"""Port for the credentials a source adapter authenticates with.

PRD 08: source credentials are **encrypted at rest** under
`USHER_SECRET_KEY`, `Source.credentials_ref` points at the encrypted row,
and the plaintext exists only in memory in the adapter that needs it. This
port is that indirection made concrete — a service holds a
`credentials_ref`, asks a `CredentialStore` for the secret, hands it
straight to a `SourceAdapter`, and never persists, returns, or logs it.

Separate from `SourceRepository` on purpose. Both could have been one port
with a `credentials` field on `Source`, and that is exactly the shape PRD
08's "credentials are never returned by any API, including admin" is
hardest to hold: every read of a source would carry the secret, and
write-only would be a convention enforced by whoever remembered. Splitting
them makes the read of a credential a deliberate, separately-auditable call
that the admin API simply never makes.

`password` is a `pydantic.SecretStr`, not a `str`, so the never-logged rule
is enforced by the type system rather than by discipline: `repr()` and
`str()` of a `SecretStr` are `'**********'`, so a credential cannot reach a
log line, a loguru record, a traceback frame summary, or an exception
message by accident. `usher.config.Settings` already holds `database_url`,
`secret_key`, and `tmdb_api_key` the same way.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import SecretStr


@dataclass(frozen=True)
class SourceCredentials:
    """What a source needs in order to authenticate. Plaintext, in memory
    only, for the lifetime of one adapter.

    A plain dataclass rather than a `DomainModel` for the same reason
    `SourceItem` is one: it crosses a port boundary, it is never persisted
    in this shape, and it is never revalidated on the way back in.
    """

    username: str
    password: SecretStr


class CredentialStore(ABC):
    """Encrypted-at-rest storage for `SourceCredentials`, addressed by an
    opaque `credentials_ref`.

    The ref is opaque and unguessable rather than derived from the source id
    (`f"source:{id}"` would have worked and been simpler): a derived ref
    makes the indirection decorative, and rotation — write the new secret
    under a new ref, flip `Source.credentials_ref`, delete the old row —
    stops being expressible at all. `owner_id` exists so a backing store can
    cascade the delete when its owner goes away, which is what stops a
    crash between "delete the source" and "delete its credential" from
    leaving an encrypted orphan nobody can attribute.
    """

    @abstractmethod
    async def put(self, ref: str, credentials: SourceCredentials, *, owner_id: uuid.UUID) -> None:
        """Store (or replace) the credentials at `ref`.

        An upsert, not an insert: re-registering a source with a corrected
        password must overwrite, and rotation writes over the same ref.
        Same session/transaction ownership as `TitleRepository` — flushes,
        never commits.
        """

    @abstractmethod
    async def get(self, ref: str) -> SourceCredentials | None:
        """Decrypt and return the credentials at `ref`, or `None` if no such
        ref exists.

        `None` means "nothing is stored here" and nothing else. A stored
        value that cannot be *decrypted* — the key was rotated, the row was
        corrupted — raises `PortDataMalformed` (`usher.ports.errors`)
        instead, because retrying will not help and the operator has to
        re-enter the credential or restore the key. Returning `None` for
        that case would present a recoverable, operator-visible problem as
        an absent source.
        """

    @abstractmethod
    async def delete(self, ref: str) -> None:
        """Remove the credentials at `ref`. Idempotent: deleting a ref that
        does not exist is not an error, so a partially-failed source
        deletion can be retried."""
```

- [ ] **Step 4: Rewrite `src/usher/ports/source.py`**

Replace the whole file. Everything before `SourceEvent` is unchanged except the new `StreamTargetKind`; the changes are `CANONICAL_PROVIDER_IDS`, `StreamTarget`, `SourceStatus`, `verify()`, and `SourceAdapterFactory`.

```python
"""Port for media sources, and the DTOs that cross that boundary."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime

from usher.domain.enums import HdrFormat
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import UsherPortError

# The provider-id keys every adapter must emit under these exact names
# whenever it knows them. Sources spell them differently -- Emby's
# `ProviderIds` uses `Tmdb`/`Imdb`/`Tvdb` -- and normalising at the adapter
# boundary is what keeps M4's matcher from having to know one casing per
# source. Keys outside this set are permitted (a source that knows an AniDB
# id should say so) but must be lowercase, so the rule is "lowercase always,
# these three names when known" rather than a closed vocabulary.
CANONICAL_PROVIDER_IDS: frozenset[str] = frozenset({"tmdb", "imdb", "tvdb"})


class SourceEventKind(StrEnum):
    ITEM_ADDED = "item_added"
    ITEM_UPDATED = "item_updated"
    ITEM_REMOVED = "item_removed"
    WATCH_STATE_CHANGED = "watch_state_changed"


class SourceItemKind(StrEnum):
    """A source's own idea of what kind of thing an item is — narrower
    than `usher.domain.enums.TitleKind` because sources address individual
    episodes directly, unlike `Title`."""

    MOVIE = "movie"
    SERIES = "series"
    EPISODE = "episode"


class StreamTargetKind(StrEnum):
    """What a client is expected to do with a `StreamTarget.url`.

    A `StrEnum` rather than the bare `str` this field carried through M1 and
    M2, for the reason `SourceItemKind` exists: PRD 07 puts these values on
    the wire, and a bare `str` invites `"deeplink"` (no underscore) to be
    serialized to a client that matches on `"deep_link"` and silently
    renders nothing.
    """

    DIRECT = "direct"
    DEEP_LINK = "deep_link"


@dataclass(frozen=True)
class SourceItem:
    """One playable item as the source describes it, already normalised.

    A plain dataclass, not a `DomainModel` — nothing here is validated at
    construction. `SourceItemKind`, `HdrFormat`, and `AwareDatetime` below
    state the contract an adapter must uphold, the same way `MediaItem`
    and `Title` enforce it on the far side of the ingest boundary;
    constructing this with a naive `datetime` or a source's raw HDR string
    (e.g. Emby's `"DolbyVision"`) will not raise here — only later, if and
    when something re-validates it, which is one layer too late.

    `provider_ids` keys are lowercase and use `CANONICAL_PROVIDER_IDS`'
    names where they apply — see that constant.
    """

    external_id: str
    name: str
    kind: SourceItemKind
    year: int | None = None
    provider_ids: dict[str, str] = field(default_factory=dict)
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    hdr_format: HdrFormat | None = None
    audio_channels: int | None = None
    file_size_bytes: int | None = None
    runtime_seconds: int | None = None
    added_at: AwareDatetime | None = None
    series_external_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    # Opaque; stored in raw_payloads (PRD 03) for debugging and future
    # reprocessing, never interpreted above the adapter boundary. The one
    # deliberate exception to "nothing source-specific escapes its
    # adapter" — every other field above exists so this one doesn't have
    # to be read by anything above the adapter.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceWatchState:
    external_id: str
    position_seconds: int
    played: bool
    play_count: int = 0
    last_played_at: AwareDatetime | None = None
    # Emby is multi-user; None means "the source didn't distinguish", which
    # today is fine because everything implicitly lands on the singleton
    # default user (PRD 01's authentication seam). Cheap to carry now —
    # becomes a breaking DTO change the moment a household has two users.
    source_user_id: str | None = None


@dataclass(frozen=True)
class WatchStateUpdate:
    position_seconds: int
    played: bool


@dataclass(frozen=True)
class SourceEvent:
    """🔶 Provisional — carries no payload, so a `WATCH_STATE_CHANGED`
    event forces the push lane to re-walk `watch_state(since=...)` to
    discover what changed, even though Emby's own `UserDataChanged`
    message already carries the position and played flag. Settle in M5,
    when the push lane is actually built and the cost of re-walking is
    measurable against just carrying the payload through.

    Reviewed in M3 and deliberately left alone: M3 builds no push lane, so
    the measurement this marker is waiting for is still not available.
    """

    kind: SourceEventKind
    external_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StreamTarget:
    """How to play an item. Clients choose between the returned targets.

    Complete information, not a decision: PRD 07's playback contract is
    that Usher "supplies complete information and never proxies bytes",
    so a target carries everything a client needs to decide whether it can
    play this — container, codecs, HDR format, resolution — rather than a
    server-side guess at which one it should use.

    `scheme` is set only for `StreamTargetKind.DEEP_LINK` targets and names
    the URL scheme (`"infuse"` for `infuse://…`), so a client can check
    whether it can handle the link without parsing the URL. `audio` is a
    single lowercase token describing the default audio track as a client
    thinks about it (`"truehd_atmos_7_1"`), which is a different thing from
    `SourceItem.audio_codec`'s raw `"truehd"` — the codec alone does not
    tell a client whether it can play the track.
    """

    kind: StreamTargetKind
    url: str
    scheme: str | None = None
    container: str | None = None
    video_codec: str | None = None
    audio: str | None = None
    hdr_format: HdrFormat | None = None
    resolution: str | None = None
    runtime_seconds: int | None = None
    resume_position_seconds: int | None = None


@dataclass(frozen=True)
class SourceStatus:
    """What `GET /admin/sources/{id}/status` (PRD 07) needs to report.

    Three booleans rather than one enum, because the states are
    independent: "reachable but the credentials are wrong" and "reachable,
    authenticated, but a proxy is stripping `Upgrade`" are both real, and a
    flat enum would have to enumerate the product.

    `push_available` is `bool | None`, and `None` — "not probed" — is the
    default. This is ADR-0004's health-check caveat in DTO form: a
    WebSocket handshake against a *nonexistent* path also upgrades and also
    receives `Sessions`, so a successful upgrade is not evidence of
    anything. Only *received messages* are. Until M5 builds a probe that
    asserts on messages, every adapter reports `None` here, and the admin
    surface renders "unknown" rather than a guess.

    `detail` is a short operator-facing string — a status line, not a
    payload. It must never carry a credential: an implementation builds it
    from its own translated `UsherPortError`s, whose messages carry a
    method, a path, and a transport error, never a token or a password.
    """

    reachable: bool
    authenticated: bool
    push_available: bool | None = None
    server_version: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.authenticated and not self.reachable:
            raise ValueError("a source cannot be authenticated without being reachable")
        if self.push_available and not self.authenticated:
            raise ValueError("push cannot be available without being authenticated")


class SourceNotSupported(UsherPortError):
    """Raised by adapters for capabilities they do not have."""


class SourceAdapter(ABC):
    """A backend that holds playable media.

    Nothing source-specific may escape an implementation of this port.
    """

    @property
    @abstractmethod
    def source_id(self) -> uuid.UUID:
        """The configured Source this adapter serves."""

    @property
    @abstractmethod
    def supports_push(self) -> bool:
        """Whether this adapter has a live push channel right now. PRD 03:
        when the socket can't be established (or drops and stays down
        after N reconnect attempts), the adapter reports this `False` and
        the reconciler's nightly walk covers the gap. Mirrors
        `usher.domain.source.Source.supports_push`, which this populates.

        Must agree with `events()`: if this is `False`, `events()` raises
        `SourceNotSupported`; if it is `True`, `events()` yields a channel.
        An adapter that advertises push it does not have makes the
        reconciler skip a source it is the only cover for.
        """

    @abstractmethod
    async def verify(self) -> SourceStatus:
        """Report reachability, authentication, and push availability.

        Returns rather than raises for every *expected* failure —
        unreachable host, rejected credentials, a rate-limited upstream —
        because its one caller (`GET /admin/sources/{id}/status`, PRD 07)
        exists to render those states, not to handle them. The taxonomy in
        `usher.ports.errors` still governs every other method on this port;
        this is the deliberate exception, and it is why the method returns
        a `SourceStatus` rather than a bool.

        Must not claim `push_available=True` without message-level
        evidence — see `SourceStatus`.
        """

    @abstractmethod
    def list_items(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        """Walk the library, or only items changed since a cursor.

        Contract an implementation must guarantee:
        - `since` is inclusive: an item changed exactly at `since` is
          included, never dropped at the boundary.
        - No ordering is promised across items; callers must not rely on
          one.
        - The same item may be yielded more than once in a single walk
          (e.g. a paginated upstream listing whose pages overlap); callers
          deduplicate by `external_id`.
        - **Must stream, not materialise.** One upstream page may be held
          at a time; the walk may not build the library into a list first.
          The deployment this was built for holds 94,395 movies across 17
          libraries.
        - **Must raise, never truncate silently.** An iterator that stops
          because the walk finished is indistinguishable from one that
          stopped because the adapter swallowed an error — and the
          reconciler cannot tell the difference; it would mark the rest of
          the library `available = false`. A partial failure raises (e.g.
          `PortUnavailable` from `usher.ports.errors`) from the generator;
          it does not just stop yielding.
        """

    @abstractmethod
    async def get_item(self, external_id: str) -> SourceItem | None:
        """Fetch one item.

        `None` means the item is gone from the source — PRD 03's
        reconcile marks it `available = false`. A transient failure to
        reach the source is a different outcome and must raise (e.g.
        `PortUnavailable` from `usher.ports.errors`), never be reported as
        `None`; conflating the two would mark a healthy item unavailable
        because of a flaky network, not because it was actually deleted.
        """

    @abstractmethod
    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        """Ranked ways to play an item, best first.

        Empty for an item there is no way to play — a series or season
        folder, or an id the source does not have. Not an error: the
        caller's next move is identical in both cases ("not playable
        here"), and `get_item` already exists to tell absence from
        presence, so raising would only make the common case
        (`POST /titles/{id}/play` for something owned but not playable)
        travel through an exception path.
        """

    @abstractmethod
    def watch_state(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceWatchState]:
        """Watch state from the source, optionally since a cursor.

        Same `since`-inclusivity, no-ordering, possible-duplicates,
        must-stream, and must-raise-never-truncate contract as
        `list_items`.

        Emits a state for every item the walk covers, including states that
        are entirely zero. Filtering those out looks like an obvious saving
        and is a correctness bug: un-marking something played *is* an
        all-zero state, so an implementation that skipped them could never
        propagate a reset — the delta walk would find the changed item and
        then discard exactly the record describing the change.
        """

    @abstractmethod
    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        """Write watch state back to the source.

        Must raise on failure, never swallow it. PRD 03's "best-effort"
        describes the *caller's* behaviour — the request that triggered
        this write never blocks or fails on a write-back error, because
        the caller enqueues a retry instead — not this method's. That
        guarantee only works if failures are visible: an implementation
        that swallows an error here means the retry never happens.
        """

    @abstractmethod
    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        """Push channel. Adapters without one raise SourceNotSupported; the
        reconciler covers them. Must agree with `supports_push`."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources — connection pools, and (from M5) the
        push WebSocket. Called when a source is deleted (`DELETE
        /admin/sources/{id}`, PRD 07) or the process shuts down.

        Idempotent: calling it twice is not an error, because a shutdown
        path and a delete path can both reach it. Afterwards every other
        method raises `PortUnavailable` rather than whatever the underlying
        client happens to raise — verified: a closed `httpx.AsyncClient`
        raises a bare `RuntimeError`, which is not an `httpx.HTTPError` and
        so escapes an adapter that only translates those.
        """


class SourceAdapterFactory(ABC):
    """Builds the right `SourceAdapter` for a configured `Source`.

    Exists because `services/` may depend only on `domain/` and `ports/`
    (PRD 01, layering rule 2), so `SourceService` cannot import
    `EmbyAdapter` — it receives one. This is also the single place a second
    source kind gets registered, which is the concrete form of PRD 01's
    "additional sources" extension seam: a Jellyfin adapter adds a
    `SourceKind` member and one branch here, and nothing else in the
    application moves.
    """

    @abstractmethod
    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        """Construct an adapter. The caller owns it and must `aclose()` it.

        Raises `SourceNotSupported` for a `Source.kind` this factory has no
        implementation for.
        """
```

- [ ] **Step 5: Add `__version__` and the three source settings**

Replace `src/usher/__init__.py` (currently empty):

```python
"""Usher — a self-hosted media catalog backend."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("usher")
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
```

In `src/usher/config.py`, add three fields immediately after the `bulk_user_agent` block:

```python
    # Source adapters (PRD 03). Same reasoning as the bulk settings above:
    # PRD 08 puts knobs like these in a TOML config layer that does not
    # exist yet. Deliberately named `source_*`, not `emby_*` -- config.py is
    # not an adapter, and a setting named for one media server would be the
    # first source-specific concept to escape `adapters/`.
    source_page_size: int = Field(default=200, ge=1, le=1000)
    source_timeout_seconds: float = Field(default=30.0, gt=0)
    # How long a rejected credential is remembered before another
    # authentication is attempted. Without this, a source configured with a
    # wrong password turns every request into two (the call, then a doomed
    # re-authentication) for as long as it stays wrong.
    source_reauth_cooldown_seconds: float = Field(default=60.0, ge=0)
```

- [ ] **Step 6: Extend `ALL_PORTS`, and let tests hold password literals**

In `tests/unit/test_ports.py`, add the three new ports to the import block and to `ALL_PORTS`:

```python
from usher.ports.credentials import CredentialStore
from usher.ports.repository import TitleRepository
from usher.ports.source import SourceAdapter, SourceAdapterFactory, SourceNotSupported
```

```python
ALL_PORTS: list[type[ABC]] = [
    SourceAdapter,
    SourceAdapterFactory,
    CredentialStore,
    MetadataProvider,
    SearchIndex,
    Embedder,
    LLMClient,
    TitleRepository,
]
```

In `pyproject.toml`, extend the per-file ignores. All three codes were verified to fire on shapes this milestone's tests are full of: `S105` on a module constant whose *name* contains `token`/`password`/`secret`, `S106` on a `password=`/`access_token=` keyword argument with a string literal, and `S107` on a `password: str = "…"` parameter default (which `FakeEmbyServer.__init__` has).

```toml
[tool.ruff.lint.per-file-ignores]
# assert is how pytest tests assert; not a bandit finding here.
# S105/S106/S107: M3's fixtures and fakes are built around literal
# usernames, passwords, and session tokens -- a test for "a rejected
# credential does not produce a request storm" has to name a wrong password
# somewhere -- and a `# noqa` on every one of them is noise that would
# eventually get copied into `src/`, where these rules still apply and
# still matter.
"tests/**" = ["S101", "S105", "S106", "S107"]
```

**Then delete the now-redundant suppression in `tests/integration/conftest.py`**, on the `PostgresContainer(...)` call:

```python
        password="usher",  # noqa: S106 -- throwaway container credential, torn down with the container
```

becomes

```python
        password="usher",
```

`RUF100` (unused `noqa`) is in this project's selected rule set, so leaving it would turn `ruff check .` red — which is the desired behaviour, but it is quicker to fix here than to discover in Step 8.

- [ ] **Step 7: Run the tests and watch them pass**

Run: `uv run pytest tests/unit/test_ports_source.py tests/unit/test_ports.py -q`
Expected: PASS.

- [ ] **Step 8: Full check and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest tests/unit -q
```
`lint-imports` must still report **5 kept, 0 broken** — `usher.ports.source` now imports `usher.domain.source` and `usher.ports.credentials`, both of which are at or below `ports` in the layer order.

```bash
git add -A && git commit -F - <<'EOF'
feat: settle the source port -- StreamTarget scheme/audio, verify() -> SourceStatus

Closes the two 🔶 markers in usher/ports/source.py that named M3, plus the
matching one in PRD 07:

- StreamTarget gains `scheme` and `audio`, both of which PRD 07's /play
  response documents, and `kind` becomes StreamTargetKind rather than a
  bare str.
- verify() returns SourceStatus. A bool cannot tell bad credentials from
  unreachable from reachable-but-push-blocked, which is exactly the split
  GET /admin/sources/{id}/status has to report.
- push_available is `bool | None` and defaults to None ("not probed"),
  because ADR-0004's caveat means a successful handshake is not evidence.

SourceEvent's empty payload is reviewed and left alone: its marker defers
to M5 on the grounds that the re-walk cost is only measurable once the
push lane exists, and M3 builds no push lane. supports_push already
existed on the shipped port and is not a gap.

Adds the credentials port PRD 08 describes but nothing implemented.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 2: Encrypted credential storage

PRD 08 has specified this since before M1 and nothing implements it: `Source.credentials_ref` is a `Text` column pointing at a row that does not exist in any table. **M3 owns it**, because M3 is the first milestone that has a credential to store and because the alternative — an adapter constructed from environment variables — would put a media-server password back in deploy-time config, which is the shape of the failure Usher exists to replace.

**Files:**
- Modify: `pyproject.toml` (add `cryptography`)
- Modify: `src/usher/db/models/source.py`
- Create: `src/usher/db/migrations/versions/d4c9b1e37a05_source_credentials.py`
- Create: `src/usher/db/repositories/credentials.py`
- Create: `tests/contract/credential_store_contract.py`
- Create: `tests/fakes/credential_store.py`
- Test: `tests/unit/test_credential_store_contract.py`, `tests/integration/test_credential_store.py`

- [ ] **Step 1: Add the dependency**

```bash
uv add cryptography
```
Verified while planning: resolves to `cryptography` 49.0.0, installs as a prebuilt wheel (no compiler), ships `py.typed`, and `HKDF` + `Fernet` + `InvalidToken` all pass mypy strict with no `type: ignore`.

- [ ] **Step 2: Write the failing contract suite and its unit runner**

```python
# tests/contract/credential_store_contract.py
"""Behaviour every `CredentialStore` implementation must satisfy.

Deliberately silent about *how* the secret is stored. "Encrypted at rest"
is a property of a persistent store and cannot be asserted against an
in-memory dict, so it is pinned directly against Postgres in
tests/integration/test_credential_store.py (three cases: the raw column is
not the plaintext, a different key cannot read it, and deleting the owning
source removes it). Asserting it here would either force the in-memory fake
to carry a cipher it has no reason to have, or -- worse -- be written so
loosely that both implementations pass it while only one is actually
encrypting.

Subclass and provide a `store` fixture plus an `owner` hook:

    class TestFakeCredentialStore(CredentialStoreContract):
        @pytest.fixture
        def store(self) -> FakeCredentialStore:
            return FakeCredentialStore()

        async def owner(self, store: CredentialStore) -> uuid.UUID:
            return new_id()
"""

import uuid

from pydantic import SecretStr

from usher.ports.credentials import CredentialStore, SourceCredentials

WRONG = SourceCredentials(username="usher", password=SecretStr("wrong-password"))
RIGHT = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))


class CredentialStoreContract:
    async def owner(self, store: CredentialStore) -> uuid.UUID:
        """An id that `put`'s `owner_id` may legitimately reference.

        A hook rather than a plain `new_id()` because a real store may
        enforce referential integrity against it -- the Postgres
        implementation's `source_credentials.source_id` is a foreign key
        with `ON DELETE CASCADE`, so its subclass has to insert a source
        row first.
        """
        raise NotImplementedError

    async def test_put_then_get_round_trips(self, store: CredentialStore) -> None:
        owner = await self.owner(store)
        await store.put("ref-1", RIGHT, owner_id=owner)
        fetched = await store.get("ref-1")
        assert fetched is not None
        assert fetched.username == "usher"
        assert fetched.password.get_secret_value() == "correct-horse-battery"

    async def test_get_returns_none_for_an_unknown_ref(self, store: CredentialStore) -> None:
        assert await store.get("never-stored") is None

    async def test_put_replaces_an_existing_secret(self, store: CredentialStore) -> None:
        """Both re-registering a source with a corrected password and PRD
        08's key rotation land here. A store that inserted instead of
        upserting would raise on the second call, or -- worse -- keep
        serving the old secret."""
        owner = await self.owner(store)
        await store.put("ref-1", WRONG, owner_id=owner)
        await store.put("ref-1", RIGHT, owner_id=owner)
        fetched = await store.get("ref-1")
        assert fetched is not None
        assert fetched.password.get_secret_value() == "correct-horse-battery"

    async def test_refs_are_independent(self, store: CredentialStore) -> None:
        """Rules out a store keyed on the owner rather than the ref, which
        would make PRD 08's rotation (write under a new ref, flip
        `Source.credentials_ref`, delete the old) overwrite the very secret
        it is meant to be replacing."""
        owner = await self.owner(store)
        await store.put("ref-old", WRONG, owner_id=owner)
        await store.put("ref-new", RIGHT, owner_id=owner)
        old = await store.get("ref-old")
        new = await store.get("ref-new")
        assert old is not None and old.password.get_secret_value() == "wrong-password"
        assert new is not None and new.password.get_secret_value() == "correct-horse-battery"

    async def test_delete_removes_the_secret(self, store: CredentialStore) -> None:
        owner = await self.owner(store)
        await store.put("ref-1", RIGHT, owner_id=owner)
        await store.delete("ref-1")
        assert await store.get("ref-1") is None

    async def test_delete_is_idempotent(self, store: CredentialStore) -> None:
        """`DELETE /admin/sources/{id}` removes a source and its credentials
        in two steps; a retry after a partial failure must not fail on the
        step that already succeeded."""
        await store.delete("never-stored")
        owner = await self.owner(store)
        await store.put("ref-1", RIGHT, owner_id=owner)
        await store.delete("ref-1")
        await store.delete("ref-1")
        assert await store.get("ref-1") is None
```

```python
# tests/unit/test_credential_store_contract.py
"""The credential contract against the in-memory double. No Docker.

tests/integration/test_credential_store.py runs the identical assertions
against Postgres, plus the three at-rest encryption cases this fake has no
way to satisfy.
"""

import uuid

import pytest

from tests.contract.credential_store_contract import CredentialStoreContract
from tests.fakes.credential_store import FakeCredentialStore
from usher.domain.ids import new_id
from usher.ports.credentials import CredentialStore


class TestFakeCredentialStore(CredentialStoreContract):
    @pytest.fixture
    def store(self) -> FakeCredentialStore:
        return FakeCredentialStore()

    async def owner(self, store: CredentialStore) -> uuid.UUID:
        return new_id()
```

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/unit/test_credential_store_contract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fakes.credential_store'`

- [ ] **Step 4: Write the fake**

```python
# tests/fakes/credential_store.py
"""In-memory CredentialStore.

Holds plaintext, on purpose. Encryption at rest is a property of a
persistent store, and a fake that encrypted into a dict would be modelling
ceremony rather than behaviour -- see the contract suite's module docstring
for why that property is asserted directly against Postgres instead. Never
used to assert anything about the shape of stored data.
"""

import uuid
from dataclasses import dataclass

from usher.ports.credentials import CredentialStore, SourceCredentials


@dataclass(frozen=True)
class _Entry:
    credentials: SourceCredentials
    owner_id: uuid.UUID


class FakeCredentialStore(CredentialStore):
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    async def put(self, ref: str, credentials: SourceCredentials, *, owner_id: uuid.UUID) -> None:
        self._entries[ref] = _Entry(credentials=credentials, owner_id=owner_id)

    async def get(self, ref: str) -> SourceCredentials | None:
        entry = self._entries.get(ref)
        return None if entry is None else entry.credentials

    async def delete(self, ref: str) -> None:
        self._entries.pop(ref, None)

    def owner_of(self, ref: str) -> uuid.UUID | None:
        """Test-only probe. Not part of the port -- nothing in `src/` reads
        an owner back, because `owner_id` exists solely so a real backing
        store can cascade a delete."""
        entry = self._entries.get(ref)
        return None if entry is None else entry.owner_id
```

- [ ] **Step 5: Run and watch it pass**

Run: `uv run pytest tests/unit/test_credential_store_contract.py -q`
Expected: PASS — 6 tests.

- [ ] **Step 6: Add the table and its migration**

Append to `src/usher/db/models/source.py` (and extend its imports with `LargeBinary`):

```python
class SourceCredentialRow(Base):
    """Encrypted source credentials, addressed by the opaque
    `Source.credentials_ref`.

    A separate table rather than two more columns on `sources`, so a plain
    `SELECT * FROM sources` -- what every admin read, every debugging
    session, and every glance at a `pg_dump` does -- cannot return a
    ciphertext at all. PRD 08's "credentials are never returned by any API,
    including admin" becomes a property of the schema rather than of
    whoever wrote the serializer.

    `source_id` is a foreign key with `ON DELETE CASCADE` even though the
    primary key is `ref`: deleting a source is two writes (drop the
    credential, drop the source), and a crash between them would otherwise
    leave an encrypted orphan with nothing left to attribute it to.

    No `set_updated_at` trigger, unlike titles/sources/media_items: this
    table has exactly one writer (`PostgresCredentialStore`), which sets
    `updated_at` on both branches of its upsert. The three existing
    triggers exist because their tables are also written by bulk `COPY` and
    raw SQL paths that bypass the ORM; nothing bulk-loads credentials.
    """

    __tablename__ = "source_credentials"

    ref: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_source_credentials_source_id", "source_id"),
        CheckConstraint("ref <> ''", name="ck_source_credentials_ref_not_empty"),
    )
```

```python
# src/usher/db/migrations/versions/d4c9b1e37a05_source_credentials.py
"""source credentials

Revision ID: d4c9b1e37a05
Revises: c7a2e51d8b40
Create Date: 2026-07-30

The encrypted-at-rest table PRD 08 has specified since before M1 and that
`Source.credentials_ref` has pointed at nothing until now. No BEFORE UPDATE
trigger -- see db/models/source.py's SourceCredentialRow docstring.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4c9b1e37a05"
down_revision: str | Sequence[str] | None = "c7a2e51d8b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "source_credentials",
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("ref <> ''", name="ck_source_credentials_ref_not_empty"),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_credentials_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("ref", name=op.f("pk_source_credentials")),
    )
    op.create_index(
        "ix_source_credentials_source_id", "source_credentials", ["source_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_source_credentials_source_id", table_name="source_credentials")
    op.drop_table("source_credentials")
```

- [ ] **Step 7: Write `PostgresCredentialStore`**

```python
# src/usher/db/repositories/credentials.py
"""Encrypted-at-rest storage for source credentials.

PRD 08: credentials are encrypted using a key supplied via
`USHER_SECRET_KEY`, `Source.credentials_ref` points at the encrypted row,
and the plaintext exists only in memory in the adapter that needs it.

Fernet (AES-128-CBC with an HMAC-SHA256 authentication tag) over a key
derived from `USHER_SECRET_KEY` with HKDF-SHA256. HKDF rather than a
password-based KDF such as scrypt because the input is already
high-entropy: the documented way to produce this value is
`openssl rand -hex 32`, `Settings.secret_key` enforces `min_length=32`, and
`Settings` rejects the example placeholder outright. HKDF is the primitive
designed for deriving subkeys from an existing strong secret; scrypt's work
factor buys nothing against 32 random bytes and would cost a full KDF run
per call.

The `info` string is versioned so a future scheme change becomes a new
derivation rather than a silent reinterpretation of old ciphertext, and so
this subkey is domain-separated from any other use a later milestone makes
of `USHER_SECRET_KEY`.

The authentication tag is what makes a rotated key a *diagnosable* failure
rather than a garbage read: decrypting with the wrong key raises
`InvalidToken`, which becomes `PortDataMalformed` with the ref (never the
payload, never the key) so an operator can find the row and re-enter the
credential.

`SecretStr.get_secret_value()` is unwrapped exactly once, in `__init__`,
and the plaintext secret is not retained -- only the derived Fernet key,
which is an HKDF output and not the secret. That satisfies CLAUDE.md's
"never store the unwrapped value in a variable that outlives that call",
and re-deriving per call would be strictly worse for no benefit.
"""

import base64
import json
import uuid
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.source import SourceCredentialRow
from usher.ports.credentials import CredentialStore, SourceCredentials
from usher.ports.errors import PortDataMalformed, RepositoryConflict

_HKDF_INFO = b"usher.source-credentials.v1"


def build_cipher(secret_key: SecretStr) -> Fernet:
    """Derive this deployment's credential-encryption key.

    Module-level and public so a rotation command (PRD 08's "a documented
    rotation command handles the bulk case") can build both the old and the
    new cipher without instantiating two repositories.
    """
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        secret_key.get_secret_value().encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(derived))


class PostgresCredentialStore(CredentialStore):
    def __init__(self, session: AsyncSession, secret_key: SecretStr) -> None:
        self._session = session
        self._cipher = build_cipher(secret_key)

    async def put(
        self, ref: str, credentials: SourceCredentials, *, owner_id: uuid.UUID
    ) -> None:
        blob = self._cipher.encrypt(
            json.dumps(
                {
                    "username": credentials.username,
                    "password": credentials.password.get_secret_value(),
                }
            ).encode("utf-8")
        )
        now = datetime.now(UTC)
        statement = (
            pg_insert(SourceCredentialRow)
            .values(ref=ref, source_id=owner_id, ciphertext=blob, updated_at=now)
            .on_conflict_do_update(
                index_elements=["ref"],
                set_={"ciphertext": blob, "source_id": owner_id, "updated_at": now},
            )
        )
        # SAVEPOINT rather than session.rollback(), for the reason
        # PostgresTitleRepository's module docstring spells out: the caller
        # owns the transaction, and a full rollback here would discard
        # whatever else it had pending -- which, for the one caller that
        # exists, is the `sources` INSERT this row's foreign key points at.
        try:
            async with self._session.begin_nested():
                await self._session.execute(statement)
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"credentials for ref {ref} could not be stored; the owning source "
                "does not exist"
            ) from exc

    async def get(self, ref: str) -> SourceCredentials | None:
        with self._session.no_autoflush:
            row = await self._session.get(SourceCredentialRow, ref)
        if row is None:
            return None
        try:
            payload = self._cipher.decrypt(row.ciphertext)
            record = json.loads(payload.decode("utf-8"))
            return SourceCredentials(
                username=str(record["username"]),
                password=SecretStr(str(record["password"])),
            )
        except (InvalidToken, ValueError, KeyError, TypeError) as exc:
            # `detail` names the row, never its contents -- PortDataMalformed's
            # own docstring: "It must never carry a credential or a whole
            # payload." `str(exc)` is deliberately not interpolated either;
            # a json decoder's message quotes the text it choked on.
            raise PortDataMalformed(
                "stored source credentials could not be decrypted -- USHER_SECRET_KEY "
                "may have been rotated, or the row corrupted",
                detail=f"credentials_ref={ref}",
            ) from exc

    async def delete(self, ref: str) -> None:
        await self._session.execute(
            delete(SourceCredentialRow).where(SourceCredentialRow.ref == ref)
        )
```

- [ ] **Step 8: Write the integration tests**

```python
# tests/integration/test_credential_store.py
"""PostgresCredentialStore against real Postgres.

The contract suite runs here unchanged; the four cases below are the ones
the in-memory fake cannot express, and they are the ones PRD 08's rules
actually reduce to.
"""

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.credential_store_contract import RIGHT, CredentialStoreContract
from usher.db.models.source import SourceCredentialRow, SourceRow
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.ports.credentials import CredentialStore
from usher.ports.errors import PortDataMalformed, RepositoryConflict

KEY = SecretStr("0" * 32)
OTHER_KEY = SecretStr("1" * 32)


async def _seed_source(session: AsyncSession) -> uuid.UUID:
    source_id = new_id()
    session.add(
        SourceRow(
            id=source_id,
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-1",
            device_id=str(new_id()),
        )
    )
    await session.flush()
    return source_id


class TestPostgresCredentialStoreContract(CredentialStoreContract):
    @pytest.fixture
    def store(self, session: AsyncSession) -> PostgresCredentialStore:
        return PostgresCredentialStore(session, KEY)

    async def owner(self, store: CredentialStore) -> uuid.UUID:
        # Reaches into the store's own session rather than taking a second
        # `session` fixture argument: the credential row's foreign key must
        # point at a source visible in *this* store's transaction, and two
        # sessions on the same connection would not see each other's
        # unflushed work. (`flake8-self`/SLF is not in this project's ruff
        # selection, so no suppression is needed.)
        assert isinstance(store, PostgresCredentialStore)
        return await _seed_source(store._session)


async def test_the_stored_column_is_not_the_plaintext(session: AsyncSession) -> None:
    """PRD 08's whole point. Reads the raw column rather than going through
    `get`, because `get` decrypts -- a store that "encrypted" by base64ing
    would satisfy a round-trip test and fail this one."""
    owner = await _seed_source(session)
    await PostgresCredentialStore(session, KEY).put("ref-1", RIGHT, owner_id=owner)
    await session.flush()
    stored = (
        await session.execute(
            select(SourceCredentialRow.ciphertext).where(SourceCredentialRow.ref == "ref-1")
        )
    ).scalar_one()
    assert b"correct-horse-battery" not in stored
    assert b"usher" not in stored


async def test_a_different_secret_key_cannot_read_it(session: AsyncSession) -> None:
    """Rotating USHER_SECRET_KEY must be a loud, diagnosable failure that
    names the row, not a silent garbage read and not a `None` that would
    look like an unconfigured source."""
    owner = await _seed_source(session)
    await PostgresCredentialStore(session, KEY).put("ref-1", RIGHT, owner_id=owner)
    await session.flush()
    with pytest.raises(PortDataMalformed) as exc_info:
        await PostgresCredentialStore(session, OTHER_KEY).get("ref-1")
    assert exc_info.value.detail == "credentials_ref=ref-1"
    assert "correct-horse-battery" not in str(exc_info.value)


async def test_deleting_the_source_cascades_to_its_credentials(session: AsyncSession) -> None:
    """The reason `owner_id` is on the port at all. Without the cascade, a
    crash between "delete the credential" and "delete the source" leaves an
    encrypted row nothing can attribute or clean up."""
    owner = await _seed_source(session)
    await PostgresCredentialStore(session, KEY).put("ref-1", RIGHT, owner_id=owner)
    await session.flush()
    await session.execute(delete(SourceRow).where(SourceRow.id == owner))
    await session.flush()
    remaining = (
        await session.execute(
            select(SourceCredentialRow.ref).where(SourceCredentialRow.ref == "ref-1")
        )
    ).scalars().all()
    assert remaining == []


async def test_put_for_an_unknown_owner_is_a_port_error(session: AsyncSession) -> None:
    """A raw sqlalchemy.exc.IntegrityError escaping here would break the
    "db is driven, not driving" contract exactly the way it did in
    PostgresTitleRepository before its translation was added."""
    with pytest.raises(RepositoryConflict):
        await PostgresCredentialStore(session, KEY).put("ref-1", RIGHT, owner_id=new_id())


async def test_the_session_survives_that_conflict(session: AsyncSession) -> None:
    """The SAVEPOINT, not just the translation. `SourceService.register`
    inserts the source and then the credential on one session; if a failed
    `put` poisoned the transaction, the caller's rollback path could not
    even read back what it had already written."""
    store = PostgresCredentialStore(session, KEY)
    with pytest.raises(RepositoryConflict):
        await store.put("ref-1", RIGHT, owner_id=new_id())
    owner = await _seed_source(session)
    await store.put("ref-2", RIGHT, owner_id=owner)
    assert await store.get("ref-2") is not None
```

- [ ] **Step 9: Run everything and commit**

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration/test_credential_store.py tests/integration/test_migrations.py -q
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports
```
`tests/integration/test_migrations.py` is the one that matters most here: it runs an autogenerate diff against the migrated database and fails if the hand-written migration and `SourceCredentialRow` disagree.

```bash
git add -A && git commit -F - <<'EOF'
feat: encrypted-at-rest source credentials

PRD 08 has specified this since before M1 and nothing implemented it --
Source.credentials_ref was a Text column pointing at no table. M3 is the
first milestone with a credential to store, so M3 owns it.

Fernet over an HKDF-SHA256 subkey of USHER_SECRET_KEY. Its own table
rather than columns on `sources`, so "credentials are never returned by
any API, including admin" is a property of the schema and not of whoever
writes the serializer. ON DELETE CASCADE from sources, so a crash between
the two writes of a source deletion cannot orphan a ciphertext.

A wrong key raises PortDataMalformed naming the ref -- diagnosable and
recoverable, rather than a None that would read as an unconfigured source.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 3: `SourceRepository`

Nothing persists a `Source`. Without it there is nowhere to keep the `DeviceId` that makes the client *durable* — regenerate it per process and Usher becomes exactly the accumulating pile of Emby sessions PRD 03 designed it not to be. ADR-0009 already names `SourceRepository`/`PostgresSourceRepository` as the pattern M2+ would need.

**Files:**
- Modify: `src/usher/ports/repository.py`
- Create: `src/usher/db/repositories/source.py`
- Create: `tests/contract/source_repository_contract.py`
- Create: `tests/fakes/source_repository.py`
- Test: `tests/unit/test_source_repository_contract.py`, `tests/integration/test_source_repository.py`

- [ ] **Step 1: Write the failing contract suite and unit runner**

```python
# tests/contract/source_repository_contract.py
"""Behaviour every `SourceRepository` implementation must satisfy.

The load-bearing case is `test_update_writes_the_device_id_it_is_given`:
`device_id` is what makes Usher one durable Emby client instead of an
accumulating pile of sessions (PRD 03), and an `update()` that quietly
dropped the column from its SET clause would make a deliberate rotation a
silent no-op with nothing to notice.
"""

from datetime import UTC, datetime

import pytest

from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SourceRepository


def _source(name: str = "Living Room Emby", **overrides: object) -> Source:
    values: dict[str, object] = {
        "kind": SourceKind.EMBY,
        "name": name,
        "base_url": "https://emby.invalid",
        "credentials_ref": "ref-1",
        "device_id": "2f0c9a1e-0000-7000-8000-000000000001",
    }
    values.update(overrides)
    return Source.model_validate(values)


class SourceRepositoryContract:
    async def test_add_then_get_round_trips(self, repo: SourceRepository) -> None:
        source = _source(
            credentials_ref="opaque-ref", device_id="c0ffee00-0000-7000-8000-00000000c0de"
        )
        await repo.add(source)
        fetched = await repo.get(source.id)
        assert fetched is not None
        assert fetched.model_dump(exclude={"created_at", "updated_at"}) == source.model_dump(
            exclude={"created_at", "updated_at"}
        )

    async def test_created_at_is_not_taken_from_the_caller(self, repo: SourceRepository) -> None:
        """Same rule the title repository already holds: the database is the
        authoritative clock. Pinned here too, because the fake had to be
        written to match and the two would otherwise drift."""
        backdated = datetime(2020, 1, 1, tzinfo=UTC)
        source = _source(created_at=backdated, updated_at=backdated)
        await repo.add(source)
        fetched = await repo.get(source.id)
        assert fetched is not None
        assert fetched.created_at != backdated

    async def test_add_rejects_a_duplicate_id(self, repo: SourceRepository) -> None:
        source = _source()
        await repo.add(source)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(source)
        assert exc_info.value.constraint == "pk_sources"

    async def test_get_returns_none_for_an_unknown_id(self, repo: SourceRepository) -> None:
        assert await repo.get(new_id()) is None

    async def test_update_mutates_an_existing_source(self, repo: SourceRepository) -> None:
        source = _source()
        await repo.add(source)
        await repo.update(source.evolve(enabled=False, supports_push=True))
        fetched = await repo.get(source.id)
        assert fetched is not None
        assert fetched.enabled is False
        assert fetched.supports_push is True

    async def test_update_writes_the_device_id_it_is_given(self, repo: SourceRepository) -> None:
        """Deliberately tampers rather than leaving the field alone: an
        `update()` that omitted `device_id` from its SET clause would pass a
        leave-it-alone assertion and silently turn a rotation into a no-op.
        Asserting the *new* value landed is the only version of this that
        can fail."""
        source = _source()
        await repo.add(source)
        await repo.update(source.evolve(device_id="rotated-0000-7000-8000-000000000002"))
        fetched = await repo.get(source.id)
        assert fetched is not None
        assert fetched.device_id == "rotated-0000-7000-8000-000000000002"

    async def test_update_rejects_an_unknown_id(self, repo: SourceRepository) -> None:
        with pytest.raises(RepositoryNotFound):
            await repo.update(_source())

    async def test_list_all_is_ordered_by_name(self, repo: SourceRepository) -> None:
        """The admin listing is rendered in the order this returns; a set
        comparison could not tell an unordered implementation from an
        ordered one."""
        await repo.add(_source("Zeta"))
        await repo.add(_source("Alpha"))
        await repo.add(_source("Mid"))
        assert [source.name for source in await repo.list_all()] == ["Alpha", "Mid", "Zeta"]

    async def test_list_all_is_empty_before_anything_is_added(
        self, repo: SourceRepository
    ) -> None:
        assert await repo.list_all() == []

    async def test_delete_reports_whether_it_removed_anything(
        self, repo: SourceRepository
    ) -> None:
        """`DELETE /admin/sources/{id}` returns 404 for an unknown id and 204
        otherwise, so the bool is the endpoint's whole branch. An
        implementation that always returned True would make the endpoint
        claim it deleted something that never existed."""
        source = _source()
        await repo.add(source)
        assert await repo.delete(source.id) is True
        assert await repo.get(source.id) is None
        assert await repo.delete(source.id) is False
```

```python
# tests/unit/test_source_repository_contract.py
"""The source-repository contract against the in-memory double. No Docker.

tests/integration/test_source_repository.py runs the identical assertions
against Postgres.
"""

import pytest

from tests.contract.source_repository_contract import SourceRepositoryContract
from tests.fakes.source_repository import FakeSourceRepository


class TestFakeSourceRepository(SourceRepositoryContract):
    @pytest.fixture
    def repo(self) -> FakeSourceRepository:
        return FakeSourceRepository()
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/unit/test_source_repository_contract.py -q`
Expected: FAIL — `ImportError: cannot import name 'SourceRepository' from 'usher.ports.repository'`

- [ ] **Step 3: Add the port**

Append to `src/usher/ports/repository.py` (and add `from usher.domain.source import Source` to its imports):

```python
class SourceRepository(ABC):
    """Persistence for configured sources.

    Same session/transaction ownership as `TitleRepository`: every method
    flushes so conflicts surface immediately, none commits.

    Credentials are deliberately absent from this port. `Source` carries
    only `credentials_ref`, an opaque pointer, and the secret itself lives
    behind `CredentialStore` (`usher.ports.credentials`) -- so a read here,
    which is what the admin API performs, cannot return a credential even
    by accident. That split is PRD 08's "credentials are never returned by
    any API, including admin", expressed as a type rather than as a rule.
    """

    @abstractmethod
    async def add(self, source: Source) -> None:
        """Insert. A duplicate id raises `RepositoryConflict`."""

    @abstractmethod
    async def update(self, source: Source) -> None:
        """Update an existing row. An unknown id raises
        `RepositoryNotFound`. Writes every mutable column it is given,
        including `device_id` -- PRD 08's key/credential rotation and a
        deliberate device rotation both go through here."""

    @abstractmethod
    async def get(self, source_id: uuid.UUID) -> Source | None:
        """Fetch by id, or None."""

    @abstractmethod
    async def list_all(self) -> list[Source]:
        """Every configured source, ordered by name. Includes disabled ones:
        `GET /admin/sources` has to show a source in order for an operator
        to re-enable it."""

    @abstractmethod
    async def delete(self, source_id: uuid.UUID) -> bool:
        """Remove a source. Returns whether a row was actually removed, so
        `DELETE /admin/sources/{id}` can answer 404 rather than claiming to
        have deleted something that never existed. Idempotent."""
```

- [ ] **Step 4: Write the fake**

```python
# tests/fakes/source_repository.py
"""In-memory SourceRepository.

Stamps `created_at`/`updated_at` itself rather than honouring the caller's,
because Postgres does -- the same divergence the title fake had to be
corrected for, where "the fake preserved caller timestamps and the real
repository never did" made a round-trip assertion pass against the fake
alone.
"""

import uuid
from datetime import UTC, datetime

from usher.domain.source import Source
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SourceRepository


class FakeSourceRepository(SourceRepository):
    def __init__(self) -> None:
        self._sources: dict[uuid.UUID, Source] = {}

    async def add(self, source: Source) -> None:
        if source.id in self._sources:
            raise RepositoryConflict(
                f"source {source.id} conflicts with an existing source", constraint="pk_sources"
            )
        now = datetime.now(UTC)
        self._sources[source.id] = source.evolve(created_at=now, updated_at=now)

    async def update(self, source: Source) -> None:
        existing = self._sources.get(source.id)
        if existing is None:
            raise RepositoryNotFound(f"source {source.id} does not exist")
        self._sources[source.id] = source.evolve(
            created_at=existing.created_at, updated_at=datetime.now(UTC)
        )

    async def get(self, source_id: uuid.UUID) -> Source | None:
        return self._sources.get(source_id)

    async def list_all(self) -> list[Source]:
        return sorted(self._sources.values(), key=lambda source: source.name)

    async def delete(self, source_id: uuid.UUID) -> bool:
        return self._sources.pop(source_id, None) is not None
```

- [ ] **Step 5: Run and watch it pass**

Run: `uv run pytest tests/unit/test_source_repository_contract.py -q`
Expected: PASS — 10 tests.

- [ ] **Step 6: Write `PostgresSourceRepository`**

```python
# src/usher/db/repositories/source.py
"""Persistence for configured sources.

Follows PostgresTitleRepository's two structural decisions verbatim, for
the reasons its module docstring works through at length:

- `add()`/`update()` wrap their flush in `session.begin_nested()`, a
  SAVEPOINT, rather than `session.rollback()` -- the caller owns the
  transaction, and this repository's one real caller (`SourceService.
  register`) has the credential write pending on the same session.
- Reads run inside `session.no_autoflush`, so unrelated pending state left
  on a shared session cannot make a pure read raise a storage exception
  from behind this port.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.source import SourceRow
from usher.domain.source import Source
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SourceRepository

# Written by update(); created_at is excluded because Postgres owns it, and
# id is excluded because it is the lookup key, not a mutable column.
_MUTABLE = (
    "kind",
    "name",
    "base_url",
    "credentials_ref",
    "device_id",
    "enabled",
    "supports_push",
)


def _to_domain(row: SourceRow) -> Source:
    return Source.model_validate(
        {column.name: getattr(row, column.name) for column in SourceRow.__table__.columns}
    )


class PostgresSourceRepository(SourceRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, source: Source) -> None:
        row = SourceRow(**source.model_dump(exclude={"created_at", "updated_at"}))
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"source {source.id} conflicts with an existing source", constraint="pk_sources"
            ) from exc

    async def update(self, source: Source) -> None:
        try:
            async with self._session.begin_nested():
                row = await self._session.get(SourceRow, source.id)
                if row is None:
                    raise RepositoryNotFound(f"source {source.id} does not exist")
                for field in _MUTABLE:
                    setattr(row, field, getattr(source, field))
                await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"source {source.id} conflicts with an existing source", constraint=None
            ) from exc

    async def get(self, source_id: uuid.UUID) -> Source | None:
        with self._session.no_autoflush:
            row = await self._session.get(SourceRow, source_id)
        return None if row is None else _to_domain(row)

    async def list_all(self) -> list[Source]:
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(select(SourceRow).order_by(SourceRow.name))
            ).scalars()
            return [_to_domain(row) for row in rows]

    async def delete(self, source_id: uuid.UUID) -> bool:
        result = await self._session.execute(delete(SourceRow).where(SourceRow.id == source_id))
        await self._session.flush()
        return result.rowcount > 0
```

> **Note for the implementer:** `RepositoryNotFound` is raised *inside* the `begin_nested()` block in `update()`. That is deliberate — it exits the SAVEPOINT by rolling it back, which is correct, and it is not an `IntegrityError` so the `except` below does not swallow it. Confirm by running `test_update_rejects_an_unknown_id` and seeing `RepositoryNotFound`, not `RepositoryConflict`.

- [ ] **Step 7: Write the integration runner**

```python
# tests/integration/test_source_repository.py
"""PostgresSourceRepository against real Postgres."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.source_repository_contract import SourceRepositoryContract, _source
from usher.db.repositories.source import PostgresSourceRepository
from usher.ports.errors import RepositoryConflict


class TestPostgresSourceRepositoryContract(SourceRepositoryContract):
    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresSourceRepository:
        return PostgresSourceRepository(session)


async def test_the_session_survives_a_conflict(session: AsyncSession) -> None:
    """The SAVEPOINT, not just the translation: without it Postgres leaves
    the whole transaction aborted and the caller's very next statement
    raises PendingRollbackError instead of running. `SourceService.register`
    is exactly such a caller -- it writes the credential on this same
    session immediately afterwards."""
    repo = PostgresSourceRepository(session)
    source = _source()
    await repo.add(source)
    with pytest.raises(RepositoryConflict):
        await repo.add(source)
    assert await repo.get(source.id) is not None
```

- [ ] **Step 8: Run everything and commit**

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration/test_source_repository.py -q
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports
```

```bash
git add -A && git commit -F - <<'EOF'
feat: SourceRepository -- somewhere to keep the durable DeviceId

Nothing persisted a Source, so nothing could persist the DeviceId that
makes Usher one durable Emby client rather than an accumulating pile of
sessions (PRD 03). ADR-0009 already named this port as the pattern M2+
would need.

Credentials are deliberately not on this port: Source carries only the
opaque credentials_ref, so an admin read cannot return a secret even by
accident.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 4: The source-agnostic contract suite

The headline deliverable. The spec calls it "the test that proves the abstraction is real"; PRD 08 calls it "the load-bearing one".

**How it stays source-agnostic.** The suite never sees a wire format. It talks to a `SourceHarness` ABC whose vocabulary is the port's own DTOs — `given_item(SourceItem)`, `given_watch_state(SourceWatchState)`, `go_offline()`, `expire_credentials()` — and each implementation translates those into whatever its upstream needs. `FakeSourceHarness` stores the DTOs; `EmbyHarness` (Task 10) renders them into Emby JSON served by an in-memory server. A Jellyfin adapter writes a third harness and the suite runs unchanged.

Three alternatives were considered and rejected, recorded in ADR-0013: recorded HTTP cassettes (they pin one server's bytes, so the suite becomes Emby's, not the port's), a real containerised Emby (proprietary, no redistributable image, and it would make the load-bearing suite Docker-gated), and giving each adapter its own hand-written suite (which is precisely the duplication the contract-suite pattern exists to remove — M1's `TitleRepositoryContract` docstring already works through this).

**Why the harness's mutators are `async` even though both M3 implementations are synchronous:** the strongest possible version of this suite is one run against a *live* server, and that harness would have to await. Making the hooks async now costs `await` noise in ~30 tests; making them async later costs touching all of them.

**Files:**
- Create: `tests/contract/source_harness.py`
- Create: `tests/contract/source_adapter_contract.py`
- Create: `tests/fakes/source_adapter.py`
- Create: `docs/prd/decisions/0013-contract-suite-drives-a-source-harness.md`
- Test: `tests/unit/test_source_adapter_contract.py`

- [ ] **Step 1: Write the harness ABC**

```python
# tests/contract/source_harness.py
"""The seam that makes `SourceAdapterContract` source-agnostic.

The contract suite never constructs an adapter, never touches HTTP, and
never mentions a wire format. It arranges state through this ABC, whose
whole vocabulary is `usher.ports.source`'s own DTOs, and each
implementation translates that into whatever its upstream actually needs:
`FakeSourceHarness` stores the DTOs directly, `EmbyHarness` renders them
into Emby JSON served by an in-memory server. A Jellyfin or Plex adapter
adds a third implementation of this ABC and the suite runs unchanged --
which is the only sense in which "the abstraction is real" is a testable
claim rather than an aspiration.

Every mutator is `async` even though both M3 implementations are
synchronous. The strongest form of this suite is one driven against a live
server, and that harness has to await; paying the `await` noise now is
cheaper than rewriting thirty tests later.
"""

from abc import ABC, abstractmethod

from pydantic import AwareDatetime

from usher.domain.source import Source
from usher.ports.source import SourceAdapter, SourceItem, SourceWatchState


class SourceHarness(ABC):
    @property
    @abstractmethod
    def source(self) -> Source:
        """The `Source` the adapter under test was configured with."""

    @property
    @abstractmethod
    def adapter(self) -> SourceAdapter:
        """The adapter under test. The same instance for the whole test."""

    @abstractmethod
    async def given_item(self, item: SourceItem, *, changed_at: AwareDatetime) -> None:
        """Make the source hold `item`, last changed at `changed_at`.

        `changed_at` is what a `since` cursor filters on, and it is separate
        from `SourceItem.added_at` on purpose: an item added last year and
        edited this morning must be found by a delta walk, and a DTO field
        named `added_at` cannot express that.

        An implementation renders `item` into its own upstream's shape. It
        must round-trip every field it is given -- the point of this hook is
        that `adapter.get_item(item.external_id)` returns something equal to
        `item` in the fields the port promises.
        """

    @abstractmethod
    async def given_watch_state(self, state: SourceWatchState) -> None:
        """Make the source hold `state` for `state.external_id`."""

    @abstractmethod
    async def remove_item(self, external_id: str) -> None:
        """Delete an item from the source, as a user deleting a file would."""

    @abstractmethod
    async def recorded_watch_state(self, external_id: str) -> tuple[int, bool] | None:
        """`(position_seconds, played)` as the source now holds it after a
        `push_watch_state`, or `None` if nothing was ever written.

        Read back from the source's own state, never from a log of calls the
        adapter made -- a harness that recorded "push_watch_state was
        called" would pass against an adapter that called the wrong upstream
        endpoint and got a 200 from something that ignored it.
        """

    @abstractmethod
    async def go_offline(self) -> None:
        """Make every subsequent request fail at the transport layer, the
        way an unplugged server or a dead DNS entry does. Not a 5xx: a
        transport failure is the case an adapter is most likely to translate
        wrongly."""

    @abstractmethod
    async def fail_after_items(self, count: int) -> None:
        """Serve at least `count` items successfully during a walk, then
        fail.

        "At least" because upstreams page, and a page boundary rarely lands
        exactly on `count`: an implementation that serves items in pages of
        two will serve four before failing when asked for three. The
        contract only asserts that `count` items arrived before the failure
        did, which is what distinguishes a streaming walk from one that
        materialised the library and raised before yielding anything.
        """

    @abstractmethod
    async def reject_credentials(self) -> None:
        """Make the stored credentials wrong, as a changed password does.

        Must also invalidate any live session. Without that, an adapter that
        already authenticated keeps working and every assertion about
        rejected credentials passes vacuously.
        """

    @abstractmethod
    async def expire_credentials(self) -> None:
        """Invalidate the adapter's *session*, leaving the stored
        credentials correct -- the exact failure that motivated this
        project, where a token in a Home Assistant dashboard silently began
        returning 401 with no way to renew it.

        A source with no expiring session may implement this as a no-op; the
        contract's assertions still hold (the operation succeeds, and no
        storm of authentications follows).
        """

    @abstractmethod
    def authentications(self) -> int:
        """How many times the source has been asked to authenticate since
        the harness was created. `0` for a source with no authentication
        step."""

    @abstractmethod
    async def aclose(self) -> None:
        """Tear the harness down. Not the same as `adapter.aclose()` -- the
        contract closes the adapter itself in some cases, and this must
        still be safe afterwards."""
```

- [ ] **Step 2: Write the contract suite**

```python
# tests/contract/source_adapter_contract.py
"""Behaviour every `SourceAdapter` implementation must satisfy.

PRD 08: "when a Jellyfin adapter is written, it either passes the same
tests the Emby adapter passes, or the port was wrong."

Nothing in this module knows what a media server is. State is arranged
through `SourceHarness` (tests/contract/source_harness.py) in the port's
own DTOs, so the same file runs against a pure in-memory adapter with no
HTTP at all and against a real `EmbyAdapter` speaking Emby's JSON. Both
runs matter: the first proves the assertions are not secretly Emby-shaped,
the second proves they survive a wire format.

Subclass and provide a `harness` fixture:

    class TestFakeSourceAdapter(SourceAdapterContract):
        @pytest_asyncio.fixture
        async def harness(self) -> AsyncIterator[SourceHarness]:
            harness = FakeSourceHarness()
            try:
                yield harness
            finally:
                await harness.aclose()
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.contract.source_harness import SourceHarness
from usher.domain.enums import HdrFormat
from usher.ports.errors import PortAuthFailed, PortUnavailable
from usher.ports.source import (
    SourceItem,
    SourceItemKind,
    SourceNotSupported,
    SourceWatchState,
    StreamTargetKind,
    WatchStateUpdate,
)

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=1)

MOVIE = SourceItem(
    external_id="movie-1",
    name="Example Movie",
    kind=SourceItemKind.MOVIE,
    year=2021,
    provider_ids={"tmdb": "438631", "imdb": "tt1160419"},
    container="mkv",
    video_codec="hevc",
    audio_codec="truehd",
    width=3840,
    height=2160,
    hdr_format=HdrFormat.DOLBY_VISION,
    audio_channels=8,
    file_size_bytes=68_719_476_736,
    runtime_seconds=9360,
    added_at=datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC),
)
SERIES = SourceItem(
    external_id="series-1",
    name="Example Series",
    kind=SourceItemKind.SERIES,
    year=2011,
    provider_ids={"tmdb": "1399", "imdb": "tt0944947", "tvdb": "121361"},
)
EPISODE = SourceItem(
    external_id="episode-1",
    name="Example Episode",
    kind=SourceItemKind.EPISODE,
    year=2013,
    provider_ids={"imdb": "tt2178782", "tvdb": "4517466"},
    container="mkv",
    video_codec="h264",
    audio_codec="eac3",
    width=1920,
    height=1080,
    audio_channels=6,
    runtime_seconds=3300,
    series_external_id="series-1",
    season_number=2,
    episode_number=5,
    added_at=datetime(2024, 5, 4, 9, 0, 0, tzinfo=UTC),
)


def _filler(index: int) -> SourceItem:
    return SourceItem(
        external_id=f"filler-{index}",
        name=f"Filler {index}",
        kind=SourceItemKind.MOVIE,
        year=2000 + index,
        provider_ids={"imdb": f"tt900000{index}"},
        container="mkv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        audio_channels=2,
        runtime_seconds=5400,
        added_at=T0,
    )


class SourceAdapterContract:
    async def _seed_library(self, harness: SourceHarness) -> None:
        """Seven items, so any implementation that pages will page. The Emby
        harness deliberately runs a page size of two."""
        for index in range(7):
            await harness.given_item(_filler(index), changed_at=T0)

    # --- identity ------------------------------------------------------

    async def test_source_id_is_the_configured_source(self, harness: SourceHarness) -> None:
        assert harness.adapter.source_id == harness.source.id

    # --- listing -------------------------------------------------------

    async def test_list_items_yields_every_seeded_item(self, harness: SourceHarness) -> None:
        """Seven items across a page size of two is four pages. An adapter
        that stops after the first returns two."""
        await self._seed_library(harness)
        seen = {item.external_id async for item in harness.adapter.list_items()}
        assert seen == {f"filler-{index}" for index in range(7)}

    async def test_list_items_raises_rather_than_truncating(
        self, harness: SourceHarness
    ) -> None:
        """The guarantee the reconciler's correctness rests on. A generator
        that swallowed the error and stopped is indistinguishable from one
        that finished, and PRD 03's nightly walk would mark every item it
        never reached `available = false`.

        Asserts both halves: the error surfaces, *and* the items served
        before it did were actually yielded. An adapter that raised on the
        first `__anext__` would satisfy `pytest.raises` alone.
        """
        await self._seed_library(harness)
        await harness.fail_after_items(3)
        seen: list[SourceItem] = []
        with pytest.raises(PortUnavailable):
            async for item in harness.adapter.list_items():
                seen.append(item)
        assert len(seen) >= 3

    async def test_list_items_streams_rather_than_materialising(
        self, harness: SourceHarness
    ) -> None:
        """94,395 movies across 17 libraries on the deployment this was
        built for. An adapter that collected the walk into a list before
        yielding would raise here before producing anything, because the
        failure is arranged to land partway through."""
        await self._seed_library(harness)
        await harness.fail_after_items(3)
        iterator = harness.adapter.list_items()
        first = await anext(iterator)
        assert first.external_id.startswith("filler-")
        # Drain to the failure so no half-consumed async generator is left
        # for the garbage collector to close at an arbitrary later point.
        with pytest.raises(PortUnavailable):
            async for _ in iterator:
                pass

    async def test_list_items_since_is_inclusive(self, harness: SourceHarness) -> None:
        """"An item changed exactly at `since` is included, never dropped at
        the boundary" -- an exclusive `>` upstream filter fails this, and
        the item it drops is exactly the one the previous walk's cursor was
        set from."""
        await harness.given_item(MOVIE, changed_at=T1)
        seen = {item.external_id async for item in harness.adapter.list_items(since=T1)}
        assert "movie-1" in seen

    async def test_list_items_since_does_not_invert_the_window(
        self, harness: SourceHarness
    ) -> None:
        """Extra items are permitted by the port (callers deduplicate);
        missing ones are not. An adapter that sent its comparison the wrong
        way round returns only the item that did *not* change."""
        await harness.given_item(SERIES, changed_at=T0)
        await harness.given_item(MOVIE, changed_at=T1)
        seen = {item.external_id async for item in harness.adapter.list_items(since=T1)}
        assert "movie-1" in seen

    # --- mapping -------------------------------------------------------

    async def test_a_movie_round_trips_its_quality_facts(
        self, harness: SourceHarness
    ) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        item = await harness.adapter.get_item("movie-1")
        assert item is not None
        assert item.kind is SourceItemKind.MOVIE
        assert item.name == "Example Movie"
        assert item.year == 2021
        assert item.container == "mkv"
        assert item.video_codec == "hevc"
        assert item.audio_codec == "truehd"
        assert (item.width, item.height) == (3840, 2160)
        assert item.audio_channels == 8
        assert item.file_size_bytes == 68_719_476_736
        assert item.runtime_seconds == 9360

    async def test_hdr_format_is_the_canonical_enum(self, harness: SourceHarness) -> None:
        """PRD 02 names this failure explicitly: Emby emits strings like
        `"DolbyVision"`, and the adapter -- not `MediaItem`, not the API --
        is where that becomes `HdrFormat`. A raw string would satisfy
        `== "DV"` under `StrEnum` comparison, so this asserts identity."""
        await harness.given_item(MOVIE, changed_at=T0)
        item = await harness.adapter.get_item("movie-1")
        assert item is not None
        assert item.hdr_format is HdrFormat.DOLBY_VISION

    async def test_provider_ids_use_canonical_lowercase_keys(
        self, harness: SourceHarness
    ) -> None:
        """M4's matcher reads `provider_ids["tmdb"]`. It must not have to
        know that Emby spells it `Tmdb`."""
        await harness.given_item(MOVIE, changed_at=T0)
        item = await harness.adapter.get_item("movie-1")
        assert item is not None
        assert item.provider_ids.get("tmdb") == "438631"
        assert item.provider_ids.get("imdb") == "tt1160419"
        assert all(key == key.lower() for key in item.provider_ids)

    async def test_added_at_is_timezone_aware(self, harness: SourceHarness) -> None:
        """`SourceItem` is a plain dataclass, so a naive datetime is
        constructed without complaint and only fails much later, at a
        `TIMESTAMPTZ` column. Verified while planning: Python 3.13's
        `fromisoformat` returns a naive datetime for any timestamp with no
        offset, which several sources emit."""
        await harness.given_item(MOVIE, changed_at=T0)
        item = await harness.adapter.get_item("movie-1")
        assert item is not None
        assert item.added_at is not None
        assert item.added_at.tzinfo is not None
        assert item.added_at.utcoffset() is not None

    async def test_an_episode_carries_its_place_in_the_series(
        self, harness: SourceHarness
    ) -> None:
        """TV is in scope throughout (PRD 09), and `SourceItem` already has
        the three fields for it. Persisting them is M4's -- there is no
        `episodes` table -- but an adapter that flattened episodes into
        movies would make that milestone impossible."""
        await harness.given_item(SERIES, changed_at=T0)
        await harness.given_item(EPISODE, changed_at=T0)
        item = await harness.adapter.get_item("episode-1")
        assert item is not None
        assert item.kind is SourceItemKind.EPISODE
        assert item.series_external_id == "series-1"
        assert item.season_number == 2
        assert item.episode_number == 5

    async def test_a_series_is_not_mistaken_for_an_episode(
        self, harness: SourceHarness
    ) -> None:
        await harness.given_item(SERIES, changed_at=T0)
        item = await harness.adapter.get_item("series-1")
        assert item is not None
        assert item.kind is SourceItemKind.SERIES
        assert item.season_number is None
        assert item.episode_number is None

    # --- get_item ------------------------------------------------------

    async def test_get_item_returns_none_after_a_deletion(
        self, harness: SourceHarness
    ) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        assert await harness.adapter.get_item("movie-1") is not None
        await harness.remove_item("movie-1")
        assert await harness.adapter.get_item("movie-1") is None

    async def test_get_item_returns_none_for_an_id_the_source_never_had(
        self, harness: SourceHarness
    ) -> None:
        assert await harness.adapter.get_item("never-existed") is None

    async def test_get_item_raises_when_the_source_is_unreachable(
        self, harness: SourceHarness
    ) -> None:
        """The most dangerous wrong implementation on this port. The item is
        seeded first on purpose: against an empty source, an adapter that
        returned `None` for a transport failure would look correct, and PRD
        03's reconcile would mark a healthy library unavailable because of a
        flaky network."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.go_offline()
        with pytest.raises(PortUnavailable):
            await harness.adapter.get_item("movie-1")

    # --- authentication ------------------------------------------------

    async def test_rejected_credentials_raise_port_auth_failed(
        self, harness: SourceHarness
    ) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.reject_credentials()
        with pytest.raises(PortAuthFailed):
            await harness.adapter.get_item("movie-1")

    async def test_operations_recover_from_an_expired_credential(
        self, harness: SourceHarness
    ) -> None:
        """The failure that motivated this whole project, and its fix: a
        session that silently dies is re-minted from stored credentials with
        no human pasting a token.

        Four concurrent calls, and at most one authentication between them.
        Both halves fail a real wrong implementation: no re-authentication
        at all raises, and a re-authentication per in-flight request counts
        four. `<= 1` rather than `== 1` so a source with no expiring session
        (whose `expire_credentials` is a no-op) is not forced to invent one.
        """
        await harness.given_item(MOVIE, changed_at=T0)
        assert await harness.adapter.get_item("movie-1") is not None
        before = harness.authentications()
        await harness.expire_credentials()
        results = await asyncio.gather(
            *(harness.adapter.get_item("movie-1") for _ in range(4))
        )
        assert all(result is not None for result in results)
        assert harness.authentications() - before <= 1

    async def test_rejected_credentials_do_not_produce_a_request_storm(
        self, harness: SourceHarness
    ) -> None:
        """A genuinely wrong password must not turn every call into a doomed
        authentication. Without negative caching this counts five."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.reject_credentials()
        for _ in range(5):
            with pytest.raises(PortAuthFailed):
                await harness.adapter.get_item("movie-1")
        assert harness.authentications() <= 1

    # --- playback ------------------------------------------------------

    async def test_stream_targets_rank_a_direct_target_first(
        self, harness: SourceHarness
    ) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        targets = await harness.adapter.stream_targets("movie-1")
        assert targets
        assert targets[0].kind is StreamTargetKind.DIRECT
        assert targets[0].url

    async def test_stream_targets_carry_the_quality_facts(
        self, harness: SourceHarness
    ) -> None:
        """PRD 07: Usher "supplies complete information" so the client can
        choose. A target with no container and no codec is a URL, not a
        choice."""
        await harness.given_item(MOVIE, changed_at=T0)
        direct = (await harness.adapter.stream_targets("movie-1"))[0]
        assert direct.container == "mkv"
        assert direct.video_codec == "hevc"
        assert direct.hdr_format is HdrFormat.DOLBY_VISION
        assert direct.resolution == "3840x2160"
        assert direct.runtime_seconds == 9360
        assert direct.audio is not None
        assert direct.audio.startswith("truehd")
        assert direct.scheme is None

    async def test_stream_targets_include_a_deep_link_with_its_scheme(
        self, harness: SourceHarness
    ) -> None:
        """PRD 07: "the deep-link construction currently done by hand in the
        Home Assistant card moves here, where it is testable." If an adapter
        produces no deep link, it has not moved. Any source with a direct
        HTTP URL can produce one, because the Infuse scheme wraps an
        arbitrary URL."""
        await harness.given_item(MOVIE, changed_at=T0)
        targets = await harness.adapter.stream_targets("movie-1")
        links = [target for target in targets if target.kind is StreamTargetKind.DEEP_LINK]
        assert links
        for link in links:
            assert link.scheme
            assert link.url.startswith(f"{link.scheme}:")

    async def test_stream_targets_carry_the_resume_position(
        self, harness: SourceHarness
    ) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.given_watch_state(
            SourceWatchState(external_id="movie-1", position_seconds=1840, played=False)
        )
        direct = (await harness.adapter.stream_targets("movie-1"))[0]
        assert direct.resume_position_seconds == 1840

    async def test_stream_targets_are_empty_for_something_unplayable(
        self, harness: SourceHarness
    ) -> None:
        """A series is a folder. An adapter that fabricated a stream URL for
        one would hand a client a link that 404s at play time."""
        await harness.given_item(SERIES, changed_at=T0)
        assert await harness.adapter.stream_targets("series-1") == []

    async def test_stream_targets_are_empty_for_an_unknown_item(
        self, harness: SourceHarness
    ) -> None:
        assert await harness.adapter.stream_targets("never-existed") == []

    # --- watch state ---------------------------------------------------

    async def test_watch_state_reports_position_and_played(
        self, harness: SourceHarness
    ) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.given_watch_state(
            SourceWatchState(
                external_id="movie-1", position_seconds=1840, played=False, play_count=1
            )
        )
        states = {state.external_id: state async for state in harness.adapter.watch_state()}
        assert states["movie-1"].position_seconds == 1840
        assert states["movie-1"].played is False

    async def test_watch_state_reports_a_played_item(self, harness: SourceHarness) -> None:
        await harness.given_item(EPISODE, changed_at=T0)
        await harness.given_watch_state(
            SourceWatchState(external_id="episode-1", position_seconds=0, played=True)
        )
        states = {state.external_id: state async for state in harness.adapter.watch_state()}
        assert states["episode-1"].played is True

    async def test_watch_state_emits_a_zero_state_rather_than_skipping_it(
        self, harness: SourceHarness
    ) -> None:
        """Filtering empty states looks like an obvious saving and is a
        correctness bug: un-marking something played *is* an all-zero state,
        so an adapter that skipped them could never propagate a reset -- the
        delta walk would find the changed item and then discard exactly the
        record describing the change."""
        await harness.given_item(MOVIE, changed_at=T0)
        states = {state.external_id async for state in harness.adapter.watch_state()}
        assert "movie-1" in states

    async def test_watch_state_since_is_inclusive(self, harness: SourceHarness) -> None:
        await harness.given_item(MOVIE, changed_at=T1)
        await harness.given_watch_state(
            SourceWatchState(external_id="movie-1", position_seconds=90, played=False)
        )
        states = {state.external_id async for state in harness.adapter.watch_state(since=T1)}
        assert "movie-1" in states

    async def test_watch_state_raises_rather_than_truncating(
        self, harness: SourceHarness
    ) -> None:
        await self._seed_library(harness)
        await harness.fail_after_items(3)
        seen: list[SourceWatchState] = []
        with pytest.raises(PortUnavailable):
            async for state in harness.adapter.watch_state():
                seen.append(state)
        assert len(seen) >= 3

    async def test_push_watch_state_is_visible_to_the_source(
        self, harness: SourceHarness
    ) -> None:
        """Read back from the source's own state, not from a record of the
        call -- a `pass` body, or a call to an endpoint that answers 200 and
        ignores the payload, both fail this and neither would fail an
        "it didn't raise" assertion."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.adapter.push_watch_state(
            "movie-1", WatchStateUpdate(position_seconds=600, played=False)
        )
        assert await harness.recorded_watch_state("movie-1") == (600, False)

    async def test_push_watch_state_marks_played(self, harness: SourceHarness) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.adapter.push_watch_state(
            "movie-1", WatchStateUpdate(position_seconds=0, played=True)
        )
        recorded = await harness.recorded_watch_state("movie-1")
        assert recorded is not None
        assert recorded[1] is True

    async def test_push_watch_state_raises_on_failure(self, harness: SourceHarness) -> None:
        """The port's docstring: "best-effort" describes the caller, not
        this method. An adapter that swallowed the error would mean the
        caller's retry never gets enqueued and the write is simply lost."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.go_offline()
        with pytest.raises(PortUnavailable):
            await harness.adapter.push_watch_state(
                "movie-1", WatchStateUpdate(position_seconds=600, played=False)
            )

    # --- status --------------------------------------------------------

    async def test_verify_reports_a_healthy_source(self, harness: SourceHarness) -> None:
        status = await harness.adapter.verify()
        assert status.reachable is True
        assert status.authenticated is True

    async def test_verify_reports_bad_credentials_without_raising(
        self, harness: SourceHarness
    ) -> None:
        """The 🔶 this settles. `GET /admin/sources/{id}/status` renders
        these states; it does not handle them, so `verify` returns rather
        than raising -- and reachable-but-unauthenticated is a *different*
        answer from unreachable, which is exactly what a bool could not
        say."""
        await harness.reject_credentials()
        status = await harness.adapter.verify()
        assert status.reachable is True
        assert status.authenticated is False

    async def test_verify_reports_an_unreachable_source(
        self, harness: SourceHarness
    ) -> None:
        await harness.go_offline()
        status = await harness.adapter.verify()
        assert status.reachable is False
        assert status.authenticated is False

    async def test_verify_does_not_claim_push_without_evidence(
        self, harness: SourceHarness
    ) -> None:
        """ADR-0004: a WebSocket handshake against a *nonexistent* path also
        upgrades and also receives `Sessions`, so an upgrade is not
        evidence. Only received messages are. Until a probe asserts on
        messages, `push_available` must be `None`, not `True`."""
        status = await harness.adapter.verify()
        assert status.push_available is not True

    async def test_events_is_offered_exactly_when_supports_push_says_so(
        self, harness: SourceHarness
    ) -> None:
        """An adapter that advertises push it does not have makes the
        reconciler skip the only source it is cover for; one that has push
        and denies it doubles the load on a slow upstream forever."""
        offered: bool
        try:
            async with harness.adapter.events():
                offered = True
        except SourceNotSupported:
            offered = False
        assert offered is harness.adapter.supports_push

    # --- lifecycle -----------------------------------------------------

    async def test_aclose_is_idempotent(self, harness: SourceHarness) -> None:
        """Both a `DELETE /admin/sources/{id}` and process shutdown can
        reach this."""
        await harness.adapter.aclose()
        await harness.adapter.aclose()

    async def test_operations_after_aclose_raise_port_unavailable(
        self, harness: SourceHarness
    ) -> None:
        """Verified while planning: a closed `httpx.AsyncClient` raises a
        bare `RuntimeError`, which is *not* an `httpx.HTTPError` -- so an
        adapter that translates only `httpx.HTTPError` lets a raw stdlib
        exception cross the port boundary, where no caller written against
        `usher.ports.errors` can catch it."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.adapter.aclose()
        with pytest.raises(PortUnavailable):
            await harness.adapter.get_item("movie-1")
        with pytest.raises(PortUnavailable):
            async for _ in harness.adapter.list_items():
                pass
```

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/contract/source_adapter_contract.py -q`
Expected: collected 0 items — the suite class does not start with `Test`, so pytest never instantiates it. That is the design (same as `TitleRepositoryContract`), and the real failure comes from Step 5.

- [ ] **Step 4: Write the fake adapter and its harness**

```python
# tests/fakes/source_adapter.py
"""A `SourceAdapter` with no wire format at all, and its harness.

Exists to prove `SourceAdapterContract` is expressible without reference to
Emby. If the suite passes here *and* against `EmbyAdapter`, the assertions
are about the port; if it only passed against Emby, they would only be
about Emby.

Its round-trip cases are close to tautological -- it hands back the
`SourceItem`s it was seeded with. That is deliberate and not a defect: the
round-trip has teeth in `EmbyHarness`, where the same seeded item has to
survive being rendered into JSON and parsed back. What this fake models for
real is the two behaviours a no-op would let pass on *both* sides:

- a session token that can expire and must be silently re-minted, with
  concurrent expiries collapsing into a single authentication; and
- a rejected credential that is remembered, so a wrong password cannot turn
  every subsequent call into another doomed authentication.

Without those, `test_operations_recover_from_an_expired_credential` and
`test_rejected_credentials_do_not_produce_a_request_storm` would pass here
against an adapter that did nothing at all, and a reviewer would have no
signal that the assertions mean anything.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from urllib.parse import quote

from pydantic import AwareDatetime

from tests.contract.source_harness import SourceHarness
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.errors import PortAuthFailed, PortUnavailable
from usher.ports.source import (
    SourceAdapter,
    SourceEvent,
    SourceItem,
    SourceItemKind,
    SourceNotSupported,
    SourceStatus,
    SourceWatchState,
    StreamTarget,
    StreamTargetKind,
    WatchStateUpdate,
)

# Same layout table the Emby mapper uses. Duplicated rather than imported so
# this fake stays independent of the adapter it is meant to be an
# alternative to -- importing Emby's mapper here would make "the suite is
# not Emby-shaped" untrue by construction.
_LAYOUTS = {1: "1_0", 2: "2_0", 6: "5_1", 8: "7_1"}


class FakeSourceAdapter(SourceAdapter):
    def __init__(self, source: Source) -> None:
        self._source = source
        self._items: dict[str, SourceItem] = {}
        self._changed_at: dict[str, AwareDatetime] = {}
        self._states: dict[str, SourceWatchState] = {}
        self._offline = False
        self._credentials_valid = True
        self._closed = False
        self._fail_after: int | None = None
        # The session model. `_server_token` is what the source currently
        # accepts; `_token` is what this adapter last obtained. Expiring a
        # session rotates the former, so the next call sees a mismatch and
        # must re-authenticate -- exactly the shape of the Emby failure.
        self._server_token = "session-0"
        self._token: str | None = None
        self._auth_rejected = False
        self._lock = asyncio.Lock()
        self.authentications = 0

    # -- harness-facing state ------------------------------------------

    def seed(self, item: SourceItem, changed_at: AwareDatetime) -> None:
        self._items[item.external_id] = item
        self._changed_at[item.external_id] = changed_at

    def seed_state(self, state: SourceWatchState) -> None:
        self._states[state.external_id] = state

    def forget(self, external_id: str) -> None:
        self._items.pop(external_id, None)
        self._changed_at.pop(external_id, None)

    def recorded(self, external_id: str) -> tuple[int, bool] | None:
        state = self._states.get(external_id)
        return None if state is None else (state.position_seconds, state.played)

    def go_offline(self) -> None:
        self._offline = True

    def fail_after(self, count: int) -> None:
        self._fail_after = count

    def reject_credentials(self) -> None:
        self._credentials_valid = False
        self._token = None

    def expire_credentials(self) -> None:
        self._server_token = f"session-{self.authentications + 1}"

    # -- the port ------------------------------------------------------

    @property
    def source_id(self) -> uuid.UUID:
        return self._source.id

    @property
    def supports_push(self) -> bool:
        return False

    async def _ready(self) -> None:
        if self._closed:
            raise PortUnavailable("adapter is closed")
        if self._offline:
            raise PortUnavailable("source is unreachable")
        async with self._lock:
            if self._token is not None and self._token == self._server_token:
                return
            if self._auth_rejected:
                raise PortAuthFailed("credentials were rejected; not retrying yet")
            self.authentications += 1
            if not self._credentials_valid:
                self._auth_rejected = True
                raise PortAuthFailed("credentials were rejected")
            self._token = self._server_token

    async def verify(self) -> SourceStatus:
        if self._closed or self._offline:
            return SourceStatus(reachable=False, authenticated=False, detail="unreachable")
        try:
            await self._ready()
        except PortAuthFailed as exc:
            return SourceStatus(reachable=True, authenticated=False, detail=str(exc))
        return SourceStatus(reachable=True, authenticated=True, server_version="fake-1.0")

    def list_items(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        return self._walk_items(since)

    async def _walk_items(self, since: AwareDatetime | None) -> AsyncIterator[SourceItem]:
        await self._ready()
        yielded = 0
        for external_id, item in list(self._items.items()):
            if since is not None and self._changed_at[external_id] < since:
                continue
            if self._fail_after is not None and yielded >= self._fail_after:
                raise PortUnavailable("source went away mid-walk")
            yield item
            yielded += 1

    async def get_item(self, external_id: str) -> SourceItem | None:
        await self._ready()
        return self._items.get(external_id)

    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        await self._ready()
        item = self._items.get(external_id)
        if item is None or item.kind is SourceItemKind.SERIES or item.container is None:
            return []
        url = f"{self._source.base_url}/play/{external_id}.{item.container}"
        state = self._states.get(external_id)
        audio_parts = [part for part in (item.audio_codec,) if part]
        layout = _LAYOUTS.get(item.audio_channels or 0)
        if audio_parts and layout:
            audio_parts.append(layout)
        return [
            StreamTarget(
                kind=StreamTargetKind.DIRECT,
                url=url,
                container=item.container,
                video_codec=item.video_codec,
                audio="_".join(audio_parts) or None,
                hdr_format=item.hdr_format,
                resolution=(
                    f"{item.width}x{item.height}"
                    if item.width is not None and item.height is not None
                    else None
                ),
                runtime_seconds=item.runtime_seconds,
                resume_position_seconds=None if state is None else state.position_seconds,
            ),
            StreamTarget(
                kind=StreamTargetKind.DEEP_LINK,
                url=f"infuse://x-callback-url/play?url={quote(url, safe='')}",
                scheme="infuse",
            ),
        ]

    def watch_state(
        self, since: AwareDatetime | None = None
    ) -> AsyncIterator[SourceWatchState]:
        return self._walk_states(since)

    async def _walk_states(
        self, since: AwareDatetime | None
    ) -> AsyncIterator[SourceWatchState]:
        await self._ready()
        yielded = 0
        for external_id in list(self._items):
            if since is not None and self._changed_at[external_id] < since:
                continue
            if self._fail_after is not None and yielded >= self._fail_after:
                raise PortUnavailable("source went away mid-walk")
            # An item with no recorded state yields an all-zero state rather
            # than being skipped -- see the contract's
            # test_watch_state_emits_a_zero_state_rather_than_skipping_it.
            yield self._states.get(external_id) or SourceWatchState(
                external_id=external_id, position_seconds=0, played=False
            )
            yielded += 1

    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        await self._ready()
        self._states[external_id] = SourceWatchState(
            external_id=external_id,
            position_seconds=state.position_seconds,
            played=state.played,
        )

    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        raise SourceNotSupported("this adapter has no push channel")

    async def aclose(self) -> None:
        self._closed = True


class FakeSourceHarness(SourceHarness):
    def __init__(self) -> None:
        self._source = Source(
            id=new_id(),
            kind=SourceKind.EMBY,
            name="Fake Source",
            base_url="https://fake.invalid",
            credentials_ref="ref-fake",
            device_id=str(new_id()),
        )
        self._adapter = FakeSourceAdapter(self._source)

    @property
    def source(self) -> Source:
        return self._source

    @property
    def adapter(self) -> SourceAdapter:
        return self._adapter

    async def given_item(self, item: SourceItem, *, changed_at: AwareDatetime) -> None:
        self._adapter.seed(item, changed_at)

    async def given_watch_state(self, state: SourceWatchState) -> None:
        self._adapter.seed_state(state)

    async def remove_item(self, external_id: str) -> None:
        self._adapter.forget(external_id)

    async def recorded_watch_state(self, external_id: str) -> tuple[int, bool] | None:
        return self._adapter.recorded(external_id)

    async def go_offline(self) -> None:
        self._adapter.go_offline()

    async def fail_after_items(self, count: int) -> None:
        self._adapter.fail_after(count)

    async def reject_credentials(self) -> None:
        self._adapter.reject_credentials()

    async def expire_credentials(self) -> None:
        self._adapter.expire_credentials()

    def authentications(self) -> int:
        return self._adapter.authentications

    async def aclose(self) -> None:
        await self._adapter.aclose()
```

> **Transcription note.** `events()` is a plain `def` that raises rather than an `@asynccontextmanager`, so `SourceNotSupported` surfaces at *call* time, not at `__aenter__`. The contract's `async with harness.adapter.events():` evaluates the call inside its own `try`, so both shapes are caught — but mypy has to be told the function's declared return type is unreachable, which it infers automatically from a body that only raises. `AbstractAsyncContextManager` is imported solely for that annotation and is genuinely used; `asynccontextmanager` is deliberately *not* imported, because ruff's `F401` flags an unused import and nothing here yields a real channel until M5.

- [ ] **Step 5: Write the runner, run it, and watch it pass**

```python
# tests/unit/test_source_adapter_contract.py
"""The source-adapter contract against an adapter with no wire format.

The companion run is tests/unit/test_adapters_emby_contract.py, which
executes the identical assertions against the real EmbyAdapter over an
in-memory Emby. Both are needed: this one proves the assertions are not
secretly Emby-shaped, that one proves they survive a wire format.
"""

from collections.abc import AsyncIterator

import pytest_asyncio

from tests.contract.source_adapter_contract import SourceAdapterContract
from tests.contract.source_harness import SourceHarness
from tests.fakes.source_adapter import FakeSourceHarness


class TestFakeSourceAdapter(SourceAdapterContract):
    @pytest_asyncio.fixture
    async def harness(self) -> AsyncIterator[SourceHarness]:
        harness = FakeSourceHarness()
        try:
            yield harness
        finally:
            await harness.aclose()
```

Run: `uv run pytest tests/unit/test_source_adapter_contract.py -q`
Expected: PASS — **40** tests (the number of `test_*` methods on `SourceAdapterContract`; count them if the run disagrees, because a mis-indented method silently disappears from the class rather than erroring).

- [ ] **Step 6: Write ADR-0013**

```markdown
# ADR-0013 — The source contract suite drives a harness, not a cassette

**Status:** Accepted

## Context

PRD 08 calls the adapter contract suite "the load-bearing one": it is what
makes "Emby is replaceable" a testable claim rather than a slogan. The
question is what the suite talks to. Four options were live:

1. **Recorded HTTP cassettes** (VCR-style) replayed per adapter.
2. **A real Emby in a container**, driven for real.
3. **A hand-written suite per adapter**, sharing nothing.
4. **A harness ABC** the suite arranges state through, implemented once per
   adapter.

## Decision

Option 4. `tests/contract/source_adapter_contract.py` speaks only
`usher.ports.source`'s own DTOs and arranges every precondition through
`tests/contract/source_harness.py`'s `SourceHarness`. Each adapter supplies
a harness that translates those DTOs into its own upstream's shape.

## Consequences

**Gained:** a Jellyfin or Plex adapter passes the *same file*, unmodified.
The suite cannot accidentally encode Emby's field names, because it has no
way to mention them. Two implementations run it in M3 — a pure in-memory
adapter and the real Emby one — and the pair is what makes the claim
checkable: if only the Emby run existed, "source-agnostic" would be
untested.

**Accepted cost:** every adapter writes a harness, which is real work
(`FakeEmbyServer` is the largest single file in M3's test tree). Some of a
harness's own behaviour is untested — nothing verifies that `EmbyHarness`
renders a `SourceItem` into JSON the *real* Emby would also produce.

**Mitigation for that cost:** the fixtures the fake server renders are
shape-recorded from a real response, and a separate test parses each
committed fixture through the adapter's own mapper with no fake server
involved. A wrong *shape* therefore fails a test that does not depend on
the harness at all. A wrong *endpoint path* is the residual gap, and only a
live run closes it — which is why M3's definition of done requires one.

## Why not the others

**Cassettes** pin one server's bytes. The suite would become "Emby's
recorded responses replayed", and a second adapter could not run it at all
without recording its own — at which point the two suites are the
hand-written duplicates option 3 already loses on, with extra machinery.

**A real containerised Emby** is not redistributable, needs a licence key
for some features, and would make the *load-bearing* suite Docker-gated —
`tests/unit` exists precisely so the fast lane needs nothing. It is
valuable as a manual verification step, and M3's definition of done keeps
it as exactly that.

**A suite per adapter** is what `TitleRepositoryContract`'s own docstring
already argues against: "two hand-maintained copies of these assertions
would drift the moment someone updated one and not the other."
```

- [ ] **Step 7: Check and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest tests/unit -q
```

```bash
git add -A && git commit -F - <<'EOF'
test: the source-agnostic adapter contract suite, and an adapter with no wire format

The spec calls this "the test that proves the abstraction is real". It is
written against a SourceHarness ABC whose whole vocabulary is the port's
own DTOs, so nothing in the suite can mention Emby -- a Jellyfin or Plex
adapter writes a harness and passes the same file unchanged.

FakeSourceAdapter runs it first, before any Emby code exists, which is the
only way to know the assertions are about the port rather than about one
upstream's JSON. It models an expiring session and a remembered credential
rejection for real, because a no-op there would let two of the suite's
sharpest cases pass against an adapter that did nothing.

ADR-0013 records why a harness rather than cassettes or a containerised
server.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 5: The fixtures, and Emby's JSON → the port's DTOs

Pure functions and committed payloads, before any HTTP exists. Testing the translation with no server at all is what makes the fixtures a real drift guard: a wrong *shape* fails a test that does not involve the fake server, so `EmbyHarness` cannot ratify a mapping bug by rendering the same wrong shape it reads back.

**Are Emby payloads redistributable?** The question was checked and the answer is: **do not commit a real one.** Emby Server's own licence governs the software, not a response describing your library — but a response *body* embeds TMDb-sourced metadata (overview, ratings, artwork paths), which TMDb's terms prohibit redistributing and which CLAUDE.md's "ship importers, never data" already forbids committing; it also carries a real server id, a real user id, and, if `Path` is requested, real filesystem paths. So the committed fixtures are **shape-recorded and value-synthetic**: field names, nesting, and types transcribed from real responses; every value invented. `scripts/capture_emby_fixture.py` (Task 13) regenerates a scrubbed capture locally for an operator who wants to diff shapes, and never commits one.

**One privacy decision falls out of this:** `list_items` does not request `Fields=Path`. Nothing in M3 or M4 needs a filesystem path, and not asking for it keeps it out of `SourceItem.raw`, which PRD 03 stores verbatim in `raw_payloads`.

**Files:**
- Create: `src/usher/adapters/http.py`
- Modify: `src/usher/adapters/bulk/download.py`, `src/usher/adapters/bulk/wikidata.py`
- Create: `src/usher/adapters/emby/__init__.py`, `src/usher/adapters/emby/mapping.py`
- Create: `tests/fixtures/emby/movie_item.json`, `series_item.json`, `episode_item.json`
- Create: `tests/fakes/emby_fixtures.py`
- Test: `tests/unit/test_adapters_emby_mapping.py`

- [ ] **Step 1: Extract the shared 429 parser**

PRD 01 anticipates this: "a shared `BaseHTTPAdapter` carries the httpx client lifecycle, retry/backoff, and rate-limit handling that the Emby and TMDb adapters both need". `_retry_after_seconds` is already shared between two M2 modules by importing a private name across them; Emby needs it too, and `usher.adapters.emby` importing `usher.adapters.bulk` would be worse.

Create `src/usher/adapters/http.py` with the function moved verbatim from `bulk/download.py`, renamed public:

```python
"""HTTP helpers shared by every adapter that talks to an upstream over
httpx.

PRD 01 anticipates this module: "a shared `BaseHTTPAdapter` carries the
httpx client lifecycle, retry/backoff, and rate-limit handling that the
Emby and TMDb adapters both need, instead of each reimplementing it." This
is the first piece of it -- the client lifecycle stays per-adapter for now,
because `CachedDatasetFile` is handed a shared client it does not own while
`EmbyAdapter` owns one per source, and forcing those two into one base
class would be shape for its own sake.
"""

import datetime as dt
import email.utils


def retry_after_seconds(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds from now, or `None`
    if there was no header or it couldn't be parsed at all.

    RFC 9110 permits `Retry-After` to be *either* an integer number of
    seconds *or* an HTTP-date -- `float(value)` alone raises `ValueError`
    on the latter (`could not convert string to float: 'Wed, 21 Oct 2026
    07:28:00 GMT'`), and this is the 429 path: the one moment upstream is
    explicitly asking for backoff. A caller that only handled the numeric
    form would raise instead of backing off exactly when backing off
    matters most. Shared by every adapter's 429 handling rather than
    duplicated -- the bug this fixes existed in two places for exactly
    that reason.
    """
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        target = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.UTC)
    return max(0.0, (target - dt.datetime.now(dt.UTC)).total_seconds())
```

Then in `src/usher/adapters/bulk/download.py`: delete the `_retry_after_seconds` function and the now-unused `import email.utils`, add `from usher.adapters.http import retry_after_seconds`, and change the one call site in `_raise_for_status` to `retry_after_seconds(...)`. Note `import datetime as dt` stays — `download.py` uses `dt` elsewhere. In `src/usher/adapters/bulk/wikidata.py`, replace `from usher.adapters.bulk.download import _retry_after_seconds` with `from usher.adapters.http import retry_after_seconds` and update its one call site.

Run: `uv run pytest tests/unit/test_adapters_bulk_download.py tests/unit/test_adapters_bulk_wikidata.py -q`
Expected: PASS, unchanged. Both suites exercise this through `revision()`'s 429 handling, including the HTTP-date case, so a botched extraction fails here rather than silently.

- [ ] **Step 2: Commit the fixtures**

`tests/fixtures/emby/movie_item.json`:

```json
{
  "Name": "Example Movie",
  "OriginalTitle": "Example Movie",
  "ServerId": "0000000000000000000000000000feed",
  "Id": "0000000000000000000000000000a001",
  "DateCreated": "2024-03-01T18:22:11.0000000Z",
  "PremiereDate": "2021-09-15T00:00:00.0000000Z",
  "ProductionYear": 2021,
  "RunTimeTicks": 93600000000,
  "Type": "Movie",
  "IsFolder": false,
  "MediaType": "Video",
  "ProviderIds": { "Tmdb": "438631", "Imdb": "tt1160419" },
  "UserData": {
    "PlaybackPositionTicks": 18400000000,
    "PlayCount": 1,
    "IsFavorite": false,
    "Played": false,
    "LastPlayedDate": "2026-07-20T21:04:00.0000000Z"
  },
  "MediaSources": [
    {
      "Id": "0000000000000000000000000000b001",
      "Name": "Example Movie 2160p",
      "Container": "mkv",
      "Size": 68719476736,
      "RunTimeTicks": 93600000000,
      "MediaStreams": [
        {
          "Index": 0,
          "Type": "Video",
          "Codec": "hevc",
          "Profile": "Main 10",
          "Width": 3840,
          "Height": 2160,
          "VideoRange": "HDR",
          "VideoRangeType": "DOVI",
          "DvProfile": 8,
          "DvLevel": 9
        },
        {
          "Index": 1,
          "Type": "Audio",
          "Codec": "truehd",
          "Profile": "TrueHD Atmos",
          "Channels": 8,
          "Language": "eng",
          "IsDefault": true
        },
        {
          "Index": 2,
          "Type": "Subtitle",
          "Codec": "subrip",
          "Language": "eng",
          "IsDefault": false
        }
      ]
    }
  ]
}
```

`tests/fixtures/emby/series_item.json`:

```json
{
  "Name": "Example Series",
  "ServerId": "0000000000000000000000000000feed",
  "Id": "0000000000000000000000000000a002",
  "DateCreated": "2024-05-01T09:00:00.0000000Z",
  "ProductionYear": 2011,
  "EndDate": "2019-05-19T00:00:00.0000000Z",
  "RunTimeTicks": null,
  "Type": "Series",
  "IsFolder": true,
  "ProviderIds": { "Tmdb": "1399", "Imdb": "tt0944947", "Tvdb": "121361" },
  "UserData": {
    "PlaybackPositionTicks": 0,
    "PlayCount": 0,
    "IsFavorite": false,
    "Played": false,
    "UnplayedItemCount": 12
  }
}
```

`tests/fixtures/emby/episode_item.json`:

```json
{
  "Name": "Example Episode",
  "ServerId": "0000000000000000000000000000feed",
  "Id": "0000000000000000000000000000a003",
  "SeriesId": "0000000000000000000000000000a002",
  "SeriesName": "Example Series",
  "SeasonId": "0000000000000000000000000000a004",
  "ParentIndexNumber": 2,
  "IndexNumber": 5,
  "DateCreated": "2024-05-04T09:00:00.0000000Z",
  "ProductionYear": 2013,
  "RunTimeTicks": 33000000000,
  "Type": "Episode",
  "IsFolder": false,
  "MediaType": "Video",
  "ProviderIds": { "Imdb": "tt2178782", "Tvdb": "4517466" },
  "UserData": {
    "PlaybackPositionTicks": 0,
    "PlayCount": 1,
    "IsFavorite": false,
    "Played": true
  },
  "MediaSources": [
    {
      "Id": "0000000000000000000000000000b003",
      "Name": "Example Episode 1080p",
      "Container": "mkv",
      "Size": 3221225472,
      "RunTimeTicks": 33000000000,
      "MediaStreams": [
        {
          "Index": 0,
          "Type": "Video",
          "Codec": "h264",
          "Profile": "High",
          "Width": 1920,
          "Height": 1080,
          "VideoRange": "SDR"
        },
        {
          "Index": 1,
          "Type": "Audio",
          "Codec": "eac3",
          "Profile": "Dolby Digital+",
          "Channels": 6,
          "Language": "eng",
          "IsDefault": true
        }
      ]
    }
  ]
}
```

`tests/fakes/emby_fixtures.py`:

```python
"""Loader for the committed Emby payload fixtures.

Shape-recorded, value-synthetic: field names, nesting, and types were
transcribed from real Emby 4.9.5.0 responses; every value is invented. See
`usher.adapters.emby.mapping`'s module docstring for why a real capture is
not committed.
"""

import json
from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "emby"


def load_emby_fixture(name: str) -> dict[str, Any]:
    """One fixture, freshly parsed on every call.

    Freshly, not cached: callers mutate what they get back -- the fake
    server overwrites fields to render a seeded item -- and a shared,
    cached dict would let one test's mutation leak into the next.
    """
    payload: dict[str, Any] = json.loads(
        (_FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    )
    return payload
```

- [ ] **Step 3: Write the failing mapping test**

```python
# tests/unit/test_adapters_emby_mapping.py
"""Emby's JSON -> the port's DTOs, against the committed fixtures.

No HTTP and no fake server: this is the test that makes the fixtures a real
drift guard rather than decoration. If `FakeEmbyServer` and the mapper both
got a field name wrong in the same way, the contract suite would still
pass; this would not.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tests.fakes.emby_fixtures import load_emby_fixture
from usher.adapters.emby.mapping import (
    audio_token,
    emby_datetime,
    hdr_format,
    parse_datetime,
    primary_media_source,
    stream_of,
    to_source_item,
    to_watch_state,
)
from usher.domain.enums import HdrFormat
from usher.ports.errors import PortDataMalformed
from usher.ports.source import SourceItemKind


def test_a_movie_maps_every_field_the_port_promises() -> None:
    item = to_source_item(load_emby_fixture("movie_item"))
    assert item is not None
    assert item.external_id == "0000000000000000000000000000a001"
    assert item.name == "Example Movie"
    assert item.kind is SourceItemKind.MOVIE
    assert item.year == 2021
    assert item.provider_ids == {"tmdb": "438631", "imdb": "tt1160419"}
    assert item.container == "mkv"
    assert item.video_codec == "hevc"
    assert item.audio_codec == "truehd"
    assert (item.width, item.height) == (3840, 2160)
    assert item.audio_channels == 8
    assert item.file_size_bytes == 68719476736
    assert item.runtime_seconds == 9360
    assert item.added_at == datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC)
    assert item.series_external_id is None
    assert item.season_number is None
    assert item.episode_number is None


def test_provider_id_keys_are_lowercased() -> None:
    """Emby spells them `Tmdb`/`Imdb`/`Tvdb`. M4's matcher reads
    `provider_ids["tmdb"]` and must not know that."""
    item = to_source_item(load_emby_fixture("series_item"))
    assert item is not None
    assert item.provider_ids == {"tmdb": "1399", "imdb": "tt0944947", "tvdb": "121361"}


def test_dolby_vision_wins_over_the_hdr10_fallback_layer() -> None:
    """The movie fixture carries `VideoRange: "HDR"`, `VideoRangeType:
    "DOVI"`, and a `DvProfile` all at once, which is what a real DV file
    looks like -- the HDR10 base layer is genuinely there. Ordering the
    checks the other way round catalogues every DV file as HDR10."""
    item = to_source_item(load_emby_fixture("movie_item"))
    assert item is not None
    assert item.hdr_format is HdrFormat.DOLBY_VISION


def test_sdr_maps_to_no_hdr_format_at_all() -> None:
    item = to_source_item(load_emby_fixture("episode_item"))
    assert item is not None
    assert item.hdr_format is None


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ({"VideoRangeType": "HDR10"}, HdrFormat.HDR10),
        ({"VideoRangeType": "HDR10Plus"}, HdrFormat.HDR10),
        ({"VideoRangeType": "HLG"}, HdrFormat.HLG),
        ({"VideoRange": "HDR"}, HdrFormat.HDR10),
        ({"VideoRange": "SDR"}, None),
        ({"VideoRangeType": "DOVI"}, HdrFormat.DOLBY_VISION),
        ({"Profile": "Dolby Vision"}, HdrFormat.DOLBY_VISION),
        ({}, None),
    ],
)
def test_hdr_vocabulary(stream: dict[str, object], expected: HdrFormat | None) -> None:
    """`HdrFormat` has no HDR10+ member, so HDR10Plus deliberately maps to
    HDR10 -- lossy, but true (HDR10+ carries an HDR10 base layer), and
    better than dropping the fact that the file is HDR at all."""
    assert hdr_format(stream) is expected


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ({"Codec": "truehd", "Profile": "TrueHD Atmos", "Channels": 8}, "truehd_atmos_7_1"),
        ({"Codec": "eac3", "Profile": "Dolby Digital+", "Channels": 6}, "eac3_5_1"),
        ({"Codec": "dts", "Profile": "DTS-HD MA", "Channels": 8}, "dts_hd_ma_7_1"),
        ({"Codec": "aac", "Channels": 2}, "aac_2_0"),
        ({"Codec": "flac", "Channels": 1}, "flac_1_0"),
        ({"Codec": "pcm", "Channels": 12}, "pcm_12ch"),
        ({"Codec": "aac"}, "aac"),
        ({"Channels": 6}, None),
    ],
)
def test_audio_token_vocabulary(stream: dict[str, object], expected: str | None) -> None:
    """PRD 07's example value is exactly `truehd_atmos_7_1`, and this is
    where "the deep-link construction currently done by hand in the Home
    Assistant card moves here, where it is testable" becomes literally
    true."""
    assert audio_token(stream) == expected


def test_an_episode_carries_its_place_in_the_series() -> None:
    item = to_source_item(load_emby_fixture("episode_item"))
    assert item is not None
    assert item.kind is SourceItemKind.EPISODE
    assert item.series_external_id == "0000000000000000000000000000a002"
    assert item.season_number == 2
    assert item.episode_number == 5


def test_a_series_has_no_media_and_no_runtime() -> None:
    """`RunTimeTicks` is literally `null` in the fixture, and there is no
    `MediaSources` key at all -- both are how Emby describes a folder, and
    both are places a mapper that assumed a value would raise."""
    item = to_source_item(load_emby_fixture("series_item"))
    assert item is not None
    assert item.kind is SourceItemKind.SERIES
    assert item.runtime_seconds is None
    assert item.container is None
    assert item.video_codec is None
    assert primary_media_source(load_emby_fixture("series_item")) is None


def test_an_unmodelled_item_type_is_skipped_not_raised() -> None:
    """Seasons, box sets, and playlists come back from a server that
    ignores `IncludeItemTypes`. Skipping keeps the walk going; raising
    would abort a 94,395-item reconcile over a box set."""
    assert to_source_item({"Id": "x", "Type": "BoxSet", "Name": "Franchise"}) is None


def test_an_item_with_no_id_is_malformed() -> None:
    """Distinct from an unmodelled type: an item with no id cannot be
    upserted on `(source_id, external_id)`, so silently skipping it would
    lose a real item with no trace."""
    with pytest.raises(PortDataMalformed):
        to_source_item({"Type": "Movie", "Name": "Nameless"})


def test_watch_state_converts_ticks_to_seconds() -> None:
    state = to_watch_state(load_emby_fixture("movie_item"), source_user_id="user-1")
    assert state is not None
    assert state.position_seconds == 1840
    assert state.played is False
    assert state.play_count == 1
    assert state.last_played_at == datetime(2026, 7, 20, 21, 4, 0, tzinfo=UTC)
    assert state.source_user_id == "user-1"


def test_watch_state_reads_a_played_flag() -> None:
    state = to_watch_state(load_emby_fixture("episode_item"), source_user_id="user-1")
    assert state is not None
    assert state.played is True
    assert state.position_seconds == 0


def test_missing_user_data_is_not_a_zero_state() -> None:
    """A zero state and an absent one are different claims. `UserData` is
    absent when the field was not requested; emitting a zero state for that
    would push "unwatched" over whatever Usher already knows."""
    assert to_watch_state({"Id": "x", "Type": "Movie"}, source_user_id="user-1") is None


def test_a_timestamp_without_an_offset_is_still_aware() -> None:
    """Verified on Python 3.13: `fromisoformat` returns a *naive* datetime
    for a value with no offset, and `SourceItem` is a plain dataclass that
    would carry it happily all the way to a TIMESTAMPTZ insert. Emby's
    timestamps are UTC, so the offset is attached rather than the value
    rejected."""
    parsed = parse_datetime("2024-03-01T18:22:11.0000000")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed == datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "", "not-a-date", 17, "2024-13-45"])
def test_unparseable_timestamps_become_none(value: object) -> None:
    assert parse_datetime(value) is None


def test_a_cursor_is_widened_by_one_second() -> None:
    """The port promises `since` is inclusive; whether Emby's own
    comparison is `>=` or `>` is unverified. Sending one second earlier is
    correct under either, and the port explicitly permits a superset."""
    assert emby_datetime(datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)) == "2026-07-20T11:59:59Z"


def test_a_cursor_is_normalised_to_utc() -> None:
    """A caller's cursor may carry any offset -- `AwareDatetime` only
    promises it has one. Sending a local-time string to a server that reads
    it as UTC shifts the whole delta window."""
    local = datetime(2026, 7, 20, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert emby_datetime(local) == "2026-07-20T11:59:59Z"


def test_the_default_audio_stream_is_preferred_over_the_first() -> None:
    """A file whose first audio track is a commentary and whose default is
    the feature audio is normal. Taking `[0]` would report the commentary's
    codec and channel layout as the item's."""
    media_source = {
        "MediaStreams": [
            {"Type": "Audio", "Codec": "aac", "Channels": 2, "IsDefault": False},
            {"Type": "Audio", "Codec": "truehd", "Channels": 8, "IsDefault": True},
        ]
    }
    chosen = stream_of(media_source, "Audio")
    assert chosen is not None
    assert chosen["Codec"] == "truehd"
```

- [ ] **Step 4: Run and watch it fail**

Run: `uv run pytest tests/unit/test_adapters_emby_mapping.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.emby'`

- [ ] **Step 5: Write the mapper**

`src/usher/adapters/emby/__init__.py` is empty (package marker), matching `adapters/bulk/__init__.py`.

```python
# src/usher/adapters/emby/mapping.py
"""Emby's JSON, translated into `usher.ports.source`'s DTOs.

Pure functions, no HTTP: everything here is tested against the committed
fixtures with no server of any kind, which is what makes those fixtures a
real drift guard. If the fake server and this module got a field name wrong
in the same way, the contract suite would still pass and
`tests/unit/test_adapters_emby_mapping.py` would not.

**No Emby field name appears anywhere else in `src/`.** This module is the
whole of the translation, which is how PRD 01's "raw Emby or TMDb JSON
never escapes its adapter package" is enforced rather than merely stated.

### On the fixtures

`tests/fixtures/emby/*.json` are shape-recorded and value-synthetic: field
names, nesting, and types transcribed from real Emby 4.9.5.0 responses;
every value invented. A real capture is not committed, for three separate
reasons -- it embeds TMDb-sourced metadata that TMDb's terms forbid
redistributing (and CLAUDE.md's "ship importers, never data" already
forbids committing), it identifies a real library, and it carries real
server and user ids. `scripts/capture_emby_fixture.py` regenerates a
scrubbed capture locally for anyone who wants to diff shapes.

### Three traps this module exists to close

1. **Dolby Vision reports itself several ways at once**, and a DV stream
   commonly *also* advertises HDR10, because the HDR10 base layer is
   genuinely present. Checking `VideoRangeType` first would catalogue every
   DV file as HDR10, so any DV marker wins.
2. **Naive datetimes.** Verified on Python 3.13: `fromisoformat` accepts
   Emby's seven-digit fractional seconds and a trailing `Z`, but a value
   with no offset yields a naive datetime -- and `SourceItem` is a plain
   dataclass, so nothing catches it until a `TIMESTAMPTZ` insert much
   later. Emby's timestamps are UTC, so the offset is attached.
3. **The first audio stream is not the default one.** Commentary tracks
   are routinely index 0. `IsDefault` decides.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from usher.domain.enums import HdrFormat
from usher.ports.errors import PortDataMalformed
from usher.ports.source import SourceItem, SourceItemKind, SourceWatchState

# Emby counts in 100-nanosecond ticks, everywhere: runtimes, playback
# positions, durations.
TICKS_PER_SECOND = 10_000_000

_ITEM_KINDS: dict[str, SourceItemKind] = {
    "Movie": SourceItemKind.MOVIE,
    "Series": SourceItemKind.SERIES,
    "Episode": SourceItemKind.EPISODE,
}

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

# HDR10Plus deliberately maps to HDR10: `HdrFormat` has no HDR10+ member,
# and HDR10+ genuinely carries an HDR10 base layer, so this is lossy rather
# than wrong -- and far better than reporting the file as SDR.
_HDR_BY_TOKEN: dict[str, HdrFormat] = {
    "DOVI": HdrFormat.DOLBY_VISION,
    "DOLBYVISION": HdrFormat.DOLBY_VISION,
    "DV": HdrFormat.DOLBY_VISION,
    "HDR10": HdrFormat.HDR10,
    "HDR10PLUS": HdrFormat.HDR10,
    "HDR": HdrFormat.HDR10,
    "HLG": HdrFormat.HLG,
}

# Ordered: the first match wins, so "DTS-HD MA" is not also matched by a
# looser "master audio" rule further down producing a different token.
_AUDIO_FEATURES: tuple[tuple[str, str], ...] = (
    ("atmos", "atmos"),
    ("dts:x", "x"),
    ("dts-x", "x"),
    ("dts-hd ma", "hd_ma"),
    ("master audio", "hd_ma"),
)

_CHANNEL_LAYOUTS: dict[int, str] = {1: "1_0", 2: "2_0", 6: "5_1", 8: "7_1"}


def _as_int(value: object) -> int | None:
    # `bool` is an `int` subclass, and Emby's JSON is full of booleans in
    # fields adjacent to numeric ones -- without this guard `Played: true`
    # in the wrong slot would become the integer 1.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _lower(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


def parse_datetime(value: object) -> datetime | None:
    """Emby's ISO 8601 into an aware datetime, or `None` if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def emby_datetime(value: datetime) -> str:
    """Format a `since` cursor for Emby's date query parameters.

    Normalised to UTC and **widened by one second**, deliberately. The port
    promises `since` is inclusive; whether Emby's own comparison is `>=` or
    `>` is not verified against the live server. One second earlier is
    correct under either -- an inclusive server returns a superset, which
    the port explicitly permits because callers deduplicate by
    `external_id`; an exclusive one still returns the boundary item. The
    opposite mistake, assuming inclusivity and being wrong, silently drops
    exactly the item the previous walk's cursor was set from, once per
    walk, forever.
    """
    return (value.astimezone(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def provider_ids(raw: object) -> dict[str, str]:
    """Emby's `ProviderIds` into the port's lowercase canonical keys."""
    if not isinstance(raw, Mapping):
        return {}
    return {
        key.lower(): value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


def primary_media_source(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The first `MediaSources` entry, or `None` for a folder item."""
    sources = payload.get("MediaSources")
    if not isinstance(sources, list):
        return None
    for source in sources:
        if isinstance(source, Mapping):
            return source
    return None


def stream_of(media_source: Mapping[str, Any], stream_type: str) -> Mapping[str, Any] | None:
    """The default stream of `stream_type`, falling back to the first.

    `IsDefault` rather than index 0: commentary tracks are routinely the
    first audio stream, and reporting a commentary's codec and channel
    layout as the item's is both wrong and the kind of wrong nobody
    notices.
    """
    streams = media_source.get("MediaStreams")
    if not isinstance(streams, list):
        return None
    candidates = [
        stream
        for stream in streams
        if isinstance(stream, Mapping) and stream.get("Type") == stream_type
    ]
    if not candidates:
        return None
    for stream in candidates:
        if stream.get("IsDefault"):
            return stream
    return candidates[0]


def hdr_format(video: Mapping[str, Any]) -> HdrFormat | None:
    """The canonical `HdrFormat` for a video stream, or `None` for SDR.

    Any Dolby Vision marker wins outright -- see the module docstring.
    """
    profile = str(video.get("Profile") or "").lower()
    if video.get("DvProfile") is not None or "dolby vision" in profile or "dvhe" in profile:
        return HdrFormat.DOLBY_VISION
    for key in ("VideoRangeType", "VideoRange"):
        mapped = _HDR_BY_TOKEN.get(_NON_ALNUM.sub("", str(video.get(key) or "")).upper())
        if mapped is not None:
            return mapped
    return None


def audio_token(audio: Mapping[str, Any]) -> str | None:
    """A single lowercase token describing an audio stream as a client
    thinks about it: `truehd_atmos_7_1`, `eac3_5_1`, `aac_2_0`.

    This is `StreamTarget.audio`, and it is a different thing from
    `SourceItem.audio_codec`'s raw `"truehd"` -- the codec alone does not
    tell a client whether it can play the track, which is the whole point
    of PRD 07 returning ranked targets rather than one URL. An unknown
    channel count falls back to `{n}ch` rather than being dropped, so a
    9.1.6 track is still described rather than silently reported as
    channel-less.
    """
    codec = _lower(audio.get("Codec"))
    if codec is None:
        return None
    parts = [codec]
    descriptor = f"{audio.get('Profile') or ''} {audio.get('Title') or ''}".lower()
    for needle, token in _AUDIO_FEATURES:
        if needle in descriptor:
            parts.append(token)
            break
    channels = _as_int(audio.get("Channels"))
    if channels is not None and channels > 0:
        parts.append(_CHANNEL_LAYOUTS.get(channels, f"{channels}ch"))
    return "_".join(parts)


def to_source_item(payload: Mapping[str, Any]) -> SourceItem | None:
    """One Emby item into a `SourceItem`.

    `None` for an item type Usher does not model -- Season, BoxSet,
    Playlist, Folder. `list_items` asks for only the three types below, but
    a server that ignores `IncludeItemTypes` must not abort a 94,395-item
    walk over a box set. An item with no `Id` is different: it cannot be
    upserted on `(source_id, external_id)` at all, so skipping it would
    lose a real item with no trace, and it raises `PortDataMalformed`.
    """
    external_id = _text(payload.get("Id"))
    if external_id is None:
        raise PortDataMalformed(
            "Emby item has no Id",
            # The name, truncated -- enough to find the item in the Emby UI,
            # short enough not to be a payload dump.
            detail=str(payload.get("Name", "<unnamed>"))[:60],
        )
    kind = _ITEM_KINDS.get(str(payload.get("Type") or ""))
    if kind is None:
        return None
    media_source = primary_media_source(payload) or {}
    video = stream_of(media_source, "Video") or {}
    audio = stream_of(media_source, "Audio") or {}
    runtime_ticks = _as_int(payload.get("RunTimeTicks"))
    return SourceItem(
        external_id=external_id,
        name=_text(payload.get("Name")) or external_id,
        kind=kind,
        year=_as_int(payload.get("ProductionYear")),
        provider_ids=provider_ids(payload.get("ProviderIds")),
        container=_lower(media_source.get("Container")),
        video_codec=_lower(video.get("Codec")),
        audio_codec=_lower(audio.get("Codec")),
        # Item-level Width/Height are the fallback: Emby sets them on the
        # item for some libraries and only on the video stream for others.
        width=_as_int(video.get("Width")) or _as_int(payload.get("Width")),
        height=_as_int(video.get("Height")) or _as_int(payload.get("Height")),
        hdr_format=hdr_format(video),
        audio_channels=_as_int(audio.get("Channels")),
        file_size_bytes=_as_int(media_source.get("Size")),
        runtime_seconds=None if runtime_ticks is None else runtime_ticks // TICKS_PER_SECOND,
        added_at=parse_datetime(payload.get("DateCreated")),
        series_external_id=_text(payload.get("SeriesId")),
        season_number=_as_int(payload.get("ParentIndexNumber")),
        episode_number=_as_int(payload.get("IndexNumber")),
        # PRD 03 stores this verbatim in `raw_payloads`. Copied rather than
        # aliased so a caller that mutates the DTO cannot reach back into
        # whatever buffer the response was parsed from.
        raw=dict(payload),
    )


def to_watch_state(
    payload: Mapping[str, Any], *, source_user_id: str | None
) -> SourceWatchState | None:
    """One Emby item's `UserData` into a `SourceWatchState`.

    `None` when the item carries no `UserData` at all, which means the
    field was not requested or this item type has none. That is a different
    claim from a zero state: emitting zeros here would push "unwatched"
    over whatever Usher already knows. A `UserData` block that *is* present
    and happens to be all zeros is emitted -- see the port's `watch_state`
    docstring for why filtering those is a correctness bug.
    """
    external_id = _text(payload.get("Id"))
    user_data = payload.get("UserData")
    if external_id is None or not isinstance(user_data, Mapping):
        return None
    ticks = _as_int(user_data.get("PlaybackPositionTicks")) or 0
    return SourceWatchState(
        external_id=external_id,
        position_seconds=max(ticks, 0) // TICKS_PER_SECOND,
        played=bool(user_data.get("Played", False)),
        play_count=max(_as_int(user_data.get("PlayCount")) or 0, 0),
        last_played_at=parse_datetime(user_data.get("LastPlayedDate")),
        source_user_id=source_user_id,
    )
```

- [ ] **Step 6: Run and watch it pass**

Run: `uv run pytest tests/unit/test_adapters_emby_mapping.py -q`
Expected: PASS — **36** tests: 15 plain functions plus 21 parametrized cases (8 HDR, 8 audio, 5 timestamp).

- [ ] **Step 7: Check and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest tests/unit -q
```

```bash
git add -A && git commit -F - <<'EOF'
feat: Emby's JSON -> the port's DTOs, with shape-recorded fixtures

Pure functions and committed payloads, before any HTTP exists, so a wrong
field name fails a test that does not involve a fake server -- otherwise
the fake and the mapper could agree on the same mistake and the contract
suite would ratify it.

Three traps closed with tests: Dolby Vision reports itself several ways at
once and also advertises its HDR10 base layer (checking VideoRangeType
first catalogues every DV file as HDR10); Python 3.13's fromisoformat
returns a *naive* datetime for an offsetless timestamp, which SourceItem
being a plain dataclass would carry all the way to a TIMESTAMPTZ insert;
and the first audio stream is routinely a commentary track, so IsDefault
decides.

The `since` cursor is widened by one second: the port promises inclusivity
and Emby's own comparison is unverified, so one second earlier is correct
under either reading and the port permits the superset.

Fixtures are shape-recorded and value-synthetic. A real capture embeds
TMDb-sourced metadata, identifies a real library, and carries real server
and user ids.

Also extracts retry_after_seconds into usher/adapters/http.py -- PRD 01
anticipates this shared layer, and emby importing bulk would be worse than
the private cross-module import it replaces.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 6: `EmbySession` — durable-client auth, single-flight re-auth, and error translation

The heart of the milestone. PRD 03: `DeviceId` is generated once and persisted, and **any 401 triggers silent re-authentication with the stored credentials and the same `DeviceId`**. A dead token in a Home Assistant dashboard is the failure that motivated this project.

**Where the re-auth loop lives, and why.** An explicit `request()` helper on this class, not an `httpx.Auth` hook and not a decorator. `httpx.Auth`'s `async_auth_flow` can express a 401 retry, but the two things that make this correct rather than merely present — coalescing concurrent 401s into a single `AuthenticateByName`, and remembering a rejected credential — are state shared across requests, which is awkward to hold in an auth-flow generator and awkward to assert on. Two mechanisms, both directly tested:

1. **Single flight.** An `asyncio.Lock` plus a generation counter. A request that gets a 401 asks for a refresh *quoting the generation it used*; if the generation has already moved, someone else re-authenticated and it reuses that session. Four concurrent 401s produce one authentication.
2. **Negative caching.** When `AuthenticateByName` itself returns 401, a monotonic deadline is set and every subsequent call raises `PortAuthFailed` without touching the network until it expires. Without this, a wrong password turns every call into two requests forever, against an upstream measured at 1–5 s per request. The clock is injected so the cooldown's *expiry* is testable without sleeping.

And exactly **one** retry per call, never a loop: a loop is how a genuinely wrong password becomes an infinite storm.

**Files:**
- Create: `src/usher/adapters/emby/session.py`
- Create: `tests/fakes/emby_server.py`
- Test: `tests/unit/test_adapters_emby_session.py`

- [ ] **Step 1: Write the in-memory Emby**

```python
# tests/fakes/emby_server.py
"""An in-memory Emby, served through `httpx.MockTransport`.

Every response body is rendered from a committed fixture template
(tests/fixtures/emby/) with the seeded `SourceItem`'s values substituted
in, so the *shape* comes from a recording and the *values* come from the
test. That split is what stops this file from being a restatement of the
adapter's own assumptions: `tests/unit/test_adapters_emby_mapping.py`
parses those same fixtures with no server involved, so a wrong field name
fails there even if this file and the mapper agreed on it.

The residual gap this cannot close is a wrong-but-self-consistent
*endpoint path*: nothing here knows what the real Emby routes are. That is
why M3's definition of done requires a live run. Every path below is
written out independently of the adapter's own constants, deliberately, so
a typo on one side fails rather than cancelling out.

`_TICKS_PER_SECOND` is defined here rather than imported from
`usher.adapters.emby.mapping` for the same reason: the fake encodes Emby's
protocol, and importing the adapter's constant would make a wrong constant
invisible.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import AwareDatetime

from tests.fakes.emby_fixtures import load_emby_fixture
from usher.domain.enums import HdrFormat
from usher.ports.source import SourceItem, SourceItemKind, SourceWatchState

_TICKS_PER_SECOND = 10_000_000
SERVER_ID = "0000000000000000000000000000feed"
USER_ID = "0000000000000000000000000000c0de"
SERVER_VERSION = "4.9.5.0"

_TEMPLATES = {
    SourceItemKind.MOVIE: "movie_item",
    SourceItemKind.SERIES: "series_item",
    SourceItemKind.EPISODE: "episode_item",
}
# Emby's own capitalisation. Rendered back out on purpose: the contract's
# `test_provider_ids_use_canonical_lowercase_keys` only means something if
# the server actually speaks the casing the adapter has to normalise away.
_EMBY_PROVIDER_KEYS = {"tmdb": "Tmdb", "imdb": "Imdb", "tvdb": "Tvdb"}
_HDR_WIRE: dict[HdrFormat | None, tuple[str, str | None]] = {
    None: ("SDR", None),
    HdrFormat.HDR10: ("HDR", "HDR10"),
    HdrFormat.HLG: ("HDR", "HLG"),
    HdrFormat.DOLBY_VISION: ("HDR", "DOVI"),
}

_DEVICE_ID = re.compile(r'DeviceId="([^"]*)"')
_DEVICE = re.compile(r'Device="([^"]*)"')
_ITEMS = re.compile(r"^/Users/(?P<user>[^/]+)/Items$")
_ITEM = re.compile(r"^/Users/(?P<user>[^/]+)/Items/(?P<item>[^/]+)$")
_PROGRESS = re.compile(r"^/Users/(?P<user>[^/]+)/PlayingItems/(?P<item>[^/]+)/Progress$")
_PLAYED = re.compile(r"^/Users/(?P<user>[^/]+)/PlayedItems/(?P<item>[^/]+)$")


def _stamp(value: datetime) -> str:
    """The coarse form used for `MinDateLastSaved` comparisons, matching
    what `usher.adapters.emby.mapping.emby_datetime` produces. Compared as
    strings, which is chronological for same-format UTC ISO stamps."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emby_stamp(value: datetime) -> str:
    """The seven-digit-fraction form Emby actually emits in payloads."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


class FakeEmbyServer:
    def __init__(
        self,
        *,
        page_size: int = 2,
        username: str = "usher",
        password: str = "correct-horse-battery",
    ) -> None:
        self.page_size = page_size
        self.username = username
        self.password = password
        self.credentials_valid = True
        self.offline = False
        self.fail_after: int | None = None
        self.authentications = 0
        self.device_ids: list[str] = []
        self.devices: list[str] = []
        self.requests: list[str] = []
        self._items: dict[str, tuple[SourceItem, AwareDatetime]] = {}
        self._states: dict[str, SourceWatchState] = {}
        self._sessions = 0
        self._session_token: str | None = None

    # -- controls ------------------------------------------------------

    def add_item(self, item: SourceItem, changed_at: AwareDatetime) -> None:
        self._items[item.external_id] = (item, changed_at)

    def remove_item(self, external_id: str) -> None:
        self._items.pop(external_id, None)
        self._states.pop(external_id, None)

    def set_watch_state(self, state: SourceWatchState) -> None:
        self._states[state.external_id] = state

    def recorded_watch_state(self, external_id: str) -> tuple[int, bool] | None:
        state = self._states.get(external_id)
        return None if state is None else (state.position_seconds, state.played)

    def expire_session(self) -> None:
        """The exact Emby failure: the credentials are still right, the
        session token simply stopped working."""
        self._session_token = None

    def reject_credentials(self) -> None:
        self.credentials_valid = False
        self._session_token = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- routing -------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("connection refused")
        self.requests.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path == "/Users/AuthenticateByName":
            return self._authenticate(request)
        if path == "/System/Info/Public":
            return httpx.Response(
                200,
                json={"ServerName": "Fake Emby", "Version": SERVER_VERSION, "Id": SERVER_ID},
            )
        # Ordering matters: `self._session_token is None` is checked first,
        # because `request.headers.get(...)` is also None when the header is
        # absent and `None != None` is False -- so the obvious single
        # comparison would authorise an unauthenticated request against an
        # expired session.
        if self._session_token is None or request.headers.get("X-Emby-Token") != (
            self._session_token
        ):
            return httpx.Response(401, json={"Error": "Access token is invalid or expired."})
        if path == "/System/Info":
            return httpx.Response(
                200,
                json={
                    "ServerName": "Fake Emby",
                    "Version": SERVER_VERSION,
                    "Id": SERVER_ID,
                    "OperatingSystem": "Linux",
                },
            )
        if request.method == "GET" and _ITEMS.match(path):
            return self._list(request)
        item_match = _ITEM.match(path)
        if request.method == "GET" and item_match:
            return self._one(item_match.group("item"))
        progress_match = _PROGRESS.match(path)
        if request.method == "POST" and progress_match:
            return self._progress(request, progress_match.group("item"))
        played_match = _PLAYED.match(path)
        if played_match and request.method in {"POST", "DELETE"}:
            return self._played(played_match.group("item"), request.method == "POST")
        return httpx.Response(404, json={"Error": f"no route for {request.method} {path}"})

    def _authenticate(self, request: httpx.Request) -> httpx.Response:
        self.authentications += 1
        identity = request.headers.get("Authorization", "")
        device_id = _DEVICE_ID.search(identity)
        device = _DEVICE.search(identity)
        if 'Client="Usher"' not in identity or device_id is None or device is None:
            # Emby derives the session's device from this header. Rejecting a
            # request without it is what makes the durable-client header a
            # tested requirement rather than decoration.
            return httpx.Response(400, json={"Error": "missing MediaBrowser authorization"})
        self.device_ids.append(device_id.group(1))
        self.devices.append(device.group(1))
        body = json.loads(request.content or b"{}")
        if (
            not self.credentials_valid
            or body.get("Username") != self.username
            or body.get("Pw") != self.password
        ):
            return httpx.Response(401, json={"Error": "Invalid username or password"})
        self._sessions += 1
        self._session_token = f"session-token-{self._sessions}"
        return httpx.Response(
            200,
            json={
                "AccessToken": self._session_token,
                "ServerId": SERVER_ID,
                "User": {"Id": USER_ID, "Name": self.username},
            },
        )

    def _ordered(self, since: str | None) -> list[str]:
        entries = sorted(self._items.items(), key=lambda entry: (entry[1][1], entry[0]))
        return [
            external_id
            for external_id, (_, changed_at) in entries
            if since is None or _stamp(changed_at) >= since
        ]

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start = int(params.get("StartIndex", "0"))
        limit = int(params.get("Limit", str(self.page_size)))
        since = params.get("MinDateLastSaved") or params.get("MinDateLastSavedForUser")
        ordered = self._ordered(since)
        if self.fail_after is not None and start >= self.fail_after:
            raise httpx.ReadTimeout("upstream stopped responding")
        page = ordered[start : start + limit]
        return httpx.Response(
            200,
            json={
                "Items": [self._payload(external_id) for external_id in page],
                "TotalRecordCount": len(ordered),
            },
        )

    def _one(self, external_id: str) -> httpx.Response:
        if external_id not in self._items:
            return httpx.Response(404, json={"Error": "Not Found"})
        return httpx.Response(200, json=self._payload(external_id))

    def _progress(self, request: httpx.Request, external_id: str) -> httpx.Response:
        if external_id not in self._items:
            return httpx.Response(404, json={"Error": "Not Found"})
        ticks = int(request.url.params.get("PositionTicks", "0"))
        previous = self._states.get(external_id)
        self._states[external_id] = SourceWatchState(
            external_id=external_id,
            position_seconds=ticks // _TICKS_PER_SECOND,
            played=False if previous is None else previous.played,
        )
        return httpx.Response(204)

    def _played(self, external_id: str, played: bool) -> httpx.Response:
        if external_id not in self._items:
            return httpx.Response(404, json={"Error": "Not Found"})
        previous = self._states.get(external_id)
        # Marking an item played clears its resume position, the way Emby
        # does. The adapter writes position first and the played flag last
        # precisely because of this.
        self._states[external_id] = SourceWatchState(
            external_id=external_id,
            position_seconds=0 if played else (0 if previous is None else previous.position_seconds),
            played=played,
            play_count=(0 if previous is None else previous.play_count) + (1 if played else 0),
        )
        return httpx.Response(200, json={"Played": played, "PlaybackPositionTicks": 0})

    # -- rendering -----------------------------------------------------

    def _payload(self, external_id: str) -> dict[str, Any]:
        item, _ = self._items[external_id]
        payload = load_emby_fixture(_TEMPLATES[item.kind])
        payload["Id"] = item.external_id
        payload["Name"] = item.name
        payload["OriginalTitle"] = item.name
        payload["ProductionYear"] = item.year
        payload["ProviderIds"] = {
            _EMBY_PROVIDER_KEYS.get(key, key.title()): value
            for key, value in item.provider_ids.items()
        }
        payload["RunTimeTicks"] = (
            None if item.runtime_seconds is None else item.runtime_seconds * _TICKS_PER_SECOND
        )
        if item.added_at is not None:
            payload["DateCreated"] = _emby_stamp(item.added_at)
        else:
            payload.pop("DateCreated", None)
        payload["UserData"] = self._user_data(external_id)
        if item.series_external_id is not None:
            payload["SeriesId"] = item.series_external_id
        if item.season_number is not None:
            payload["ParentIndexNumber"] = item.season_number
        if item.episode_number is not None:
            payload["IndexNumber"] = item.episode_number
        self._render_media(payload, item)
        return payload

    def _render_media(self, payload: dict[str, Any], item: SourceItem) -> None:
        if item.container is None:
            payload.pop("MediaSources", None)
            return
        media = payload.setdefault("MediaSources", load_emby_fixture("movie_item")["MediaSources"])[
            0
        ]
        media["Container"] = item.container
        media["Size"] = item.file_size_bytes
        media["RunTimeTicks"] = payload["RunTimeTicks"]
        for stream in media["MediaStreams"]:
            if stream["Type"] == "Video":
                stream["Codec"] = item.video_codec
                stream["Width"] = item.width
                stream["Height"] = item.height
                # Rendered purely from VideoRange/VideoRangeType, with the
                # DV-specific keys dropped: the DvProfile path is covered
                # directly against the raw fixture in the mapping tests, and
                # exercising the token path here keeps the two independent.
                stream.pop("DvProfile", None)
                stream.pop("DvLevel", None)
                video_range, range_type = _HDR_WIRE[item.hdr_format]
                stream["VideoRange"] = video_range
                if range_type is None:
                    stream.pop("VideoRangeType", None)
                else:
                    stream["VideoRangeType"] = range_type
            elif stream["Type"] == "Audio" and stream.get("IsDefault"):
                stream["Codec"] = item.audio_codec
                stream["Channels"] = item.audio_channels
                # Cleared so the rendered audio token is a deterministic
                # function of codec and channel count. The Atmos/DTS-HD
                # vocabulary is covered against the raw fixtures instead.
                stream["Profile"] = ""

    def _user_data(self, external_id: str) -> dict[str, Any]:
        state = self._states.get(external_id)
        if state is None:
            return {
                "PlaybackPositionTicks": 0,
                "PlayCount": 0,
                "IsFavorite": False,
                "Played": False,
            }
        return {
            "PlaybackPositionTicks": state.position_seconds * _TICKS_PER_SECOND,
            "PlayCount": state.play_count,
            "IsFavorite": False,
            "Played": state.played,
        }
```

> **Transcription note.** `_render_media`'s `payload.setdefault(...)[0]` handles the episode template (which has its own `MediaSources`) and any template that lacks one. Both committed templates that carry a container already have a `MediaSources[0]`, so the `setdefault` default is a safety net rather than a live path — leave it, because an implementer adding a fourth template shape should not have to rediscover this.

- [ ] **Step 2: Write the failing session test**

```python
# tests/unit/test_adapters_emby_session.py
"""EmbySession: the durable-client header, silent re-authentication, and
error translation. Driven entirely by httpx.MockTransport -- no network.
"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from tests.fakes.emby_server import FakeEmbyServer
from usher.adapters.emby.session import SYSTEM_INFO_PATH, EmbySession
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)
from usher.ports.source import SourceItem, SourceItemKind

DEVICE_ID = "9d1f0b6c-0000-7000-8000-000000000001"
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
ITEM = SourceItem(
    external_id="movie-1", name="Example Movie", kind=SourceItemKind.MOVIE, container="mkv"
)


class _Clock:
    """An injected monotonic clock, so the re-auth cooldown's *expiry* is
    testable without a real sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _session(
    server: FakeEmbyServer,
    *,
    source_name: str = "Living Room Emby",
    credentials: SourceCredentials = CREDENTIALS,
    clock: _Clock | None = None,
) -> tuple[EmbySession, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=server.transport(), base_url="https://emby.invalid")
    session = EmbySession(
        client,
        credentials,
        source_name=source_name,
        device_id=DEVICE_ID,
        app_version="0.1.0",
        reauth_cooldown_seconds=60.0,
        clock=clock or _Clock(),
    )
    return session, client


async def test_the_durable_client_header_names_usher_and_the_device() -> None:
    """PRD 03's `Authorization: MediaBrowser Client="Usher", Device=…,
    DeviceId=…, Version=…`. The fake rejects an authentication without it,
    so this fails loudly rather than subtly if the header is dropped."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.device_ids == [DEVICE_ID]
    assert server.devices == ["Living Room Emby"]


async def test_the_same_device_id_is_reused_across_reauthentication() -> None:
    """The durable-client invariant, and the whole reason `device_id` is
    persisted on the `Source` row: a new id per authentication makes Usher
    an accumulating pile of sessions in Emby's dashboard, which is exactly
    what PRD 03 designed it not to be."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        server.expire_session()
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.authentications == 2
    assert server.device_ids == [DEVICE_ID, DEVICE_ID]


async def test_a_source_name_with_quotes_cannot_break_the_header() -> None:
    """`My "Home" Emby` is a name an operator can type straight into
    `POST /admin/sources`. Interpolated raw it closes the quoted field
    early and Emby parses the header as something else entirely."""
    server = FakeEmbyServer()
    session, client = _session(server, source_name='My "Home" Emby')
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.devices == ["My _Home_ Emby"]


async def test_an_expired_session_is_silently_re_minted() -> None:
    """The failure that motivated this project: a token that silently
    started returning 401 with no way to renew it. No human pastes
    anything here."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        server.expire_session()
        body = await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert body["Id"]
    assert server.authentications == 2


async def test_concurrent_401s_produce_one_authentication() -> None:
    """Single flight. Eight in-flight requests all hitting an expired
    session must not mint eight sessions -- the pile-of-sessions failure
    again, arrived at from the other direction."""
    server = FakeEmbyServer()
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        server.expire_session()
        await asyncio.gather(
            *(session.json_body("GET", SYSTEM_INFO_PATH, op="info") for _ in range(8))
        )
    finally:
        await client.aclose()
    assert server.authentications == 2


async def test_wrong_credentials_raise_and_are_remembered() -> None:
    """Negative caching. Without it, five calls against a wrong password
    are five authentications, against an upstream measured at 1-5 s per
    request."""
    server = FakeEmbyServer()
    session, client = _session(server, credentials=SourceCredentials(
        username="usher", password=SecretStr("wrong")
    ))
    try:
        for _ in range(5):
            with pytest.raises(PortAuthFailed):
                await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.authentications == 1


async def test_the_cooldown_expires_and_authentication_is_retried() -> None:
    """The other half of negative caching: a corrected password must not
    require a restart. Advances the injected clock rather than sleeping."""
    server = FakeEmbyServer()
    clock = _Clock()
    session, client = _session(server, credentials=SourceCredentials(
        username="usher", password=SecretStr("wrong")
    ), clock=clock)
    try:
        with pytest.raises(PortAuthFailed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
        assert server.authentications == 1
        clock.now += 61.0
        with pytest.raises(PortAuthFailed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert server.authentications == 2


async def test_a_transport_error_becomes_port_unavailable() -> None:
    server = FakeEmbyServer()
    server.offline = True
    session, client = _session(server)
    try:
        with pytest.raises(PortUnavailable):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_a_429_becomes_port_rate_limited_with_its_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(
                200,
                json={"AccessToken": "t", "User": {"Id": "u"}},
            )
        return httpx.Response(429, headers={"retry-after": "12"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortRateLimited) as exc_info:
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert exc_info.value.retry_after == 12.0


async def test_a_5xx_becomes_port_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": "u"}})
        return httpx.Response(502, text="bad gateway")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortUnavailable):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_a_non_json_body_becomes_port_data_malformed() -> None:
    """A reverse proxy serving an HTML error page with status 200 is the
    realistic case. A raw `json.JSONDecodeError` escaping here is not
    something any caller written against `usher.ports.errors` can catch."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": "u"}})
        return httpx.Response(200, text="<html>maintenance</html>")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortDataMalformed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_an_authentication_response_without_a_token_is_malformed() -> None:
    """Distinguished from a 401 on purpose: a 200 with no AccessToken means
    something answered that is not Emby -- a captive portal, a proxy's
    landing page -- and retrying with the same credentials will not help."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Welcome": "to the hotel wifi"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://emby.invalid"
    )
    session = EmbySession(
        client, CREDENTIALS, source_name="E", device_id=DEVICE_ID, app_version="0.1.0"
    )
    try:
        with pytest.raises(PortDataMalformed):
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()


async def test_no_error_message_ever_contains_the_password() -> None:
    """PRD 08: credentials are never logged, "including in error paths".
    Every message this class builds is interpolated from a method, a path,
    and a transport error -- none of which can carry the secret -- and the
    request body that does carry it is never formatted into one."""
    server = FakeEmbyServer()
    server.offline = True
    session, client = _session(server)
    try:
        with pytest.raises(PortUnavailable) as exc_info:
            await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()
    assert "correct-horse-battery" not in str(exc_info.value)
    assert "correct-horse-battery" not in repr(exc_info.value)


async def test_requests_after_aclose_raise_port_unavailable() -> None:
    """Verified while planning: a closed `httpx.AsyncClient` raises a bare
    `RuntimeError`, which is not an `httpx.HTTPError` -- so translation
    alone does not cover this and an explicit closed-flag does."""
    server = FakeEmbyServer()
    session, client = _session(server)
    await session.aclose()
    await client.aclose()
    with pytest.raises(PortUnavailable):
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")


async def test_every_upstream_request_produces_a_span() -> None:
    """Instrumentation is cross-cutting: "every subsequent milestone
    instruments its own work as it is built". PRD 10's span tree gets
    `source.request`, carrying the source and the operation so "why was
    this reconcile slow" is one query.

    Installs the in-memory exporter before the call, the same way
    tests/unit/test_telemetry.py does -- the module-level tracer is a
    ProxyTracer and resolves the global provider per call, so this works
    despite the module having been imported first.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    server = FakeEmbyServer()
    server.add_item(ITEM, T0)
    session, client = _session(server)
    try:
        await session.json_body("GET", SYSTEM_INFO_PATH, op="info")
    finally:
        await client.aclose()

    spans = [span for span in exporter.get_finished_spans() if span.name == "source.request"]
    assert spans
    assert spans[0].attributes is not None
    assert spans[0].attributes["usher.op"] == "info"
    assert spans[0].attributes["usher.source"] == "Living Room Emby"
```

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/unit/test_adapters_emby_session.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.emby.session'`

- [ ] **Step 4: Write `EmbySession`**

```python
# src/usher/adapters/emby/session.py
"""One authenticated HTTP session against one Emby server.

PRD 03's durable-client authentication, in full:

    Authorization: MediaBrowser Client="Usher", Device="<source name>",
                   DeviceId="<persisted UUID>", Version="<app version>"
    POST /Users/AuthenticateByName  {"Username": ..., "Pw": ...}
    -> AccessToken, User.Id

The identity header goes on **every** request, not just the authentication
one: that is what makes Emby attribute all of Usher's traffic to a single
device rather than to an anonymous client per call. The session token rides
alongside in `X-Emby-Token`.

**Emby has no OAuth2**, so there is no refresh-token flow to build against.
The refresh mechanism is this: any 401 re-authenticates silently with the
stored credentials and the *same* `DeviceId`, and no human ever pastes a
token. That is the whole fix for the failure this project exists to
address, where a token stored in a Home Assistant dashboard quietly started
returning 401 on every authenticated endpoint.

Two mechanisms keep that from becoming a request storm, and both are
tested:

1. **Single flight.** One `asyncio.Lock` and a generation counter. A
   request that receives a 401 asks for a refresh *quoting the generation
   whose token it used*; if the generation has already advanced, another
   in-flight request re-authenticated and this one reuses that session.
   Eight concurrent 401s therefore produce one `AuthenticateByName`.
2. **Negative caching.** If `AuthenticateByName` itself is rejected, a
   monotonic deadline is recorded and every call raises `PortAuthFailed`
   without a network request until it passes. Without it a wrong password
   doubles every request forever, against an upstream PRD 01 measures at
   1-5 s per call. The clock is injected so the *expiry* is testable
   without sleeping.

And exactly one retry per call, never a loop. A loop is how a genuinely
wrong password becomes an infinite storm.

**Which paths below are verified.** `POST /Users/AuthenticateByName` is
verified -- it is the call ADR-0004's own end-to-end session used to mint
its token. `/System/Info/Public` and `/System/Info` are the standard Emby
4.9 routes and are not yet verified against the live server; M3's
definition of done requires a live run before the milestone is closed.
`/System/Info/Public` is load-bearing for `verify()`: because it answers
*without* authentication, a failure there is a reachability failure and
nothing else, which is what lets `SourceStatus` separate "unreachable"
from "bad credentials".
"""

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from opentelemetry import metrics, trace

from usher import __version__
from usher.adapters.http import retry_after_seconds
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)

AUTHENTICATE_PATH = "/Users/AuthenticateByName"
PUBLIC_INFO_PATH = "/System/Info/Public"
SYSTEM_INFO_PATH = "/System/Info"

# Named `_EMBY_AUTH_HEADER`, not `_TOKEN_HEADER`: ruff's S105 flags any
# module constant whose *name* contains "token" and whose value is a string
# literal, and a `# noqa` on a header name is worse than a clear name.
_EMBY_AUTH_HEADER = "X-Emby-Token"

_UNSAFE_HEADER_CHARS = re.compile(r"[^A-Za-z0-9 ._+-]")

_tracer = trace.get_tracer("usher.source.emby")
_meter = metrics.get_meter("usher.source.emby")
# PRD 10's catalogue, M3's one metric. Labels `source` and `op`, exactly as
# that table specifies. Created at import time against whatever
# MeterProvider `configure_metrics` installed -- always a real SDK provider,
# exported only when an OTLP endpoint is configured.
_request_duration = _meter.create_histogram(
    "usher.source.request.duration",
    unit="s",
    description="Wall time per request to a media source",
)


def _header_safe(value: str) -> str:
    """Make a value safe to interpolate into the quoted MediaBrowser header.

    Its fields are quoted strings, so a source named `My "Home" Emby` -- a
    name an operator can type straight into `POST /admin/sources` -- would
    close the quote early and leave Emby parsing something else entirely.
    Substitution rather than percent-encoding, because whether Emby decodes
    these fields is not a thing this adapter should have to be right about;
    a mangled display name in a dashboard is a cosmetic cost, a malformed
    header is a broken source.
    """
    return _UNSAFE_HEADER_CHARS.sub("_", value).strip()[:64] or "Usher"


def decode_json(response: httpx.Response, path: str) -> dict[str, Any]:
    """Parse a JSON object body, or raise `PortDataMalformed`.

    Public because `EmbyAdapter.get_item` needs it: that call must inspect
    a 404 before decoding, so it uses `request()` rather than `json_body()`
    and decodes the success path itself.
    """
    try:
        body = response.json()
    except ValueError as exc:
        # A reverse proxy serving an HTML error page with status 200 is the
        # realistic case, and a raw json.JSONDecodeError escaping the port
        # is not something any caller written against usher.ports.errors
        # can catch.
        raise PortDataMalformed(f"{path} did not return JSON", detail=path) from exc
    if not isinstance(body, dict):
        raise PortDataMalformed(
            f"{path} returned a {type(body).__name__}, not an object", detail=path
        )
    return body


class EmbySession:
    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: SourceCredentials,
        *,
        source_name: str,
        device_id: str,
        app_version: str = __version__,
        reauth_cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._credentials = credentials
        self._source_name = source_name
        self._device_id = device_id
        self._app_version = app_version
        self._reauth_cooldown = reauth_cooldown_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._user_id: str | None = None
        self._generation = 0
        self._blocked_until: float | None = None
        self._closed = False

    # -- identity ------------------------------------------------------

    def _identity_header(self) -> str:
        return (
            f'MediaBrowser Client="Usher", Device="{_header_safe(self._source_name)}", '
            f'DeviceId="{_header_safe(self._device_id)}", '
            f'Version="{_header_safe(self._app_version)}"'
        )

    def _headers(self, token: str) -> dict[str, str]:
        # Both headers, deliberately. `Authorization` carries the durable
        # client identity on every request, which is what makes Emby treat
        # all of Usher's traffic as one device; `X-Emby-Token` carries the
        # session. Neither can carry the password.
        return {"Authorization": self._identity_header(), _EMBY_AUTH_HEADER: token}

    # -- authentication ------------------------------------------------

    def _raise_if_closed(self) -> None:
        """Every public entry point calls this, not just `request`.

        `user_id()` and `access_token()` are entry points too -- `EmbyAdapter
        ._fetch` calls `user_id()` *before* it calls `request()` -- so a
        check only on `request` would let a closed adapter authenticate
        against a live transport and succeed. Verified while planning that
        a closed `httpx.AsyncClient` raises a bare `RuntimeError` rather
        than an `httpx.HTTPError`, so translation alone does not cover this
        even when the client really is closed; and when the client was
        *injected* it is not closed at all, so nothing but this flag stands
        between a closed adapter and a working request.
        """
        if self._closed:
            raise PortUnavailable("this source adapter has been closed")

    def _raise_if_blocked(self) -> None:
        if self._blocked_until is not None and self._clock() < self._blocked_until:
            raise PortAuthFailed(
                "Emby rejected the stored credentials for this source; not retrying yet"
            )

    async def _authenticate_locked(self) -> tuple[str, str]:
        """Mint a session. Caller must hold `self._lock`."""
        response = await self._send(
            "POST",
            AUTHENTICATE_PATH,
            params=None,
            payload={
                "Username": self._credentials.username,
                "Pw": self._credentials.password.get_secret_value(),
            },
            headers={"Authorization": self._identity_header()},
            op="authenticate",
        )
        if response.status_code == 401:
            self._blocked_until = self._clock() + self._reauth_cooldown
            self._token = None
            raise PortAuthFailed("Emby rejected the stored credentials for this source")
        if response.status_code == 429:
            raise PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
        if response.status_code >= 400:
            raise PortUnavailable(
                f"POST {AUTHENTICATE_PATH} returned HTTP {response.status_code}"
            )
        body = decode_json(response, AUTHENTICATE_PATH)
        user = body.get("User")
        token = body.get("AccessToken")
        user_id = user.get("Id") if isinstance(user, Mapping) else None
        if not isinstance(token, str) or not token:
            raise PortDataMalformed(
                "Emby authentication returned no AccessToken", detail=AUTHENTICATE_PATH
            )
        if not isinstance(user_id, str) or not user_id:
            raise PortDataMalformed(
                "Emby authentication returned no User.Id", detail=AUTHENTICATE_PATH
            )
        self._token = token
        self._user_id = user_id
        self._generation += 1
        self._blocked_until = None
        return token, user_id

    async def _session(self) -> tuple[str, int]:
        async with self._lock:
            self._raise_if_blocked()
            if self._token is not None:
                return self._token, self._generation
            token, _ = await self._authenticate_locked()
            return token, self._generation

    async def _refresh(self, seen_generation: int) -> str:
        async with self._lock:
            if self._generation != seen_generation and self._token is not None:
                # Another in-flight request already re-authenticated while
                # this one was waiting for the lock. Reusing its session is
                # what turns N concurrent 401s into one AuthenticateByName.
                return self._token
            self._raise_if_blocked()
            token, _ = await self._authenticate_locked()
            return token

    async def user_id(self) -> str:
        """The authenticated Emby user's id, authenticating if needed.

        Emby's item and user-data routes are all under `/Users/{userId}/`,
        so this is a precondition for almost everything the adapter does --
        which is exactly why it checks `_raise_if_closed` itself.
        """
        self._raise_if_closed()
        async with self._lock:
            self._raise_if_blocked()
            if self._user_id is not None:
                return self._user_id
            _, user_id = await self._authenticate_locked()
            return user_id

    async def access_token(self) -> str:
        """The current session token. Used only to build direct-play URLs
        -- see ADR-0012 for why a playback URL carries one at all."""
        self._raise_if_closed()
        token, _ = await self._session()
        return token

    # -- requests ------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None,
        payload: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        op: str,
    ) -> httpx.Response:
        started = self._clock()
        try:
            return await self._client.request(
                method, path, params=params, json=payload, headers=dict(headers)
            )
        except httpx.HTTPError as exc:
            # `exc` carries a method and a URL, never a header or a body,
            # so this message cannot leak the credential -- and the one
            # request that does carry it is never formatted into a message.
            raise PortUnavailable(f"{method} {path} failed: {exc}") from exc
        finally:
            _request_duration.record(
                self._clock() - started, {"source": self._source_name, "op": op}
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        op: str,
    ) -> httpx.Response:
        """Send an authenticated request, re-authenticating once on a 401.

        Returns 4xx responses other than 401 to the caller rather than
        raising, so `get_item` can tell a 404 ("gone") from a transport
        failure ("unreachable") -- the distinction the port's own docstring
        calls out as the one that must not be conflated. Use `ok()` or
        `json_body()` when any 4xx is a failure.
        """
        self._raise_if_closed()
        token, generation = await self._session()
        with _tracer.start_as_current_span("source.request") as span:
            span.set_attribute("usher.source", self._source_name)
            span.set_attribute("usher.op", op)
            response = await self._send(
                method, path, params=params, payload=payload, headers=self._headers(token), op=op
            )
            if response.status_code == 401:
                span.set_attribute("usher.reauthenticated", True)
                token = await self._refresh(generation)
                response = await self._send(
                    method,
                    path,
                    params=params,
                    payload=payload,
                    headers=self._headers(token),
                    op=op,
                )
                if response.status_code == 401:
                    raise PortAuthFailed(
                        f"{method} {path} still returned 401 after re-authenticating"
                    )
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code == 429:
                raise PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
            return response

    async def ok(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        op: str,
    ) -> httpx.Response:
        response = await self.request(method, path, params=params, payload=payload, op=op)
        if response.status_code >= 400:
            raise PortUnavailable(f"{method} {path} returned HTTP {response.status_code}")
        return response

    async def json_body(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        op: str,
    ) -> dict[str, Any]:
        response = await self.ok(method, path, params=params, payload=payload, op=op)
        return decode_json(response, path)

    async def anonymous_json(self, path: str, *, op: str) -> dict[str, Any]:
        """A request carrying the client identity but no session token.

        The whole reason `verify()` can separate "unreachable" from "bad
        credentials": `/System/Info/Public` answers without authentication,
        so a failure here is a reachability failure and cannot be anything
        else.
        """
        self._raise_if_closed()
        response = await self._send(
            "GET",
            path,
            params=None,
            payload=None,
            headers={"Authorization": self._identity_header()},
            op=op,
        )
        if response.status_code == 429:
            raise PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
        if response.status_code >= 400:
            raise PortUnavailable(f"GET {path} returned HTTP {response.status_code}")
        return decode_json(response, path)

    async def aclose(self) -> None:
        """Mark the session closed. The `httpx.AsyncClient` belongs to
        whoever constructed it -- `EmbyAdapter` closes the one it created
        and leaves an injected one alone."""
        self._closed = True
```

- [ ] **Step 5: Run and watch it pass**

Run: `uv run pytest tests/unit/test_adapters_emby_session.py -q`
Expected: PASS — 15 tests.

- [ ] **Step 6: Check and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest tests/unit -q
```

```bash
git add -A && git commit -F - <<'EOF'
feat: EmbySession -- durable-client auth and silent re-authentication

PRD 03's whole auth story. The MediaBrowser identity header goes on every
request, not just the authentication one, so Emby attributes all of
Usher's traffic to one device rather than an anonymous client per call;
any 401 re-authenticates with the stored credentials and the *same*
DeviceId. Emby has no OAuth2 -- this is the refresh mechanism, and no
human ever pastes a token.

Two mechanisms stop that becoming a request storm, both tested: an
asyncio.Lock plus a generation counter, so eight concurrent 401s produce
one AuthenticateByName; and a monotonic cooldown on a rejected credential,
so a wrong password does not double every request forever. Exactly one
retry per call, never a loop.

Also: a source name containing a quote character cannot break the quoted
header; every error message is built from a method, a path and a transport
error, so none can carry the password; and a request after aclose() raises
PortUnavailable rather than httpx's bare RuntimeError, which is not an
httpx.HTTPError and would otherwise escape the port.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 7: `StreamTarget` construction, and the token that rides in the URL

PRD 07: "the deep-link construction currently done by hand in the Home Assistant card moves here, where it is testable." This task is that move, and it is the one place M3 knowingly bends a PRD 08 rule — so it also writes the ADR.

**The tension, stated plainly.** A direct-play URL for Emby needs an `api_key` in the query string, or the client cannot fetch the bytes. PRD 08 says "No credential ever reaches a client. This is the failure of the setup Usher replaces, where a raw Emby token lived in browser-delivered dashboard config." PRD 07 says Usher "never proxies bytes". Both cannot be fully honoured in v1, and pretending otherwise by omitting the token would ship an unplayable URL. ADR-0012 records the decision, the bounded difference from the failure being replaced, and the two options that remove the tension in M9.

**Files:**
- Create: `src/usher/adapters/emby/playback.py`
- Create: `docs/prd/decisions/0012-playback-urls-carry-a-source-token.md`
- Test: `tests/unit/test_adapters_emby_playback.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_adapters_emby_playback.py
"""StreamTarget construction, against the committed fixtures.

No HTTP: `build_stream_targets` is a pure function of one item payload, and
keeping it that way is what makes "the deep-link construction moves here,
where it is testable" (PRD 07) actually true.
"""

from urllib.parse import parse_qs, unquote, urlparse

from tests.fakes.emby_fixtures import load_emby_fixture
from usher.adapters.emby.playback import build_stream_targets
from usher.domain.enums import HdrFormat
from usher.ports.source import StreamTarget, StreamTargetKind

BASE = "https://emby.invalid"
TOKEN = "session-token-1"
DEVICE = "9d1f0b6c-0000-7000-8000-000000000001"


def _targets(fixture: str, base_url: str = BASE) -> list[StreamTarget]:
    return build_stream_targets(
        load_emby_fixture(fixture), base_url=base_url, access_token=TOKEN, device_id=DEVICE
    )


def test_the_direct_target_is_ranked_first() -> None:
    """A client that can play the container should. The deep link
    surrenders playback to another application, which is a fallback, not a
    preference."""
    targets = _targets("movie_item")
    assert [target.kind for target in targets] == [
        StreamTargetKind.DIRECT,
        StreamTargetKind.DEEP_LINK,
    ]


def test_the_direct_url_is_a_static_stream_with_everything_emby_needs() -> None:
    direct = _targets("movie_item")[0]
    parsed = urlparse(direct.url)
    assert parsed.path == "/Videos/0000000000000000000000000000a001/stream.mkv"
    query = parse_qs(parsed.query)
    assert query["static"] == ["true"]
    assert query["MediaSourceId"] == ["0000000000000000000000000000b001"]
    assert query["DeviceId"] == [DEVICE]
    assert query["api_key"] == [TOKEN]


def test_the_direct_target_carries_the_quality_facts() -> None:
    """PRD 07's `/play` response shape, field for field."""
    direct = _targets("movie_item")[0]
    assert direct.container == "mkv"
    assert direct.video_codec == "hevc"
    assert direct.audio == "truehd_atmos_7_1"
    assert direct.hdr_format is HdrFormat.DOLBY_VISION
    assert direct.resolution == "3840x2160"
    assert direct.runtime_seconds == 9360
    assert direct.resume_position_seconds == 1840
    assert direct.scheme is None


def test_the_deep_link_wraps_the_direct_url_intact() -> None:
    """Percent-encoded, and reversible: a deep link that lost the query
    string would hand Infuse a URL Emby answers 401 to."""
    direct, deep = _targets("movie_item")
    assert deep.scheme == "infuse"
    assert deep.url.startswith("infuse://x-callback-url/play?url=")
    wrapped = unquote(deep.url.split("url=", 1)[1])
    assert wrapped == direct.url


def test_the_deep_link_carries_no_quality_facts() -> None:
    """Deliberate: the client is not choosing a stream, it is handing the
    URL to another application, and duplicating the facts would invite a
    client to render them twice."""
    _, deep = _targets("movie_item")
    assert deep.container is None
    assert deep.video_codec is None
    assert deep.hdr_format is None


def test_an_episode_gets_targets_too() -> None:
    """TV is in scope throughout (PRD 09), and Emby addresses episodes
    directly."""
    targets = _targets("episode_item")
    assert targets[0].kind is StreamTargetKind.DIRECT
    assert targets[0].container == "mkv"
    assert targets[0].audio == "eac3_5_1"
    assert targets[0].resume_position_seconds == 0


def test_a_series_has_no_targets() -> None:
    """A series is a folder with no `MediaSources`. Fabricating a URL for
    it would hand a client a link that fails at play time."""
    assert _targets("series_item") == []


def test_a_media_source_with_no_container_has_no_targets() -> None:
    """The URL's file extension *is* the container. With none there is no
    direct-play URL to build, and guessing one is worse than reporting
    nothing."""
    payload = load_emby_fixture("movie_item")
    del payload["MediaSources"][0]["Container"]
    assert (
        build_stream_targets(payload, base_url=BASE, access_token=TOKEN, device_id=DEVICE) == []
    )


def test_a_base_url_with_a_trailing_slash_does_not_double_it() -> None:
    """`POST /admin/sources` takes whatever an operator pastes, and pasting
    a URL with a trailing slash is the norm, not the exception."""
    direct = _targets("movie_item", base_url="https://emby.invalid/")[0]
    assert "//Videos" not in direct.url
    assert direct.url.startswith("https://emby.invalid/Videos/")
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/unit/test_adapters_emby_playback.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.emby.playback'`

- [ ] **Step 3: Write `playback.py`**

```python
# src/usher/adapters/emby/playback.py
"""`StreamTarget`s for one Emby item.

PRD 07: Usher "supplies complete information and never proxies bytes", and
"the deep-link construction currently done by hand in the Home Assistant
card moves here, where it is testable". This module is that move, and it is
a pure function of one item payload so that it stays testable.

Two targets per playable item, ranked:

1. **direct** -- `/Videos/{id}/stream.{container}?static=true`, the
   byte-for-byte file, carrying every fact a client needs to decide whether
   it can play it.
2. **deep_link** -- `infuse://x-callback-url/play?url=<the direct URL,
   percent-encoded>`.

Direct first, because a client that *can* play the container should: a deep
link hands playback to another application, which is a fallback rather than
a preference. The deep link deliberately carries no quality facts -- the
client is not choosing a stream there, it is delegating, and duplicating
the facts would invite a UI to render them twice.

`/Items/{id}/PlaybackInfo` is deliberately not called. That endpoint exists
for transcode negotiation, which Usher explicitly does not do, and
everything the direct URL needs -- container, `MediaSourceId`, resume
position -- is already on the item. One fewer endpoint to have guessed
wrong, and one fewer round trip against an upstream PRD 01 measures at
1-5 s per request.

**The direct URL carries the source's access token**, because without it
the bytes are not fetchable and Usher does not proxy them. That is
knowingly in tension with PRD 08's "no credential ever reaches a client";
ADR-0012 records the decision, how it differs from the failure Usher
replaces, and what removes it in M9.
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

from usher.adapters.emby.mapping import (
    TICKS_PER_SECOND,
    audio_token,
    hdr_format,
    primary_media_source,
    stream_of,
)
from usher.ports.source import StreamTarget, StreamTargetKind

INFUSE_SCHEME = "infuse"


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def build_stream_targets(
    payload: Mapping[str, Any],
    *,
    base_url: str,
    access_token: str,
    device_id: str,
) -> list[StreamTarget]:
    """Ranked ways to play one Emby item, or `[]` if there are none.

    Empty for a folder item (a series or season, which has no
    `MediaSources`) and for a media source with no container -- the
    container *is* the URL's file extension, and guessing one would hand a
    client a link that fails at play time. The port documents `[]` as the
    answer for "no way to play this", so neither case is an error.
    """
    external_id = payload.get("Id")
    media_source = primary_media_source(payload)
    if media_source is None or not isinstance(external_id, str) or not external_id:
        return []
    container = media_source.get("Container")
    if not isinstance(container, str) or not container:
        return []
    container = container.lower()

    video = stream_of(media_source, "Video") or {}
    audio = stream_of(media_source, "Audio") or {}
    width = _int(video.get("Width")) or _int(payload.get("Width"))
    height = _int(video.get("Height")) or _int(payload.get("Height"))
    runtime_ticks = _int(payload.get("RunTimeTicks")) or _int(media_source.get("RunTimeTicks"))
    user_data = payload.get("UserData")
    position_ticks = (
        _int(user_data.get("PlaybackPositionTicks")) if isinstance(user_data, Mapping) else None
    )

    query = urlencode(
        {
            "static": "true",
            "MediaSourceId": str(media_source.get("Id") or external_id),
            "DeviceId": device_id,
            "api_key": access_token,
        }
    )
    url = (
        f"{base_url.rstrip('/')}/Videos/{quote(external_id, safe='')}"
        f"/stream.{container}?{query}"
    )
    return [
        StreamTarget(
            kind=StreamTargetKind.DIRECT,
            url=url,
            container=container,
            video_codec=(
                video["Codec"].lower()
                if isinstance(video.get("Codec"), str) and video["Codec"]
                else None
            ),
            audio=audio_token(audio),
            hdr_format=hdr_format(video),
            resolution=(
                f"{width}x{height}" if width is not None and height is not None else None
            ),
            runtime_seconds=(
                None if runtime_ticks is None else runtime_ticks // TICKS_PER_SECOND
            ),
            resume_position_seconds=(
                None if position_ticks is None else max(position_ticks, 0) // TICKS_PER_SECOND
            ),
        ),
        StreamTarget(
            kind=StreamTargetKind.DEEP_LINK,
            url=f"{INFUSE_SCHEME}://x-callback-url/play?url={quote(url, safe='')}",
            scheme=INFUSE_SCHEME,
        ),
    ]
```

- [ ] **Step 4: Run and watch it pass**

Run: `uv run pytest tests/unit/test_adapters_emby_playback.py -q`
Expected: PASS — 9 tests.

- [ ] **Step 5: Write ADR-0012**

```markdown
# ADR-0012 — A playback URL carries a source token, in v1

**Status:** Accepted for v1, with a named successor in M9

## Context

PRD 07: `POST /titles/{id}/play` returns ranked `StreamTarget`s, and
"Usher supplies complete information and never proxies bytes."

PRD 08: "No credential ever reaches a client. This is the failure of the
setup Usher replaces, where a raw Emby token lived in browser-delivered
dashboard config."

Both cannot hold at once for a direct-play target. Emby authenticates the
`/Videos/{id}/stream.{container}` route; without an `api_key` in the query
string (or the equivalent header, which a `<video>` element and a deep link
cannot set) the client gets a 401. Omitting the token would ship a URL that
looks complete and does not play — worse than either honest option.

## Decision

`StreamTarget.url` carries the source's current session token, for v1.

## Consequences

**What a client can do with it.** Everything Usher's Emby user can: read
the library, read and write that user's watch state, stream anything. It is
a real capability grant, not an opaque ticket, and this ADR does not
pretend otherwise.

**How it differs from the failure being replaced — and where it does not.**
The Home Assistant failure had two halves: a token in browser-delivered
dashboard config, *and* no way to renew it when it died. M3 fixes the
second half completely — the token is minted on demand from encrypted
credentials, is never stored anywhere a client can read at rest, and is
silently re-minted on any 401. The first half is genuinely still present:
a client that receives a play response holds a working token until Emby
prunes the session. The improvement is real and partial; calling it solved
would be wrong.

**Blast radius is bounded** by the same thing that makes the fix possible:
the token belongs to *one* durable device registered as Usher, so
revocation is one action in Emby's dashboard, and re-authentication after
revocation is automatic.

**Handling rules that follow, and are enforced in code:**

- Never a span attribute, never a log field, never an exception message.
  `EmbySession` builds every error string from a method, a path, and a
  transport error; the URL is only ever a return value.
- Never persisted. `StreamTarget`s are built per request from a live
  session token; nothing writes one to a table or a cache.
- `verify()`'s `SourceStatus.detail` is built from translated port errors
  for the same reason.

## The successor, in M9

Two options, either of which removes this entirely:

1. **A playback ticket.** `POST /titles/{id}/play` returns
   `https://usher/stream/{opaque}` and Usher answers it with a `302` to the
   real Emby URL, minting the redirect per request with a short TTL. Usher
   still never proxies bytes — the redirect target is fetched directly by
   the client — so PRD 07's constraint is untouched.
2. **A per-client scoped token**, once the authentication seam in PRD 01 is
   filled and there is a client identity to scope one to.

Option 1 is preferred: it needs no authentication work and is a pure
addition to the API surface M9 is building anyway.

## Why not now

M3 has no HTTP surface for playback — `POST /titles/{id}/play` is M9's, and
the redirect endpoint would have to live beside it. Building the ticket
store in M3 would mean designing a TTL cache for a route that does not
exist, against a client that does not exist. PRD 07 and PRD 08 are updated
to say what v1 actually does rather than leaving the contradiction
implicit, which is the part that could not wait.
```

- [ ] **Step 6: Check and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest tests/unit -q
```

```bash
git add -A && git commit -F - <<'EOF'
feat: StreamTarget construction, and ADR-0012 on the token in the URL

PRD 07's "the deep-link construction currently done by hand in the Home
Assistant card moves here, where it is testable", as a pure function of
one item payload. Direct target first, Infuse deep link second; the deep
link wraps the direct URL percent-encoded and reversibly.

PlaybackInfo is deliberately not called: it exists for transcode
negotiation, which Usher does not do, and everything the direct URL needs
is already on the item -- one fewer guessed endpoint and one fewer round
trip against an upstream measured at 1-5 s per request.

ADR-0012 records the one place M3 knowingly bends PRD 08: a direct-play
URL must carry an api_key or the bytes are not fetchable, and Usher does
not proxy them. It states what the token grants, which half of the
original failure this does and does not fix, the handling rules that
follow, and the M9 playback-ticket redirect that removes it.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 8: `EmbyAdapter`

The port, implemented. Four decisions in here are worth naming before the code, because each is a place a reasonable implementation goes wrong quietly.

**Paging.** `StartIndex`/`Limit` over `SortBy=DateCreated&SortOrder=Ascending`. Ascending is not cosmetic: with a stable ascending sort, newly added items land at the *end*, so an insertion mid-walk cannot shift an item backwards past a page boundary that has already been read. Deletions can still shift one item out of view; that is a bounded, known imprecision the nightly full reconcile covers, and it is exactly why the port permits duplicates but forbids silent truncation. The walk stops on an empty page, and additionally when `TotalRecordCount` (requested explicitly) says it has read everything — the second condition only fires for a *positive* count, so a server that omits the count cannot cause a truncation at page one.

**Memory.** One page in flight, always. `_walk` is an async generator that yields each payload as it parses it and never accumulates; `list_items` and `watch_state` both drive it. At 94,395 movies across 17 libraries and a default page size of 200, the resident set is one page of JSON plus one `SourceItem`.

**Spans do not wrap generators.** `get_item`, `stream_targets`, `push_watch_state`, and `verify` each open a span; `list_items` and `watch_state` do not. `start_as_current_span` sets a context variable, and a `with` block that spans a `yield` leaks that context to whoever resumes the generator and leaves the span open for as long as a caller holds a half-consumed iterator. Per-request spans inside `EmbySession` already cover the walk, at exactly the granularity `usher.source.request.duration` is bucketed by.

**Two writes, not one.** Emby has no single endpoint that sets position and played together, so `push_watch_state` issues two calls, **position first and the played flag last**. That order is load-bearing: marking an item played clears its resume position server-side, so writing the position afterwards leaves a just-finished film resumable at whatever second the client last reported — which is how a finished film reappears in Continue Watching. A partial failure (position written, played not) raises, and PRD 03's caller enqueues a retry; the operation is idempotent, so the retry is safe.

**Files:**
- Create: `src/usher/adapters/emby/adapter.py`
- Test: `tests/unit/test_adapters_emby_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_adapters_emby_adapter.py
"""EmbyAdapter behaviours the source-agnostic contract cannot express.

The contract suite (run against this adapter in the next task) pins what
every `SourceAdapter` must do. This module pins what *Emby's* adapter must
do: which query parameters the walk sends, how it terminates, which
endpoints a write-back uses and in which order, and how `verify` tells
"unreachable" from "bad credentials".
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from tests.fakes.emby_server import SERVER_VERSION, USER_ID, FakeEmbyServer
from usher.adapters.emby.adapter import EmbyAdapter
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.source import (
    SourceItem,
    SourceItemKind,
    SourceNotSupported,
    SourceWatchState,
    WatchStateUpdate,
)

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
SOURCE = Source(
    id=new_id(),
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-1",
    device_id="9d1f0b6c-0000-7000-8000-000000000001",
)


def _movie(index: int) -> SourceItem:
    return SourceItem(
        external_id=f"movie-{index}",
        name=f"Movie {index}",
        kind=SourceItemKind.MOVIE,
        year=2000 + index,
        provider_ids={"imdb": f"tt000000{index}"},
        container="mkv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        audio_channels=2,
        runtime_seconds=5400,
        added_at=T0,
    )


def _adapter(server: FakeEmbyServer, *, page_size: int = 2) -> EmbyAdapter:
    return EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(transport=server.transport(), base_url=SOURCE.base_url),
        page_size=page_size,
    )


async def test_the_walk_pages_until_the_library_is_exhausted() -> None:
    server = FakeEmbyServer(page_size=2)
    for index in range(5):
        server.add_item(_movie(index), T0)
    adapter = _adapter(server, page_size=2)
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert sorted(seen) == [f"movie-{index}" for index in range(5)]
    listings = [entry for entry in server.requests if entry.endswith("/Items")]
    # 5 items over pages of 2 is three requests: TotalRecordCount stops the
    # walk after the third rather than paying a fourth for an empty page.
    assert len(listings) == 3


async def test_the_walk_asks_for_the_types_and_fields_the_mapper_needs() -> None:
    """A missing `Fields=MediaSources` is the failure mode worth pinning:
    every item comes back with no container, no codec and no HDR, and
    nothing raises -- the catalog just quietly has no quality facts."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    captured: list[httpx.Request] = []
    original = server.handle

    def spy(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return original(request)

    adapter = EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(spy), base_url=SOURCE.base_url
        ),
    )
    try:
        item = [entry async for entry in adapter.list_items()][0]
    finally:
        await adapter.aclose()
    listing = next(r for r in captured if r.url.path.endswith("/Items"))
    fields = listing.url.params["Fields"]
    assert "MediaSources" in fields
    assert "ProviderIds" in fields
    assert "Path" not in fields
    assert listing.url.params["IncludeItemTypes"] == "Movie,Series,Episode"
    assert item.container == "mkv"


async def test_the_walk_sends_a_widened_delta_cursor() -> None:
    """The port promises `since` is inclusive and Emby's own comparison is
    unverified, so the parameter goes out one second early -- see
    `mapping.emby_datetime`."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T1)
    adapter = _adapter(server)
    try:
        seen = [item.external_id async for item in adapter.list_items(since=T1)]
    finally:
        await adapter.aclose()
    assert seen == ["movie-0"]


async def test_the_delta_cursor_actually_narrows_the_window() -> None:
    """The other half of the previous test: inclusive at the boundary, and
    still a filter rather than a no-op."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    server.add_item(_movie(1), T1)
    adapter = _adapter(server)
    try:
        seen = {item.external_id async for item in adapter.list_items(since=T1)}
    finally:
        await adapter.aclose()
    assert seen == {"movie-1"}


async def test_an_unmodelled_item_type_in_a_page_is_skipped_not_fatal() -> None:
    """A server that ignores `IncludeItemTypes` returns Seasons and
    BoxSets. Aborting a 94,395-item reconcile over one of them would be
    worse than ignoring it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(
                200, json={"AccessToken": "t", "User": {"Id": USER_ID}}
            )
        return httpx.Response(
            200,
            json={
                "Items": [
                    {"Id": "season-1", "Type": "Season", "Name": "Season 1"},
                    {"Id": "movie-9", "Type": "Movie", "Name": "Kept"},
                ],
                "TotalRecordCount": 2,
            },
        )

    adapter = EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=SOURCE.base_url
        ),
    )
    try:
        seen = [item.external_id async for item in adapter.list_items()]
    finally:
        await adapter.aclose()
    assert seen == ["movie-9"]


async def test_a_listing_with_no_items_array_is_malformed() -> None:
    """Not a truncation: a caller has to be able to tell "the library ended"
    from "the response was not a listing at all"."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": USER_ID}})
        return httpx.Response(200, json={"TotalRecordCount": 3})

    adapter = EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=SOURCE.base_url
        ),
    )
    try:
        with pytest.raises(PortDataMalformed):
            _ = [item async for item in adapter.list_items()]
    finally:
        await adapter.aclose()


async def test_get_item_raises_rather_than_returning_none_on_a_server_error() -> None:
    """The distinction the port's docstring calls out: `None` means the
    item was deleted, and a 500 does not mean that. Reporting it as `None`
    marks a healthy item unavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "t", "User": {"Id": USER_ID}})
        return httpx.Response(500, text="boom")

    adapter = EmbyAdapter(
        SOURCE,
        CREDENTIALS,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=SOURCE.base_url
        ),
    )
    try:
        with pytest.raises(PortUnavailable):
            await adapter.get_item("movie-0")
    finally:
        await adapter.aclose()


async def test_watch_state_is_attributed_to_the_authenticated_user() -> None:
    """`source_user_id` exists so a household with two Emby users is a
    migration rather than a silent mis-attribution. Leaving it `None` when
    the id is right there is throwing that away."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    server.set_watch_state(
        SourceWatchState(external_id="movie-0", position_seconds=600, played=False)
    )
    adapter = _adapter(server)
    try:
        states = [state async for state in adapter.watch_state()]
    finally:
        await adapter.aclose()
    assert states[0].source_user_id == USER_ID
    assert states[0].position_seconds == 600


async def test_push_writes_the_position_before_the_played_flag() -> None:
    """Load-bearing order, asserted two ways. Emby clears an item's resume
    position when it is marked played, so the reverse order leaves a
    just-finished film resumable at the last reported second -- which is how
    it reappears in Continue Watching. The request order pins the mechanism;
    the resulting state pins the consequence."""
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    try:
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=600, played=True)
        )
    finally:
        await adapter.aclose()
    writes = [entry for entry in server.requests if "PlayingItems" in entry or "PlayedItems" in entry]
    assert len(writes) == 2
    assert "PlayingItems" in writes[0]
    assert "PlayedItems" in writes[1]
    assert server.recorded_watch_state("movie-0") == (0, True)


async def test_push_deletes_the_played_flag_when_unplaying() -> None:
    server = FakeEmbyServer()
    server.add_item(_movie(0), T0)
    adapter = _adapter(server)
    try:
        await adapter.push_watch_state(
            "movie-0", WatchStateUpdate(position_seconds=600, played=False)
        )
    finally:
        await adapter.aclose()
    assert any(entry.startswith("DELETE ") and "PlayedItems" in entry for entry in server.requests)
    assert server.recorded_watch_state("movie-0") == (600, False)


async def test_verify_reports_the_server_version() -> None:
    server = FakeEmbyServer()
    adapter = _adapter(server)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert status.reachable is True
    assert status.authenticated is True
    assert status.server_version == SERVER_VERSION
    assert status.push_available is None


async def test_verify_separates_unreachable_from_bad_credentials() -> None:
    """The whole reason the public info endpoint is probed first. With one
    authenticated call there is no way to tell a dead host from a wrong
    password, which is exactly what PRD 07's 🔶 was about."""
    server = FakeEmbyServer()
    server.reject_credentials()
    adapter = _adapter(server)
    try:
        bad_credentials = await adapter.verify()
        server.offline = True
        unreachable = await adapter.verify()
    finally:
        await adapter.aclose()
    assert (bad_credentials.reachable, bad_credentials.authenticated) == (True, False)
    assert (unreachable.reachable, unreachable.authenticated) == (False, False)


async def test_verify_never_leaks_the_password_into_its_detail() -> None:
    """`SourceStatus.detail` is rendered by `GET /admin/sources/{id}/status`
    straight into an admin response body."""
    server = FakeEmbyServer()
    server.reject_credentials()
    adapter = _adapter(server)
    try:
        status = await adapter.verify()
    finally:
        await adapter.aclose()
    assert status.detail is not None
    assert "correct-horse-battery" not in status.detail


async def test_push_is_not_supported_yet_and_says_so() -> None:
    """PRD 03's documented fallback: an adapter with no socket reports
    `supports_push = False` and the reconciler covers the gap. M5 builds
    the socket; nothing here pretends to."""
    server = FakeEmbyServer()
    adapter = _adapter(server)
    try:
        assert adapter.supports_push is False
        with pytest.raises(SourceNotSupported):
            async with adapter.events():
                pass
    finally:
        await adapter.aclose()


async def test_aclose_closes_a_client_it_created_and_leaves_an_injected_one() -> None:
    """`EmbyAdapter` is normally constructed with no client and owns the one
    it makes. A test (and, later, a pooled registry) injects one and keeps
    ownership; closing someone else's client out from under them is the
    same mistake the bulk adapters' no-op `aclose` exists to avoid."""
    owned = EmbyAdapter(SOURCE, CREDENTIALS)
    await owned.aclose()
    assert owned._client.is_closed is True

    server = FakeEmbyServer()
    injected = httpx.AsyncClient(transport=server.transport(), base_url=SOURCE.base_url)
    adapter = EmbyAdapter(SOURCE, CREDENTIALS, client=injected)
    await adapter.aclose()
    assert injected.is_closed is False
    await injected.aclose()
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/unit/test_adapters_emby_adapter.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.emby.adapter'`

- [ ] **Step 3: Write `EmbyAdapter`**

```python
# src/usher/adapters/emby/adapter.py
"""`EmbyAdapter` -- the `SourceAdapter` implementation for Emby.

Everything Emby-specific above the wire lives here and in this package's
`mapping`, `playback`, and `session` modules; nothing outside
`usher.adapters.emby` names an Emby field, route, or concept.

**Which routes are verified against the live server.** `POST
/Users/AuthenticateByName` is -- ADR-0004's own end-to-end session used it,
and it used the played/unplayed toggle below too. The rest are the standard
Emby 4.9 routes and have not been exercised from this code; M3's definition
of done requires a live run before the milestone closes. The failure mode
of a wrong *query parameter* is deliberately benign: Emby ignores
parameters it does not know, so a wrong delta filter degrades to a full
walk -- a safe superset, and exactly what the nightly reconcile does -- and
never to a silently empty result.

### Paging

`StartIndex`/`Limit` over `SortBy=DateCreated&SortOrder=Ascending`.
Ascending is not cosmetic: with a stable ascending sort, items added during
a walk land at the *end*, so an insertion cannot shift an unread item
backwards past a page boundary already consumed. A deletion mid-walk can
still shift one item out of view; that is a bounded imprecision the nightly
full reconcile covers, and it is why the port permits duplicates but
forbids silent truncation.

The walk terminates on an empty page, and also when `TotalRecordCount` says
everything has been read. The second condition is guarded on a *positive*
count, because a server that omits or zeroes the count would otherwise stop
the walk at page one -- a silent truncation, which is the one failure this
port exists to make impossible.

### Memory

One page in flight, always. `_walk` yields each payload as it parses it and
never accumulates. The deployment this was built for holds 94,395 movies
across 17 libraries; at the default page size that is one page of JSON
resident, not a library.

### Spans

`get_item`, `stream_targets`, `push_watch_state`, and `verify` each open
one. `list_items` and `watch_state` deliberately do not:
`start_as_current_span` sets a context variable, and a `with` block that
spans a `yield` leaks that context to whoever resumes the generator and
holds the span open for as long as a caller keeps a half-consumed iterator.
`EmbySession` already opens a span per HTTP request, which is the
granularity `usher.source.request.duration` is bucketed by anyway.
"""

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
from opentelemetry import trace
from pydantic import AwareDatetime

from usher.adapters.emby.mapping import (
    TICKS_PER_SECOND,
    emby_datetime,
    to_source_item,
    to_watch_state,
)
from usher.adapters.emby.playback import build_stream_targets
from usher.adapters.emby.session import (
    PUBLIC_INFO_PATH,
    SYSTEM_INFO_PATH,
    EmbySession,
    decode_json,
)
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import (
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    UsherPortError,
)
from usher.ports.source import (
    SourceAdapter,
    SourceEvent,
    SourceItem,
    SourceNotSupported,
    SourceStatus,
    SourceWatchState,
    StreamTarget,
    WatchStateUpdate,
)

_tracer = trace.get_tracer("usher.source.emby")

# The three types Usher models. A server that ignores this filter returns
# Seasons and BoxSets too; the mapper skips them rather than failing.
ITEM_TYPES = "Movie,Series,Episode"

# Deliberately no `Path`: nothing in M3 or M4 needs a filesystem path, and
# not requesting one keeps it out of `SourceItem.raw`, which PRD 03 stores
# verbatim in `raw_payloads`.
ITEM_FIELDS = (
    "ProviderIds,MediaSources,DateCreated,ProductionYear,RunTimeTicks,"
    "OriginalTitle,ParentIndexNumber,IndexNumber,SeriesId,SeriesName"
)

# Two different delta filters, because a library edit and a watch-state
# change do not touch the same timestamp.
LIBRARY_SINCE_PARAM = "MinDateLastSaved"
USER_DATA_SINCE_PARAM = "MinDateLastSavedForUser"


def _version_of(body: Mapping[str, Any]) -> str | None:
    version = body.get("Version")
    return version if isinstance(version, str) and version else None


class EmbyAdapter(SourceAdapter):
    def __init__(
        self,
        source: Source,
        credentials: SourceCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        page_size: int = 200,
        timeout_seconds: float = 30.0,
        reauth_cooldown_seconds: float = 60.0,
    ) -> None:
        self._source = source
        self._page_size = page_size
        # Ownership is tracked, not assumed: `aclose()` closes a client this
        # adapter created and leaves an injected one alone. Closing someone
        # else's client is the mistake the bulk adapters' no-op `aclose`
        # exists to avoid, arrived at from the other direction.
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=source.base_url.rstrip("/"), timeout=timeout_seconds
        )
        self._session = EmbySession(
            self._client,
            credentials,
            source_name=source.name,
            device_id=source.device_id,
            reauth_cooldown_seconds=reauth_cooldown_seconds,
        )
        self._closed = False

    @property
    def source_id(self) -> uuid.UUID:
        return self._source.id

    @property
    def supports_push(self) -> bool:
        """`False` until M5 builds the WebSocket listener.

        Not a placeholder: PRD 03 specifies exactly this as the fallback for
        a source whose socket cannot be established, and the reconciler's
        nightly walk covers it. Push itself is verified working (ADR-0004);
        it is sequenced, not blocked.
        """
        return False

    async def verify(self) -> SourceStatus:
        with _tracer.start_as_current_span("source.verify") as span:
            span.set_attribute("usher.source", self._source.name)
            try:
                public = await self._session.anonymous_json(
                    PUBLIC_INFO_PATH, op="verify_public"
                )
            except PortRateLimited as exc:
                # Rate limited means something answered, so the host is up.
                # Must be caught before UsherPortError -- it is a subclass.
                return SourceStatus(reachable=True, authenticated=False, detail=str(exc))
            except UsherPortError as exc:
                return SourceStatus(reachable=False, authenticated=False, detail=str(exc))
            version = _version_of(public)
            try:
                info = await self._session.json_body("GET", SYSTEM_INFO_PATH, op="verify")
            except UsherPortError as exc:
                return SourceStatus(
                    reachable=True,
                    authenticated=False,
                    server_version=version,
                    detail=str(exc),
                )
            span.set_attribute("usher.authenticated", True)
            return SourceStatus(
                reachable=True,
                authenticated=True,
                # `None`, never `True`. ADR-0004: a WebSocket handshake
                # against a *nonexistent* path also upgrades and also
                # receives `Sessions`, so an upgrade is not evidence of
                # anything. Only received messages are, and M5 builds the
                # probe that asserts on them.
                push_available=None,
                server_version=_version_of(info) or version,
            )

    async def _walk(
        self, *, since_param: str, since: AwareDatetime | None
    ) -> AsyncIterator[dict[str, Any]]:
        user_id = await self._session.user_id()
        start = 0
        while True:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": ITEM_TYPES,
                "Fields": ITEM_FIELDS,
                "SortBy": "DateCreated",
                "SortOrder": "Ascending",
                "StartIndex": str(start),
                "Limit": str(self._page_size),
                "EnableTotalRecordCount": "true",
            }
            if since is not None:
                params[since_param] = emby_datetime(since)
            body = await self._session.json_body(
                "GET", f"/Users/{user_id}/Items", params=params, op="list"
            )
            items = body.get("Items")
            if not isinstance(items, list):
                # Not a truncation: a caller must be able to tell "the
                # library ended" from "that was not a listing at all".
                raise PortDataMalformed(
                    "Emby's item listing carried no Items array",
                    detail=f"StartIndex={start}",
                )
            if not items:
                return
            for payload in items:
                if isinstance(payload, dict):
                    yield payload
            start += len(items)
            total = body.get("TotalRecordCount")
            # `total > 0`, not `total >= 0`: a server that omits the count
            # (or reports zero while returning items) must not stop the walk
            # at page one.
            if isinstance(total, int) and total > 0 and start >= total:
                return

    def list_items(self, since: AwareDatetime | None = None) -> AsyncIterator[SourceItem]:
        return self._list_items(since)

    async def _list_items(self, since: AwareDatetime | None) -> AsyncIterator[SourceItem]:
        async for payload in self._walk(since_param=LIBRARY_SINCE_PARAM, since=since):
            item = to_source_item(payload)
            if item is not None:
                yield item

    async def _fetch(self, external_id: str) -> dict[str, Any] | None:
        user_id = await self._session.user_id()
        path = f"/Users/{user_id}/Items/{external_id}"
        # `request`, not `json_body`: a 404 is "gone", which is a value, and
        # every other failure is an error. Conflating them would mark a
        # healthy item unavailable over a flaky network.
        response = await self._session.request(
            "GET", path, params={"Fields": ITEM_FIELDS}, op="get_item"
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise PortUnavailable(f"GET {path} returned HTTP {response.status_code}")
        payload = decode_json(response, path)
        # Some builds answer an unknown id with 200 and an empty object
        # rather than 404. An item with no `Id` is not an item.
        return payload if payload.get("Id") else None

    async def get_item(self, external_id: str) -> SourceItem | None:
        with _tracer.start_as_current_span("source.get_item") as span:
            span.set_attribute("usher.source", self._source.name)
            span.set_attribute("usher.external_id", external_id)
            payload = await self._fetch(external_id)
            span.set_attribute("usher.found", payload is not None)
            return None if payload is None else to_source_item(payload)

    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        with _tracer.start_as_current_span("source.stream_targets") as span:
            span.set_attribute("usher.source", self._source.name)
            span.set_attribute("usher.external_id", external_id)
            payload = await self._fetch(external_id)
            if payload is None:
                return []
            # The URL this builds carries a session token (ADR-0012), so it
            # is never set as a span attribute and never logged.
            return build_stream_targets(
                payload,
                base_url=self._source.base_url,
                access_token=await self._session.access_token(),
                device_id=self._source.device_id,
            )

    def watch_state(
        self, since: AwareDatetime | None = None
    ) -> AsyncIterator[SourceWatchState]:
        return self._watch_state(since)

    async def _watch_state(
        self, since: AwareDatetime | None
    ) -> AsyncIterator[SourceWatchState]:
        user_id = await self._session.user_id()
        async for payload in self._walk(since_param=USER_DATA_SINCE_PARAM, since=since):
            state = to_watch_state(payload, source_user_id=user_id)
            if state is not None:
                yield state

    async def push_watch_state(self, external_id: str, state: WatchStateUpdate) -> None:
        """Write watch state back to Emby, in two calls.

        Emby has no endpoint that sets position and played together, so this
        is not atomic -- and the order is load-bearing. **Position first,
        played flag last:** marking an item played clears its resume
        position server-side, so writing the position afterwards leaves a
        just-finished film resumable at whatever second the client last
        reported, which is how it reappears in Continue Watching.

        A partial failure (position written, played not) raises, exactly as
        the port requires, and PRD 03's caller enqueues a retry. Both writes
        are idempotent, so the retry is safe.
        """
        with _tracer.start_as_current_span("source.push_watch_state") as span:
            span.set_attribute("usher.source", self._source.name)
            span.set_attribute("usher.external_id", external_id)
            span.set_attribute("usher.played", state.played)
            user_id = await self._session.user_id()
            await self._session.ok(
                "POST",
                f"/Users/{user_id}/PlayingItems/{external_id}/Progress",
                params={
                    "PositionTicks": str(max(state.position_seconds, 0) * TICKS_PER_SECOND)
                },
                op="push_progress",
            )
            await self._session.ok(
                "POST" if state.played else "DELETE",
                f"/Users/{user_id}/PlayedItems/{external_id}",
                op="push_played",
            )

    def events(self) -> AbstractAsyncContextManager[AsyncIterator[SourceEvent]]:
        raise SourceNotSupported(
            "the Emby push channel lands in M5; until then this source is covered by "
            "the reconciler"
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._session.aclose()
        if self._owns_client:
            await self._client.aclose()
```

- [ ] **Step 4: Run and watch it pass**

Run: `uv run pytest tests/unit/test_adapters_emby_adapter.py -q`
Expected: PASS — 15 tests.

- [ ] **Step 5: Check and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest tests/unit -q
```

```bash
git add -A && git commit -F - <<'EOF'
feat: EmbyAdapter -- the SourceAdapter implementation

Paging with an ascending, stable sort, so an item added mid-walk lands at
the end and cannot shift an unread item backwards past a consumed page
boundary. The walk stops on an empty page or on TotalRecordCount, with the
count guarded on being positive -- a server that omits it must not be able
to truncate the walk at page one, which is the one failure this port
exists to make impossible.

One page in flight, always: _walk is a generator and never accumulates. At
94,395 movies that is the difference between a page of JSON and a library.

No span wraps a generator: start_as_current_span sets a context variable,
and a `with` block spanning a `yield` leaks it to whoever resumes the
iterator and keeps the span open while a caller holds one half-consumed.
EmbySession's per-request spans already cover the walk.

push_watch_state is two calls, position first and played last, because
Emby clears the resume position when an item is marked played -- the
reverse order is how a finished film reappears in Continue Watching.
Asserted both by request order and by the resulting state.

verify probes /System/Info/Public before authenticating, which is what
lets it report unreachable and bad-credentials as different answers --
the split PRD 07's provisional marker asked for.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 9: Run the contract suite against the real adapter

The moment the milestone exists for. The same 40 assertions that passed against an adapter with no wire format must now pass against one that serialises to Emby's JSON and parses it back.

**Files:**
- Create: `tests/fakes/emby_harness.py`
- Test: `tests/unit/test_adapters_emby_contract.py`

- [ ] **Step 1: Write the harness and the runner**

```python
# tests/fakes/emby_harness.py
"""Binds a real `EmbyAdapter` to `FakeEmbyServer` for the contract suite.

Page size two, deliberately: the contract seeds seven items for its paging
cases, so the walk crosses four page boundaries rather than trivially
fitting in one.

The `httpx.AsyncClient` is injected, so `EmbyAdapter.aclose()` leaves it
open -- the contract closes the adapter itself in two of its cases, and
this harness's own `aclose()` is what finally disposes of the client.

**The transport really awaits, and that is not a detail.** This runs on
`tests/fakes/slow_transport.py` rather than the bare `httpx.MockTransport`
`FakeEmbyServer.transport()` hands out, so `observed_overlap` can return a
real number and the contract's expired-credential case can mean what it
looks like it means. See Step 2's correction note for the measurement.
"""

import httpx
from pydantic import AwareDatetime, SecretStr

from tests.contract.source_harness import SourceHarness
from tests.fakes.emby_server import FakeEmbyServer
from tests.fakes.slow_transport import SlowTransport
from usher.adapters.emby.adapter import EmbyAdapter
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import SourceAdapter, SourceItem, SourceWatchState

PAGE_SIZE = 2


class EmbyHarness(SourceHarness):
    def __init__(self) -> None:
        self._server = FakeEmbyServer(page_size=PAGE_SIZE)
        self._source = Source(
            id=new_id(),
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-emby",
            device_id=str(new_id()),
        )
        self._transport = SlowTransport(self._server.handle)
        self._client = httpx.AsyncClient(transport=self._transport, base_url=self._source.base_url)
        self._adapter = EmbyAdapter(
            self._source,
            SourceCredentials(
                username=self._server.username, password=SecretStr(self._server.password)
            ),
            client=self._client,
            page_size=PAGE_SIZE,
        )

    @property
    def source(self) -> Source:
        return self._source

    @property
    def adapter(self) -> SourceAdapter:
        return self._adapter

    async def given_item(self, item: SourceItem, *, changed_at: AwareDatetime) -> None:
        """Render `item` into Emby's JSON, held as changed at `changed_at`.

        Takes **both** widenings `SourceHarness.given_item` permits, and
        says so here because that ABC requires an implementation taking
        either to declare it: `changed_at` is compared at whole-second
        resolution (Emby's `MinDateLastSaved` is a whole-second stamp), and
        an item with no `container` is a folder, which has no `MediaSources`
        entry to hang a codec, a file size, a channel count, or an HDR
        format off.
        """
        self._server.add_item(item, changed_at)

    async def given_watch_state(self, state: SourceWatchState) -> None:
        self._server.set_watch_state(state)

    async def remove_item(self, external_id: str) -> None:
        self._server.remove_item(external_id)

    async def recorded_watch_state(self, external_id: str) -> tuple[int, bool] | None:
        return self._server.recorded_watch_state(external_id)

    async def go_offline(self) -> None:
        self._server.offline = True

    async def fail_after_items(self, count: int) -> None:
        self._server.fail_after = count

    async def reject_credentials(self) -> None:
        self._server.reject_credentials()

    async def expire_credentials(self) -> None:
        self._server.expire_session()

    def authentications(self) -> int:
        return self._server.authentications

    def observed_overlap(self) -> int | None:
        """The most requests this harness ever saw in flight at once.

        A real number rather than the ABC's `None`, which is what upgrades
        the contract's expired-credential case from "recovery happened" to
        "recovery happened under genuine concurrency, once".
        """
        return self._transport.max_in_flight

    async def aclose(self) -> None:
        await self._adapter.aclose()
        await self._client.aclose()
```

```python
# tests/unit/test_adapters_emby_contract.py
"""The source-adapter contract, against the real EmbyAdapter.

Same file of assertions that tests/unit/test_source_adapter_contract.py
runs against an adapter with no wire format at all. Both runs are needed:
that one proves the assertions are not secretly Emby-shaped, this one
proves they survive a serialisation. Neither alone is evidence.

No Docker and no network -- the whole thing rides on httpx.MockTransport,
which is why the load-bearing suite stays in the fast lane.
"""

from collections.abc import AsyncIterator

import pytest_asyncio

from tests.contract.source_adapter_contract import SourceAdapterContract
from tests.contract.source_harness import SourceHarness
from tests.fakes.emby_harness import EmbyHarness


class TestEmbyAdapter(SourceAdapterContract):
    @pytest_asyncio.fixture
    async def harness(self) -> AsyncIterator[SourceHarness]:
        harness = EmbyHarness()
        try:
            yield harness
        finally:
            await harness.aclose()
```

- [ ] **Step 2: Run it and fix what it finds**

Run: `uv run pytest tests/unit/test_adapters_emby_contract.py -q`
Expected: PASS — 40 tests, the same count as the fake's run.

This is a real, load-bearing run and it may well not pass first time. If a case fails, **fix the adapter, not the assertion** — unless the assertion is wrong for every source, in which case fix it in `tests/contract/` and re-run *both* runners.

> **Corrected while implementing.** This step used to predict two specific failures, and mutation testing showed both predictions were wrong. Kept here with the measurements, because "the contract catches X" is exactly the kind of claim this milestone is not allowed to assert without evidence.
>
> - **Was:** "`test_operations_after_aclose_raise_port_unavailable` fails if `EmbySession._raise_if_closed` is not called from `user_id()`." **It does not.** `EmbyAdapter._fetch` calls `user_id()` first; with the check gone that call authenticates successfully against the still-open injected transport, and `request()`'s *own* `_raise_if_closed` then raises the `PortUnavailable` the case is waiting for. Measured directly: all 41 tests stay green while the closed adapter emits `POST /Users/AuthenticateByName` and mints a live Emby session (`authentications` 0 → 1). The constraint is real but this is not what pins it — `tests/unit/test_adapters_emby_session.py::test_the_other_entry_points_also_refuse_to_run_after_aclose` is, because it asserts the authentication count is zero rather than only that an error surfaced.
> - **Was:** "`test_operations_recover_from_an_expired_credential` fails with four authentications if `_refresh` compares the wrong generation." **Not over the transport this step originally specified.** With the plan's `httpx.MockTransport` harness, deleting *both* of `EmbySession`'s locks and the generation short-circuit outright leaves all 41 green: nothing in that transport ever awaits on the way to its handler, so the event loop runs one gathered call all the way through its own re-auth before starting the next and `assert authentications() - before <= 1` never discriminates. The fix is in Step 1 above — `EmbyHarness` runs on `tests/fakes/slow_transport.py` and implements `SourceHarness.observed_overlap`, so the contract asserts the four gathered calls really did overlap. Over *that* transport the prediction holds: deleting `_refresh`'s lock raises `PortAuthFailed`, deleting both locks and the short-circuit raises `PortAuthFailed`, and deleting the short-circuit alone trips `<= 1` with four authentications.
>
> One further limit found the same way, and left as a limit: making `EmbyAdapter._fetch` report every `>= 400` as `None` — the "a 500 is not a deletion" bug — passes all 40 contract cases, because `SourceHarness.go_offline` is a *transport* failure by design and no hook here can arrange a failing HTTP status. `tests/unit/test_adapters_emby_adapter.py::test_get_item_raises_rather_than_returning_none_on_a_server_error` catches it. Status-level behaviour stays a per-implementation test; both the contract module's docstring and this note say so rather than leaving the gap to be assumed closed.

- [ ] **Step 3: Assert the two runs are the same suite**

Add to `tests/unit/test_adapters_emby_contract.py`:

```python
def test_both_implementations_run_the_same_assertions() -> None:
    """A contract suite is only evidence if both subclasses actually run all
    of it. Nothing stops a subclass from overriding a case with a weaker one
    -- so this asserts neither does, and that the count is not silently
    drifting as cases are added.
    """
    from tests.unit.test_source_adapter_contract import TestFakeSourceAdapter

    cases = {name for name in dir(SourceAdapterContract) if name.startswith("test_")}
    assert len(cases) == 40
    for subclass in (TestEmbyAdapter, TestFakeSourceAdapter):
        overridden = {
            name
            for name in cases
            if getattr(subclass, name) is not getattr(SourceAdapterContract, name)
        }
        assert overridden == set(), f"{subclass.__name__} overrides {overridden}"
```

Run: `uv run pytest tests/unit/test_adapters_emby_contract.py -q`
Expected: PASS — 41 tests.

> If a later milestone adds contract cases, this test's `40` has to move with them. That is the point: a suite whose size can drift silently is a suite nobody notices shrinking.

- [ ] **Step 4: Check and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest tests/unit -q
```

```bash
git add -A && git commit -F - <<'EOF'
test: the contract suite passes against the real Emby adapter

The moment M3 exists for. The same 40 assertions that pass against an
adapter with no wire format now pass against one that serialises to Emby's
JSON and parses it back -- including the paging walk (seven items over
pages of two), the must-raise-never-truncate guarantee, a 404 that means
"gone" versus a transport failure that must not, and a session that
expires and is silently re-minted without a storm.

A forty-first test asserts neither subclass overrides a case and that
the suite is still 40 cases, because a contract suite that can silently shrink
is not evidence of anything.

No Docker and no network: the whole run rides on httpx.MockTransport, so
the load-bearing suite stays in the fast lane.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 10: The adapter factory and `SourceService`

`services/` may not import `adapters/`, so `SourceService` receives a `SourceAdapterFactory`. That indirection is also PRD 01's "additional sources" extension seam made concrete: a Jellyfin adapter adds a `SourceKind` member and one branch here, and nothing else moves.

**Files:**
- Create: `src/usher/adapters/factory.py`
- Create: `src/usher/services/sources.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/test_services_sources.py`
- Test: `tests/unit/test_adapters_factory.py` — **added while implementing.** As drafted, this task shipped `ConfiguredSourceAdapterFactory` with no test of its own: every assertion below runs against `RecordingFactory`, so a factory that dropped `page_size`, dropped `timeout_seconds`, or collapsed its `if` into an unconditional `return EmbyAdapter(...)` would pass all of it. All four of those are now mutations that fail.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_services_sources.py
"""SourceService against port fakes. No network, no database."""

import uuid

from pydantic import SecretStr

from tests.fakes.credential_store import FakeCredentialStore
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.source_repository import FakeSourceRepository
from usher.domain.enums import SourceKind
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import SourceAdapter, SourceAdapterFactory
from usher.services.sources import SourceService

CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))


class RecordingFactory(SourceAdapterFactory):
    """Counts what the service builds and closes, and can hand back an
    adapter whose credentials the source rejects."""

    def __init__(self, *, reject: bool = False) -> None:
        self.built: list[tuple[Source, SourceCredentials]] = []
        self.closed = 0
        self._reject = reject

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        self.built.append((source, credentials))
        adapter = _CountingAdapter(source, self)
        if self._reject:
            adapter.reject_credentials()
        return adapter


class _CountingAdapter(FakeSourceAdapter):
    def __init__(self, source: Source, factory: RecordingFactory) -> None:
        super().__init__(source)
        self._factory = factory

    async def aclose(self) -> None:
        self._factory.closed += 1
        await super().aclose()


def _service(
    repo: FakeSourceRepository | None = None,
    store: FakeCredentialStore | None = None,
    factory: RecordingFactory | None = None,
) -> tuple[SourceService, FakeSourceRepository, FakeCredentialStore, RecordingFactory]:
    repo = repo or FakeSourceRepository()
    store = store or FakeCredentialStore()
    factory = factory or RecordingFactory()
    return SourceService(repo, store, factory), repo, store, factory


async def test_register_persists_a_source_and_its_credentials() -> None:
    service, repo, store, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY,
        name="Living Room Emby",
        base_url="https://emby.invalid",
        credentials=CREDENTIALS,
    )
    stored = await repo.get(source.id)
    assert stored is not None
    assert stored.name == "Living Room Emby"
    secret = await store.get(stored.credentials_ref)
    assert secret is not None
    assert secret.password.get_secret_value() == "correct-horse-battery"


async def test_register_generates_a_stable_device_id() -> None:
    """PRD 03: the DeviceId is generated *once* and persisted, so Usher is
    one device in Emby's dashboard rather than an accumulating pile of
    sessions. Generating it here, at registration, is what makes that
    true -- an adapter that made one up per process could not."""
    service, _, _, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY,
        name="A",
        base_url="https://emby.invalid",
        credentials=CREDENTIALS,
    )
    assert source.device_id
    uuid.UUID(source.device_id)


async def test_two_sources_get_different_device_ids_and_refs() -> None:
    """Rules out a constant. A shared DeviceId would make two Emby servers
    fight over one session identity; a shared credentials_ref would make
    the second registration overwrite the first's password."""
    service, _, _, _ = _service()
    first = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    second = await service.register(
        kind=SourceKind.EMBY, name="B", base_url="https://b.invalid", credentials=CREDENTIALS
    )
    assert first.device_id != second.device_id
    assert first.credentials_ref != second.credentials_ref


async def test_the_credentials_ref_is_not_derived_from_the_source_id() -> None:
    """PRD 08 calls `credentials_ref` an indirection. A ref that is just
    the id spelled differently is not one, and rotation -- write the new
    secret under a new ref, flip the pointer, delete the old -- stops being
    expressible."""
    service, _, _, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    assert str(source.id) not in source.credentials_ref


async def test_status_verifies_through_a_freshly_built_adapter() -> None:
    service, _, _, factory = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None
    assert status.reachable is True
    assert factory.built[0][0].id == source.id
    assert factory.built[0][1].password.get_secret_value() == "correct-horse-battery"


async def test_status_closes_the_adapter_it_built() -> None:
    """One adapter owns one connection pool. A status endpoint that leaked
    one per call would exhaust file descriptors on a dashboard that polls."""
    service, _, _, factory = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    await service.status(source.id)
    await service.status(source.id)
    assert factory.closed == 2


async def test_status_is_none_for_an_unknown_source() -> None:
    service, _, _, _ = _service()
    assert await service.status(uuid.uuid4()) is None


async def test_status_reports_missing_credentials_rather_than_crashing() -> None:
    """A source row whose credential row was deleted out from under it is
    an operator-visible misconfiguration, not a 500. PRD 08's degradation
    rule: narrow the functionality, never fail the request."""
    service, _, store, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    await store.delete(source.credentials_ref)
    status = await service.status(source.id)
    assert status is not None
    assert status.authenticated is False
    assert status.detail is not None


async def test_remove_deletes_the_source_and_its_credentials() -> None:
    service, repo, store, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    assert await service.remove(source.id) is True
    assert await repo.get(source.id) is None
    assert await store.get(source.credentials_ref) is None


async def test_remove_reports_an_unknown_source() -> None:
    service, _, _, _ = _service()
    assert await service.remove(uuid.uuid4()) is False


async def test_list_sources_returns_what_was_registered() -> None:
    service, _, _, _ = _service()
    await service.register(
        kind=SourceKind.EMBY, name="Zeta", base_url="https://z.invalid", credentials=CREDENTIALS
    )
    await service.register(
        kind=SourceKind.EMBY, name="Alpha", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    assert [source.name for source in await service.list_sources()] == ["Alpha", "Zeta"]


async def test_the_service_never_returns_a_credential() -> None:
    """PRD 08: "Credentials are never returned by any API, including admin.
    Write-only." `Source` cannot carry one -- it has only the ref -- and
    this asserts the service does not smuggle one out some other way."""
    service, _, _, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    assert "correct-horse-battery" not in repr(source)
    assert "correct-horse-battery" not in repr(await service.list_sources())
    assert "correct-horse-battery" not in repr(await service.status(source.id))


async def test_a_rejected_credential_is_reported_not_raised() -> None:
    """`GET /admin/sources/{id}/status` renders this state rather than
    handling it: `SourceAdapter.verify()` already returns rather than
    raising, and the service must not reintroduce an exception path on top
    of it. Distinguished from the missing-credentials case above, which is
    the service's own answer -- this one comes from the adapter."""
    service, _, _, _ = _service(factory=RecordingFactory(reject=True))
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None
    assert status.reachable is True
    assert status.authenticated is False
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/unit/test_services_sources.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.services.sources'`

- [ ] **Step 3: Write the factory**

```python
# src/usher/adapters/factory.py
"""The one place a `SourceKind` becomes a concrete adapter.

PRD 01 lists "additional sources" as an extension seam left open in v1.
This module is that seam's actual hinge: a Jellyfin adapter adds a member
to `SourceKind`, an implementation under `usher/adapters/jellyfin/`, and one
branch below. Nothing in `services/` or `api/` moves, because neither ever
names an adapter class -- they hold a `SourceAdapterFactory`.

Lives in `adapters/`, not `services/`, because it imports every adapter and
`services/` may depend only on `domain/` and `ports/` (PRD 01, layering
rule 2). The composition roots -- `usher.api.deps` and `usher.cli` -- are
the only things allowed to construct one.
"""

from usher.adapters.emby.adapter import EmbyAdapter
from usher.domain.enums import SourceKind
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import SourceAdapter, SourceAdapterFactory, SourceNotSupported


class ConfiguredSourceAdapterFactory(SourceAdapterFactory):
    """Builds adapters with this deployment's tuning applied.

    Named for what it does rather than for a service, because it is not one
    -- it is the registry. The settings it carries come from
    `usher.config.Settings` at the composition root, so no adapter has to
    read configuration itself.
    """

    def __init__(
        self,
        *,
        page_size: int = 200,
        timeout_seconds: float = 30.0,
        reauth_cooldown_seconds: float = 60.0,
    ) -> None:
        self._page_size = page_size
        self._timeout_seconds = timeout_seconds
        self._reauth_cooldown_seconds = reauth_cooldown_seconds

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        if source.kind is SourceKind.EMBY:
            return EmbyAdapter(
                source,
                credentials,
                page_size=self._page_size,
                timeout_seconds=self._timeout_seconds,
                reauth_cooldown_seconds=self._reauth_cooldown_seconds,
            )
        raise SourceNotSupported(f"no adapter is registered for source kind {source.kind}")
```

> **Note:** `SourceKind` currently has exactly one member, so the `raise` below the `if` is unreachable at runtime today and mypy will not complain about it. It is kept rather than replaced by an unconditional `return`, because the *next* member added must land on it rather than on a silently-wrong Emby adapter.

- [ ] **Step 4: Add a sixth import-linter contract**

The spec's acceptance criteria table has a row nothing enforces: **"Source abstraction — zero source-specific concepts outside `adapters/emby/`."** With `ConfiguredSourceAdapterFactory` now the single place that names `EmbyAdapter`, that criterion becomes expressible as a contract rather than a hope. Append to `pyproject.toml`:

```toml
# The spec's "zero source-specific concepts outside adapters/emby/"
# acceptance criterion, enforced rather than asserted. Only
# usher.adapters.factory (the registry) and usher.cli (a composition root,
# which the contract above already isolates) may name a concrete source
# adapter. Without this, nothing would catch `api/routers/sources.py`
# importing EmbyAdapter directly -- which would type-check, pass every
# test, and quietly make the abstraction decorative.
[[tool.importlinter.contracts]]
name = "no concrete source adapter escapes its package"
type = "forbidden"
source_modules = [
    "usher.domain",
    "usher.ports",
    "usher.services",
    "usher.api",
    "usher.db",
]
forbidden_modules = ["usher.adapters.emby"]
allow_indirect_imports = true
```

> **`allow_indirect_imports` was added while implementing, and it is load-bearing rather than a loosening.** A `forbidden` contract reports *chains* by default, so without this line the contract breaks on the one import the factory exists for: `usher.api.deps -> usher.adapters.factory -> usher.adapters.emby.adapter`. Verified by planting exactly that import — BROKEN without the flag, KEPT with it — which means Task 11's composition root would otherwise have had to choose between wiring the factory and keeping the contract green. Also verified that the flag does not blunt it: a direct `from usher.adapters.emby.adapter import EmbyAdapter` in `usher.api` or in `usher.db` is still BROKEN. That is exactly the line the acceptance criterion draws — nothing outside the registry may *name* a concrete adapter, and everything is free to reach one through the port.

Run: `uv run lint-imports`
Expected: **6 contracts kept, 0 broken.** If it reports the new one broken, something outside the factory imports `EmbyAdapter` — fix the import, not the contract.

**Verify the contract actually fires rather than assuming it does.** Plant `from usher.adapters.emby.adapter import EmbyAdapter` in `src/usher/api/deps.py` and run `lint-imports` twice, once with the five existing contracts and once with all six. Measured: the five report **KEPT** — none of them constrains `usher.api` or `usher.db` against `usher.adapters` at all — and the sixth reports `usher.api.deps -> usher.adapters.emby.adapter (l.9)`. The same probe in `src/usher/db/repositories/source.py` behaves identically. A contract added without this check is indistinguishable from one that matches nothing.

- [ ] **Step 5: Write `SourceService`**

```python
# src/usher/services/sources.py
"""Registering, inspecting, and removing configured sources.

Depends only on `domain/` and `ports/` (PRD 01, layering rule 2): it never
names `EmbyAdapter`, it receives a `SourceAdapterFactory`.

**Adapters are built per call and closed immediately.** That is wasteful --
each one authenticates from scratch -- and it is right for M3: a long-lived
adapter is a long-lived connection pool and, from M5, a long-lived
WebSocket, and the thing that owns those is the push lane's registry, which
does not exist yet. Building a pooled registry here would mean designing
the lifecycle for a consumer that has not been written. What matters now is
that nothing *leaks*: every adapter this service builds is closed in a
`finally`.
"""

import secrets
import uuid

from loguru import logger

from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import CredentialStore, SourceCredentials
from usher.ports.repository import SourceRepository
from usher.ports.source import SourceAdapterFactory, SourceStatus


class SourceService:
    def __init__(
        self,
        sources: SourceRepository,
        credentials: CredentialStore,
        adapters: SourceAdapterFactory,
    ) -> None:
        self._sources = sources
        self._credentials = credentials
        self._adapters = adapters

    async def register(
        self,
        *,
        kind: SourceKind,
        name: str,
        base_url: str,
        credentials: SourceCredentials,
    ) -> Source:
        """Persist a new source and its encrypted credentials.

        The `device_id` is generated **here, once**, and persisted -- that
        is what PRD 03's durable client actually is. An adapter that
        generated one per process would appear in Emby's dashboard as a new
        device every restart, which is the accumulating-sessions failure the
        design exists to avoid.

        The `credentials_ref` is a random token, not a function of the
        source id. A derived ref would make PRD 08's indirection
        decorative and make rotation -- write the new secret under a new
        ref, flip the pointer, delete the old row -- impossible to express.
        """
        source = Source(
            kind=kind,
            name=name,
            base_url=base_url,
            credentials_ref=secrets.token_urlsafe(24),
            device_id=str(new_id()),
        )
        await self._sources.add(source)
        await self._credentials.put(
            source.credentials_ref, credentials, owner_id=source.id
        )
        # The name and the id, never the credential. PRD 08: credentials are
        # never logged, "including in error paths and request dumps".
        logger.info(
            "registered source {name} ({source_id})", name=source.name, source_id=source.id
        )
        return source

    async def list_sources(self) -> list[Source]:
        return await self._sources.list_all()

    async def status(self, source_id: uuid.UUID) -> SourceStatus | None:
        """Connection, authentication, and push availability for one source.

        `None` only when the source itself does not exist -- every other
        outcome is a `SourceStatus`, including a source whose credential row
        has gone missing. PRD 08: "a degraded subsystem narrows
        functionality; it never fails a request local state can answer", and
        "this source is misconfigured" is exactly the answer an admin screen
        is asking for.
        """
        source = await self._sources.get(source_id)
        if source is None:
            return None
        credentials = await self._credentials.get(source.credentials_ref)
        if credentials is None:
            return SourceStatus(
                reachable=False,
                authenticated=False,
                detail="no stored credentials for this source; re-enter them to reconnect",
            )
        adapter = self._adapters.build(source, credentials)
        # No `except UsherPortError` here, deliberately: `verify()` already
        # promises not to raise for an expected failure, so anything that
        # does escape is a bug, and catching it would hide that behind a
        # green status. The `finally` is what this needs -- one adapter is
        # one connection pool, and a status endpoint a dashboard polls
        # would otherwise leak one per call.
        try:
            return await adapter.verify()
        finally:
            await adapter.aclose()

    async def remove(self, source_id: uuid.UUID) -> bool:
        """Delete a source and its credentials. Returns whether it existed.

        The credential is deleted first. If the process dies between the two
        writes, what survives is a source row with no credential -- which
        `status()` reports as a misconfiguration an operator can see and
        fix. The other order would survive as an encrypted row with no owner
        (the `ON DELETE CASCADE` covers this within a transaction, but not a
        crash between two separately-committed calls), which nothing
        surfaces and nothing can attribute.
        """
        source = await self._sources.get(source_id)
        if source is None:
            return False
        await self._credentials.delete(source.credentials_ref)
        return await self._sources.delete(source_id)
```

- [ ] **Step 6: Run and watch it pass**

Run: `uv run pytest tests/unit/test_services_sources.py tests/unit/test_adapters_factory.py -q`
Expected: PASS — 21 tests (17 service + 4 factory). The plan drafted 13 service cases; four more were added while implementing, each because a mutation survived all thirteen: the credential is stored under the *registering source's* `owner_id` (a wrong id passes everything else), the persisted `device_id` and `credentials_ref` are the ones handed back (a service that returned a freshly-stamped copy satisfies "a device id was generated"), `status()`'s `finally` still closes the adapter when `verify()` raises, and removing one source leaves another's credential alone.

- [ ] **Step 7: Check and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest tests/unit -q
```
`lint-imports` now reports **6 kept, 0 broken**: `usher.services.sources` imports only `usher.domain` and `usher.ports`; `usher.adapters.factory` is imported by neither; and nothing outside that factory names `EmbyAdapter`.

```bash
git add -A && git commit -F - <<'EOF'
feat: SourceService and the adapter factory

The factory is PRD 01's "additional sources" extension seam made concrete:
a Jellyfin adapter adds a SourceKind member, a package, and one branch, and
nothing in services/ or api/ moves because neither ever names an adapter
class. It lives in adapters/, not services/, because services/ may depend
only on domain/ and ports/.

SourceService generates the DeviceId once, at registration, and persists
it -- which is what PRD 03's durable client actually is; an adapter that
made one up per process would be a new device in Emby's dashboard every
restart. credentials_ref is a random token rather than a function of the
source id, so PRD 08's indirection is real and rotation is expressible.

Adapters are built per call and closed in a finally. Wasteful and correct
for M3: the thing that should own a long-lived adapter is M5's push
registry, and designing its lifecycle now would be designing for a
consumer that does not exist. What matters today is that nothing leaks.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 11: The admin sources API

PRD 07's 🔶 says to settle `verify()` "when the Emby adapter and this endpoint are built together". An endpoint nothing serves has not been settled, so M3 ships four of the five admin-source routes. `POST /admin/sources/{id}/sync` is not among them — it triggers a reconcile, and there is no reconciler until M5.

This is also where PRD 08's write-only credential rule stops being a convention and becomes a test.

**Files:**
- Create: `src/usher/api/dto/source.py`, `src/usher/api/routers/sources.py`
- Modify: `src/usher/api/deps.py`, `src/usher/api/app.py`
- Test: `tests/integration/test_admin_sources.py`

- [ ] **Step 1: Write the failing test**

Integration, not unit: these routes write to Postgres, and the cascade and the encryption are the parts worth exercising for real.

```python
# tests/integration/test_admin_sources.py
"""The admin source routes, end to end against real Postgres.

The adapter behind them is the real `EmbyAdapter` pointed at
`FakeEmbyServer` through a `MockTransport`, injected by overriding the
factory dependency -- so `GET /admin/sources/{id}/status` exercises the
whole stack (route, service, repository, credential store, adapter,
session, mapper) without a live Emby.
"""

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.emby_server import SERVER_VERSION, FakeEmbyServer
from usher.adapters.emby.adapter import EmbyAdapter
from usher.api.app import create_app
from usher.api.deps import get_source_adapter_factory
from usher.config import Settings
from usher.db.base import build_engine
from usher.db.models.source import SourceCredentialRow
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import SourceAdapter, SourceAdapterFactory

PASSWORD = "correct-horse-battery"


class _FakeServerFactory(SourceAdapterFactory):
    """Builds the *real* `EmbyAdapter`, pointed at an in-memory server.

    The client is injected, so `EmbyAdapter.aclose()` deliberately leaves it
    open (it only closes clients it created) -- which means this factory has
    to keep them and the fixture has to dispose of them. One instance per
    app, not one per request, so that list survives to teardown.
    """

    def __init__(self, server: FakeEmbyServer) -> None:
        self._server = server
        self.clients: list[httpx.AsyncClient] = []

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        client = httpx.AsyncClient(
            transport=self._server.transport(), base_url=source.base_url
        )
        self.clients.append(client)
        return EmbyAdapter(source, credentials, client=client)


@pytest_asyncio.fixture
async def server() -> AsyncIterator[FakeEmbyServer]:
    yield FakeEmbyServer(password=PASSWORD)


@pytest_asyncio.fixture
async def app(postgres_url: str, server: FakeEmbyServer) -> AsyncIterator[FastAPI]:
    """A real app against the session-scoped container.

    These routes go through the app's *own* session factory and commit for
    real, so the `session` fixture's transaction-rollback isolation does not
    apply to them -- without the truncate below, one test's sources leak
    into the next and the ordering assertion in
    `test_listing_sources_never_carries_a_credential` fails depending on
    collection order. `TRUNCATE ... CASCADE` also clears
    `source_credentials`, which is the foreign key's whole point.
    """
    engine = build_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE sources CASCADE"))
    await engine.dispose()

    application = create_app(Settings(database_url=postgres_url, secret_key="0" * 32))
    factory = _FakeServerFactory(server)
    application.dependency_overrides[get_source_adapter_factory] = lambda: factory
    try:
        yield application
    finally:
        for client in factory.clients:
            await client.aclose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


def _payload(name: str = "Living Room Emby") -> dict[str, str]:
    return {
        "kind": "emby",
        "name": name,
        "base_url": "https://emby.invalid",
        "username": "usher",
        "password": PASSWORD,
    }


async def test_creating_a_source_returns_it_without_the_credential(
    client: AsyncClient,
) -> None:
    """PRD 08: "Credentials are never returned by any API, including admin.
    Write-only." Asserted against the whole serialized body, not against a
    field list -- a field added later that happens to carry the password
    fails this without anyone having to remember to update it."""
    response = await client.post("/admin/sources", json=_payload())
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Living Room Emby"
    assert body["kind"] == "emby"
    assert PASSWORD not in response.text
    assert "credentials_ref" not in response.text


async def test_the_device_id_is_visible_and_stable(client: AsyncClient) -> None:
    """Not a secret, and genuinely useful: it is how an operator finds
    Usher's session in Emby's own dashboard. Stable across reads is the
    durable-client property, seen from the outside."""
    created = (await client.post("/admin/sources", json=_payload())).json()
    listed = (await client.get("/admin/sources")).json()
    assert created["device_id"]
    assert listed[0]["device_id"] == created["device_id"]


async def test_listing_sources_never_carries_a_credential(client: AsyncClient) -> None:
    await client.post("/admin/sources", json=_payload("Zeta"))
    await client.post("/admin/sources", json=_payload("Alpha"))
    response = await client.get("/admin/sources")
    assert response.status_code == 200
    assert [source["name"] for source in response.json()] == ["Alpha", "Zeta"]
    assert PASSWORD not in response.text


async def test_status_reports_a_healthy_source(client: AsyncClient) -> None:
    created = (await client.post("/admin/sources", json=_payload())).json()
    response = await client.get(f"/admin/sources/{created['id']}/status")
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["authenticated"] is True
    assert body["push_available"] is None
    assert body["server_version"] == SERVER_VERSION


async def test_status_distinguishes_bad_credentials_from_unreachable(
    client: AsyncClient, server: FakeEmbyServer
) -> None:
    """The 🔶 in PRD 07, closed. Both states are 200 with a body an admin UI
    renders -- a bad password is not a server error and must not be a 5xx."""
    created = (await client.post("/admin/sources", json=_payload())).json()
    server.reject_credentials()
    rejected = (await client.get(f"/admin/sources/{created['id']}/status")).json()
    server.offline = True
    unreachable = (await client.get(f"/admin/sources/{created['id']}/status")).json()
    assert (rejected["reachable"], rejected["authenticated"]) == (True, False)
    assert (unreachable["reachable"], unreachable["authenticated"]) == (False, False)


async def test_status_never_leaks_the_credential_into_its_detail(
    client: AsyncClient, server: FakeEmbyServer
) -> None:
    created = (await client.post("/admin/sources", json=_payload())).json()
    server.reject_credentials()
    response = await client.get(f"/admin/sources/{created['id']}/status")
    assert PASSWORD not in response.text


async def test_status_of_an_unknown_source_is_404(client: AsyncClient) -> None:
    response = await client.get(
        "/admin/sources/01936f2a-0000-7000-8000-000000000000/status"
    )
    assert response.status_code == 404


async def test_deleting_a_source_removes_its_credential_row(
    client: AsyncClient, app: FastAPI
) -> None:
    """Not just the 204: the encrypted row must be gone, or a deployment
    accumulates orphaned secrets nothing can attribute."""
    created = (await client.post("/admin/sources", json=_payload())).json()
    assert (await client.delete(f"/admin/sources/{created['id']}")).status_code == 204
    assert (await client.delete(f"/admin/sources/{created['id']}")).status_code == 404

    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with factory() as session:
        remaining = (await session.execute(select(SourceCredentialRow.ref))).scalars().all()
    assert list(remaining) == []


async def test_a_blank_name_is_rejected_before_anything_is_written(
    client: AsyncClient,
) -> None:
    """`Source.name` has `min_length=1` and the table has a CHECK. Catching
    it at the DTO turns a 500 from a constraint violation into a 422 with a
    field name."""
    payload = _payload()
    payload["name"] = ""
    assert (await client.post("/admin/sources", json=payload)).status_code == 422


async def test_the_openapi_schema_has_no_password_in_a_response(
    client: AsyncClient,
) -> None:
    """A generated client is built from this document. A response schema
    that declared a password field would put one in every generated model,
    whether or not the server ever populates it."""
    schema = (await client.get("/openapi.json")).json()
    responses = schema["components"]["schemas"]["SourceResponse"]["properties"]
    assert "password" not in responses
    assert "credentials_ref" not in responses
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/integration/test_admin_sources.py -q`
Expected: FAIL — `ImportError: cannot import name 'get_source_adapter_factory' from 'usher.api.deps'`

- [ ] **Step 3: Write the DTOs**

```python
# src/usher/api/dto/source.py
"""Request and response shapes for the admin source routes.

`api/dto/` types are distinct from `domain/` models (PRD 07): the wire
contract is versioned independently. Here the split earns its keep
immediately -- `SourceResponse` deliberately omits `credentials_ref`, which
`Source` carries and no client has any use for, and `SourceCreateRequest`
carries a `password` that no response type does.

**The password is write-only, structurally.** It appears on the request
model and on no response model, so PRD 08's "credentials are never returned
by any API, including admin" is a property of the type graph rather than of
whoever wrote the handler -- there is no response type with a field to put
one in. Holding it as `SecretStr` closes the second half: FastAPI's own
422 body for a failed validation, and any log line that ever formats the
parsed request, render it as `**********`.
"""

import uuid

from pydantic import AwareDatetime, BaseModel, Field, SecretStr

from usher.domain.enums import SourceKind
from usher.domain.source import Source
from usher.ports.source import SourceStatus


class SourceCreateRequest(BaseModel):
    kind: SourceKind
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: SecretStr


class SourceResponse(BaseModel):
    id: uuid.UUID
    kind: SourceKind
    name: str
    base_url: str
    # Not a secret, and useful: it is how an operator finds Usher's session
    # in Emby's own dashboard in order to revoke it.
    device_id: str
    enabled: bool
    supports_push: bool
    created_at: AwareDatetime

    @classmethod
    def of(cls, source: Source) -> "SourceResponse":
        return cls(
            id=source.id,
            kind=source.kind,
            name=source.name,
            base_url=source.base_url,
            device_id=source.device_id,
            enabled=source.enabled,
            supports_push=source.supports_push,
            created_at=source.created_at,
        )


class SourceStatusResponse(BaseModel):
    """PRD 07's `GET /admin/sources/{id}/status`.

    `push_available` is `bool | None` and `null` means "not probed" -- see
    `SourceStatus`. An admin UI renders that as "unknown", which is the
    honest answer until M5's probe asserts on received messages.
    """

    reachable: bool
    authenticated: bool
    push_available: bool | None
    server_version: str | None
    detail: str | None

    @classmethod
    def of(cls, status: SourceStatus) -> "SourceStatusResponse":
        return cls(
            reachable=status.reachable,
            authenticated=status.authenticated,
            push_available=status.push_available,
            server_version=status.server_version,
            detail=status.detail,
        )
```

- [ ] **Step 4: Wire the dependency and the router**

Append to `src/usher/api/deps.py`:

```python
def get_source_adapter_factory(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SourceAdapterFactory:
    """The composition root's adapter registry.

    Its own dependency, not inlined into `get_source_service`, so a test can
    override exactly this one thing -- pointing the real `EmbyAdapter` at an
    in-memory server -- without also replacing the repository, the
    credential store, or the service.
    """
    return ConfiguredSourceAdapterFactory(
        page_size=settings.source_page_size,
        timeout_seconds=settings.source_timeout_seconds,
        reauth_cooldown_seconds=settings.source_reauth_cooldown_seconds,
    )


def get_source_service(
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
    adapters: Annotated[SourceAdapterFactory, Depends(get_source_adapter_factory)],
) -> SourceService:
    return SourceService(
        PostgresSourceRepository(session),
        PostgresCredentialStore(session, settings.secret_key),
        adapters,
    )


SourceServiceDep = Annotated[SourceService, Depends(get_source_service)]
```

and extend its imports:

```python
from usher.adapters.factory import ConfiguredSourceAdapterFactory
from usher.config import Settings, get_settings
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.source import PostgresSourceRepository
from usher.ports.source import SourceAdapterFactory
from usher.services.sources import SourceService
```

`api/` importing `adapters/` and `db/` is what a composition root is for; the import-linter contracts forbid only `domain`/`ports`/`services` from doing it, and `api` already imports `usher.db.base`.

```python
# src/usher/api/routers/sources.py
"""Admin routes for configured sources (PRD 07).

Four of the five: `POST /admin/sources/{id}/sync` triggers a reconcile, and
there is no reconciler until M5.

`GET /admin/sources/{id}/status` is the endpoint PRD 07's provisional
marker was about. It answers 200 for *every* state a configured source can
be in, including "the credentials are wrong" and "the host is unreachable"
-- those are facts about the source being described, not failures of this
request, and an admin screen has to render them side by side. 404 is
reserved for the one case that really is a failed lookup: no such source.
"""

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from usher.api.deps import SourceServiceDep
from usher.api.dto.source import SourceCreateRequest, SourceResponse, SourceStatusResponse
from usher.ports.credentials import SourceCredentials

router = APIRouter(prefix="/admin/sources", tags=["admin"])


@router.post("", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(
    request: SourceCreateRequest, sources: SourceServiceDep
) -> SourceResponse:
    source = await sources.register(
        kind=request.kind,
        name=request.name,
        base_url=request.base_url,
        # The one place the plaintext exists in this layer: unwrapped
        # straight into the port DTO and never bound to a name that
        # outlives the call.
        credentials=SourceCredentials(username=request.username, password=request.password),
    )
    return SourceResponse.of(source)


@router.get("", response_model=list[SourceResponse])
async def list_sources(sources: SourceServiceDep) -> list[SourceResponse]:
    return [SourceResponse.of(source) for source in await sources.list_sources()]


@router.get("/{source_id}/status", response_model=SourceStatusResponse)
async def source_status(
    source_id: uuid.UUID, sources: SourceServiceDep
) -> SourceStatusResponse:
    result = await sources.status(source_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return SourceStatusResponse.of(result)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: uuid.UUID, sources: SourceServiceDep) -> Response:
    if not await sources.remove(source_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

In `src/usher/api/app.py`, add `sources` to the router import and include it:

```python
from usher.api.routers import health, sources
```

```python
    app.include_router(health.router)
    app.include_router(sources.router)
```

- [ ] **Step 5: Run and watch it pass**

Run: `uv run pytest tests/integration/test_admin_sources.py -q`
Expected: PASS — 10 tests.

Note `SourceCredentials(username=..., password=request.password)` passes the `SecretStr` straight through — the DTO field and the port DTO field are the same type, so no `get_secret_value()` call appears anywhere in `api/`. That is deliberate: the only `get_secret_value()` on this path is inside `PostgresCredentialStore` (to encrypt) and inside `EmbySession` (to authenticate).

- [ ] **Step 6: Check and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest -q
```

```bash
git add -A && git commit -F - <<'EOF'
feat: the admin source routes, and a write-only credential

Four of PRD 07's five admin-source endpoints; the fifth triggers a
reconcile that does not exist until M5. This is what settles PRD 07's
provisional marker on verify(): a status endpoint nothing serves has not
been settled.

GET /admin/sources/{id}/status answers 200 for every state a configured
source can be in, including bad credentials and unreachable -- those are
facts about the source, not failures of the request, and an admin screen
renders them side by side. 404 is reserved for a source that does not
exist.

The password is write-only structurally, not by convention: it exists on
the request model and on no response model, so there is no response type
with a field to put one in. Asserted against whole serialized bodies and
against the OpenAPI document, so a field added later that happened to
carry one fails without anyone remembering to update a list.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Task 12: Documentation, PRD corrections, and live verification

M3 changed enough of PRD 03's and PRD 07's stated behaviour that leaving them alone would make them wrong. Per CLAUDE.md this lands with the change, not after it.

**Files:**
- Modify: `docs/prd/03-sources-and-sync.md`, `docs/prd/07-client-api.md`, `docs/prd/08-operations.md`, `docs/prd/09-roadmap.md`, `docs/prd/README.md`, `CLAUDE.md`
- Create: `scripts/capture_emby_fixture.py`

- [ ] **Step 1: Correct PRD 03**

Seven changes. Each is a place the document was wrong or silent about something M3 had to decide.

1. **The durable-client block never says how the token is presented afterwards.** It shows the pre-auth header and `AuthenticateByName`, then stops. Add, immediately after the fenced block:

```markdown
Authenticated requests carry the **same** identity header — that is what
makes every request attributable to one device rather than only the login —
plus the session token in `X-Emby-Token`. Emby has no OAuth2 and therefore
no refresh-token flow; this pattern *is* the refresh mechanism.
```

2. **"Any 401 triggers silent re-authentication" is silent about the storm.** Extend that bullet:

```markdown
- The token is cached, and **any 401 triggers silent re-authentication**
  with the stored credentials and the same `DeviceId`. That is the refresh
  mechanism; no human ever pastes a token. Re-authentication is
  **single-flight** — concurrent 401s collapse into one
  `AuthenticateByName` — and exactly one retry is attempted per request. A
  credential that is genuinely *wrong* is remembered for a cooldown, so a
  bad password cannot turn every call into two requests against a source
  measured at 1–5 s per request.
```

3. **`credentials_ref` points at nothing in the document.** Replace that bullet:

```markdown
- Credentials live behind `credentials_ref` indirection: an opaque, random
  token addressing a row in `source_credentials`, encrypted at rest under a
  key derived from `USHER_SECRET_KEY` ([08](08-operations.md)). The
  plaintext exists only in memory in the adapter. The ref is random rather
  than derived from the source id so rotation — write the new secret under
  a new ref, flip the pointer, delete the old row — is expressible at all.
```

4. **Add a "Walking the library" subsection** after "Push events", before "Reconciliation is not optional":

```markdown
### Walking the library

`list_items` and `watch_state` page over the source's own listing, one page
in flight at a time — this deployment holds **94,395 movies across 17
libraries**, so materialising a walk is not an option. Three properties the
adapter contract enforces:

- **A stable ascending sort by creation date.** Items added during a walk
  land at the end, so an insertion cannot shift an unread item backwards
  past a page boundary already consumed. A *deletion* mid-walk can still
  shift one item out of view; that is a bounded imprecision the nightly
  full reconcile covers, and it is why the contract permits duplicates but
  forbids silent truncation.
- **The delta cursor is widened by one second.** `since` is contractually
  inclusive; whether the upstream's own comparison is `>=` or `>` is not
  something Usher should have to be right about. Sending one second early
  is correct either way, and a superset is explicitly allowed because
  callers deduplicate by `external_id`.
- **An unrecognised filter degrades to a full walk, never to an empty
  result.** Emby ignores query parameters it does not know, so the worst
  case of a wrong delta-filter name is the nightly reconcile's own
  behaviour.

Two different filters are sent, because a library edit and a watch-state
change do not touch the same timestamp: `MinDateLastSaved` for
`list_items`, `MinDateLastSavedForUser` for `watch_state`.

### Health and status

`verify()` returns a `SourceStatus`, not a bool: `GET
/admin/sources/{id}/status` ([07](07-client-api.md)) has to report bad
credentials, unreachable, and reachable-but-push-blocked as separate
states. The unauthenticated `/System/Info/Public` probe is what separates
the first two — a failure there is a reachability failure and cannot be
anything else.

`push_available` is deliberately three-valued, and `null` ("not probed") is
what every adapter reports until M5. See the health-check caveat above: a
handshake against a nonexistent path also upgrades, so an upgrade is not
evidence and only received messages are.
```

5. **The ingest section implies items are only movies.** In "### 1. Ingest", after the existing paragraph:

```markdown
The adapter emits movies, series, **and episodes** — Emby addresses
episodes directly, and `SourceItem` carries `series_external_id`,
`season_number`, and `episode_number` for exactly this. Persisting the
series hierarchy waits on `Season`/`Episode` domain models and an
`episodes` table, both of which land with the enrich stage in **M4**;
until then episode items are produced and not yet stored.
```

6. **The watch-state section does not say that a write-back is two calls.** Extend the "Outbound" bullet:

```markdown
- **Outbound:** client actions write `WatchState` with `origin = api`, then
  push to the source best-effort. Failure enqueues a retry and never blocks
  the API response. On Emby this is **two calls, not one** — there is no
  endpoint that sets position and played together — and the order is load
  bearing: **position first, played flag last**, because marking an item
  played clears its resume position server-side. The reverse order leaves a
  just-finished film resumable at the last reported second, which is how it
  reappears in Continue Watching. Both writes are idempotent, so the retry
  after a partial failure is safe.
```

7. **The Playback section omits the token.** Extend it:

```markdown
A direct-play target's URL necessarily carries the source's own access
token: Emby authenticates the stream route, and Usher does not proxy bytes.
That is knowingly in tension with [08](08-operations.md)'s "no credential
ever reaches a client" — see
[ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md) for what
the token grants, which half of the original failure this does and does not
fix, and the M9 playback-ticket redirect that removes it.

`StreamTarget` also carries `scheme` (for deep links) and `audio` (a single
composite token such as `truehd_atmos_7_1`, which is a different thing from
the raw codec) — the 🔶 that named M3 is settled.
```

Finally, **delete the `SourceEvent` 🔶 block's M3 relevance by leaving it exactly as it is** — it names M5 and its reasoning still holds. Add one sentence to it: `Reviewed during M3 and deliberately unchanged: M3 builds no push lane, so the measurement this is waiting for is still unavailable.`

- [ ] **Step 2: Correct PRD 07 and PRD 08**

In `docs/prd/07-client-api.md`, delete both 🔶 blocks (the one under the Admin table and the one under Playback) and replace them with settled text.

Under the Admin table:

```markdown
`GET /admin/sources/{id}/status` returns a `SourceStatus` — `reachable`,
`authenticated`, `push_available`, `server_version`, `detail` — with 200
for every state a configured source can be in, including bad credentials
and unreachable. Those are facts about the source, not failures of the
request. 404 is reserved for a source that does not exist.
`push_available` is `bool | null`, and `null` means "not probed": until M5
asserts on *received* push messages, no adapter may claim `true`
([ADR-0004](decisions/0004-push-over-polling.md)).

`POST /admin/sources/{id}/sync` lands with the reconciler in M5.
```

Under Playback, replace the 🔶 with:

```markdown
`StreamTarget` carries `scheme` and `audio`. `audio` is a single composite
token describing the default track as a client thinks about it
(`truehd_atmos_7_1`), which is a different thing from the raw codec —
the codec alone does not tell a client whether it can play the track.

The direct target's URL carries the source's access token, because the
stream route is authenticated and Usher does not proxy bytes. See
[ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md).
```

In `docs/prd/08-operations.md`, the Secrets rules list says "No credential ever reaches a client" without qualification, which is now false. Replace that bullet:

```markdown
- No *stored* credential ever reaches a client — no username, no password,
  no `credentials_ref`, from any endpoint including admin. The one
  deliberate exception is a direct-play URL, which must carry the source's
  own session token or the bytes are not fetchable and Usher does not proxy
  them; that token is minted on demand, never persisted, never logged, and
  revocable as a single device.
  [ADR-0012](decisions/0012-playback-urls-carry-a-source-token.md) records
  the trade and its M9 successor. This is still a real improvement on the
  setup Usher replaces, where a raw Emby token lived in browser-delivered
  dashboard config *with no way to renew it when it died* — that second
  half is fixed completely.
```

Also add a row to the Failure and degradation table, since M3 makes it real:

```markdown
| Source credentials rejected | `GET /admin/sources/{id}/status` reports `authenticated: false`; re-authentication is retried after a cooldown rather than on every call. Catalog unaffected. |
```

- [ ] **Step 3: Add the capture script**

```python
# scripts/capture_emby_fixture.py
"""Re-derive a scrubbed Emby fixture from a live server. NOT a test.

`tests/fixtures/emby/*.json` are shape-recorded and value-synthetic: the
field names, nesting, and types come from real responses, every value is
invented. This script is how an operator regenerates a *scrubbed* capture
locally to check whether their server's shape has drifted from what the
mapper expects. Its output is deliberately not committed -- a real response
embeds TMDb-sourced metadata (which TMDb's terms forbid redistributing and
CLAUDE.md's "ship importers, never data" forbids committing), identifies a
real library, and carries real server and user ids.

    export USHER_EMBY_URL=https://emby.example
    export USHER_EMBY_USER=someone
    export USHER_EMBY_PASSWORD=...
    uv run python scripts/capture_emby_fixture.py > /tmp/shape.json

The output keeps every key and replaces every leaf value with its type
name, so the diff against a committed fixture is a diff of *shape*.
"""

import asyncio
import json
import os
import sys
from typing import Any

import httpx
from pydantic import SecretStr

from usher.adapters.emby.adapter import ITEM_FIELDS, ITEM_TYPES
from usher.adapters.emby.session import EmbySession
from usher.ports.credentials import SourceCredentials


def _shape(value: object) -> Any:
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


async def main() -> int:
    base_url = os.environ.get("USHER_EMBY_URL")
    username = os.environ.get("USHER_EMBY_USER")
    password = os.environ.get("USHER_EMBY_PASSWORD")
    if not base_url or not username or not password:
        print("set USHER_EMBY_URL, USHER_EMBY_USER, USHER_EMBY_PASSWORD", file=sys.stderr)
        return 2
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=60.0) as client:
        session = EmbySession(
            client,
            SourceCredentials(username=username, password=SecretStr(password)),
            source_name="Usher fixture capture",
            device_id="usher-fixture-capture",
        )
        user_id = await session.user_id()
        body = await session.json_body(
            "GET",
            f"/Users/{user_id}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": ITEM_TYPES,
                "Fields": ITEM_FIELDS,
                "Limit": "3",
                "SortBy": "DateCreated",
                "SortOrder": "Ascending",
            },
            op="capture",
        )
    print(json.dumps(_shape(body), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 4: Verify against the live server**

This is the step the fake server cannot substitute for: it is the only thing that catches a wrong-but-self-consistent endpoint path. Run it before calling M3 done, and **record the result in `CLAUDE.md`'s "Verified facts" section** including anything that turned out to be wrong.

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="$(openssl rand -hex 32)"
uv run alembic upgrade head
uv run uvicorn usher.api.app:create_app --factory --port 8000 &

curl -sS -X POST http://localhost:8000/admin/sources \
  -H 'content-type: application/json' \
  -d '{"kind":"emby","name":"Living Room Emby","base_url":"https://emby.example","username":"...","password":"..."}'

# 1. Authentication and reachability.
curl -sS http://localhost:8000/admin/sources/<id>/status
#    Expect reachable/authenticated true, a real server_version,
#    push_available null.

# 2. One device, not many. Check Emby's dashboard -> Devices: exactly one
#    "Usher" entry, and restarting the server does not add a second.

# 3. A wrong password reports, does not crash. Re-register with a bad
#    password and confirm reachable=true, authenticated=false.
```

Then, from a Python shell against the live source (`uv run python`), confirm the four routes the fake server cannot validate:

```python
# list_items: the first page arrives, items carry container/codec/HDR
# get_item: a known id returns, a made-up id returns None (404, not 500)
# stream_targets: the direct URL actually plays -- paste it into a browser
#                 or mpv; a 401 means the api_key parameter name is wrong
# push_watch_state: set a position, confirm it in Emby's UI, mark played,
#                   confirm the resume position cleared
```

**If a path is wrong**, fix the constant in `adapter.py` or `session.py`, fix `FakeEmbyServer`'s matching route independently, and re-run both contract runners.

- [ ] **Step 5: Update the roadmap, the index, and CLAUDE.md**

- `docs/prd/09-roadmap.md`: mark **M3 — Emby adapter** ✅.
- `docs/prd/README.md`: add a row to the Implementation plans table:

```markdown
| [2026-07-30-m3-emby-adapter.md](../plans/2026-07-30-m3-emby-adapter.md) | M3 — Emby adapter (PRD [03](03-sources-and-sync.md)) | ✅ complete |
```

- `docs/prd/decisions/README.md`: index ADR-0012 and ADR-0013.
- `CLAUDE.md`: update the Status paragraph, add the new commands, and add M3's verified facts — **including anything the live run corrected**. Do not restate this plan's predictions as results.

- [ ] **Step 6: Check the links, run everything, and commit**

```bash
python3 - <<'EOF'
import re, pathlib
bad = []
for md in pathlib.Path("docs").rglob("*.md"):
    for link in re.findall(r'\]\(([^)#][^)]*\.md)\)', md.read_text()):
        if not (md.parent / link).resolve().exists():
            bad.append(f"{md}: {link}")
print("\n".join(bad) if bad else "OK")
EOF
uv run ruff format . && uv run ruff check . && uv run mypy && uv run lint-imports && uv run pytest -q
```

```bash
git add -A && git commit -F - <<'EOF'
docs: record M3, and correct PRD 03's under-specified adapter behaviour

Seven corrections to PRD 03, each a place it was silent about something
M3 had to decide: how the session token is presented after authentication
(the document showed the login header and stopped); that re-authentication
is single-flight with a cooldown, not a retry per call; what
credentials_ref actually points at; how the library walk pages and why the
delta cursor is widened; that the adapter emits episodes and that
persisting them is M4's; that a watch-state write-back is two ordered
calls on Emby, not one; and that a direct-play URL carries a token.

PRD 07's two provisional markers are removed and replaced with settled
text. PRD 08's "no credential ever reaches a client" is qualified rather
than left silently false, with ADR-0012 carrying the reasoning.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
```

---

## Definition of done

- [ ] `uv run pytest` passes, unit and integration
- [ ] `uv run mypy` is clean under strict mode, including `tests/`
- [ ] `uv run ruff check .` and `uv run ruff format --check .` are clean — **including no `RUF100`**, which fires on the two `# noqa`s this milestone makes redundant
- [ ] `uv run lint-imports` reports **6 kept, 0 broken** — the sixth is M3's own, encoding the spec's "zero source-specific concepts outside `adapters/emby/`" criterion
- [ ] `alembic upgrade head` → `downgrade base` → `upgrade head` round-trips, and `test_migration_matches_the_orm_metadata` reports no drift against `source_credentials`
- [ ] **The contract suite passes against both implementations**, with the same case count, and `test_both_implementations_run_the_same_assertions` confirms neither overrides a case
- [ ] `uv run pytest tests/unit` still needs no Docker and no network — the load-bearing suite stays in the fast lane
- [ ] **No real Emby payload is committed.** Every file under `tests/fixtures/emby/` is hand-written and obviously synthetic; no server id, user id, filesystem path, or third-party overview appears in the repository
- [ ] `POST /admin/sources` → `GET /admin/sources/{id}/status` works against the **live** Emby server, reporting reachable, authenticated, and a real `server_version`
- [ ] Emby's dashboard shows **exactly one** "Usher" device after several restarts — the durable-client property, observed rather than asserted
- [ ] A **deliberately wrong password** reports `authenticated: false` and does not produce a request per call
- [ ] `list_items` walks the live library and the items carry container, codec, and HDR facts
- [ ] A `stream_targets` direct URL **actually plays** when pasted into a player
- [ ] `push_watch_state` sets a position visible in Emby's own UI, and marking played clears it
- [ ] No response body, log line, span attribute, or error message contains the source password — checked against the live run, not only in tests
- [ ] PRD 03, 07, and 08 carry no 🔶 marker that names M3; the one remaining marker in `usher/ports/source.py` names M5 and says why
- [ ] `docs/prd/README.md` indexes this plan and `docs/prd/09-roadmap.md` marks M3 complete
- [ ] `CLAUDE.md` records what the live run actually found, including anything this plan predicted wrongly

---

## What M3 deliberately does not do

Recorded so M4 and M5 do not re-litigate it.

| Not done | Why | Where it lands |
|---|---|---|
| The WebSocket push listener, heartbeat, and reconnect | Push is *verified working* (ADR-0004), not blocked — it is sequenced. `supports_push` reports `False` and `events()` raises `SourceNotSupported`, which is precisely PRD 03's documented fallback | M5 |
| A push *health* probe | Must assert on received messages, because a handshake against any path upgrades. `SourceStatus.push_available` is `bool \| None` specifically so M3 can say "not probed" rather than guess | M5 |
| `SourceEvent` carrying a payload | Its 🔶 defers to M5 on the grounds that the cost of re-walking is only measurable once the push lane exists. M3 builds no push lane, so the measurement is still unavailable | M5 |
| The reconciler and `POST /admin/sources/{id}/sync` | The endpoint triggers a reconcile that does not exist | M5 |
| A long-lived adapter registry | `SourceService` builds one per call and closes it in a `finally`. The thing that should own a long-lived adapter is the push lane's registry — designing its lifecycle now would be designing for a consumer that has not been written | M5 |
| Ingest, match, enrich, index | M3 produces `SourceItem`s; nothing writes a `MediaItem`, a `Title`, or a `raw_payloads` row | M4 |
| `Season`/`Episode` models and an `episodes` table | The adapter emits fully-formed episode `SourceItem`s; there is nowhere to persist them, and `MediaItem.episode_id` is still a dangling column with no FK target | M4 |
| Persisting `Source.supports_push` from a probe | It is `False` for every source in M3, so writing it back would store a constant | M5 |
| A playback ticket that removes the token from the URL | Needs `POST /titles/{id}/play` and a redirect endpoint beside it, neither of which exists. ADR-0012 names the design | M9 |
| A `USHER_SECRET_KEY` rotation command | `build_cipher` is public so a rotation tool can hold two ciphers at once, but PRD 08's "documented rotation command" is an operator tool with no caller yet | M10 |
| Retry/backoff around `PortRateLimited` | The adapter raises it correctly with the upstream's own hint. A real backoff loop belongs with the job queue, the same call M2 made for its importers | M4 |
| A second `SourceAdapter` implementation | The contract suite is written so one passes unchanged, and `ConfiguredSourceAdapterFactory` is the single registration point. Actually writing Jellyfin is post-v1 | post-v1 |
| Multi-user watch state | `SourceWatchState.source_user_id` is now populated with the authenticated Emby user id, so the data is there. Nothing consumes it: v1 resolves every request to the singleton default user (PRD 01's authentication seam) | post-v1 |
