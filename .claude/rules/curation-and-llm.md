---
paths:
  - "src/usher/adapters/llm/**"
  - "src/usher/services/curation.py"
  - "src/usher/services/curation_pool.py"
  - "src/usher/services/curation_prompt.py"
  - "src/usher/services/curation_validate.py"
  - "src/usher/services/query_expansion.py"
  - "src/usher/services/llm_ledger.py"
---

# The LLM client, the candidate pool, the prompt and the validator

Rules for this subsystem. ⚠️ **Every model figure here came from one local vLLM,
one pool, one evening.** The *ordering* of the options and the *shapes* of the
failures transfer; the percentages transfer to nothing.

## Running one

```bash
export USHER_LLM_ENABLED=true
export USHER_LLM_BASE_URL="http://127.0.0.1:8000/v1"   # any OpenAI-compatible endpoint
export USHER_LLM_MODEL="<what /v1/models says>"        # printed back as report.usage.model
uv run usher curate            # one generation for the default household
```

- **`usher curate` takes no arguments at all** — no `--user`, deliberately,
  because PRD 01 leaves authentication as a seam and `usher.db.users` stands in it
  as a singleton `is_default` row. It is the one surface returning the answer —
  pool, rows, drops, tokens, cost — in the same breath as the request, and prints
  to stdout, never through the log sink (`report=False`, and `configure_telemetry`
  quiets `httpx`'s per-request INFO line).
- **The `dropped` block prints all five reasons, zeros included** — an absent line
  and a reason nobody counts read the same. **Two of the five count rows and three
  count cards, so a sum across the label is meaningless.** `cost_usd` stays a
  `Decimal` to eight places, matching `llm_calls.cost_usd`'s `NUMERIC(12,8)`.
- **Two other surfaces reach the same `CurationService.generate` and neither has
  been run live**: `JobKind.CURATE` claimed by `usher work`, and
  `POST /admin/rows/regenerate`, which enqueues one and promises only a 202.
- With `USHER_LLM_ENABLED=false` there is no `CurationService` to build at all —
  `composition.llm_client` answers `(None, no-op)` and the service spells its
  client `LLMClient`, never `LLMClient | None`, so *no client, no curation* is a
  `mypy` fact at the composition root rather than a branch (ADR-0026).
- ⚠️ **An empty pool, a generation validating to zero rows and an unproducible
  completion all exit 1 saying the previous rows still stand — which is not
  "nothing was written".** Only the empty pool attempts no call; the other two
  reach `_settle`, which commits an `llm_calls` row with `ok = false` and the
  real token counts, so the operator has been charged. Check with
  `SELECT at, purpose, ok, tokens_in, tokens_out, cost_usd FROM llm_calls` — the
  column is `at`, not `created_at`.

| setting | default | what to know |
|---|---|---|
| `USHER_CURATION_POOL_SIZE` | 200 | `le=1000`, but the reference endpoint 400s above 600 |
| `USHER_LLM_MAX_OUTPUT_TOKENS` | 2048 | raising it silently lowers the workable pool |
| `USHER_LLM_PRICE_{IN,OUT}_PER_MTOK` | `Decimal(0)` | 0 is honest self-hosted, and invisible billing hosted |
| `USHER_QUERY_EXPANSION_ENABLED` | `false` | measured *worse*; `true` without `USHER_LLM_ENABLED` is refused at startup |

## The pool ceiling

🔴 **`USHER_CURATION_POOL_SIZE`'s `le=1000` is a bound the reference endpoint
cannot serve**: at shipped defaults 600 works, 700 and 1,000 are HTTP 400, and
accuracy does not degrade at 600. The real constraint is
`prompt_tokens + llm_max_output_tokens ≤ max_model_len`, and **nothing couples
the two settings**, so raising `USHER_LLM_MAX_OUTPUT_TOKENS` silently lowers the
workable pool with no warning anywhere. The bound is deliberately **not** lowered
to 600 — that is one endpoint's answer, and PRD 08's argument for the setting is
that the right number is a deployment fact. A real fix needs a startup check that
knows `max_model_len`, which nothing in `Settings` carries. The failure is at
least legible: `_decode`'s 4xx branch translates the 400 to `PortDataMalformed`
and `JobWorker` parks rather than spending four more completions on the wall.

**A per-item prompt decoration is priced per candidate and paid at the pool
ceiling — check it against the ceiling, not against the default pool.** The
candidate line already costs ~20.4 tokens each (it renders the genre list). A
proposed ownership marker was declined against a **pre-registered bar of 2.0
tokens/candidate** that the cheapest legible wording missed by 45% — not against
a 400. Terse (~2.9) fits pool 600 with ~56 tokens to spare under `max_model_len`;
only the verbose (~4.9) wording goes over. Check a decoration against the pool
*ceiling* and against the bar it was registered on, never against the default.

## The coercion is the primary path, not a fallback

🔴 `curation._schema` declares `item_ids` items as `{"type": "integer",
"minimum": 1, "maximum": pool_size}` — a handle *is* a number, and a numeric
bound is what guided decoding can enforce — while `curation_validate` keys
`by_handle` on `str(index)`. **So with `strict: true` honoured, every identifier
arrives as a JSON `int` and `_handle`'s `int` branch runs on 100% of cards of
every generation. Deleting `str(value).strip()` drops every card of every
generation** and `row_too_short` eats every row behind them. The alternative —
schema asks for strings, map keyed on `int` — moves the coercion rather than
removing it, gives up the bounds, and asks a model to quote a number.

- **`strict: true` is honoured for the *numeric* bound, not only the shape** —
  though the reason for the index scheme is that an index is bounds-checked, not
  any observed out-of-pool rate.
- **An unsatisfiable *value* bound makes guided decoding loop to the ceiling**,
  `finish_reason == "length"` fires, and the adapter's truncation guard refuses
  the completion. Its real justification is not "rows missing off the end" — it is
  what stops **a degenerate loop being read as a valid answer**. It also vindicates
  `_schema`'s deliberate omission of `minItems`: with the card floor left as a
  `description` hint, a pool that cannot answer **narrows** and is discarded as
  `row_too_short`, counted and legible, rather than looping at full price.

## Product findings, recorded rather than fixed

- 🔴 **On this model the curated shelf is substantively what
  `GenreAffinityProvider` already gives away free** — 88% of headings were genre
  labels the prompt explicitly forbids, and several recurred verbatim across
  separate generations. The 88% is one model's; **what is a property of the design
  is that the prompt's grouping instruction is not self-enforcing and nothing in
  this system checks it.** Carried in PRD 06 and PRD 09.
- **`_cards` de-duplicates within a row only** — a title on two shelves of one
  generation is not counted `duplicate` and is not prevented. The prompt's *"Do not
  use the same candidate in more than one row"*
  is the only defence, and a prompt rule is not a guarantee.
- **The pool has no ownership *filter*.** `list_unwatched_candidates` uses
  ownership as an `ORDER BY` key, deliberately, so PRD 06's *"the pool spans the
  whole catalog"* stays true — and since owned titles are therefore a prefix of
  the answer, a filter is subtractive by construction. The prompt's opening
  sentence, which claimed the household owned every candidate, is what gave way —
  `test_the_opening_line_does_not_claim_the_household_owns_every_candidate` pins
  that. **Whether a prompt sentence is framing prose is not a question of how it
  reads — it is whether any query, constant or validator in the system would have
  to be true for the sentence to be.**
- **`CurationService.generate`'s empty-pool guard is
  `len(candidates) < self._min_cards`**, not `not candidates`, so a pool too small
  to make a row refuses in front of `complete_json` and nothing is billed. The
  pool being `min(catalog_unwatched, POOL_SIZE)` and ownership a sort key, only a
  catalog whose whole unwatched set is below five reaches it — rare, not nightly.
- **Four of the five `DropReason` members never fired, and the vocabulary is
  still right.** Under a provider honouring `strict: true`, `unparseable`,
  `row_unusable` and `not_in_pool` are near-unreachable. Keep exporting the zeros:
  when a `base_url` change puts a schema-ignoring provider behind this port,
  `unparseable` going 0 → spike is the only signal, the call still returning 200.

## What no live run has reached

- `media_items` and `title_embeddings` were both **0**, so ownership sorting, the
  other nine row providers and the centroid re-rank have never run against real
  data. End-to-end retrieval through `PostgresSearchIndex`, `JobKind.CURATE` via
  `usher work` and `POST /admin/rows/regenerate` are unexercised too.
- ⚠️ **The re-rank is `_reranked`, a module-level function in
  `services/curation_pool.py` — not a `CandidatePoolService` method.**
  `for_user` calls it after two early returns, which are why a live run misses it:
  an empty pool returns before any centroid read, and a `centroid()` of `None` —
  the shipped default and every new household — returns the base order whole
  rather than coalescing to a zero vector, which would rank every candidate
  identically. Module-level is what makes it testable: a pure
  `(pool, centroid, vectors) -> list[Title]`.
- ⚠️ **`m09e` moved the re-rank further out of reach**, widening the embedding
  columns and **deleting every embedding and taste centroid**, so any deployment
  past `_MIN_TITLES` went back to `centroid() is None`. Reaching it now costs
  `usher index --backfill`, then `usher work`, then enough watch history to
  rebuild a centroid, before a single `usher curate`
  (`.claude/rules/search-and-embeddings.md` has the width argument).
- **No hosted provider has ever been touched**, so ADR-0027's *"two providers'
  quirks are unmeasured"* stands: whether `json_schema` is honoured, whether
  `strict: true` is accepted, and 429/`Retry-After` semantics are all
  one-endpoint knowledge.

## Query expansion is off by default because it measured worse

MRR and recall@10 both fell against the typed query, which won four of five mood
queries outright and tied the fifth. **The label-free control says why**: pairwise
cosine *between the queries themselves* rose, so five distinct searches came back
more alike than they went in — what a rewrite regressing toward a genre vocabulary
does. So it ships behind **its own** setting, `USHER_QUERY_EXPANSION_ENABLED`,
default `false` *even where `USHER_LLM_ENABLED` is true*, and `true` with no
client is refused at startup rather than ignored (PRD 05 carries the caveats).
A consequence for PRD 10: on the shipped default `llm_calls` is **100%
`curation`** and every `generation_id` is non-NULL, so the partial index on
`generation_id IS NOT NULL` is right under both populations.

## The client and the ledger

- **`json.loads` raises `RecursionError`, which is not a `ValueError` and not a
  `UsherPortError` either** — `OpenAICompatibleClient._decode` and `_parse` must
  catch it, or it escapes the port and `CurationService`'s `except UsherPortError`
  and takes the worker down instead of parking one job. ⚠️ The C scanner's budget
  is an order of magnitude past `sys.getrecursionlimit()` (~9,999 levels at a
  limit of 1,000), so a case built on "a bit over 1,000" passes unfixed code.
- **`_decode` is the live half and `_parse` the defensive one.** `_parse`'s
  subject is bounded by `max_output_tokens` and shielded by `_content`, which
  refuses `finish_reason == "length"` before anything is parsed; the envelope has
  no token bound and is whatever the endpoint, or a proxy in front of it, put on
  the wire. **Ask which side of a guard the untrusted bytes are on before ranking
  two instances of one defect.**
- **Never publish a single summed `dropped` count** — two reasons count rows and
  three count cards. The span carries `dropped_rows`/`dropped_cards`, derived from
  the `row_` prefix rather than from a second list, so a sixth reason cannot be
  added to one and forgotten in the other. A fixture that drops exactly one card
  cannot tell a mixed total from a pure one.
- **Both spenders mint their ledger row through `services/llm_ledger.py`**, and an
  `ast` walk — not a substring scan, since both modules discuss `LLMCall` in
  prose — asserts neither service mints one of its own. When a sweep's repair is
  "collapse the copies", ask whether the collapse was scoped to the class or to
  the codebase.
- **The cost warning is gated on `llm_api_key`, not on the price.** Both price
  settings default to `Decimal(0)` — the honest value self-hosted, invisible
  billing on a paid endpoint — so warning on price alone fires on every
  correctly-configured deployment, and a warning everyone sees is one nobody
  reads. A key separates the populations. In `composition.llm_client`: once per
  process, never once per pass.
