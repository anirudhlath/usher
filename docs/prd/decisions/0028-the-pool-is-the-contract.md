# ADR-0028 — The pool is the contract: candidates are indices, and the validator does not trust the schema

**Status:** Accepted — settles [06](../06-rows-and-recommendations.md)'s
validation step

## Context

[06](../06-rows-and-recommendations.md) specifies curation in four steps, and
step 3 is one sentence: *"**Validate** — IDs not in the pool are dropped; rows
below a minimum length are discarded. **Hallucinated identifiers never reach a
client.**"*

The sentence is right and it is not a design. Two questions it leaves open
decide whether the guarantee holds:

1. **What is an identifier, on the wire?** The pool's titles are Usher
   UUIDv7s. The obvious implementation puts them in the prompt and takes them
   back out.
2. **What does "not in the pool" mean, mechanically?** The obvious
   implementation is `id in pool_ids`.

Both obvious answers are wrong, and the second one fails in a way that is
invisible.

**And there is a third answer that looks like it makes the whole question go
away:** modern providers accept a JSON schema and enforce it during decoding.
If the model *cannot* emit malformed output, why validate at all? That reading
is the thing this ADR exists to refuse — schema enforcement is a guarantee
about **shape**, and every failure mode here is about **denotation**.

## Decision

**Three rules, each of which a case pins.**

**1. The prompt addresses candidates by a small integer index into the pool,
and the service owns the index→UUID map.** The model never sees a UUID and
never returns one.

**2. The validator coerces before it compares** — `str(value).strip()` against
a `set[str]` of the indices that were actually sent — and an index outside the
pool's range is dropped and *counted*.

🔴 **Amended 2026-08-07 by the live verification: rule 2 is stronger than this
ADR claimed, and the original wording understated it in the direction that
invites deletion.** Everything below in Evidence measures the coercion on the
`json_object` arm — a provider that *ignored* the schema — which reads as a
defence against a bad provider and makes guided decoding look like the safe
case where the coercion is redundant. It is not. Read the two shipped modules
together:

- `curation._schema` declares `item_ids` items as `{"type": "integer",
  "minimum": 1, "maximum": pool_size}` — the correct schema, because a handle
  *is* a number and a numeric bound is the one thing guided decoding can
  enforce during decoding (measured below).
- `curation_validate` builds its lookup keyed on `str(index)`.

So on the **happy path**, with `strict: true` honoured — the configuration
this milestone was designed against and verified against — **every identifier
arrives as a JSON `int` and the coercion runs on 100% of cards, on every
generation.** Measured 2026-08-07: **405 identifiers over 20 generations
across 5 pool shapes, every one an `int`, none out of pool.** Deleting
`str(value).strip()` does not degrade a fallback path; it drops **every card
of every generation** and `row_too_short` then eats every row behind them.

**This is a documentation correction and not a code change.** The two modules
are right as they stand, and the alternative — ask the schema for strings and
key the map on `int` — moves the coercion rather than removing it, gives up
`minimum`/`maximum`, and asks a model to quote a number. What was wrong was
this ADR's account of *which* of the two statements of the bound is
load-bearing on the ordinary path. It is the validator's, always, and never
the schema's.

**3. A generation that validates to zero rows is a failure**, recorded as
`llm_calls.ok = false` with a reason, not a success that wrote nothing.

`usher.curation.dropped` carries a `reason` label with a **closed** vocabulary,
because `not_in_pool` and `unparseable` produce the same empty screen and have
opposite fixes.

🔶 **Amended 2026-08-06 by the implementation (Task 13): the vocabulary is
five, not two.** This paragraph read *"exactly two values"*, and that pair is
still the load-bearing one — it is the split the 108/108 run produced and the
only one this ADR has evidence for. What building the validator showed is that
three more drops exist and that folding any of them into the original two
misreports what happened.

**The honest claim is a different *diagnosis*, not a different lever.** Two of
the five share a fix with a member of the original pair, and pretending
otherwise would be the inflation a closed vocabulary exists to resist:
`duplicate` and `not_in_pool` both point at the prompt or the temperature, and
`row_unusable` and `unparseable` both point at the schema or
`response_format`. What differs is what an operator *concludes* from the
number, and that is enough — a counter whose value means two different things
is one nobody can act on:

- **`duplicate`** (a card): the model named a real candidate twice. Merged into
  `not_in_pool` it reports **invention where none occurred** — the same lever,
  pulled for a reason that is not true.
- **`row_unusable`** (a **row**): not an object, no title, a non-string title
  or reason, prose past the bound.
