# Changelog

All notable changes to Usher are documented here.

The format is [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Versioning is `0.x` while the wire contract may still move —
[README's Versioning section](README.md#versioning) says why, and what to pin.

## [Unreleased]

### Added

- Listing requests to a source are capped at `USHER_SYNC_WALKERS` (default 4)
  and back off on their own: an outage or a 429 drops the cap to one,
  and every ten pages that succeed raise it by one. The gauge
  `usher.source.listing.concurrency` shows the cap.
- A whole-library walk — a full sync, or a source's first delta — walks a plan:
  what the account is watching, then each library's movies and series, then its
  episodes in chunks of `USHER_SYNC_UNIT_MAX_ITEMS` (default 100,000).
  `USHER_SYNC_WALKERS` units are fetched at once while one writer commits them;
  each unit's position is kept in `sync_run_units`, and the run's
  `heartbeat_at` shows the writer is alive.
- A whole-library walk that fails or is killed resumes where it stopped: the
  same run, each unit from the position it committed. While one is alive — its
  heartbeat under 10 minutes old — a second is refused, and `usher sync` exits
  non-zero. A watch-state walk started while another is alive runs beside it
  instead of closing or resuming the other's run.
- The watch lane runs as soon as a whole-library walk has stored what the
  account is watching — played, in progress and next up — so a household's own
  shelves fill long before the walk ends. It still runs after the walk.

### Changed

- **`USHER_SOURCE_PAGE_SIZE` defaults to 1,000, up from 200.** A page of 1,000
  costs a source little more than a page of 200, so a walk makes a fifth of the
  requests. A `.env` copied from an earlier `.env.example` still sets
  `USHER_SOURCE_PAGE_SIZE=200`; change that line or delete it.
- **A walk asks for its next page while it writes the current one**, so the
  source and the database work at once. One page is read ahead, and it is
  cancelled when the walk stops.
- **A library listing page may take 120 s**, where every other request still
  gets `USHER_SOURCE_TIMEOUT_SECONDS`; a timeout set longer than 120 s applies
  to listing pages too. Deep pages of a large library outlast 30 s, and asking
  again early only queued a second copy of the slowest query. At the defaults,
  a source that accepts connections and then stalls now holds a walk about 20
  minutes before it fails, where it was about 11.
- **A source's first watch-state walk asks only for what was watched.** It lists
  played items, then in-progress ones: a few requests, where it used to walk the
  whole library, which on a million-item library took most of a day. If most of
  the played listing reads unwatched, the server is taken to ignore the filter,
  and the walk lists once and logs a WARNING.
- **An unfinished first watch-state walk starts again rather than resuming.**
  Its position counted the old whole-library walk, so resuming it would skip
  every played item that is not also in progress; its row is closed `failed`
  as superseded.
- **`USHER_PUSH_STALE_AFTER_SECONDS` defaults to 300, up from 90.** An idle
  library's push channel routinely went longer than 90 s between messages, and
  every such gap cost a reconnect. `usher push --probe` listens for one window,
  so it now takes five minutes. A `.env` copied from an earlier `.env.example`
  still sets `USHER_PUSH_STALE_AFTER_SECONDS=90`; change that line or delete it.
- **The README is a short introduction and quickstart.** Its how-to material
  moved to [`docs/guide/`](docs/guide/): configuration, the command line, and
  building a client.

### Fixed

- **Items deleted from the source mid-walk no longer hide others from the
  walk**, unless more of them vanish between two pages than a page re-reads.
  Each page re-reads the end of the page before, so a full walk no longer marks
  a file that is still there unavailable.
- **An embedding model that fails the embedder's norm check now fails every
  batch, not only the first.** The first batch used to park one `index` job and
  let every later vector through unchecked.
- **A fused search whose query can't be embedded is served as full text**, and
  says so through `requested_mode` ≠ `mode`, instead of answering 500.
- **An out-of-range number from Emby no longer ends a sync.** A runtime of 111
  years used to crash the whole walk and leave its run `running`, and so did a
  TMDb or TVDB id too large for its column. A negative width, runtime, size or
  episode number marked the run failed on every sync. Such a value is now
  recorded as unknown and logged, and such a provider id is ignored. A watch
  state whose position is too large to store is skipped.
- **A brief Emby outage no longer throws away a sync.** One 502, 429 or dropped
  connection used to fail the whole walk, and the next sync started its item walk
  again from the first item — on a large library, hours lost to a blip. A page
  that fails that way is now asked for again, for about eight minutes, before the
  walk gives up, and a 429's `Retry-After` is honoured up to four minutes a wait.
- **The push lane no longer gives up on a library that is merely quiet.** On a
  library where nothing is changing, Emby's only messages are `Sessions` frames
  minutes apart, and those never reset the lane's failure count, so a few quiet
  spells — however far apart — marked push unavailable until a restart. Any
  message now counts as delivery, so only unbroken silence parks the lane: about
  25 minutes of it at the defaults.

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
