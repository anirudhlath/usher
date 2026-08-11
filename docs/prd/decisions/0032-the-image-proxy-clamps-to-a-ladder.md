# ADR-0032 — The image proxy clamps to a ladder of provider rungs, and no decoder is taken

**Status:** Accepted — corrects [02](../02-data-model.md),
[07](../07-client-api.md) and [08](../08-operations.md)

## Context

[02](../02-data-model.md)'s `### Image` sketch said the proxy *"fetches,
resizes, and stores on first request"* and [07](../07-client-api.md) promised
`GET /images/{image_id}?w=&h=&fmt=` — both corrected by this ADR, and quoted
here in the wording that was there. Neither named a mechanism, and the
mechanism is a **dependency decision**: Usher's runtime is fastapi / uvicorn /
pydantic / pydantic-settings / sqlalchemy / asyncpg / alembic / pgvector /
uuid6 / loguru / httpx / websockets / six OTel packages / cryptography, and
**not one of them can decode an image**.

This project has refused a dependency of that shape twice, both times on
measured marginal cost —
[ADR-0022](0022-the-embedder-is-optional-and-its-contract-is-measured.md) for
`sentence-transformers` and
[ADR-0027](0027-the-llm-client-is-one-http-call.md) for `litellm` at **+146 MB
and 29 distributions against +0 and 0** — and both times the deciding fact was
not the megabytes but *what the distributions were*. So the arithmetic is
familiar. What is different here is that there is a live alternative to price
it against: if the provider publishes a size ladder its own CDN serves at
`{base}{rung}{path}`, then a proxy whose ladder is a **subset** of the
provider's needs no decoder in-process at all, and *"resize"* is the wrong word
for what should ship.

**One measured fact a drafting pass got wrong, stated here so this ADR does not
repeat it: Pillow is already in `uv.lock`.** `pillow==12.3.0` resolves as a
transitive of `fastembed`, so it is installed today on every deployment that
runs `uv sync --extra embedding` and on none that does not — verified both ways
below. The honest measurement is therefore a delta against the **default**
install, and the argument is entirely about that one: for an embedding-enabled
deployment the marginal cost of taking Pillow as a hard runtime dependency is
**zero**. A bar written against *"a new distribution"* would be measuring
something that is not new.

## The bar, written before anything was measured

Taking Pillow as a hard runtime dependency requires **both** of the following.
The ceilings are stated as proportions of things this project has already
recorded, so that they are not numbers chosen around an answer:

1. **Marginal cost against a default (no-extra) venv is at or under all four of
   these** — the three axes ADR-0027 used, plus the one it did not have:
   - **venv delta ≤ +25 MB**, a quarter of a default venv;
   - **distributions added ≤ 2**, and *none of them* a second runtime of
     something already present, nor a runtime for a capability the deployment
     may never use — ADR-0022's and ADR-0027's shared deciding fact, restated as
     a bar rather than as a preference;
   - **cumulative `import` ≤ 100 ms**, an order of magnitude below the 1,343 ms
     ADR-0027 called *"the number that would be felt"*;
   - **image delta ≤ +10%** of the **356 MB** recorded in
     `.claude/rules/config-cli-and-deployment.md`, measured with `docker images`
     and not `docker image inspect`, and against a baseline rebuilt the same day.
2. **The provider-ladder arm is shown insufficient** — that is, there is a width
   a real client needs that the provider will not serve.

Above the ceiling, or with arm 2 unmet, it ships as an optional extra with a
stated degradation, or not at all.

## Decision

**No decoder. Pillow is not taken — not as a dependency, not as an extra.**
`pyproject.toml` and `uv.lock` are unchanged by this ADR. The proxy fetches a
provider rung, stores the bytes it was given, and serves them; it never decodes
and never re-encodes. **[02](../02-data-model.md)'s one-line `### Image` sketch
is corrected in the same commit**, because *"resize"* is the word this
measurement refutes and leaving it in one section while striking it from
another is the silent drift `CLAUDE.md` forbids. **The first conjunct of the bar passes on all four axes
and the second one fails**, which is the whole finding: the dependency is
declined for want of a *capability*, not for want of budget.