- **`row_too_short`** (a **row**): fewer than the minimum survived, which is
  ADR-0014's discard-rather-than-pad arriving as a *number*. It is the
  second-order effect of the three card reasons, so a counter it shared with
  any of them could not answer the one question it exists for — whether the
  rows collapsed because the cards were eaten or because the pool had nothing
  to say.

**The load-bearing half of the widening is the unit split, not the count.** Two
of the five count **rows** and three count **cards**, which is why the row ones
carry the prefix: summing the whole label set is meaningless, and the names say
so rather than a comment saying so. The tally always carries all five keys,
zeros included — a reason absent from a tally is indistinguishable from a
reason nobody counts, which is this ADR's own subject one level up.
`src/usher/services/curation_validate.py` holds the one copy of the argument.

## Consequences

**Gained:**

- **A hallucinated identifier becomes unrepresentable rather than rejected.**
  `pool[i]` for `i` outside `0..n-1` cannot name a title. A hallucinated UUID
  is a well-formed identifier that denotes nothing; a hallucinated IMDb id is
  worse, because it may denote **a real film the household does not own**, and
  a validator that only checks *resolvability* would pass it.
- **The pool becomes the whole security boundary**, and it is one small,
  local, testable object. Everything the model can possibly say about which
  titles to show is a choice among things the household could already see.
- **A total drop is legible.** Separate counters mean "the model gave me
  nothing" and "my comparison was wrong" are different lines in the same
  report, which is the only reason the second one below was ever found.
  (**Amended: five counters, not two** — see the amendment in Decision above.
  This bullet read *"two counters and two reasons"* and the argument is
  unchanged by the count.)
- **Three times the prompt budget back.** A 200-title pool addressed by UUID
  is 9,041 prompt tokens before the history or the instructions; by index it
  is 2,924. On a 16k-context model that is the difference between a pool that
  fits and one that does not. ⚠️ **Both figures are the *probe* prompt's, and
  the shipped prompt is 47% larger** — see the correction under Evidence. The
  ratio between the arms is what this bullet claims and it is unaffected; the
  absolute number is not the one to quote for a budget.

**Given up:**

- **The index is meaningless outside its generation**, so nothing may persist
  one. `curated_rows` stores resolved `title_id`s, and a stored index would be
  a foreign key into a list that no longer exists.
- **A stable id would have let the model reason about titles across calls.**
  It cannot, and nothing wants it to: each generation is independent by
  construction, which is also what makes `replace_for_user` atomic.

**Also:**

- **The order the model returned is preserved.** A curated row *is* an
  ordering; re-sorting its cards by popularity or by year discards the only
  judgement the completion was bought for. Pinned by a case, because the
  hydration path is shared with nine providers that do sort.
- **This is where [ADR-0014](0014-absence-is-not-zero.md) lands for curation.**
  A row that loses cards to validation is shortened, and if it falls below the
  minimum it is discarded **whole** rather than padded from the pool. A padded
  row is a fabricated recommendation wearing a model's reason string.

**Rejected:**

- **Trusting `response_format`.** The subject of this ADR. It is a guarantee
  about shape from *one* provider version, it does not survive a `base_url`
  change ([ADR-0027](0027-the-llm-client-is-one-http-call.md) makes that a
  setting), and shape was never the risk.
- **Constraining ids with a JSON-schema `enum` of the pool.** Genuinely
  attractive — guided decoding would make an out-of-pool id structurally
  impossible — and refused because it is a 200-member enum inlined into every
  request, it is honoured by a subset of providers, and it would make the
  validator *look* redundant on the deployment where it was tested and be the
  only defence on every other. Availability-dependent safety is not safety.
- **Names instead of ids.** "Return the titles you chose by name" and match
  them back is the same problem with fuzzy matching added: 81,054 lower-cased
  names in this catalog are shared by more than one title (measured in M6).
- **Dropping the validator when the provider enforces the schema.** The
  performance argument is nil — it is a set membership test over ≤ 200 items.

## Evidence

All measured 2026-08-06 UTC against a local vLLM serving `gemma-4-26b-a4b`,
over one pool of **200 real feature films** sampled from the on-disk IMDb
dumps (`numVotes >= 50,000`, `titleType = movie`, `random.Random(20260806)`),
five completions per arm at `temperature = 0.8`, identical prompt but for the
handle column.

**Handle shape — the UUID arm loses on every axis at once:**

| handle | out-of-pool | prompt tokens | output tokens | median latency |
|---|---|---|---|---|
| **integer index `1…200`** | **0 / 105 — 0.0%** | **2,924** | **316** | **1,995 ms** |
| IMDb `tt` id | 2 / 107 — 1.9% | 4,050 | 440 | 2,794 ms |
| UUID | 3 / 105 — 2.9% | 9,041 | 957 | 6,474 ms |

