---
paths:
  - "src/usher/adapters/llm/**"
  - "src/usher/services/curation.py"
  - "src/usher/services/curation_pool.py"
  - "src/usher/services/curation_prompt.py"
  - "src/usher/services/curation_validate.py"
  - "src/usher/services/query_expansion.py"
---

# The LLM client, the candidate pool, the prompt and the validator

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

⚠️ **Read this caveat before quoting any number below, and carry it with the
number.** Every model measurement in this file is **one model, one pool, one
evening**: a local vLLM serving `gemma-4-26b-a4b`
(`cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit`, `max_model_len` 16,384) over
`http://127.0.0.1:8000/v1`, driven from throwaway scripts outside the working
tree against a service belonging to something else on the host. The 2026-08-06
design probes were bounded at 45 completions; the 2026-08-07 live verification
was bounded at 45 and spent 36, against a real **1,271,138**-title catalog. A
frontier model will do better and a 3B model much worse. **What transfers is
the *ordering* of options and the *shapes* of failures. The percentages
transfer to nothing.**

## The refutations

**`~14.6 prompt tokens a candidate` was wrong, and it was wrong in the way a
derived number usually is.** It was ADR-0028's whole-prompt figure (2,924)
divided by its candidate count (200) — a *total* charged to the candidates,
including the instructions and the shape example — and the probe it came from
rendered a candidate as name and year. **The shipped prompt renders the genre
list too** (`curation_prompt._genres`), and measured against *it* at four pool
sizes the **marginal** cost is **20.40 tokens/candidate** from 8 → 200 and
**20.45** from 200 → 600. **+40%.** The two marginal figures agreeing to 0.05
of a token across a 75× range is what makes it a rate rather than a sample.
Whole-prompt, pool 200: **4,304 cold**, **4,359 with three history lines**
(+47% on the probe's 2,924). A history line is **~18 tokens**, so
`HISTORY_SIZE = 25` costs a real household **~460**. Output tokens went the
other way — median **219.5** (192–277) against the probe's 316, **−31%** — and
latency improved, median **1,420 ms** (1,230–1,787) against 1,995. Neither of
those two was load-bearing for any decision. Corrected in `composition.py`,
`config.py`, PRD 08 and two test docstrings; the probe table in ADR-0028 and in
the M8 plan is annotated rather than rewritten, because it is a record of what
was measured and what the decision rested on.

**🔴 `USHER_CURATION_POOL_SIZE`'s `le=1000` is a bound the reference endpoint
cannot serve.** Shipped defaults, `llm_max_output_tokens = 2048`:

| pool | result |
|---|---|
| 600 | **works** — 12,540 prompt tokens |
| 700 | **HTTP 400** |
| 1,000 | **HTTP 400** |

Accuracy did **not** degrade at 600 — that arm is inside the 0-out-of-405
result below. What fails is arithmetic: the real constraint is
`prompt_tokens + llm_max_output_tokens ≤ max_model_len`, and **nothing couples
the two settings**, so raising `USHER_LLM_MAX_OUTPUT_TOKENS` silently lowers
the workable pool with no warning anywhere. **The mechanism is right and was
verified end to end** — `_decode`'s 4xx branch (every 4xx **except 401, 403,
408 and 429**, which are three other families; the table in
`config-cli-and-deployment.md` measures all six rows) translated the
400 to `PortDataMalformed` and `JobWorker` parked immediately rather than
spending four more completions on the same wall, which is M8 plan trap 13
firing exactly as designed. The **bound** is the problem. It is deliberately
**not** lowered to 600: 600 is *this* endpoint's answer and PRD 08's whole
argument for the setting is that the right number is a deployment fact. What a
real fix needs is a startup check that knows `max_model_len`, which nothing in
`Settings` carries. Recorded in `config.py`, PRD 08 and ADR-0028.

