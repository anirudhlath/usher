# E1 baseline — the prefix window disagrees, and the bars stay pending

**The first `--full` run of the harness failed its own pre-registered window,
so Task 14 stopped at Step 2 and the three pending bars were not filled in.**
This file is the write-up the plan asks for in place of widening the window.

## The run

```
suggest: 2991 cases, seed 20260803, full, digest 21678a1e2ed3
  prefix  recall_at_5        0.0237  n=2991   fail
  prefix  mrr                0.0212  n=2991   unbarred
  prefix  latency_p50_ms     0.6413  n=2991   unbarred
  prefix  latency_p95_ms     2.5930  n=2991   pass
  prefix  latency_max_ms   367.8288  n=2991   unbarred
  fuzzy   recall_at_5        0.8094  n=2991   pending
  fuzzy   mrr                0.7652  n=2991   unbarred
  fuzzy   latency_p50_ms    53.1202  n=2991   unbarred
  fuzzy   latency_p95_ms   388.5356  n=2991   unbarred
  fuzzy   latency_max_ms  2409.8505  n=2991   unbarred
  recorded: eval.runs + docs/evals/ledger.jsonl
```

exit 1. Log at `/var/tmp/e1-baseline.log`, sha256
`26eccb140b39363c80616f24060bfecbf9d5e98062687eb565d83b6a82b31598`.
Run provenance: git `2d20b9d`, inputs digest `21678a1e2ed3`, bars file sha256
`ae51d05c1f9abdeb9f9f7bfef5d752a56cff492e3f80798d065012bf0268c6a5`, against
`usher_m10a` (the m10a clone, 1,272,870 titles).

**The failing run was recorded rather than discarded.** `docs/evals/ledger.jsonl`
holds one line and it is this one, so the ledger's first entry is a red. That is
the intended behaviour and it is worth stating, because the alternative — rerun
until green, record that — is the exact failure the two-sink design exists to
make expensive.

## Two things that look wrong and are not

**2,991 cases, where every plan document says 2,993.** Already re-pinned in
`src/usher/eval/goldens/suggest.py` on 2026-08-19 with the cause recorded: the
2-4 band now draws nine names admitting no deletion where it drew seven, so that
band contributes 591 against 600 from each of the other four. `check_frame` did
not refuse. The stale number is in the older plan prose, not in the run.

**`prefix latency_p95_ms` failed the 10 ms ceiling on the preflight (13.15 ms)
and passed on the baseline (2.59 ms).** The preflight is a 100-case quick run,
where p95 is the 95th of 100 values and one cold-start outlier (max 201 ms)
carries it. At 2,991 cases the same outliers land below p95. No finding —
recorded only so the next person who sees a red preflight does not chase it.

## What the number actually is

`prefix recall_at_5` is **entirely the deletion class**, and the decomposition
is exact:

| typo class | n | prefix recall | contribution |
|---|---|---|---|
| deletion | 741 | 0.0945 | 0.023412 |
| transposition | 750 | 0.0013 | 0.000326 |
| doubled | 750 | 0.0000 | 0.000000 |
| substitution | 750 | 0.0000 | 0.000000 |
| | | **total** | **0.023738** |

against a measured `0.023738`. Agreement to six decimals, so this is the whole
mechanism and not a correlation.

It is also the right mechanism for what tier 1 *is*. An exact-prefix btree probe
returns a row only when the typed string is still a prefix of the name.
Substitution and doubling always destroy that property, which is why both are
exactly zero rather than small. Deletion preserves it whenever the deleted
character was the last one — or inside a repeated run — so its recall is
approximately `E[1/L]` over the drawn names.

**Anything that moves `prefix recall_at_5` is therefore moving the deletion
class or the length distribution, and nothing else can move it.** That is a much
narrower search than "recall changed".

## The obvious explanation, tested and refuted

The re-anchored frame shifted toward longer names (`20+` up 259, `8-11` down 81,
`12-19` down 95). The tempting story is that longer names raise prefix recall.

Per-band, they do not:

| band | 2-4 | 5-7 | 8-11 | 12-19 | 20+ |
|---|---|---|---|---|---|
| prefix recall | 0.0169 | **0.0433** | 0.0283 | 0.0217 | **0.0083** |

Recall *falls* with length past 5-7 — consistent with `E[1/L]` — and the `20+`
band is the worst of the five. A frame shifted toward `20+` pushes the number
**down**, not up. The hypothesis predicts the wrong sign.

The curve's low end is the minimum prefix length ADR-0031 imposes: in the 2-4
band a deletion often leaves a string shorter than the probe will accept, so
those cases cannot be recalled at all.