**`GET /images/{image_id}?w=` clamps to a closed tuple of four widths:**

```python
IMAGE_LADDER = (154, 342, 780, 1280)   # a code constant, not a setting
```

- **Clamp up**, to the smallest rung greater than or equal to the requested
  width; a request above the top rung gets the top rung. **This costs bandwidth
  and the cost is accepted, which is worth stating rather than implying**, since
  bandwidth is half of what the clamp is defended on elsewhere in this document.
  Measured over the same 10 titles per kind: a client asking for 512 px gets
  `w780`, which is **2.0–2.2× the bytes** an exact `w500` would have been
  (poster 232 KB against 105 KB and logo 129 KB against 65 KB, both 2.2× and
  2.0×; backdrop 75 KB against 34 KB, 2.2×). The worst case on this ladder is a
  request of 343, which gets `w780` at **4.3×** what `w342` would have cost for
  a poster — 4.1× for a backdrop, 3.4× for a logo. Down-clamping
  reverses that and is worse: it answers a 780-px card with a 342-px image,
  which is a visible softness on every device rather than an invisible one on
  fast links — and the party who pays is the user looking at it, not the
  operator's bill. The ladder bounds the cache either way, so the choice is
  purely which error to make, and this one is recoverable by asking for the next
  rung up.
- **`w` absent → `342`**, the row-card rung. A default that is already on the
  ladder creates no fifth cache entry, and the row card is the surface both of
  M9's two artwork consumers paint.
- **`w` non-integer or `≤ 0` → 422**, which FastAPI's own validation gives for
  free.
- **Four rungs, and the reason for each end.** Bottom **154**: a type-ahead or
  search-result thumbnail at 77 CSS px on a 2× display. Below it the provider
  publishes `w92` and `w45`, which are logo-scale — a poster at `w92` has a
  median of **6,366 bytes** over the 10-title sample in the table below, and is
  smaller than any card in a `portrait` rail. Top **1280**: the largest card
  any consumer [00](../00-overview.md) names actually paints is the Home
  Assistant panel's full-bleed hero, 640 CSS px at 2×, and `w1280` is
  independently the largest backdrop width the provider publishes. Two arguments
  landing on the same rung. `342` is the row card at 2× and `780` is the detail
  poster and the same card at 3×.
- **Every rung is one the provider publishes for at least one kind**, and all
  four were measured to serve all three kinds M9 emits. `w200`, `w400`, `w1920`
  and `h100` are served and are published for **no** kind; they are deliberately
  not on the ladder, because a rung resting on undocumented behaviour is one the
  provider can withdraw without changing its own contract.

**No `original` rung**, on the wire or on the fetch. It is not expressible
through `?w=<int>`, and the fetch adapter must never request it either.
🔴 **The ratio this rested on was wrong until 2026-08-11** — it read *"4× to
12× the top rung"* against *"232 KB at the top rung"*, and 232 KB is `w780`,
one rung below the 1280 this document defines as the top. Recomputed per kind,
median `original` ÷ median `w1280` over 10 titles each:

| kind | `w1280` median | `original` median | ratio | `original` max |
|---|---|---|---|---|
| poster | 563,378 | 864,021 | **1.53×** | 2,200,704 |
| backdrop | 179,356 | 1,459,462 | **8.14×** | 2,456,955 |
| logo | 273,344 | 1,387,107 | **5.07×** | 4,731,805 |