**🔴 The coercion in `curation_validate` is the primary path, not a fallback,
and the docs said otherwise.** ADR-0028's 108/108 finding was measured on the
`json_object` arm — a provider *ignoring* the schema — which reads as a defence
against a bad provider and makes guided decoding look like the case where the
coercion is redundant. Read the two shipped modules together instead:
`curation._schema` declares `item_ids` items as `{"type": "integer", "minimum":
1, "maximum": pool_size}` (the correct schema — a handle *is* a number, and a
numeric bound is what guided decoding can enforce), and `curation_validate`
keys `by_handle` on `str(index)`. So **with `strict: true` honoured, every
identifier arrives as a JSON `int` and `_handle`'s `int` branch runs on 100% of
cards on every generation.** Deleting `str(value).strip()` drops **every card
of every generation** and `row_too_short` eats every row behind them.
**Stronger claim, same code** — a documentation correction, not a fix. The
alternative (schema asks for strings, map keyed on `int`) moves the coercion
rather than removing it, gives up `minimum`/`maximum`, and asks a model to
quote a number.

## Confirmations, some stronger than the claim they confirm

- **Integer handles: 0 out-of-pool over 405 ids, 20 generations, 5 pool
  shapes** — **3.9× the denominator** ADR-0028's 0.0% was measured on. Still
  not a guarantee and still not the reason for the index scheme; the reason is
  that an index is bounds-checked.
- **`json_schema` `strict: true` is honoured for the *numeric* bound and not
  only the shape.** With `maximum: 5` declared against a prompt begging for
  1–200, **zero** integers above 5 appeared across 2,048 output tokens.
- **The pool that cannot answer narrows rather than inventing, across four
  hostile shapes** — pool 8, pool 5, 200 unknown titles, 200 bare-number
  titles; 199 ids, 0 out of pool. ADR-0028's 2026-08-06 result was one shape.
- **🔴 An unsatisfiable *value* bound makes guided decoding loop to the
  ceiling.** `maximum: 5` against a prompt asking 1–200 produced
  `1,2,3,4,3,1,2,3,4…` for the entire 2,048-token budget; `finish_reason ==
  "length"` fired and the adapter's truncation guard refused it. **The first
  live firing of that defence in this project**, and its real justification is
  stronger than the docstring's *"rows missing off the end"*: it is what stops
  **a degenerate loop being read as a valid answer**. It also **vindicates
  `_schema`'s deliberate omission of `minItems`** — with the card floor left as
  a `description` hint, the pool-8 and pool-5 arms **narrowed** to 2–3 cards
  and were discarded as `row_too_short`, which is counted and legible, rather
  than looping at full price.
- **Zero rows ⇒ `ok = false`, confirmed live**, with the reason, the tokens and
  the cost recorded in full and `curated_rows` untouched. ADR-0028's rule 3 is
  now an observed behaviour rather than only a designed one.
- **`cost_usd` holds end to end.** `0.00000000` against the local model (the
  honest value); with prices 3/15 per Mtok configured, `0.01658700`, exactly
  `Decimal((4359×3 + 234×15) / 1e6)`. The column is `numeric` and `SUM()`
  agrees to 8 decimal places.

## The product findings, recorded rather than fixed