**And the band pools cannot be the cause in any case**, which is the cleaner
argument: `GATE_DRAW_PER_BAND = 150` draws a *fixed* 150 names from every band
regardless of pool size, so the pools changing from
432/2532/7178/20520/17887 to 428/2541/7097/20425/18146 leaves the drawn length
mix untouched. Only *within-band* name choice varies, which matters most in the
open-ended `20+` band.

## What is left, and what would discriminate it

Three candidates survive. This run does not choose between them.

**1 — Draw variance, and a window narrower than it.** The window is ADR-0031's
0.019 ± 0.003. Re-drawing the names at other seeds, all else identical:

| seed | prefix recall_at_5 | vs window |
|---|---|---|
| 20260803 (pinned) | 0.0237 | fail |
| 20260804 | 0.0214 | pass |
| 20260805 | 0.0221 | fail (by 0.0001) |
| 20260806 | 0.0197 | pass |

Four draws, two outside, spanning 0.0197–0.0237 — **a range twice the window's
full width.** The lowest of them lands on ADR-0031's 0.019 almost exactly, which
is the observation that reframes the whole disagreement: **the pinned seed sits
high in a distribution whose centre is near where the window was set**, rather
than the system having moved away from it.

At p ≈ 0.02 and n ≈ 2,991 the naive binomial standard error is ≈ 0.0026, so the
window's half-width is barely **1.2 standard errors** of a single draw. The
naive figure is itself an *under*estimate: the 2,991 cases come from ~750 names,
four typo classes each, and recall is strongly correlated within a name — a long
name is hard for all four of its cases at once. The clustered standard error is
larger than 0.0026, so the true half-width is under one standard error.

A bar that a correct system fails on half its re-draws is not measuring what it
was meant to measure. More seeds are still running; this table is the part that
is settled.

**2 — The tier-1 ordering reads two columns this branch rewrote.**
`prefix.py:125` is
`ORDER BY t.tmdb_popularity DESC NULLS LAST, t.tmdb_vote_count DESC NULLS LAST,
m.title_id ASC`, and ADR-0040 renamed and partially decontaminated both. That
ADR records its decontamination as **deliberately open — 57,701 of 407,860 rows
unfixed**. `recall_at_5` is a top-5 cutoff, so ordering changes it directly
wherever more than five titles share the typed prefix, which is common in the
short bands. This is the candidate that would be a real defect rather than a
mis-set bar, and it is already half-tracked as #39.

**The seed sweep weakens this one without closing it.** A broken ordering input
should shift every draw in the same direction; four draws spanning 0.0197–0.0237
with the low end on ADR-0031's own number look more like noise around an intact
system than a systematic displacement. What keeps it open is that all four draws
share the same damaged columns, so the sweep cannot see a shift common to all of
them — only the restore experiment below can.

**3 — An 8-day-newer IMDb snapshot.** Recorded in the goldens module as the
cause of the residual +0.19% in frame size. Weakest of the three: it moves which
titles are eligible, and by argument above eligibility does not move the drawn
length mix.

**What separates 1 from 2:** re-run the pinned seed against a catalog whose
`tmdb_popularity`/`tmdb_vote_count` are restored to their pre-ADR-0040 values —
the `titles_rating_backup_20260819` table holds exactly those columns for all
1,272,870 rows, which is what it was kept for. Same seed, same names, only the
ordering inputs differ. If 0.0237 moves toward 0.019, cause 2 dominates and the
window was never wrong. If it does not move, the window is the thing to fix, and
it should be re-derived as an interval over draws rather than a point ± a guess.

## What was deliberately not done

- **The window was not widened.** Moving `high` from 0.022 to 0.024 makes the
  run green in one line and is the precise move `bars.toml`'s hash exists to
  make visible.
- **The three pending bars were not filled in**, although this run produced
  values for all three (`fuzzy recall_at_5` 0.8094 at `all`, 0.3063 at
  `band=2-4`, 0.6893 at `typo_class=transposition`). A pending bar is filled in
  once, from a baseline that is trusted; a baseline whose frame is under active
  suspicion cannot authorise it. They stay `pending` until cause 2 is settled.
- **Task 14 Steps 3 and 4 are not done**, so nothing gates yet. Step 5 (ADR-0039)
  and Step 6 (the PRD corrections) were independent of the outcome and are done.

## Recorded in passing

`fuzzy recall_at_5` on `band=2-4` is **0.3063**. ADR-0002's gate failed this
band at 27.8% against a 0.75 bar; the rebuilt frame measures it at 30.6% against
the same bar. **The gate's original failure is intact** — three points better on
a differently-drawn sample, and nowhere near the bar it was set. Whatever else
this run leaves open, it does not reopen that.
