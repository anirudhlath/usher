# ADR-0032 — The image proxy clamps to a ladder of provider rungs, and no decoder is taken

**Status:** Accepted — corrects [07](../07-client-api.md) and
[08](../08-operations.md)

## Context

[02](../02-data-model.md)'s `### Image` sketch says the proxy *"fetches,
resizes, and stores on first request"* and [07](../07-client-api.md) promises
`GET /images/{image_id}?w=&h=&fmt=`. Neither names a mechanism, and the
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
and never re-encodes. **The first conjunct of the bar passes on all four axes
and the second one fails**, which is the whole finding: the dependency is
declined for want of a *capability*, not for want of budget.

**`GET /images/{image_id}?w=` clamps to a closed tuple of four widths:**

```python
IMAGE_LADDER = (154, 342, 780, 1280)   # a code constant, not a setting
```

- **Clamp up**, to the smallest rung greater than or equal to the requested
  width; a request above the top rung gets the top rung. Up rather than down
  because a down-clamp answers a request for detail with something blurrier,
  and the ladder is what bounds the cache either way.
- **`w` absent → `342`**, the row-card rung. A default that is already on the
  ladder creates no fifth cache entry, and the row card is the surface both of
  M9's two artwork consumers paint.
- **`w` non-integer or `≤ 0` → 422**, which FastAPI's own validation gives for
  free.
- **Four rungs, and the reason for each end.** Bottom **154**: a type-ahead or
  search-result thumbnail at 77 CSS px on a 2× display. Below it the provider
  publishes `w92` and `w45`, which are logo-scale — a poster at `w92` is 5.6 KB
  and smaller than any card in a `portrait` rail. Top **1280**: the largest card
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
through `?w=<int>`, and the fetch adapter must never request it either: a
provider's original is a median **0.86–1.46 MB** and a measured maximum of
**4.7 MB**, against 232 KB at the top rung — 4× to 12× the largest thing a
client can ask for, per image, on a disk cache PRD 02 already prices at ~120 GB
if artwork is mirrored. Serving it is the disk-and-bandwidth hazard the clamp
exists to prevent.

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

**`Cache-Control: immutable` is honest only because an image id survives
re-derivation**, and that is the unique key over `(title_id, provider,
provider_path)` requested of `m09a`. The two are **one decision** and are
recorded together so that a later reader cannot relax one without seeing the
other: without the key, every `usher derive` re-run mints fresh UUIDv7s, every
client's cached artwork reference is invalidated, and the immutability promise
becomes a lie the first time a title is re-derived. If the header is ever
softened, the key stops being load-bearing; if the key is ever dropped, the
header must go with it.

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
  where 74 KB would do.
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
the published union. All four rungs served **10/10 posters, 10/10 backdrops and
6/6 logos**, median `Content-Length` in bytes:

| rung | poster | backdrop | logo |
|---|---|---|---|
| `w154` | 14,467 | 5,394 | 10,184 |
| `w342` | 53,940 | 18,226 | 31,918 |
| `w780` | 232,311 | 74,871 | 114,146 |
| `w1280` | 563,378 | 179,356 | 257,566 |
| *`original`* | *864,021* | *1,459,462* | *1,313,395* |

**`original` over a wider sample (20 titles from `/movie/popular`):** poster
min 267,320, median 954,088, max 2,200,704; backdrop min 172,815, median
1,129,342, max 2,456,955; the largest logo original measured is 4,731,805.
⚠️ *"A provider's original backdrop is multi-megabyte"* is true of the tail and
not of the median — the median is ~1.1 MB — and the honest statement of the
hazard is the **ratio**: 4× to 12× the top rung, per image, forever.

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

⚠️ **No live end-to-end run stands behind the proxy itself**, because the proxy
is not built here — C4 builds its ports and adapters and C5 puts it on the wire.
Everything above is measured against the provider and against the build, which
is what a dependency decision needs and is not the same as a working route.