**0.0% is not the reason for rule 1 and must not be quoted as one.** It is 105
identifiers from one model on one pool. The reason is that an index is
bounds-checked and the other two are not.

⚠️ **The three arms above were probes, not the shipped prompt, and the token
column is 47% low for what actually ships.** Re-measured 2026-08-07 against the
prompt `curation_prompt.build_prompt` renders, same model, same pool size:

| | probe (2026-08-06) | shipped prompt (2026-08-07) |
|---|---|---|
| prompt tokens, pool 200, cold start | 2,924 | **4,304** |
| prompt tokens, pool 200, 3 history lines | — | **4,359** (+47% on the probe) |
| implied per candidate | 14.6 (a total ÷ a count) | — |
| **marginal** per candidate, 8 → 200 | — | **20.40** |
| **marginal** per candidate, 200 → 600 | — | **20.45** |
| output tokens, median | 316 | **219.5** (192–277, −31%) |
| latency, median | 1,995 ms | **1,420 ms** (1,230–1,787) |

**The +40% per candidate is one rendering decision**: the shipped candidate
line appends the title's genre list (`curation_prompt._genres`) and the probe's
did not. **The marginal figure is the one to use** — 14.6 was a whole prompt
divided by its candidate count, so it charged the instructions and the shape
example to the candidates. The two marginal measurements agreeing to 0.05 of a
token across a 75× range in pool size is what makes it a *rate* rather than a
sample. History costs **~18 tokens a line** (three lines, 55 tokens), so a
household that has genuinely finished 25 films pays ~460 for `HISTORY_SIZE`.

**Output tokens and latency moved the other way and neither disturbs anything
here.** Fewer output tokens for a longer prompt is what a more specific
instruction block buys; the latency improvement is a busier probe host on the
first evening as much as anything, and neither number was load-bearing for any
decision in this ADR.

✅ **Confirmations from the same run, one of them stronger than the claim it
confirms.** All 2026-08-07, `gemma-4-26b-a4b`, 36 completions of a 45-completion
bound:

- **Integer handles: 0 out-of-pool over 405 ids, 20 generations, 5 pool
  shapes.** 3.9× the denominator the 0.0% above was measured on. Still not a
  guarantee, and still not the reason for rule 1.
- **`strict: true` is honoured for the *numeric* bound and not only the
  shape.** With `maximum: 5` declared against a prompt begging for numbers
  1–200, **zero** integers above 5 appeared across 2,048 output tokens. That
  is what makes stating the bound in the schema worth doing at all — and it
  changes nothing about rule 2, because the bound the schema can enforce is
  *range* and the thing the validator checks is *membership of what was sent*.
- **The pool that cannot answer narrows rather than inventing, across four
  hostile shapes** — pool 8, pool 5, 200 unknown titles, 200 bare-number
  titles; 199 ids, 0 out of pool. The 2026-08-06 result below was one shape;
  this is four, including two where the pool is smaller than one row.
- 🔴 **An unsatisfiable *value* bound makes guided decoding loop to the
  ceiling.** `maximum: 5` against a prompt asking for 1–200 produced
  `1,2,3,4,3,1,2,3,4…` for the entire 2,048-token budget; `finish_reason ==
  "length"` fired and the adapter's truncation guard refused it. **The first
  live firing of that defence**, and its real justification is stronger than
  the docstring's *"rows missing off the end"*: it is what stops a **degenerate
  loop being read as a valid answer**. It also **vindicates `_schema`'s
  deliberate omission of `minItems`** — with the card floor left as a
  `description` hint, the pool-8 and pool-5 arms **narrowed** to 2–3 cards and
  were discarded as `row_too_short`, which is counted and legible, rather than
  looping at full price.
- **Zero rows ⇒ `ok = false`, confirmed live**, with the reason, the tokens and
  the cost recorded in full and `curated_rows` untouched. Rule 3 is now an
  observed behaviour rather than only a designed one.
- **`cost_usd` arithmetic holds end to end.** `0.00000000` against the local
  model — the honest value; with prices 3/15 per Mtok configured,
  `0.01658700`, which is exactly `Decimal((4359×3 + 234×15) / 1e6)`. The column
  is `numeric` and `SUM()` agrees to 8 decimal places.

**The pool that cannot answer the question.** Four rows demanded on Studio
Ghibli, Kurosawa and the French New Wave, over a pool containing none of them:
under `json_schema` the model **narrowed rather than invented** — 0 out-of-pool
over 81 ids, with visibly fewer ids per row than the satisfiable prompt's 105
over 15. Worth recording because it is the failure this milestone most feared
and it did not reproduce here.