**So there is no single ratio, and the poster figure is the one that argues
against this decision**: a poster's original is only half again its `w1280`,
because a poster is rarely much wider than 1280 to begin with. What carries all
three kinds is not the ratio but the **absence of a bound**: `original` is
whatever the provider happened to store, from 173 KB to 4.7 MB with no ceiling
the API can state, while every rung on the ladder is bounded by its own width.
Against the **default** rung a client actually receives — `w342` — the same
originals are **16×**, **80×** and **36×**. A clamp whose top entry has no
stated maximum is not a clamp, on a disk cache PRD 02 already prices at ~120 GB
if artwork is mirrored.

**`fmt=` is refused, and [07](../07-client-api.md) is corrected in the same
commit.** A provider-ladder proxy cannot honour it — measured, the CDN will
give you WebP and will *not* transcode to a format it does not hold — and a
query parameter is the wrong mechanism for a thing HTTP already negotiates. The
proxy asks the provider with no format preference and stores exactly the bytes
and the `Content-Type` it got: one cache entry per `(image id, rung)`. **The
named additive successor is `Accept`**, not `fmt=`, and it is priced in Evidence
rather than hand-waved.

**`h=` is refused too**, and for its own reason: the provider publishes exactly
one height rung, `h632`, and only for `profile` — a kind M9 does not emit. Two
heights is not a ladder, and artwork aspect ratio is fixed by kind (2:3 poster,
16:9 backdrop), so a height is a width divided by a constant the client already
knows.

**The ladder is a code constant**, and [08](../08-operations.md)'s
Configuration table cell naming *"image cache ladder"* as a TOML-layer concern
is corrected to say so. There is no TOML layer, and a setting nothing reads is
dead config wearing a control's name — the same rule that document already
applies to a typo, applied to a knob.

**`Cache-Control: immutable` is honest only if an image id survives
re-derivation, and 🔴 as shipped it does not.** This ADR claimed until
2026-08-11 that the id was stable *"because"* of a unique key over
`(title_id, provider, provider_path)` *"requested of `m09a`"*. **`m09a` had
already merged** — commit `1bd94c2`, before this ADR — and it carries no such
key. Verified in `src/usher/db/migrations/versions/m09a_api_surface_tables.py`
and `src/usher/db/models/image.py`: `images` has a primary key on `id`, **five**
CHECK constraints (`ck_images_exactly_one_owner`, `..._provider_not_empty`,
`..._remote_url_not_empty`, `..._width_positive`, `..._height_positive`), three
**non-unique** indexes on the owner columns, and **no unique constraint of any
kind**. The column is **`remote_url`**, not `provider_path`, and there is no
`sort_order`.

So the two are still one argument, and the honest statement of it is a
**dependency, not a consequence**: the header this ADR specifies is
**conditional on a key that does not exist yet**. Without it, every
`usher derive` re-run mints fresh UUIDv7s for unchanged artwork, every client's
cached reference is invalidated, and `immutable` becomes a lie the first time a
title is re-derived. **Until the key lands, `GET /images/{id}` must not send
`Cache-Control: immutable`** — a long `max-age` without `immutable` is the
honest interim, because it lets a client revalidate.

### The request: `m09c`, for C2 to mint

`m09a`'s own docstring says *"`m09c` is spare and must be requested, never
minted"*, and this is the request. It is written out because the spelling is
**not** the obvious one and the obvious one fails silently — measured below.

1. **The key, spelled `NULLS NOT DISTINCT` over the whole owner triple:**

   ```sql
   ALTER TABLE images ADD CONSTRAINT uq_images_owner_provider_path
       UNIQUE NULLS NOT DISTINCT (title_id, episode_id, person_id, provider, <path column>);
   ```

   **Not** `UNIQUE (title_id, provider, <path>)`, which is what the plan's
   wording invites and what a reviewer would wave through. Postgres defaults to
   `NULLS DISTINCT`, so on a table whose owner is one of three nullable columns
   that constraint is **inert for two of the three owner kinds** — an
   episode-owned or person-owned duplicate has `title_id IS NULL` and never
   conflicts. Measured on `pgvector/pgvector:pg17` (PostgreSQL 17.10), the
   version this project deploys: the obvious spelling admitted **2** rows where
   1 was correct, and the `NULLS NOT DISTINCT` spelling refused it. `NULLS NOT
   DISTINCT` needs PostgreSQL ≥ 15 and is therefore available; the fallback on an
   older server would be three partial unique indexes, which is three objects and
   an owner-kind-specific `ON CONFLICT` predicate in the writer.
