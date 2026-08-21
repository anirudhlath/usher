# Usher quality evals — design

**Date:** 2026-08-18
**Status:** agreed, not yet planned
**Scope:** a standing, re-runnable harness that measures the *quality* of the
four PRD surfaces a green test suite cannot judge — search relevance, suggest
typo-tolerance, similarity, and rows/curation — against pre-registered bars,
with a fingerprinted trend ledger charted in the Grafana this box already runs.

This is a point-in-time design handed to an implementation plan. The PRD is
authoritative when the two disagree.

---

## 1. Why this exists

Usher has ~4,000 unit and ~1,200 integration cases, mutation-swept, with a
five-step gate. That machinery proves the code does what the code says. **It
cannot judge whether the answers are any good**, and this project's own history
is that the quality claims are where the promises break:

| promise | what actually happened |
|---|---|
| PRD 05 typo-tolerant type-ahead | ADR-0002's gate **failed** on both halves, 2026-08-03 — 27.8% recall@5 on 2–4-char names against a 0.75 bar, 0.0% on transposition |
| PRD 06 *"group by something a person would recognise"* | **88% of generated headings (52 of 59) were the genre labels the prompt forbids**, 2026-08-07 |
| PRD 05 query expansion | measured **worse** than no expansion, M8 |
| PRD 05 tag-genome similarity term | **removed** at a 2.4746% pair rate against a 10% floor (ADR-0035) |

Every one of those was found by an **ad-hoc script written for one milestone
and then never run again**. The 88% finding is the sharpest case: the rules
file records that *"the prompt's grouping instruction is not self-enforcing and
nothing in this system checks it"* — a known, measured, unguarded regression
surface. Nothing re-measures it today, so nobody would notice it getting worse.

**This design turns that per-milestone pattern into a standing harness.** It
does not invent a new discipline; it generalises the one `scripts/measure_*.py`
already proved: a bar written down before the numbers, a seeded golden set
regenerated from the real catalog and never committed, and a run against real
services outside the working tree.

### Non-goals

- **Not a correctness suite.** It does not replace or duplicate `tests/`.
- **Not a PRD conformance matrix.** Mapping every PRD sentence to a check is a
  separate project (considered, deferred — see §12).