**🔴 The milestone's central product risk: on this model the curated shelf is
substantively what `GenreAffinityProvider` already gives away free.** Over 59
headings from 20 generations: **52 of 59 — 88% — are genre labels**, which the
prompt explicitly forbids (*"a mood, a period, a theme, a filmmaker — rather
than by one genre"*); **one heading in 59 named a filmmaker**; and *"Animated
Wonders for All Ages"*, *"Epic Sci-Fi Adventures"* and *"Mind-Bending Sci-Fi &
Thrillers"* each recur **verbatim across three separate generations**.
`GenreAffinityProvider` produces a genre shelf from a `SELECT`, for nothing,
needing no key. ⚠️ The 88% is a property of one model; **what is a property of
the design is that the prompt's grouping instruction is not self-enforcing and
nothing in this system checks it.** In PRD 06 and PRD 09, not only in a task
queue, because a reader of PRD 06 is the person who needs it.

- ✅ **The pool has no ownership *filter* and the prompt said it did —
  settled 2026-08-11, see the section below.**
  `TitleRepository.list_unwatched_candidates` uses ownership as an `ORDER BY`
  key only — deliberately, so PRD 06's *"the pool spans the whole catalog"*
  stays true — while `curation_prompt.build_prompt` opened *"one household's
  **own** film and television library."* On a household whose unwatched-owned
  set is smaller than the pool, the tail is titles it does not own under a
  sentence asserting it does. Both sentences are defensible and they disagree;
  filed as a decision (#40) rather than settled. **The prompt gave way.**
- **`_cards` de-duplicates within a row only.** A title on two shelves of one
  generation is not counted `duplicate` and is not prevented; the prompt's rule
  7 is the only defence, and a prompt rule is not a guarantee.
- ✅ **`min_cards = 5` meant a small unwatched pool yielded zero rows, every
  time, at full price — settled 2026-08-11 (M9 Task G4).** Rows carried 5–6
  cards at pool 200 and **2–3 at pool 5 and pool 8**, so every row was
  `row_too_short` and the generation was billed for nothing. That is ADR-0014
  working as designed *and* a household paying a completion a night for a
  permanently empty shelf, with nothing warning the operator before the money.
  `CurationService.generate`'s empty-pool guard is now
  `len(candidates) < self._min_cards` rather than `not candidates`, so the
  refusal is in front of `complete_json` and nothing is billed. ⚠️ **Quote the
  frequency with the fix:** the pool is
  `min(catalog_unwatched, USHER_CURATION_POOL_SIZE)` and ownership is a sort
  key, not a filter (the section below), so only a catalog whose whole
  unwatched set is below five reaches the guard — **rare, not nightly**. The
  general form, which is the transferable half: *a guard's value is the product
  of what it prevents and how often the state it fires on is reachable, and the
  second factor is a property of the read it sits behind rather than of the
  guard. Measure the read before pricing the guard.* Here the same guard would
  have been a nightly saving under the filtered pool G3 declined, and the
  arithmetic it rests on would have been identical.
- **Four of the five `DropReason` members never fired in 20 generations**, and
  under a provider honouring `strict: true` three are close to unreachable —
  `unparseable` and `row_unusable` are shape failures guided decoding prevents,
  `not_in_pool` is a range violation `minimum`/`maximum` prevents. Only
  `row_too_short` fired. **The vocabulary is still right** and the zeros are
  still exported: the day a `base_url` change puts a schema-ignoring provider
  behind this port, `unparseable` going from a permanent 0 to a spike is the
  only signal anything changed, because the call still returns 200. Worth
  knowing before an operator reads a dashboard of permanent zeros.

## What the run did not reach

Named rather than implied. **`media_items` was 0**, so ownership sorting and
the other nine row providers were never exercised against real data.
**`title_embeddings` was 0**, so `CandidatePoolService._reranked`'s centroid
re-rank **never executed** — the one half of boundary call 5 that is still
covered by the suite alone. End-to-end retrieval through `PostgresSearchIndex`,
`JobKind.CURATE` via `usher work`, and `POST /admin/rows/regenerate` were all
untested (only the `usher curate` path ran). **No hosted provider was touched
at all**, so ADR-0027's *"two providers' quirks are unmeasured"* is unchanged:
whether `json_schema` is honoured, whether `strict: true` is accepted, and
429/`Retry-After` semantics are all still one-endpoint knowledge.

## Query expansion measured *worse*, and that is a separate run

Run 2026-08-07 against the same endpoint: **five mood queries against the 150
most-voted catalog titles' real overviews**, embedded with the shipped
`compose_document` and the shipped `FastEmbedEmbedder`, **targets written down
before any cosine was computed**. MRR **0.733 → 0.373**, recall@10
**0.800 → 0.533**, with the typed query winning four of five outright and
tying the fifth. **The label-free control says why**: pairwise cosine *between
the five queries themselves* rose from **0.5417 → 0.5975** mean and
**0.6328 → 0.7784** max — five distinct searches came back more alike than
they went in, which is what a rewrite that regresses toward a genre vocabulary
does. So it ships behind **its own** setting, `USHER_QUERY_EXPANSION_ENABLED`,
default `false` *even where `USHER_LLM_ENABLED` is true*, and `true` with no
client is refused at startup rather than ignored. PRD 05 carries the caveats,
which are real: one model, one 150-document corpus, five queries. The
consequence for PRD 10 is that on the shipped default `llm_calls` is **100%
`curation`** and every `generation_id` is non-NULL, so the partial index on
`generation_id IS NOT NULL` is right under both populations rather than because
expansion rows are a majority.

## The pool's ownership claim, settled 2026-08-11 (M9 Task G3)

**The filter could add nothing, and that is the measurement that decided it.**
A pool-composition sweep through the real `PostgresTitleRepository` — its own
`pgvector/pgvector:pg17` container, schema built by the real Alembic chain, a
1,000-title catalog, no watch history, `limit = 200`, `U` unwatched-and-owned
titles chosen with `random.Random(20260811)`:

| `U` | pool as shipped | of which owned | pool filtered to owned |
|---|---|---|---|
| 0 | 200 | 0 — 0.0% | **0** |
| 3 | 200 | 3 — 1.5% | **3** |
| 5 | 200 | 5 — 2.5% | 5 |
| 8 | 200 | 8 — 4.0% | 8 |
| 20 | 200 | 20 — 10.0% | 20 |
| 200 | 200 | 200 — 100.0% | 200 |

The owned column is exactly `min(U, limit)` at every row, because `owned DESC`
is the **first** sort key and the owned titles are therefore a prefix of the
answer. So a filter deletes the tail and adds nothing; at `U = 200` both arms
return the identical set. Below `DEFAULT_MIN_CARDS = 5` it deletes the
generation, and the unreachable band is wider than the arithmetic says —
the 2026-08-07 run measured rows of **2–3 cards at pool 5 and pool 8**, all
`row_too_short`, so `U = 5` and `U = 8` clear the `>= 5` bar on paper and
produced nothing live. **The general form: before choosing between a filter and
a claim, check whether the filter's answer is a *subset* of the sort's — if the
ranking key already puts the whole population at the head, the filter is
subtractive by construction and there is nothing to weigh against its cost.**

🔴 **The ownership marker was priced and declined, against a bar written before
the number.** Four completions, `max_tokens=1`, same endpoint and same model,
pool 200 sampled from the IMDb dumps with ADR-0028's criteria:

| arm | prompt tokens | delta |
|---|---|---|
| shipped prompt | **4,251** | anchor — 4,304 on 2026-08-07, within 1.2% |
| opening sentence corrected only | **4,277** | **+26, once** |
| \+ *"in the library"* / *"not in the library"* | 5,257 | **4.900 tok/candidate** |
| \+ *"owned"* / *"not owned"* | 4,857 | **2.900 tok/candidate** |

Bar: 2.0 tokens a candidate, ~10% of the 20.40 the shipped candidate line
already costs. The cheapest legible wording missed by 45% and is **14.2%** on
top of every line. Derived from the 12,540 recorded above for pool 600, a
terse-marked pool 600 would sit ~14,280 prompt tokens, which with
`llm_max_output_tokens = 2048` leaves **56 tokens** under `max_model_len`, and
the verbose one would be over it — so the marker would have turned a measured
working configuration into HTTP 400. **A per-item prompt decoration is priced
per candidate and paid at the pool ceiling; check it against the ceiling, not
against the default pool.**

⚠️ **The prompt's role sentence is pinned, and `.claude/rules/testing-discipline.md`
used to name it as the archetype of framing prose deliberately left unpinned**
("The five left alive are framing prose … the role sentence, the two history
headers …", the 2026-08-06 sweep). The opening line was a claim about the pool's
*membership*, so a `WHERE` clause would have had to honour it, and it is pinned
by `test_the_opening_line_does_not_claim_the_household_owns_every_candidate`.
**The test a prompt sentence has to pass to be "framing" is not how it reads —
it is whether any query, constant or validator in the system would have to be
true for the sentence to be.**

**That file is now corrected at the source and this stays as the
cross-reference.** It was left as a cross-reference alone from 2026-08-11 to
2026-08-12, because G3's file set did not include it — and that was the wrong
call for a reason worth keeping: **`testing-discipline.md`'s `paths:`
frontmatter is what loads for a test author, and `curation-and-llm.md`'s does
not.** The reader who needs the corrected rule was the one reader guaranteed to
get the false one. A finding filed only in the file whose author happened to
have it open is a finding routed by *authorship* rather than by *audience*; when
they differ, edit the file the audience loads and cross-reference the other way.

**Evidence (c), named rather than implied:** the 2026-08-07 run recorded
`media_items = 0`, so **no real ownership distribution has ever been observed
on this project** and *"how often is a household's unwatched-owned set below
200?"* is unmeasured. The call is the arm insensitive to that guess — the
shipped read returns every owned candidate at every `U`, so being wrong about
the distribution costs a longer tail rather than an empty shelf. M9's live Emby
run is the first chance to measure it.

## Review findings, 2026-08-10

**`json.loads` raises `RecursionError`, which is not a `ValueError` and was not
a `UsherPortError` either.** `OpenAICompatibleClient._decode` and `_parse` both
caught `ValueError` only. Measured on CPython 3.13 at the default recursion
limit of 1,000: **nesting depth 9,998 parses and 9,999 raises** — the C scanner
has its own budget an order of magnitude past `sys.getrecursionlimit()`, so the
obvious guess of "a bit over 1,000" does not reach it and a case built on that
guess passes against the unfixed code. `RecursionError` subclasses
`RuntimeError`, so it escaped the port, escaped `CurationService`'s
`except UsherPortError`, and would take the worker down instead of parking one
job — the identical failure `_content`'s own comment about `json.loads(None)`'s
`TypeError` exists to prevent, one method over.

**The two halves are not equally exposed, and the reporting reversed them.**
`_parse`'s subject is bounded by `max_output_tokens` *and* shielded by
`_content`, which refuses `finish_reason == "length"` **before** anything is
parsed — and a model that ran away into 10,000 open brackets hits the token
ceiling first, which is the shape this file already measured (`1,2,3,4,3,1,2…`
to the ceiling under an unsatisfiable bound). The **envelope** has no token
bound and no truncation guard: it is whatever the endpoint, or a proxy in front
of it, put on the wire. So `_decode` is the live half and `_parse` is the
defensive one. **Ask which side of a guard the untrusted bytes are on before
ranking two instances of one defect.**

**`curation.generate`'s span published a sum this repository documents three
times as meaningless.** `_measure` added all five `DropReason` counts into one
`usher.curation.dropped` attribute, while `curation_validate`'s module
docstring, ADR-0028 and `_rows_dropped`'s own comment each say two of the five
count rows and three count cards. **The case that should have caught it could
not**: its fixture drops exactly one card, so a sum of one unit is
indistinguishable from a correct total of either. Same family as the
`NULLS LAST` and `media_items.available` findings in `testing-discipline.md` —
*a fixture holding one kind of thing cannot tell a mixed total from a pure
one.* Split into `dropped_rows`/`dropped_cards`, derived from the `row_` prefix
rather than from a second list, so a sixth reason cannot be added to one and
forgotten in the other.

**The ledger rule was down to one spelling inside `CurationService` and back to
two across the codebase.** `testing-discipline.md` records the sweep that made
`_settle` exist — *"a rule spelled three times is a rule one deletion is
invisible in"* — and `QueryExpansionService` then carried a verbatim copy of
`_settle`/`_ledger_row`/`_record`, differing only in the purpose constant and
the generation id. Five invariants were each argued and pinned twice. Now
`services/llm_ledger.py`, held by both spenders, with an `ast` walk (not a
substring scan — both modules discuss `LLMCall` at length in prose) asserting
neither service mints a row of its own. **The general form: when a sweep's
repair is "collapse the copies", ask whether the collapse was scoped to the
class or to the codebase.**

**A cost warning had to be gated on the credential, not on the price.**
`llm_price_*_per_mtok` both default to `Decimal(0)`, so `cost_usd` reads
`0.00000000` for an operator who never set them — which is the *honest* value
for the self-hosted vLLM this milestone was verified against, and invisible
billing on a paid endpoint. Warning on price alone breaks the rule
`test_a_configured_llm_is_built_and_says_nothing` states: *a warning every
correctly-configured deployment sees is a warning nobody reads*, and the
zero-price self-hosted deployment is the shipped shape. An `llm_api_key` is
what separates the populations — a hosted provider requires one, the local
endpoint needs none — so the warning is gated on it and reaches only the
deployment it is about. In `composition.llm_client` for the reason the TMDb
line moved there: once per process, never once per pass.