2. **The write becomes an upsert on that constraint**, which is the whole point:
   `ON CONFLICT (title_id, episode_id, person_id, provider, <path>) DO UPDATE`
   infers the constraint and **returns the id the row was first inserted with** —
   demonstrated below, same UUID before and after. That is the property the
   header depends on, and it is a property of the *write*, not of the table.
3. **A path column is a separate, smaller request, and it is this ADR's
   mechanism that wants it.** The ladder is `{base}{rung}{path}`, so with a full
   `remote_url` stored, selecting a rung means finding and replacing the
   `/t/p/{size}` segment of a URL this project did not mint — string surgery on
   somebody else's URL, on every request. A `provider_path` column makes rung
   selection concatenation. If C2 prefers to keep `remote_url` and parse, the key
   above works unchanged over `remote_url`; the cost is that parse, plus a CDN
   base change becoming a data migration across 1.27M titles. **Either is
   implementable; only the first is cheap, and this ADR does not decide it for
   C2.**
4. **`sort_order` is out of scope here** — it is the read-order requirement from
   group C's preamble, it belongs to whoever reads images rather than to the
   proxy, and bundling it into this request would hide it.

If the header is ever softened, the key stops being load-bearing; if the key is
never built, the header must not ship.

## Consequences

**Gained:**

- **Zero new distributions and zero new megabytes**, on a project whose image is
  356 MB and which has now declined a dependency of this shape three times.
- **No image-decoding surface in an internet-facing process.**
  [08](../08-operations.md)'s deployment shape puts this service behind a
  reverse proxy on the open internet, and the 19 MB Pillow would have added is
  6 MB of Python and C extension plus **14 MB of eighteen vendored C libraries**
  — libjpeg, libtiff, libwebp, libavif, libopenjp2, libfreetype, libharfbuzz and
  eleven more. Decoding attacker-influenced bytes is the classic shape of a
  memory-safety CVE, and here it would have been bought for a resize the
  provider already performs. This is a *consequence*, not the argument: the
  argument is arm 2 of the bar.
- **A fetch is one GET and a write.** No decode, no re-encode, no orientation
  handling, no colour-profile decision, no bomb guard — the four things a
  resizing proxy has to get right and the four places its CVEs live.
- **The cache is bounded by construction at four entries per image**, and the
  bound is a tuple in `src/`, so it is reviewable and it is the same on every
  deployment.

**Given up:**

- **Arbitrary widths.** A client that wants 512 px gets 780 and scales down. On
  a 2× display that is invisible; on a bandwidth-constrained one it is 232 KB
  where an exact `w500` would have been 105 KB — both poster figures, measured
  over the same sample. (This line compared a poster against a *backdrop* until
  2026-08-11, which flattered the ladder by quoting 74 KB.)
- **A 4K full-bleed backdrop at native width.** The top rung is 1280, so a
  3840-wide hero is upscaled. A decoder would not fix this either — it could
  only downscale from `original`, which is a median 1.46 MB and is frequently
  1920 wide to begin with. Recorded as a real limit rather than argued away.
- **WebP.** Clients get JPEG (or PNG for logos) and pay 1.5× the bytes for it,
  until the `Accept` successor is built. Priced in Evidence.
- **Provider independence.** This decision is a bet on the one
  `MetadataProvider` this project has. See Uncertainty.

**Also:**

