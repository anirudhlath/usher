# ADR-0027 — The `LLMClient` is one HTTP call, and `litellm` is not taken

**Status:** Accepted — corrects [01](../01-architecture.md),
[06](../06-rows-and-recommendations.md) and
[10](../10-telemetry-and-dashboards.md)

## Context

Three PRD sections name `litellm`, and they have named it since M1:
[01](../01-architecture.md)'s ports table says `LLMClient → LiteLLMClient` and
its stack table says *"LLM | litellm (provider-agnostic)"*;
[06](../06-rows-and-recommendations.md)'s generation algorithm says *"One
structured call **via litellm**"*; and
[10](../10-telemetry-and-dashboards.md) rests a design decision on it —
*"`litellm` reports per-call cost natively, so cost analysis is exact SQL
rather than estimated counters."*

So this is not a choice being made for the first time. It is a shipped
decision in three documents, and reversing it needs better evidence than a
preference.

**The case for litellm is real and should be stated first.** One interface
over every provider, native cost accounting, retries and fallbacks, and
[00](../00-overview.md) records that Alfred — the voice assistant this project
expects to integrate with — already uses it, so a shared stack means
*"integration is native rather than another HTTP hop."* A reasonable person
adds `litellm` to `pyproject.toml` and writes forty lines.

## Decision

**`ports.llm.LLMClient` is implemented by `OpenAICompatibleClient`, one
`POST /v1/chat/completions` through the `httpx.AsyncClient` this project
already depends on.** `litellm` is not a dependency, and the three sections
above are corrected in place.

**The provider abstraction is `USHER_LLM_BASE_URL`.** OpenAI, OpenRouter,
Together, Groq, DeepSeek, Mistral, vLLM, llama.cpp, Ollama and LM Studio all
serve the same route. What litellm adds *over a base URL* is Anthropic's and
Gemini's **native** wire formats — and both are reachable through OpenRouter,
which speaks this one.

**Cost is computed from two configured prices, not read from the response**,
because no provider reports it — see Evidence.

## Consequences

**Gained:**

- **Zero new distributions and zero new megabytes**, on a project whose image
  is 356 MB and which has already refused one dependency of this exact shape
  ([ADR-0022](0022-the-embedder-is-optional-and-its-contract-is-measured.md)).
- **One HTTP stack.** `litellm` brings `aiohttp` and five accessories into a
  process that already has `httpx` — and `HTTPXClientInstrumentor` is wired,
  so the shipped stack is the one whose calls appear in a trace.
- **The error taxonomy is ours.** M4 learned against TMDb that a 4xx which is
  not a 429 is `PortDataMalformed` rather than `PortUnavailable`, because five
  retries reach the identical answer. A library that normalises provider
  errors into its own hierarchy is a second taxonomy to translate, and the
  translation is where that lesson gets lost.
- **The credential path is short enough to audit.** [08](../08-operations.md)
  requires that *"never logged" covers libraries Usher hands a credential to*.
  One `Authorization` header on one client is a claim that can be checked by
  reading forty lines.

**Given up:**

- **Anthropic's and Gemini's native APIs.** Reachable through OpenRouter, and
  a household that insists on a direct Anthropic key gets a second
  `LLMClient` implementation — which is one file behind an existing port,
  which is what the port is for.
- **litellm's retry/fallback machinery.** This project has its own: the job
  queue, with equal jitter, a park-on-malformed rule and an attempt ceiling.
  A second retry layer *inside* a job handler would multiply against it.
- **A bundled price table.** litellm ships per-model prices and keeps them
  current. Two settings do not. What that costs is real and is recorded in
  Uncertainty.
- **Shared-stack symmetry with Alfred.** [00](../00-overview.md)'s sentence
  stands for Alfred; it is an argument about *Alfred's* dependencies, and
  Usher's side of that integration is an HTTP surface either way.

**Also:**

- **The reference deployment is local, which is what makes `base_url`
  sufficient rather than merely adequate.** This project is self-hosted by
  construction; the live verification for this milestone ran against a vLLM on
  the same host as Postgres. For that deployment litellm's provider breadth is
  a dependency paid for a capability nobody uses.