- **Not live third-party verification.** The Emby/TMDb live-run harness
  (M3/M4/M9's H4/H5 shape) is a separate project, also deferred.
- **Not in the merge-blocking gate by default.** See §9 for what may gate.

---

## 2. Decisions, with their evidence

Every build-vs-borrow call below was **measured on this host on 2026-08-18**,
not argued. The measurements are recorded here because a decision resting on a
claim about a third party is one measurement away from being wrong — which is
how ADR-0027 (litellm) and C4's SVG refusal both went.

### 2.1 `ranx` for the IR metrics — ADOPTED

Resolved against Python 3.13 in a scratch venv:

| candidate | packages | notes |
|---|---|---|
| `ranx` 0.3.21 | ~30 (incl. `numba`, `llvmlite`, `matplotlib`, `pandas`, `scipy`, `seaborn`) | adopted |
| `ir_measures` 0.4.3 | 4 (`numpy`, `scipy`, `pytrec-eval-terrier`) | fallback |
| `pytrec_eval` 0.5 | 1, zero deps | unmaintained original |
| hand-rolled | 0 | rejected |

Weight is **not** disqualifying here and the repo's own precedent says so:
`embedding = ["fastembed>=0.8"]` is an accepted **28-package, 167 MiB** extra.
CI runs `uv sync --frozen`, so an extra costs the production image and the
existing gate nothing.

What `ranx` buys that hand-rolled arithmetic does not (verified by
introspecting the installed package): `Qrels`/`Run`, `evaluate`, `compare`,
**`statistical_tests`**, `plot`/`Report`, and a `fusion` module containing
**`rrf`** plus `optimize_fusion` and 30 other fusion methods.

Two of those are directly load-bearing for Usher rather than generic:

- **`statistical_tests`** answers the question a trend ledger inevitably
  raises. M7 Task 36 had to argue *by hand* whether 83.4% → 82.1% mattered
  against a 2.0-point bar. That is a paired significance test and it is
  annoying to get right by hand.
- **`fusion.rrf` + `optimize_fusion`** — Usher *ships* RRF hybrid fusion and
  `search-and-embeddings.md` records *"RRF's five traps"*. This makes fusion
  weights an empirical question instead of a reasoned one.

**Residual risk:** `numba`/`llvmlite` is a JIT with an ABI-pinned LLVM that
historically lags new CPython. It resolves on the pinned 3.13.14 today; a 3.14
move could block the **eval extra** (not the project) for a while. Also
observed directly: `ranx` 0.3.21 emits `SyntaxWarning: invalid escape sequence`
on 3.13 — cosmetic now, a hard error in some future CPython.

**Mitigation:** `ranx` sits behind `eval/metrics/`, which owns all `Qrels`/`Run`
construction and exposes Usher's own scoring interface. Swapping to
`ir_measures` is then a one-file change.

### 2.2 `deepeval` for the LLM-judge half — ADOPTED

**A spike was run against the live vLLM on 2026-08-18, bounded at ≤15
completion requests, with no writes to any Usher database.** Model served:
`gemma-4-26b-a4b` (`cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit`, 32k context) — the
same model M8's curation run measured.

| question | measured answer |
|---|---|
| Fully offline? | **Yes.** 0 non-local sockets at import *and* during evaluation with `DEEPEVAL_TELEMETRY_OPT_OUT=1` |
| Import-time side effects (issue #2497: OTel hijack, Sentry, `sys.excepthook`) | **Not reproduced in 4.1.8** — 0 connections at import, opt-out or not |
| Works with `gemma-4-26b-a4b`? | **Yes, with `format="json"`** |
| Without `format="json"`? | **Deterministic hard failure, 4/4 runs**, on the commonest real input |
| Verdict quality | correct on every case once parsing succeeded |
| Integration cost | built-in `LocalModel` worked directly; **no `DeepEvalBaseLLM` wrapper needed** |
| Footprint | 66 packages / 88 MB, version 4.1.8 |
| Latency | 0.6–1.4 s per node per case, local |

**The probe was self-validated before its result was believed.** A CPython
audit hook counting `socket.connect` reported zero events for deepeval — and
also reported zero for the control run *without* the opt-out, which proves
nothing on its own. The hook was then pointed at a known-good connection
(`127.0.0.1:8000`) and reported 1 hit, establishing it can fail. Only then was
the zero taken as a result. *A run that did not run is not a pass.*

**The `format="json"` finding is the one to carry.** Unconfigured, the DAG
metric raised `DeepEvalError: Evaluation LLM outputted an invalid JSON. Please
use a better evaluation model.` on the input `"Action"`, 4 times out of 4. The
raw model response was:

```
'```json\n{\n  "verdict": true,\n  "reason": "The output \'Action\' is a single,
bare genre label without any additional descriptive text or formatting."\n}\n```'
```

— i.e. **correct JSON, with the correct verdict, wrapped in a markdown fence**,
which a single `removeprefix("```json")` parses. The framework's advice ("use a
better evaluation model") is advice this deployment cannot take: the model *is*
the deployment. With `format="json"` the same cases score correctly (`"Action"`
→ 0.0, `"Christopher Nolan's puzzle-box thrillers"` → 1.0).

**Therefore `format="json"` is pinned in code with this measurement in a
comment beside it.** Nobody should rediscover this the hard way.

**Papercut, recorded:** `DAGMetric` normalises scores to 0–1. A threshold
expressed in the node's own score units (`threshold=7` against
`VerdictNode(score=10)`) silently fails **everything**. Bars are therefore
expressed in normalised units and a unit-assertion case guards it.

**What we take:** `Golden`/`EvaluationDataset`, `evaluate()` (async
concurrency), `DAGMetric` + node types, and the contextual metrics (§5.1).

**What we do not take:** the ~20 RAG-chatbot built-ins that have no Usher
surface (faithfulness, hallucination, answer relevancy over prose answers), the
Synthesizer (§6.3), and the cloud platform (§2.4). **Also declined:
`assert_test`/`deepeval test run`** — §2.5 rejects making the harness a pytest
suite, and reaching for pytest inside the CI job reintroduces exactly that
confusion for no gain: `runner.py` already knows the bars, so CI gates on its
exit code. The one thing pytest owns here is `tests/unit/eval/`, which tests the
*harness*, not the *quality*.

### 2.3 Grafana + Postgres for the charts — ADOPTED, zero new infrastructure

This box already runs `grafana/grafana:13.1.3`, `prom/prometheus:v3.13.2`,
`grafana/loki:3.7.6` and `grafana/tempo:3.0.0`. **PRD 10 has already decided
the question**, line 12:

> *What is in the library, what do I watch, what did it cost* → **Postgres**,
> queried directly by Grafana

Eval scores are on-demand rows with rich dimensions (seed, fingerprint, judge
model, stratum), not a running service's time series — so Postgres-queried-by-
Grafana, exactly as PRD 10 splits it. This follows the pattern PRD 10 already
established (*"Two tables exist specifically to make the interesting dashboards
possible"* — `curated_rows`, `llm_calls`, `search_queries`); `eval_runs` is the
next one, and the eval view is **dashboard 6**.

A benefit no external tool could give: because eval scores live in the same
database as `search_queries`, `llm_calls` and `curated_rows`, *"did the run
where recall@5 dropped coincide with the embedding re-index?"* is a join, not a
cross-tool eyeball.

### 2.4 Confident AI cloud — DECLINED, on its published limits

Free tier is genuinely $0/month "forever", and genuinely capped:

| free tier | value |
|---|---|
| test runs | **5 per week**, "additional runs locked" |
| trace spans | 1 GB-month, additional dropped |
| projects / seats | 1 / 2 |
| retention | **not stated** (paid plans advertise unlimited) |

**A nightly full eval alone is 7 runs/week — over the cap before a single push
or local run.** Self-hosting the platform is **Enterprise-only** (containerised,
but sales-gated behind an enterprise software licence); the platform is not
open source and deepeval ships no local dashboard. Unstated free-tier retention
is disqualifying on its own for a *trend* ledger: history that silently ages
out is not a trend.

Declined. §2.3 is strictly better on every axis that matters here and needs no
account. Recorded rather than silently dropped, so it is not re-opened without
new numbers.

### 2.5 Standalone `usher.eval` package — ADOPTED

Rejected alternatives: formalising `scripts/measure_*.py` (second-class, and
CLAUDE.md already records those scripts drifting), and a `tests/eval/` pytest
suite (pytest's pass/fail semantics fight trend-tracking, and a
skipped/misconfigured eval reads green — the exact "a run that did not run is
not a pass" trap).

---

## 3. Architecture

`usher.eval` is a top-level **consumer** subsystem, peer to `api/` and `cli`.

```
src/usher/eval/
├── __init__.py
├── goldens/          # seeded, catalog-derived ground-truth generators
│   ├── search.py
│   ├── suggest.py
│   ├── similar.py
│   └── rows.py
├── metrics/          # the ONLY module that imports ranx
│   ├── ir.py         # Qrels/Run construction, recall@k, MRR, nDCG@k, P@k
│   └── significance.py
├── judge/            # the ONLY module that imports deepeval
│   ├── model.py      # LocalModel wiring; format="json" pinned here
│   ├── graphs.py     # the DAGs, one per judged property
│   └── calibration.py
├── bars.py           # loads and hashes the pre-registered bar file
├── ledger.py         # eval schema writes + JSONL append
├── fingerprint.py    # catalog/model/code provenance
├── runner.py         # generate → run → score → compare → record
└── surfaces/         # one module per surface, wiring the four together
```

**Layering.** `eval/` may import `domain/`, `ports/`, `services/`, `db/` and
`adapters/`. **Nothing outside `eval/` may import `eval/`** — enforced as the
**eleventh import-linter contract**, verified in both directions by planting an
import in its isort position (the careful spelling; the careless one dies on
ruff `F401`, which is the wrong way round for a guard).

**It never reimplements a feature.** It drives the real `SearchService`,
`PostgresSuggestIndex`/`PrefixSuggestIndex`, `SimilarityService`, `HomeService`
and `CurationService` through the real composition root, and scores what they
return. An eval that reimplements the thing it measures measures itself.

**Dependencies land in one extra:**

```toml
eval = ["ranx>=0.3.21", "deepeval>=4.1.8"]
```

Reached by `uv sync --extra eval`. Absent, `usher eval` exits with a directed
message naming that command — never a bare `ImportError`.

---

## 4. Ground truth

**Golden sets are generated from the real catalog at run time under a fixed
seed, and are never committed.** This is the M6 gate's own procedure
generalised — *"the test set is built from real catalog rows and is therefore
not committed — the measurement is"* — and it is what keeps the
ship-importers-never-data rule intact.

Every generator takes `(seed, size)` and is a pure function of
`(seed, size, catalog state)`. The catalog state is fingerprinted (§8) so that
two runs are only ever compared when their inputs match.

**Calibration label sets (§7.2) contain title names, which are third-party
metadata.** They are stored in the `eval` schema in Postgres, never in git.
Only the aggregate agreement number reaches the ledger and the repo.

---

## 5. The four surfaces

### 5.1 Search relevance — PRD 05

Drives `SearchService.search` over the real hybrid FTS + embedding + RRF path.

| golden family | construction | relevant set |
|---|---|---|
| exact-name | the title's own `name` | that title, grade 2 |
| partial-name | a token subset of the name | that title, grade 2 |
| descriptive | genres + year + up to 3 `credit_names`, rendered as a phrase | that title grade 2; same-collection titles grade 1 |

Sampled over `vote_count` strata so the set is not all blockbusters, with names
that are non-unique in the catalog excluded at sampling time (**81,054
lower-cased names are shared by more than one title** — measured 2026-08-03).

**Metrics:** recall@10, MRR, nDCG@10, and `semantic_coverage` reported
alongside (a search answering well with zero semantic coverage is a different
system from one answering well with full coverage).

**LLM-graded complement:** the descriptive family has genuinely weak synthetic
labels — a query built from *this* film's genres may legitimately be answered
by *another* film. So a sample of descriptive queries is additionally scored by
deepeval's contextual relevancy over the returned titles, reported as a
separate number and **never blended** into the deterministic one.

### 5.2 Suggest typo-tolerance — PRD 05, ADR-0002, ADR-0031

**Formalises the existing gate rather than inventing one.** The generation
procedure is already specified and is adopted verbatim so historical numbers
stay comparable: movies only, `vote_count >= 500`, non-unique names excluded,
five equal draws of 150 over `char_length(name)` bands 2–4 / 5–7 / 8–11 /
12–19 / 20+, four typo classes (substitution, deletion, transposition, doubled
letter) at a uniformly random position, `random.Random` **seed 20260803**,
yielding **2,993** cases (seven two-character names admit no deletion).

**Metrics:** recall@5 per band per typo class, plus p50/p95/max latency.
**Both tiers are measured separately and never averaged** — ADR-0031 ships two
tiers with very different latency profiles, and a mean over them describes
neither.

Bars are set against the **current shipped two-tier baseline**, not against
ADR-0002's original bars, which the shipped system already failed and which the
two-tier suggest was built in response to.

### 5.3 Similarity — PRD 05

Drives `SimilarityService` / `title_neighbors`.

**Ground-truth proxies, held out from the scoring signal:** same collection
(franchise), shared director, and high tag-genome overlap.

**The genome proxy is legitimate specifically because M9's S7 removed the
genome term from the similarity blend** (ADR-0035, 2.4746% pair rate against a
10% floor). While the genome was an input, using genome overlap as ground truth
would have been circular; now it is a genuine held-out signal. **If the genome
term is ever restored to the blend, this proxy must be retired in the same
commit** — recorded here because that is exactly the kind of coupling nobody
re-checks.

**Metrics:** precision@10 against the proxy labels, plus an LLM-judge
plausibility sample (§7).

### 5.4 Rows and curation — PRD 06

Two independent things, measured separately.

**(a) Recommendation relevance — taste holdout.** Hide a seeded sample of a
household's watched titles from `TasteService`/the candidate pool, compose the
home screen, and measure whether the held-out titles resurface. recall@k over
the composed screen, per provider and overall. This is the only surface whose
golden set needs a household with real watch history; where none exists the
surface reports **skipped-with-reason**, never a zero (a zero and an absence
are different facts, and only one of them is a regression).

**(b) Curation grouping — the 88% finding, guarded.** Every generated heading
is judged by the DAG in §7.1. **Primary metric: genre-label rate.** M8 measured
**88% (52 of 59)** on this model; the bar is set at first run against the
then-current baseline, and the *direction* is what the ledger watches.

This is the surface that most justifies the whole project: it is a known,
measured, unguarded quality failure that the correctness suite is structurally
unable to see.

---

## 6. Golden generation details

1. **Deterministic.** `random.Random(seed)`, never the global RNG, never
   `Date.now()`-style ambient state. Same seed + same catalog fingerprint ⇒
   byte-identical golden set.
2. **Regenerable, not stored.** Persisted only as a `deepeval`
   `EvaluationDataset` in a run-scoped temp dir when a judge run needs it, and
   deleted after. Never under `git`.
3. **Synthesizer declined for ground truth.** deepeval's Synthesizer generates
   goldens *with an LLM*, which makes labels non-reproducible and bakes the
   judge's biases into the thing that judges the judge. It is a defensible tool
   for *query paraphrases* if generated once and snapshotted under a seed;
   that is left as a documented extension, not built.

---

## 7. The judge, and why it is itself evaluated

### 7.1 Shape: bounded decisions, not scores

Every judged property is a `DAGMetric` over **binary** judgement nodes with an
explicit outcome→score mapping. No 1–5 vibes. The heading graph, verified
working in the spike:

```
root: "Is this heading nothing more than a bare genre label?"
  ├── true  → score 0
  └── false → "Does it name a SPECIFIC recognisable grouping —
               filmmaker, franchise, era, place, mood or theme?"
                ├── true  → score 10   (normalised 1.0)
                └── false → score 5    (normalised 0.5)
```

`temperature=0.0`, `format="json"` (§2.2), model id and prompt hash recorded in
every ledger row.

### 7.2 Calibration — the judge is not trusted until it agrees with you

**Neither deepeval nor ranx will tell you whether the judge agrees with the
person whose product it is.** An uncalibrated judge is an unvalidated oracle,
and a check that cannot fail is not a check.

- A **calibration set** of ~40 items per judged property is hand-labelled once
  by the operator (heading → genre-label yes/no; neighbour pair → plausible
  yes/no). Stored in the `eval` schema, never in git (§4).
- Before a judge's verdicts on the real set are admissible, it is scored
  against the calibration set. **Agreement below the calibration bar makes the
  run report `judge-uncalibrated` and emit no quality verdict at all** — an
  explicit refusal, not a low score.
- Calibration agreement is itself a ledger metric, so a judge-model swap that
  quietly degrades agreement is visible.

### 7.3 Variance — measured, never assumed

Judge runs repeat `N=3` at temperature 0 and record the score spread. **If the
spread exceeds the bar's own margin, the run reports `inconclusive` rather than
a number.** This is what prevents the 83.4-vs-82.1 argument-by-hand from
recurring, and it is the honest answer to G-Eval/DAG being non-deterministic by
construction (deepeval's own FAQ: task and judgement nodes still call an LLM).

---

## 8. Bars, fingerprints and the ledger

### 8.1 Bars are pre-registered and hashed

Bars live in a committed `docs/evals/bars.toml`. Every ledger row records the
**sha256 of the bar file it was judged against**, so a run proves which bar it
faced. A bar edited after seeing a number is then visible as a hash change
rather than invisible as a git blame nobody reads.

`/tmp` is tmpfs on this host, so no bar or run log is ever written there.

### 8.2 The fingerprint is what makes two runs comparable

**This is the single most important design element for CI**, because without it
eval CI gets disabled within a fortnight. If the catalog drifts — a bootstrap
re-run, an enrichment crawl landing, `m09e`-style embedding rebuild — scores
move for reasons unrelated to the diff, and the PR gets blamed.

Recorded per run: title count, enriched count, embedded count, media-item
count; embedding model name **and width**; `blend_fingerprint`; genome
vocabulary revision; git sha; seed; golden-set size; judge model id, prompt
hash and temperature; bar-file sha256.

**A run whose fingerprint differs from the baseline's is not comparable, and
the harness says so** — `baseline-invalid: catalog changed` — rather than
failing. That is *"a run that did not run is not a pass"* applied to eval
comparability.

### 8.3 Two sinks, deliberately

- **Postgres, `eval` schema** — `eval_runs` (one row per run, provenance +
  verdict) and `eval_scores` (one row per metric per stratum: value, bar,
  passed). Created idempotently by the harness from `eval/schema.sql`, **not in
  the alembic chain** — it is dev tooling, `alembic heads` must stay at one
  head, and production must never carry it. Grafana reads it directly.
- **`docs/evals/ledger.jsonl` in git** — one summary line per full run. Cheap,
  and it buys two things the table cannot: history survives a database rebuild
  (`m09e` already forced one full wipe), and a PR diff can *show* that a change
  moved recall@5 from .82 to .79.

---

## 9. CLI and CI

### 9.1 Ergonomics — a fast default, because a slow eval is an eval nobody runs

```
uv run usher eval                     # every surface, quick mode
uv run usher eval search              # one surface
uv run usher eval --full              # full golden sets, judge on, writes ledger
uv run usher eval --full --surface curation --seed 20260803
```

- **`--quick` is the default**: a seeded sample (~100 cases/surface), seconds,
  reports numbers, **enforces no bar and writes no ledger**.
- **`--full`** is the bar-enforcing, ledger-writing run.
- **Preflight fails fast and legibly** before spending minutes: catalog
  non-empty, embeddings present for the requested surface, vLLM reachable at
  `/v1/models`, `eval` extra installed. A surface whose preconditions are unmet
  reports **skipped-with-reason**; it never reports a zero.
- Reads existing `Settings`/`.env`. No second config system.

### 9.2 CI — split by determinism, not by speed

On a self-hosted runner against the local database and vLLM (the pattern
already in use on this box for `ha-home-panel`).

| job | trigger | contents | gates? |
|---|---|---|---|
| `eval-quick` | push to in-repo branches, `workflow_dispatch` | deterministic IR only, sampled | **yes** |
| `eval-full` | nightly `schedule`, `workflow_dispatch` | full goldens + judge, writes ledger | **no** — trend only, plus a wide catastrophic bar |

Three operational rules, each with a reason:

1. **The judge never gates.** It is non-deterministic by construction, and a
   flaky red teaches everyone to ignore the gate — the failure mode
   `prd-maintenance.md` already records ("a red that everyone learns to ignore
   is not a check").
2. **`concurrency: usher-eval`, no cancel-in-progress.** Eval jobs contend for
   the shared dev vLLM and the database. Same reasoning as the standing rule
   that nothing else may use the tree during a mutation sweep.
3. **No `pull_request` trigger from forks.** A self-hosted runner executes
   submitted code on an internet-facing box. Same-repo pushes,
   `workflow_dispatch` and `schedule` only.

---

## 10. Testing the harness itself

An eval that cannot fail is worse than no eval, because it ratifies the bug —
the lesson the M2 Group C reviewer established by shipping a deliberately-wrong
repository that passed all 15 contract cases.

1. **Unit tests against fakes** for every generator, scorer and ledger writer,
   under `tests/unit/eval/`, in the normal gate.
2. **The metric adapter is pinned to hand-computed fixtures** — known
   `(qrels, run)` pairs with recall/MRR/nDCG worked out by hand, so a `ranx`
   upgrade that changes tie-handling or the nDCG discount is loud.
3. **Every eval must be proven able to fail — a negative control.** Each
   surface is run against a deliberately degraded service (ranking shuffled
   under a fixed seed; the judge handed known genre labels) and the score
   **must** collapse below the bar. This is the mutation-sweep discipline
   applied to evals, and it is the only thing that distinguishes a harness with
   teeth from one that scores every run as a pass.
4. **A mutation sweep** over `eval/metrics/` and `eval/goldens/` at
   implementation time, with its plant list and expected verdicts written down
   first, per the standing rules.
5. **The unit-boundary assertion**: `format="json"` and the normalised-bar
   units each get a case, because both are silent-wrong-answer defects the
   spike already caught once.

---

## 11. Risks

| risk | mitigation |
|---|---|
| `numba` blocks a Python upgrade | `ranx` behind `eval/metrics/`; `ir_measures` (4 packages) is a one-file swap |
| deepeval API churn (3.7.x → 4.1.8 observed) | pinned in `uv.lock`; used behind `eval/judge/`; only 5 of its surfaces used |
| Judge model swap silently moves every score | model id + prompt hash in the fingerprint; calibration agreement is itself a tracked metric |
| Catalog drift blamed on the diff | §8.2 fingerprint + `baseline-invalid` verdict |
| Eval spend pollutes PRD 10 spend panels | judge calls are **not** written to `llm_calls`; that table is a *product* cost ledger joined to outcomes via `generation_id`. Judge spend goes to `eval_runs` |
| Golden sets leak third-party data into git | generated at run time, never committed; calibration labels live in the `eval` schema |
| A surface with no data reports 0 and reads as a regression | preflight → skipped-with-reason |

---

## 12. Deferred, with reasons

- **PRD conformance matrix** (every promise → a check, with a coverage report).
  Valuable and separable; it is a documentation-completeness project, not a
  measurement one.
- **Live third-party verification harness** (the H4/H5 Emby shape as a standing
  suite). Needs real credentials and bounded live runs; a different risk
  profile and a different cadence.
- **Behavioural evals from `search_queries` click/play outcomes.** M9 built the
  tables; they hold no rows until there is traffic. This is the highest-fidelity
  signal available later and the reason `search_queries` exists — revisit when
  the data is there.
- **deepeval Synthesizer for query paraphrases** (§6.3).
- **`optimize_fusion` to tune RRF weights.** The capability arrives with `ranx`;
  acting on it is a search change, not an eval change, and belongs to whichever
  milestone owns that.

---

## 13. Phasing — this is more than one plan

Four surfaces, a judge with a calibration loop, a ledger, a dashboard and CI is
too much for a single implementation plan. It decomposes cleanly, and the
ordering is chosen so that **the harness is proven end to end on the cheapest
surface before any expensive one is built**:

| phase | contents | why here |
|---|---|---|
| **E1 — skeleton + suggest** | `eval/` package, import contract, `ranx` adapter, fingerprint, `eval` schema + JSONL ledger, `usher eval`, preflight, Grafana dashboard 6, **suggest surface only** | Suggest is the one surface whose generator, seed and bars **already exist and are already recorded**, so E1 can be validated against known historical numbers instead of inventing a baseline. Ends with a working end-to-end loop. |
| **E2 — search + similarity** | the two remaining deterministic surfaces, their generators and negative controls; first baseline run; bars written down with that run's fingerprint | Pure additions to a proven skeleton. No new machinery. |
| **E3 — judge + curation + rows** | `deepeval` adapter, DAG graphs, calibration set and its refusal path, variance repeats, curation grouping + taste-holdout surfaces | The only phase needing the LLM, the calibration labels and the `inconclusive`/`judge-uncalibrated` verdicts. Isolating it keeps non-determinism out of E1/E2. |
| **E4 — CI** | self-hosted runner workflow, `eval-quick` gate, nightly `eval-full`, comparability reporting | Deliberately last: gating on numbers whose baselines do not yet exist is how a gate gets disabled. |

Each phase gets its own plan and its own review. **E1 is the one to plan next.**

## 14. Open question for the implementation plan

**What the first-run bars should be.** Every bar except suggest's must be set
from a first baseline run, because no prior measurement exists for search
relevance, similarity precision, or taste-holdout recall on this catalog. The
honest sequence is: build the harness, run it once to establish a baseline,
**write the bars down with that run's fingerprint**, and only then let bars
gate anything. The plan must not invent numbers in advance — a bar that was
reverse-engineered from the number it judges is not a bar.