- **The proxy is still worth building, and `fmt=`'s removal does not weaken
  it.** [07](../07-client-api.md)'s actual promise is that *"clients never see
  provider image URLs and never need a provider key"*, and that is unchanged: an
  Usher image id, a stable URL, a cache Usher owns, and no TMDb key in a
  frontend — which is precisely the Home Assistant failure
  [00](../00-overview.md) names as a reason this project exists.
- **`GET /images/{id}` is now a route with no new dependency at all**, which
  makes it the cheapest of M9's routes rather than the one that changes the
  release artifact. The spec said this task was the one that could; it turns out
  it is the one that decided not to.

**Rejected:**

- **Pillow as a hard runtime dependency.** +19 MB, 1 distribution, ~16 ms. It
  clears every ceiling in the bar and is still declined, because arm 2 fails.
  It is worth saying plainly that this is a *different* rejection from
  ADR-0022's and ADR-0027's: those two were too expensive, and this one is
  merely unnecessary.
- **Pillow behind an optional extra**, as `fastembed` is. An extra is right when
  a capability degrades gracefully without it
  ([ADR-0022](0022-the-embedder-is-optional-and-its-contract-is-measured.md):
  search narrows to full-text and trigram). Here it would mean two proxies with
  two ladders and two cache-key spaces, and an `?w=` whose answer depends on how
  the operator installed the service — which is the shape ADR-0028 calls a bound
  the deployment cannot keep.
- **`pyvips` / `libvips`.** Faster than Pillow and the usual recommendation for
  a thumbnailing service, and it is a system package: it would put an `apt-get`
  into a Dockerfile whose own comment records that **no dependency in `uv.lock`
  has ever needed a compiler or a system library**, and it would make the
  container the only place the code can run. Not measured, because arm 2 already
  settles it.
- **Passing the provider's rung vocabulary through to clients.** The CDN serves
  fifteen rungs; exposing them would couple Usher's API to a provider's
  vocabulary — the thing [01](../01-architecture.md)'s no-source-concept rule
  exists to prevent, arriving through a provider instead of a source — and would
  quadruple the cache surface for widths nobody paints.

## Evidence

**The refutation first, because it is the opposite of what
[07](../07-client-api.md) assumed.** That section says widths are clamped *"so
the cache can't be trivially blown up by arbitrary dimensions"*, which presumes
the provider serves arbitrary dimensions. **It does not.** Measured
2026-08-11 against the live CDN, 47 candidate rungs on both a poster and a
backdrop:

| | rungs |
|---|---|
| served, HTTP 200, on **both** kinds (15) | `w45 w92 w154 w185 w200 w300 w342 w400 w500 w780 w1280 w1920 h100 h632 original` |
| refused, **HTTP 400**, on both (32 tested) | `w66 w90 w94 w100 w128 w138 w220 w235 w250 w276 w320 w355 w375 w396 w440 w454 w533 w600 w632 w640 w710 w800 w1000 w1024 w1066 w1400 w2000 h150 h300 h450 h750 h900` |
| served but published for **no** kind (4) | `w200 w400 w1920 h100` |
| divergent between poster and backdrop | **none** |

`w0`, `wibble`, `W500` and `w500x` are all HTTP 400 as well. So the allowlist is
**closed, global across kinds, and enforced by the provider** — and it is a
strict superset of what `/configuration` publishes per kind, which is why
`w1280` serves on a poster and `w342` on a backdrop although neither is
published there. Two consequences: the hazard PRD 07 names is already bounded
upstream at fifteen entries, and Usher's clamp is therefore about *Usher's* disk
and *Usher's* vocabulary, not about making a finite space finite. PRD 07's
sentence is corrected accordingly.

**The provider's real ladder, read once from the live `/configuration` endpoint
on 2026-08-11 (HTTP 200), from a throwaway script outside the working tree
reading the operator's own secrets file.** No key, host or token reached the
repository.

