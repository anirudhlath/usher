# Changelog

All notable changes to Usher are documented here.

The format is [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Versioning is `0.x` while the wire contract may still move —
[README's Versioning section](README.md#versioning) says why, and what to pin.

## [Unreleased]

### Changed

- **The README is a short introduction and quickstart.** Its how-to material
  moved to [`docs/guide/`](docs/guide/): configuration, the command line, and
  building a client.

### Fixed

- **An embedding model that fails the embedder's norm check now fails every
  batch, not only the first.** The first batch used to park one `index` job and
  let every later vector through unchecked.
- **A fused search whose query can't be embedded is served as full text**, and
  says so through `requested_mode` ≠ `mode`, instead of answering 500.

## [0.1.0] - 2026-09-24

The first release. Ten milestones, and there is no earlier one to diff
against — the six tags in this repository are local operational backups and
none was ever published.

### Added

- **A canonical catalog of your own.** Usher imports the IMDb and TMDb bulk
  datasets and crosswalks them through Wikidata, so titles have stable
  identity that does not belong to any media server. Every phase is resumable;
  a 1.27M-title catalog is the size this was built and measured against.
- **Emby as a source, behind a port.** Credentials are encrypted at rest. The
  adapter is the only source-specific code in the tree, and a source-agnostic
  contract suite is what a second adapter would be written against.
- **An ingest pipeline** — match, ingest, reconcile, watch-sync and enrich —
  over a Postgres priority queue, with TMDb as the metadata provider.
- **A push lane over Emby's websocket**, with supervised reconnect and a
  gap-closing delta for what was missed while disconnected, plus `GET /events`
  over SSE so a client sees changes without polling.
- **Search**: full-text over a generated document with a GIN index,
  trigram type-ahead, embeddings, a neighbour table, and RRF fusion across the
  lexical and vector lanes.
  ⚠️ **Typo tolerance was gated and the gate failed** — measured 2026-08-03
  against a real 1,271,138-title catalog. Short names are the weak band and no
  configuration came close to an as-you-type latency budget. The two-tier
  suggest that obligation produced shipped instead.
- **A composed home screen** — nine row providers, a taste centroid derived
  from watch history, and a `GET /home` that paints a screen in one request.
- **LLM-curated rows**, against any OpenAI-compatible endpoint, with a cost
  ledger and a validator that counts why it dropped what it dropped.
  ⚠️ **Query expansion ships off by default because it measured worse** —
  MRR **0.733 → 0.373** and recall@10 **0.800 → 0.533** against a local model
  over five mood queries and 150 real overviews. The rewrites drift toward
  generic critic prose. One model, one corpus; the setting is there when
  somebody re-measures.
- **The HTTP surface**: screens, resources, actions, admin and meta behind one
  RFC 9457 problem envelope, keyset cursors, an image proxy, and playback via
  a ticket. A React console is served at `/console`.
- **Operability** (this milestone): an outbound rate limiter so Usher is a
  polite guest on somebody else's server, a retraction guard so a partial walk
  cannot mark your library unavailable, `usher backup` / `usher restore` /
  `usher rotate-secret`, a scheduler, and OpenTelemetry traces, metrics and
  logs.

### Fixed

- **Real IMDb dataset rows were removed from the working tree** in `1196a9d`
  and `969dcbf`. They had been committed, and a note claimed they were
  synthetic. Three files carried them; the history still does, and
  [PRD 04](docs/prd/04-catalog-bootstrap.md) records the decision not to
  rewrite it and why. `tests/unit/test_no_third_party_data.py` is the control
  that stops it recurring.

### Security

- **There is no authentication on any route.** Not a deferral with a date — an
  unbuilt feature. Do not expose this to the internet without putting
  something in front of it.
- **Playback is a `302` to a URL carrying a grant**, not a proxied byte
  stream. The token rides in the URL because neither a `<video>` element nor a
  deep link can send Emby a header; the short-lived ticket Usher hands out
  redeems to that same URL. That URL is a secret: it is never logged and never
  rendered.

[Unreleased]: https://github.com/anirudhlath/usher/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/anirudhlath/usher/releases/tag/v0.1.0