- **`adapters/llm/` keeps its capability name.** [01](../01-architecture.md)'s
  naming rule says a directory is named for the upstream *"when a port's
  implementation talks to one nameable external service"*, and gives
  `litellm` being a multi-provider abstraction as the reason `llm/` is
  capability-named. The reason changes and the name does not: an
  OpenAI-compatible client has no single upstream either — its upstream is
  whatever `base_url` points at.

**Rejected:**

- **The `openai` SDK.** The honest middle option, and measured: +12 MB and 5
  distributions, 380 ms of import. It buys typed request/response models for
  one endpoint this project calls once. Cheaper than litellm and not free, for
  a POST.
- **Waiting for M9.** The port has existed since M1 with no implementation and
  `LLMPurpose.QUERY_EXPANSION` is a member nothing emits — which this project
  treats as a defect wherever else it appears. *(That second clause described
  the state on 2026-08-06 and stopped being true on 2026-08-07:
  `QueryExpansionService` emits it, so both members of the vocabulary are now
  live. The rejection stands on its first clause, and the sentence is kept
  because it is the argument that was made.)*

  🔴 **Amended 2026-08-07, later the same day: `QUERY_EXPANSION` is live in the
  code and is emitted by no default deployment.** The retrieval measurement in
  [05](../05-search-and-similarity.md) — MRR 0.733 → 0.373 against a local
  `gemma-4-26b-a4b` — put query expansion behind its own setting,
  `USHER_QUERY_EXPANSION_ENABLED`, off even where `USHER_LLM_ENABLED` is on. So
  "both members of the vocabulary are now live" is true of `src/` and false of
  any deployment that has not opted in, and PRD 10's `usher.llm.purpose`
  group-by will read `curation` only on almost every install. **This does not
  disturb the decision**: the client is one `POST /v1/chat/completions` either
  way, it is bought by curation alone, and if anything the argument against a
  146 MB dependency is stronger now that one of its two consumers is opt-in.

## Evidence

Measured 2026-08-06 UTC on this host, Python 3.13, marginal cost over a venv
already holding Usher's own runtime dependencies (106 MB, 114 site-packages
entries):

| option | venv after | marginal | distributions | `import` (cumulative) |
|---|---|---|---|---|
| `litellm` 1.95.0 | 252 MB | **+146 MB** | **29** | **1,343 ms** |
| `openai` SDK | 118 MB | +12 MB | 5 | 380 ms |
| **plain `httpx` — ships** | 106 MB | **+0 MB** | **0** | 41 ms |

**The 29 distributions are the argument, not the 146 MB.** They are
`aiohttp` + `aiohappyeyeballs` + `aiosignal` + `frozenlist` + `multidict` +
`propcache` + `yarl` (a second async HTTP stack); `huggingface-hub` +
`hf-xet` + `filelock` + `fsspec` + `tokenizers` + `tiktoken` (a model-download
client and two tokenizer runtimes); and the `openai` SDK itself. That middle
group is precisely what
[ADR-0022](0022-the-embedder-is-optional-and-its-contract-is-measured.md)
declined `sentence-transformers` for: a runtime pulled unconditionally for a
capability the deployment may never use.

1,343 ms is the number that would be felt. Imported at module scope it is
1.3 s on `usher --help`. Lazily, as `websockets` and `fastembed` are, it is
1.3 s on the first curation — which is affordable, and is a workaround for a
dependency taken to avoid writing a POST.

🔴 **The premise of [10](../10-telemetry-and-dashboards.md)'s sentence is
false, and it is false independently of this decision.** Measured against a
live OpenAI-compatible endpoint: the `usage` object carries `prompt_tokens`,
`completion_tokens` and `total_tokens` and **no cost field at all**. litellm
does not *report* cost natively — it *computes* it, from a price table it
bundles. So "exact SQL rather than estimated counters" was describing a lookup
either way; the only question is whose table it is and how it ages. PRD 10 is
corrected, and `llm_calls.cost_usd` records the price that was applied at the
time so a later price change cannot rewrite history.

