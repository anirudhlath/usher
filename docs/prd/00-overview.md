# 00 — Overview

## The problem

Media servers own both the library *and* the experience. Their APIs are shaped
around their own UI, their metadata quality is whatever the scraper produced,
and every client you build is a client *of that server*. Replace the server and
you rebuild everything. Add a second server and you have two disjoint libraries
with two sets of watch state.

Usher inverts that. The catalog is ours; media servers become interchangeable
*sources* that answer one question: "where can this title be played?"

## What Usher is

A backend — no UI — that maintains:

1. **A canonical catalog** of films and television, keyed by our own identifiers,
   enriched to the highest-quality data available (cast, crew, runtime,
   languages, artwork, keywords, collections).
2. **Availability records** attaching each canonical title to one or more
   sources (Emby today; others later) with the quality facts for that copy.
3. **Unified watch state** attached to canonical titles, so progress survives
   changing or adding sources.
4. **Discovery** — full-text search, semantic/similarity search, and dynamically
   composed home-screen rows including LLM-curated ones.

It exposes all of this over an HTTP API rich enough to build an Infuse-class
client against, with no client ever needing to know Emby exists.

## Goals

- **Total abstraction of sources.** No source-specific concept reaches the API.
- **Fast clients.** The API is the cache. Clients should never wait on an
  upstream server; anything slow is pre-computed or served stale-then-updated.
- **Browsable during sync.** A cold catalog is usable immediately, not after an
  import completes.
- **Good data by default.** Bulk-loaded open datasets mean recommendations and
  search work meaningfully from first boot, not after months of usage.
- **Extensible by design.** New sources, metadata providers, row types, and
  search backends are new subclasses, not new branches in existing code.
- **Open source.** MIT licensed, self-hostable, ships importers rather than data.

## Non-goals

- **Not a media server.** Usher does not transcode, stream, or manage files.
  Playback is delegated to a source or a player.
- **Not a UI framework.** Usher ships exactly one client — **Usher Console**,
  in `web/`, served by this process at `/console` — and it is a consumer of the
  HTTP contract like any other, generated from `/openapi.json` and holding no
  private route. ⚠️ **This non-goal read "Not a UI. Reference clients may
  follow; they are separate projects" until 2026-08-19, and the second half was
  true for nine milestones.** What changed the call was not ambition: a
  self-hosted product whose only interface is `curl` is not one, and a client
  in a second repository could not be shipped, versioned or tested with the API
  it generates from. The boundary the non-goal was protecting still holds and
  is now structural — no route exists for the console that does not exist for
  everyone, and `USHER_CONSOLE_ENABLED=false` is a supported deployment.
- **Not multi-tenant.** Designed for a household. User records exist so watch
  state and taste are per-person, not so it can be run as a service.
- **Not collaborative filtering.** With household-scale usage there is no
  co-occurrence signal. Recommendations are content-based plus borrowed
  aggregate signals. See [06](06-rows-and-recommendations.md).

## Success criteria

| | Target |
|---|---|
| Home screen response | < 150 ms warm, fully composed |
| Search-as-you-type | < 50 ms |
| Title detail | < 100 ms for enriched titles |
| Cold start usability | Catalog browsable within seconds of first source sync starting |
| Enrichment latency for a viewed title | < 5 s from open to enriched, pushed to client |
| Source abstraction | Adding a second source type requires no change outside its adapter |

## Consumers

- **Household clients** — the media browser this is built for; tvOS/web/mobile.
- **[Alfred](https://github.com/anirudhlath/alfred)** — the household voice
  assistant. Usher becomes its media knowledge and action surface ("play the
  thing with the astronaut", "what's new"). Shared stack (Python 3.13, Pydantic,
  FastAPI, litellm) so integration is native rather than another HTTP hop.
- **Home Assistant** — replaces the current browser-side Emby card, which holds
  a raw Emby token in the frontend and breaks whenever that token rotates.

## Glossary

| Term | Meaning |
|---|---|
| **Title** | A canonical production — one film, or one series. Our identity. |
| **Source** | A configured backend that can play things (an Emby server). |
| **MediaItem** | "This title is available on that source" + quality facts. |
| **Stub** | A Title known to exist but not yet enriched. |
| **Enrichment** | Fetching high-quality metadata for a Title from a provider. |
| **Row** | A named, ordered shelf of titles on the home screen. |
| **Taste centroid** | Mean embedding of a user's recently watched titles. |