🔴 **The finding that produced rule 2, and it inverted its own first reading.**
The same hostile prompt without guided decoding (`response_format:
{"type": "json_object"}`) first scored **113 of 113 identifiers out-of-pool**,
which reads as catastrophic hallucination. It was not. Re-run with the
comparison instrumented over 108 ids:

| comparison | out-of-pool |
|---|---|
| `id in set[str]` — the obvious spelling | **108 / 108 = 100.0%** |
| `str(id).strip() in set[str]` | **0 / 108 = 0.0%** |

`json types seen = {'int': 108}`. The model returned **the right identifiers
with the wrong JSON type**, on every one. Not a single id was invented.

**What that ships as:** a generation that called the model, got a good answer,
wrote an `llm_calls` row with `ok = true` and real token counts and a real
cost, dropped every row in validation, and left the household with no curated
rows — which is byte-for-byte the state of a household whose model had nothing
to say. No exception, no failed job, no log line; and
[08](../08-operations.md)'s degradation table reads *"previous curated rows
persist"*, so even the screen looks deliberate. **Rule 3 exists because of
this run**: the ledger must be able to say that the call succeeded and the
generation did not.

**And with no `response_format` at all, 5 of 5 responses were unparseable** —
every one wrapped in a ` ```json ` fence. The adapter strips fences before
parsing, which is a measurement rather than defensive coding.

## Uncertainty

⚠️ **Every rate here is one model, one pool, one evening — 45 completions
total.** A frontier model will do better and a 3B model will do much worse.
What transfers is the *ordering* of the three handle arms and the *shapes* of
the two failures; the percentages do not.

~~⚠️ **Pool size is untested at the boundary.**~~ 🔴 **Measured 2026-08-07, and
the answer is that `USHER_CURATION_POOL_SIZE`'s `le=1000` is a bound the
reference endpoint cannot serve.** This paragraph read *"whether the index
scheme's accuracy degrades at 500, and where the prompt stops fitting a small
context, are unmeasured"*. Against the local vLLM (`gemma-4-26b-a4b`,
`max_model_len` 16,384) with the shipped defaults:

| pool | result |
|---|---|
| 600 | **works** — 12,540 prompt tokens |
| 700 | **HTTP 400** |
| 1,000 | **HTTP 400** |

**Accuracy did not degrade** — the 600 arm is inside the 0-out-of-405 result
above. What fails is arithmetic: the constraint is
`prompt_tokens + llm_max_output_tokens ≤ max_model_len`, and **nothing couples
the two settings**, so raising `USHER_LLM_MAX_OUTPUT_TOKENS` silently lowers the
workable pool with no warning anywhere. The **mechanism** is right and was
verified end to end — the adapter's 4xx branch translated the 400 to
`PortDataMalformed` and `JobWorker` parked immediately rather than spending four
more completions on the same wall, which is trap 13 firing exactly as designed.
The **bound** is the problem, and it is a promise this milestone's own reference
endpoint cannot keep.

🔶 **The ceiling is deliberately not lowered to 600.** 600 is *this* endpoint's
answer, and [08](../08-operations.md)'s whole argument for the setting is that
the right number is a deployment fact — a 200k-context hosted model has a very
different one. What a fix would have to look like is a startup check that knows
`max_model_len`, which no setting in this project carries; recorded as a known
limit in [08](../08-operations.md) rather than solved here.

🔴 **Nothing here measures whether the rows are any *good* — and 2026-08-07
measured something adjacent, which came back badly.** Validation guarantees
that every card is a title the household could watch and that the row has
enough of them. It cannot distinguish an insightful shelf from five titles that
were adjacent in the prompt, and no assertion in this repository ever will.
What *was* counted, over 59 headings from 20 live generations: **52 of 59
(88%) are genre labels**, which the prompt explicitly forbids (*"a mood, a
period, a theme, a filmmaker — rather than by one genre"*); exactly **one**
heading named a filmmaker; and three headings recur **verbatim across three
separate generations each**. So on this model the curated shelf is
substantively what `GenreAffinityProvider` already produces for free, from a
`SELECT`, at no cost and no latency. That is the milestone's central product
risk, it is recorded in [06](../06-rows-and-recommendations.md) rather than only
here, and it does not disturb any decision in this ADR — every one of those 59
headings sat over cards that were real, owned-or-reachable, and in the pool.
It is why curated rows are additive: [08](../08-operations.md)'s *"Home composes
without them"* is what makes a bad row a disappointment rather than a defect.

⚠️ **One model, one evening.** A frontier model may well obey the
group-by-something-other-than-genre instruction; the 88% is not a property of
"an LLM" and must never be quoted as one. What transfers is that **the
instruction is not self-enforcing and nothing in this system checks it**, which
is a property of the design and not of the model.