⚠️ **`litellm.__version__` does not exist** — the module's `__getattr__`
raises `AttributeError` for it. Use `importlib.metadata.version("litellm")`.
Recorded because the obvious probe looks like a broken install.

**Structured output works on the shipped path**, which is the capability the
dependency would mostly have been bought for: `response_format:
{"type": "json_schema", "json_schema": {..., "strict": true}}` returned
schema-conformant JSON in 314 ms.

✅ **Confirmed under load on 2026-08-07, and two of the confirmations are
stronger than what was claimed here.** Same endpoint, 36 completions over a
real 1,271,138-title catalog:

- **`strict: true` holds the *numeric* bound and not only the shape.** With
  `maximum: 5` declared against a prompt begging for numbers 1–200, **zero**
  integers above 5 appeared across 2,048 output tokens. The 314 ms probe
  established shape conformance; this establishes that the constraint solver is
  real, which is what makes stating a bound in the schema worth the bytes.
- 🔴 **The 4xx branch fired live for the first time, and it fired on the case
  the taxonomy was designed for.** `USHER_CURATION_POOL_SIZE` at 700 and at
  1,000 both produced **HTTP 400** from the reference endpoint (600 works, at
  12,540 prompt tokens); the adapter translated each to `PortDataMalformed` and
  `JobWorker` **parked immediately** rather than spending four more completions
  reaching the identical answer. That is M4's TMDb lesson — *a 4xx that is not
  a 429 is permanent* — holding against a second upstream, which is the
  strongest available evidence for *"the error taxonomy is ours"* above. See
  [ADR-0028](0028-the-pool-is-the-contract.md) for why the *bound* is
  nonetheless a promise the reference endpoint cannot keep.
- **The computed cost is exact.** `cost_usd` is `0.00000000` against the local
  model — the honest value — and with prices 3/15 per Mtok configured it is
  `0.01658700`, exactly `Decimal((4359×3 + 234×15) / 1e6)`, on a `numeric`
  column whose `SUM()` agrees to 8 decimal places. So *"cost is computed, not
  read"* is verified end to end, and the price table this decision declined to
  bundle is genuinely the only thing the two settings replace.
- **An unsatisfiable *value* bound makes guided decoding loop to the ceiling**
  — `1,2,3,4,3,1,2,3,4…` for the full 2,048-token budget, `finish_reason ==
  "length"`, refused by the adapter's truncation guard. **The first live firing
  of that guard**, and it is the reason the guard is not merely tidiness: it is
  what stops a degenerate loop being read as a valid answer.

⚠️ **All four are still one endpoint.** The Uncertainty below is unchanged.

## Uncertainty

⚠️ **The price table is now an operator's problem, and it will go stale
silently.** `USHER_LLM_PRICE_IN_PER_MTOK` / `..._OUT_PER_MTOK` default to `0`,
which is the *honest* value for the local model this was verified against and
the *wrong* value for a hosted one an operator forgot to price. The failure is
a cost dashboard reading zero, which is
[10](../10-telemetry-and-dashboards.md)'s own "permanently empty panel
indistinguishable from a healthy zero" — mitigated only by `tokens_in` /
`tokens_out` being recorded exactly, so spend is recomputable from the ledger
after the fact. A price table that shipped with the code would be a third-party
dataset in the repository, which rule 1 of [04](../04-catalog-bootstrap.md)
forbids for other reasons.

⚠️ **Two providers' quirks are unmeasured.** Everything here was verified
against vLLM and against nothing else. Providers differ on whether
`json_schema` is honoured (the fallback to `json_object` and to fence-stripping
exists for that, and both are tested), on whether `strict: true` is accepted at
all, and on 429 semantics including `Retry-After` — which TMDb has still never
sent in this project's history either.

⚠️ **This decision is cheap to reverse and that is part of why it is takeable.**
`LLMClient` is a port with two methods. If a household needs Anthropic's native
API, or if a price table becomes worth maintaining, `LiteLLMClient` is a second
implementation and a dependency — added deliberately, against a measured need,
rather than inherited from a sentence written before anything called it.
