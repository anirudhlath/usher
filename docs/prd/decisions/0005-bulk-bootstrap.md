# ADR-0005 — Pre-build the catalog from bulk datasets

**Status:** Accepted

## Context

The initial design populated the catalog purely on demand: an item appears on a
source, Usher resolves and enriches it. Simple, but it means the catalog only
ever knows what you already own — so similarity search is thin, recommendations
cannot suggest anything new, and every match requires a provider API round-trip.

## Decision

Bootstrap the catalog from bulk open datasets before any source is connected,
then keep on-demand enrichment for the items a source actually holds.

Phases and licensing detail: [04](../04-catalog-bootstrap.md).

## Consequences

**Gained:**

- **Matching goes local.** Resolving a source item becomes a database lookup
  against 12.7M known titles instead of a network call. This is the largest
  single speedup in the ingest pipeline.
- **Recommendations have a real candidate pool** spanning the whole catalog, so
  Usher can suggest titles you don't own — impossible with a library-only DB.
- **Search is meaningful immediately**, including for things being looked up
  before acquisition.
- **Similarity is dense from day one** rather than improving slowly with usage.

**Accepted costs:**

- A ~3–5 hour unattended first-run bootstrap (dominated by the TMDb crawl) and
  ~8–12 GB of disk.
- Two enrichment tiers to model (`skeleton` vs `enriched`), which the data model
  makes explicit rather than implicit.
- Ongoing refresh obligations, including TMDb's ≤6-month cache term.

## Evidence

Measured 2026-07-28:

- IMDb datasets: 1.83 GiB compressed across 7 files, 12.7M titles, 100M
  cast/crew rows, refreshed daily. **No plot text in any column** — this is why
  the TMDb crawl remains load-bearing for embeddings.
- TMDb daily ID export: 1,225,762 movie and 227,884 series IDs with popularity,
  31 MiB, no auth. **IDs and popularity only** — no overviews.
- Of 1,127,975 IMDb movies + series, only **188,796** have ≥100 votes. That
  subset defines the enrichment priority tier and covers any realistic home
  library.
- TMDb's original rate limit was disabled in 2019; current guidance is a ceiling
  "somewhere in the 40 requests per second range". At a self-imposed ~25 rps the
  189k tier takes 1.5–2.5 hours.
- MovieLens tag genome: 15,584,448 scores over 13,816 movies × 1,128 tags, with
  `links.csv` bridging directly to IMDb and TMDb IDs. Strong signal, ~7%
  coverage — a bonus, never the primary index.

## Constraint this imposes

**Ship importers, never data.** IMDb permits personal use of the published dumps
but prohibits redistribution and scraping; TMDb prohibits redistribution and
caps caching at 6 months. Usher's MIT license is unaffected because no
third-party data is ever committed or released — each user runs the importers
and holds their own API key.