| kind | published sizes |
|---|---|
| `secure_base_url` | `https://image.tmdb.org/t/p/` |
| backdrop | `w300 w780 w1280 original` |
| logo | `w45 w92 w154 w185 w300 w500 original` |
| poster | `w92 w154 w185 w342 w500 w780 original` |
| profile | `w45 w185 h632 original` |
| still | `w92 w185 w300 original` |

**No width is published for all three kinds M9 emits** — poster ∩ backdrop is
`{w780}` and adding logo leaves nothing — which is why the shipped ladder rests
on the measured global allowlist for kind coverage while drawing every rung from
the published union. Every rung served **10/10** in all three kinds — the logo
sample was 6 until 2026-08-11 and is now the same size as the other two, rather
than a thinner number reported in the same voice. Median `Content-Length` in
bytes, with the two rungs this document cites but does not ship (`w92`, `w500`)
included so that every figure quoted above has a row:

| rung | poster | backdrop | logo | on the ladder |
|---|---|---|---|---|
| `w92` | 6,366 | 2,603 | 5,989 | no — below the bottom rung |
| `w154` | 14,218 | 5,394 | 12,661 | **yes** |
| `w342` | 53,940 | 18,226 | 38,401 | **yes** (the default) |
| `w500` | 104,993 | 34,491 | 65,302 | no — cited for the clamp-up cost |
| `w780` | 232,311 | 74,871 | 128,922 | **yes** |
| `w1280` | 563,378 | 179,356 | 273,344 | **yes** (the top) |
| *`original`* | *864,021* | *1,459,462* | *1,387,107* | **never** |

**`original` over a wider sample (20 titles from `/movie/popular`):** poster
min 267,320, median 954,088, max 2,200,704; backdrop min 172,815, median
1,129,342, max 2,456,955; the largest logo original measured is 4,731,805.
⚠️ *"A provider's original backdrop is multi-megabyte"* is true of the tail and
not of the median — the median is ~1.1 MB. 🔴 **And the replacement claim was
wrong too**: this line read *"4× to 12× the top rung"* until 2026-08-11, which
reconciles with no consistent pairing of kind and statistic in the table above.
The per-kind ratios are **1.53× / 8.14× / 5.07×** (poster / backdrop / logo,
median `original` ÷ median `w1280`) and the argument that survives all three is
stated in the Decision: `original` is the one rung with **no width bound at
all**, ranging 173 KB to 4.7 MB across this sample, which is what a clamp exists
to remove.

**The id-stability key, measured rather than specified from the documentation**,
on a throwaway `pgvector/pgvector:pg17` container (PostgreSQL **17.10**) with
`images`' three nullable owner columns and its `num_nonnulls(...) = 1` CHECK
reproduced and its foreign keys omitted:

| spelling | title-owned duplicate | person-owned duplicate (`title_id IS NULL`) |
|---|---|---|
| `UNIQUE (title_id, provider, path)` | rejected ✅ | **admitted — 2 rows where 1 is correct** 🔴 |
| `UNIQUE NULLS NOT DISTINCT (title_id, episode_id, person_id, provider, path)` | rejected ✅ | rejected ✅ |

The second also correctly **admits** two different titles sharing one path (2
rows for `/x.jpg`, which is right — the same artwork can be referenced by two
titles), so it is not merely stricter. And the upsert behaves as the header
needs: `ON CONFLICT (title_id, episode_id, person_id, provider, path) DO UPDATE`
inferred the constraint and returned `a6517e9c-1f09-41b3-8f65-06c43f404d80`
both before and after the re-derive — **the same id, which is the entire
property `immutable` rests on.** `pg_get_constraintdef` reports it back as
`UNIQUE NULLS NOT DISTINCT (...)`, so it survives a schema dump.

This is the careless-spelling-versus-careful-spelling case from `CLAUDE.md`, in
DDL: the wrong version passes review, passes a test that only ever inserts
title-owned rows, and is silently inert for the two owner kinds M9 does not
write yet — which is precisely when nobody notices.

