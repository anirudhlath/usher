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
  fits and one that does not.

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

⚠️ **Pool size is untested at the boundary.** 200 was specified by
[06](../06-rows-and-recommendations.md) and everything here used it. Whether
the index scheme's accuracy degrades at 500, and where the prompt stops fitting
a small context, are unmeasured — `USHER_CURATION_POOL_SIZE` exists so the
question is answerable without a release, and the 400 that a too-large prompt
produces parks with the token count in its message rather than retrying into
the same wall.

⚠️ **Nothing here measures whether the rows are any *good*.** Validation
guarantees that every card is a title the household could watch and that the
row has enough of them. It cannot distinguish an insightful shelf from five
titles that were adjacent in the prompt, and no assertion in this repository
ever will. That is the honest limit of this milestone's guarantees, and it is
why curated rows are additive: [08](../08-operations.md)'s *"Home composes
without them"* is what makes a bad row a disappointment rather than a defect.