**The dependency price, measured 2026-08-11 on this host, Python 3.13.14,
against a default no-extra venv** (`uv sync --frozen --no-dev`: 105 MB, **61
third-party distributions installed** plus `usher` itself — the lock exports 63,
two of which, `colorama` and `win32-setctime`, are Windows-only markers that
install nowhere on this platform). `import` is a subprocess wall-clock median of
11 runs minus a bare interpreter at 11.1 ms — ADR-0027's method, cross-checked
against `python -X importtime`, whose cumulative for `PIL.Image` is
15.1–18.5 ms:

| option | venv after | marginal | distributions | `import` (cumulative) |
|---|---|---|---|---|
| Pillow 12.3.0 | 124 MB | **+19 MB** | **1** | **+16 ms** |
| **the provider ladder — ships** | 105 MB | **+0 MB** | **0** | **+0 ms** |
| *(reference: `httpx`, already shipped)* | — | — | — | *+46 ms* |

**The one distribution is self-contained, which is the sentence ADR-0027 asks
for.** Pillow pulls nothing: no second HTTP stack, no model downloader, no
tokenizer, no GPU runtime — the three groups that decided ADR-0022 and ADR-0027.
What it does bring is **18 vendored C libraries** in `pillow.libs` (libavif,
libbrotlicommon, libbrotlidec, libfreetype, libharfbuzz, libjpeg, liblcms2,
liblzma, libopenjp2, libpng16, libsharpyuv, libtiff, libwebp, libwebpdemux,
libwebpmux, libXau, libxcb, libzstd) totalling **14 MB against 6 MB of Python
and C extension**. That would make Pillow the **largest single distribution in
the image** — the current top five are `grpc` 17 MB, `uvloop` 16 MB,
`cryptography` 14 MB, `sqlalchemy` 13 MB, `asyncpg` 13 MB. It needs no compiler
(a prebuilt `cp313` manylinux wheel), so the Dockerfile's standing claim holds.

**The embedding-enabled row is reported separately, because averaging the two
hides the only interesting number.** `uv export --frozen --no-dev` resolves 63
distributions and **no `pillow`**; `uv export --frozen --no-dev --extra
embedding` resolves 76 and **`pillow==12.3.0`** among them. Confirmed against
two real venvs on this host rather than read off the lock: the default one has
no `PIL/`, and a `--extra embedding` one has both `PIL/` and
`pillow-12.3.0.dist-info`.

| deployment | venv delta | distributions added | `import` |
|---|---|---|---|
| default (`uv sync`) | +19 MB | 1 | +16 ms |
| embedding (`uv sync --extra embedding`) | **+0 MB** | **0** | **+0 ms** |

**The image delta, measured with `docker images` and not `docker image
inspect`** — that field is the compressed content size on this host's containerd
snapshotter and understates by ~4.2×. Both images built 2026-08-11 from the same
Dockerfile, the second from a throwaway copy of this tree outside the working
directory carrying `pillow>=12` in `[project.dependencies]`:

| image | `docker images` SIZE | content size |
|---|---|---|
| this tree, unchanged | **358 MB** | 84.6 MB |
| the same tree + Pillow | **387 MB** | 92.3 MB |

**+29 MB, +8.1%** — inside the +10% ceiling, and larger than the venv's +19 MB
because `UV_COMPILE_BYTECODE=1` writes `.pyc` for Pillow's Python layer too. The
recorded baseline is 356 MB from 2026-08-03; 358 MB is the like-for-like number
on today's lock, so five milestones plus M9's four tables have cost 2 MB.

**Taking it would have been two lines.** For the record, since a future reader
may want to reverse this: `uv add "pillow>=12"` moves one entry into
`[project.dependencies]` and adds two lines to `uv.lock` — `pillow` joins
`usher`'s dependency list and its `[package.metadata]` requires-dist. The
package node already exists in the lock. Neither file is changed by this ADR.

🔴 **`fmt=` cannot be honoured, and the first reading of why was wrong.** An
early probe saw `image/webp` at one backdrop rung and `image/jpeg` at every
other rung of the same source image, which reads as *the provider decides the
format per rung*. It is not that. Re-measured directly over 8 titles × 6 rungs ×
2 kinds: **every rung of every kind returns `image/jpeg` under `Accept: */*`**,
and the format is decided by **content negotiation**:

| `Accept:` | served |
|---|---|
| `*/*` | `image/jpeg` |
| `image/webp` | `image/webp` |
| `image/avif,image/webp` | `image/webp` |
| `image/png` | `image/jpeg` |

So the CDN *will* re-encode to WebP and *will not* transcode to a format it does
not hold — which is exactly the shape of the refusal. `fmt=jpeg|webp` would be a
query parameter duplicating an `Accept` header, and `fmt=` anything else would
need the decoder this ADR declines. **The successor is priced rather than
hand-waved:** at the same rung over 10 titles, WebP is **68%** of JPEG for a
`w342` poster (36,496 against 53,940 bytes) and **62%** for a `w780` backdrop
(46,117 against 74,871). Worth building; strictly additive; costs a
`Vary: Accept`, a second cache axis and a `?w=`-and-`Accept` cache key. Not this
milestone's, and no PRD sentence promises it.

## Uncertainty

⚠️ **This is one provider, and arm 2 of the bar is a statement about that
provider.** [ADR-0016](0016-raw-payloads-cache-providers-not-sources.md) makes
`raw_payloads` a provider cache and the only `MetadataProvider` is TMDb, so
"the provider serves every rung a client needs" is currently a fact and
structurally a bet. **What reopens this ADR is a named event, not a mood:** a
second `MetadataProvider` (or artwork sourced from Emby, which
[ADR-0016](0016-raw-payloads-cache-providers-not-sources.md) currently excludes)
whose CDN publishes no ladder. At that point arm 2 fails, the price table above
is still valid, and the answer is a decoder — behind the port, for that adapter,
measured again against whatever the venv is then.

⚠️ **The four rungs are the softest thing in this document.**
[07](../07-client-api.md) records that a card carries *"no column count, no card
width"* on purpose, so no client's real geometry is written down anywhere in
this repository, and the justifications above are reasoned from DPR arithmetic
and from the one consumer whose surface is known. A fifth rung is additive and
costs a tuple entry; **removing** one is not, because a rung that has been
served under `Cache-Control: immutable` has clients holding its URL.

⚠️ **Four of the fifteen served rungs are published for no kind, and the four on
the ladder are published for only one or two each.** `/configuration` is TMDb's
contract; the global allowlist is observed behaviour on one day. A rung
withdrawn from a kind arrives as an HTTP 400 on fetch, which by M4's taxonomy is
`PortDataMalformed` and therefore a parked job rather than a retry storm — the
right failure, and still a failure. A periodic re-read of `/configuration` would
catch a narrowing before a client did; nothing does that today.

🔴 **One decision here is unmet rather than uncertain, and it is the one a
reader is most likely to assume is done.** `Cache-Control: immutable` has no key
underneath it: `m09a` merged without one, the request to C2 is written out in
the Decision as `m09c`, and **until it lands the header must not ship**. This
is the only part of this ADR that another task has to build before the ADR is
true of the running system, which is why it is repeated here rather than left in
the Decision alone. The failure it prevents is silent — a re-derive invalidating
every client's artwork cache, visible only as artwork that reloads for no
reason.

⚠️ **No live end-to-end run stands behind the proxy itself**, because the proxy
is not built here — C4 builds its ports and adapters and C5 puts it on the wire.
Everything above is measured against the provider, against a real PostgreSQL 17
and against the build, which is what a dependency decision needs and is not the
same as a working route.
