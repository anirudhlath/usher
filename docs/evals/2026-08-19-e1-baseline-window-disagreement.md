# E1 baseline — the prefix window disagrees, and the bars stay pending

**The first `--full` run of the harness failed its own pre-registered window,
so Task 14 stopped at Step 2 and the three pending bars were not filled in.**
This file is the write-up the plan asks for in place of widening the window.

⚠️ **Superseded in part on 2026-08-20: the window has since been widened, to
`[0.016, 0.028]`.** Everything below stands as measured — this file is the
evidence the amendment rests on, not a document the amendment contradicts. What
changed is the conclusion of *"### What was deliberately not done"*, whose first
bullet is annotated where it sits. The decision and its derivation are in
[ADR-0031](../prd/decisions/0031-the-two-tier-suggest.md), under *"Bar (4)'s
window was wrong, and this ADR is where it was wrong"*.

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
| 20260807 | 0.0184 | pass |
| 20260808 | 0.0241 | fail |
| 20260809 | 0.0221 | fail |
| 20260810 | 0.0231 | fail |

**Eight draws, five outside the window.** Mean 0.02183, SD **0.00196**, range
0.0184–0.0241 — a range 1.9× the window's full width.

The window's half-width of 0.003 is **1.53 observed SD**. A bar that narrow
around a metric this noisy rejects a correctly-behaving system most of the time,
which is what happened: 5 of 8.

**The measured SD also settles a question the first four draws could not.** At
p ≈ 0.02 and n ≈ 2,991 the naive *binomial* SE is 0.0026 — larger than what the
draws actually show. The naive figure treats 2,991 cases as independent when
they come from ~750 names, four typo classes each; the true clustering runs the
other way from the usual worry, because holding the band structure fixed at 150
names per band *constrains* the length mix that drives the whole metric. Use the
observed 0.00196, not a computed one.

### The comparison against 0.019, done two ways

This is the part worth getting right, because the two readings point opposite
directions and the first one is wrong.

| reading | treats ADR-0031's 0.019 as | result |
|---|---|---|
| naive | a population constant | (0.02183 − 0.019) / 0.00069 = **4.08 SE — decisive** |
| honest | **one draw, like these eight** | (0.02183 − 0.019) / 0.00212 = **1.36 SE — not significant** |

**ADR-0031's 1.9% is a single measurement over the gate's own 750 names, not a
property of the system.** It carries the same ±0.00196 every row of the table
above carries. Comparing a mean-of-eight against it must propagate that, and
once it does, the gap is 1.36 SE and there is nothing to explain.

**Correcting an earlier reading in this file:** with only four draws it looked
as though the distribution's centre sat near 0.019 and the pinned seed was
simply high. It does not — the centre is 0.0218, and seed 20260806's 0.0197 was
a low draw being over-read. The conclusion survives the correction, but by the
other route: not "the centre is where the bar is", but "the bar was set from one
draw of a quantity whose draws move by ±0.002, and then given a half-width of
0.003."

**2 — The tier-1 ordering reads two columns this branch rewrote.**
`prefix.py:125` is
`ORDER BY t.tmdb_popularity DESC NULLS LAST, t.tmdb_vote_count DESC NULLS LAST,
m.title_id ASC`, and ADR-0040 renamed and partially decontaminated both. That
ADR records its decontamination as **deliberately open — 57,701 of 407,860 rows
unfixed**. `recall_at_5` is a top-5 cutoff, so ordering changes it directly
wherever more than five titles share the typed prefix, which is common in the
short bands. This is the candidate that would be a real defect rather than a
mis-set bar, and it is already half-tracked as #39.

**The seed sweep cannot weaken this one, and it was a mistake to think it
could.** All eight draws query the same damaged columns, so a displacement
common to all of them is exactly what the sweep is blind to — re-drawing names
varies the numerator, not the ordering inputs. The sweep bounds the *noise*
(SD 0.00196) and says nothing about the *offset*. Only the restore experiment
below can see that, and it is now cheaper to interpret because the noise it has
to beat is measured rather than assumed.

**Measured 2026-08-20, and the mechanism is not the one written above.** Joined
against `titles_rating_backup_20260819` over all 1,272,870 rows:

| column | backup non-null | current non-null | rows differing |
|---|---|---|---|
| `tmdb_popularity` | 292,320 | 292,320 | **0** |
| `tmdb_vote_count` | 540,275 | 132,415 | **407,860** |

**The primary sort key was never touched**, and every one of the 407,860
differing rows is **value → NULL** (0 null→value, 0 value→value; the
arithmetic closes exactly). So the decontamination *deleted* contaminated
counts rather than correcting them, and the whole effect runs through the
**tiebreaker** — which is not a minor role here, because 77.0% of the catalog
(980,550 rows) has a NULL `tmdb_popularity` and for all of those
`tmdb_vote_count` *is* the ordering. **25.2% of the eligible frame — 24,018 of
95,338 titles — is in the nulled set**, and those are what a typo case targets.

Tier 2 sorts by the same two columns one key down (`postgres.py:811`:
`dist ASC, tmdb_popularity DESC NULLS LAST, tmdb_vote_count DESC NULLS LAST,
id ASC`), so this reaches the fuzzy tier too.

**3 — An 8-day-newer IMDb snapshot.** Recorded in the goldens module as the
cause of the residual +0.19% in frame size. Weakest of the three: it moves which
titles are eligible, and by argument above eligibility does not move the drawn
length mix.

**What separates 1 from 2, and why it is a paired measurement.** Re-run *the
same eight seeds* against a catalog whose `tmdb_popularity`/`tmdb_vote_count`
are restored to their pre-ADR-0040 values — `titles_rating_backup_20260819`
holds exactly those columns for all 1,272,870 rows, which is what it was kept
for. Same seeds, same names, same cases; **only the ordering inputs differ.**

Pairing matters here more than anywhere else in this file. Unpaired, a shift has
to clear the 0.00196 draw SD to be visible at all, and cause 2's whole plausible
magnitude is about 0.003. Paired, the draw noise cancels: each seed is compared
against *itself*, and the statistic is the mean of eight differences. The
project's signature failure mode is running this comparison unpaired, finding
nothing, and recording "no effect".

Decision rule, written before the run: if the paired mean difference is
negative and its magnitude exceeds the difference's own standard error by 2×,
cause 2 is real and the window was never wrong. If the paired difference is
indistinguishable from zero, the window is the thing to fix — and it should then
be re-derived as an interval over draws (mean ± k·SD, with the SD measured
above) rather than one draw ± a guess.

## The paired result — run 2026-08-20

Eight seeds, both arms, quick mode over the whole frame so nothing was written
to the ledger. `n` is identical within every pair, which is the pairing holding:
restoring the tiebreaker cannot move the sampling frame, and the counts confirm
it did not.

| seed | n | damaged | restored | diff |
|---|---|---|---|---|
| 20260803 | 2991 | 0.0237 | 0.0237 | +0.0000 |
| 20260804 | 2986 | 0.0214 | 0.0218 | +0.0004 |
| 20260805 | 2989 | 0.0221 | 0.0228 | +0.0007 |
| 20260806 | 2992 | 0.0197 | 0.0197 | +0.0000 |
| 20260807 | 2992 | 0.0184 | 0.0191 | +0.0007 |
| 20260808 | 2989 | 0.0241 | 0.0244 | +0.0003 |
| 20260809 | 2989 | 0.0221 | 0.0224 | +0.0003 |
| 20260810 | 2992 | 0.0231 | 0.0231 | +0.0000 |

| tier | mean diff | SD | SE | \|mean\|/SE |
|---|---|---|---|---|
| prefix | **+0.000300** | 0.000293 | 0.000104 | **2.90** |
| fuzzy | **+0.001750** | 0.001006 | 0.000356 | **4.92** |

**No negative pair on either tier.** The damaged arm also reproduced all eight
previously-measured seeds exactly, including the `--full` baseline's 0.0237 at
n=2991 — the control that makes the restored arm comparable rather than merely
newer.

### The rule landed in a branch that was not written

The result is **significant and positive**, which is neither of the two
branches above. It was pre-registered as a possible third outcome while one of
the sixteen runs had completed, on the strength of the column measurements
alone: if the defect *nulls* a tiebreaker, it pushes targets **down**, and the
diff has to be positive.

**And the analysis script's own reporting is wrong for this input**, which is
recorded rather than quietly patched because the wrong sentence is the one that
would have reached this file. Its else-branch prints *"the paired difference is
indistinguishable from zero"* — false at 2.90 SE. The arithmetic was right and
the prose was not.

### What it settles

**The ordering defect is real, and it depresses recall rather than raising
it.** Repairing it moves prefix recall *up*, so the undamaged system's mean is
**0.022125** — *further* above the window's 0.022 ceiling than the damaged
0.021825 was. Cause 2 therefore cannot excuse a baseline that came in high; it
makes the disagreement slightly worse.

**So cause 1 stands: the window is the thing to fix**, for the reason already
recorded — its half-width is 1.53 observed draw SD, and ADR-0031's 0.019 is
itself one draw of the same noisy quantity.

**The effect is an order of magnitude smaller than the mechanism predicted, and
that gap is not explained.** A quarter of the frame gets pushed down by the
nulled tiebreaker and it moved prefix recall by 1.4% relative. The likely
reason is that tier 1 is an exact-prefix probe whose recall is entirely the
deletion class, and a probe returning fewer than five candidates never
exercises the tiebreak at all — so ordering is irrelevant to most cases. **That
is a hypothesis and nothing here measures it.**

## The widened window, run — 2026-08-21

The amendment was verified by re-running `usher eval suggest --full` against the
same scratch catalog at the same default seed, rather than by arithmetic on the
recorded value.

| | run 1 — 2026-08-20 | run 2 — 2026-08-21 |
|---|---|---|
| `inputs_digest` | `21678a1e2ed38b8a` | **`21678a1e2ed38b8a`** |
| `prefix recall_at_5` | 0.023738 | **0.023738** |
| `bars_sha256` | `ae51d05c1f9abdeb` | **`3be5bacc68d1a0f3`** |
| bar | `[0.016, 0.022]` | **`[0.016, 0.028]`** |
| verdict | **fail** | **pass** |

🔴 **The value reproduced to all six decimals and the frame digest is
byte-identical, so the only thing that moved between a failing run and a passing
one is the bar.** That is the control this amendment needed, and it is stronger
than the arithmetic would have been: had the widening been accompanied by any
drift in the measurement, these two rows would show two things changing at once
and neither could be attributed. It also demonstrates the harness is
deterministic at a fixed seed across two days and two `bars.toml` files, which
nothing had yet shown over more than one run.

The three pending bars are still pending and still reported rather than gated;
`fuzzy recall_at_5` measured 0.8094 at `all` in both runs.

## What was deliberately not done

- **The window was not widened.** Moving `high` from 0.022 to 0.024 makes the
  run green in one line and is the precise move `bars.toml`'s hash exists to
  make visible.

  ⚠️ **Reversed 2026-08-20 — the window *was* widened, to `[0.016, 0.028]`.
  The objection above is right and the amendment is built to answer it rather
  than to step around it.** The move this bullet refuses is `high = 0.024`:
  the smallest number that makes *this run* green, which is a bar
  reverse-engineered from the value it judges. The ceiling shipped is
  **0.028**, and nothing about it is derived from 0.023738 — it is the
  observed mean of sixteen draws plus 3 draw SD, and the sixteen draws are the
  ones tabulated above in this file. Three checks separate the two moves:

  - **It is not the minimum that passes.** 0.024 would do that. 0.028 is
    0.0043 further out, and that margin is where the draw noise lives.
  - **It covers a draw nobody was trying to make pass.** ADR-0031's own B3
    gate run measured **0.0267** and recorded FAIL against the old window on
    2026-08-12 — an independent failure, eight days before this harness
    existed. Any ceiling below 0.0267 leaves that draw failing, so a window
    fitted to *this* baseline would have preserved the defect. 0.024 does not
    cover it; 0.028 does.
  - **The floor did not move.** Every failure this window has ever produced is
    at the ceiling and none is at the floor, so `low` stays 0.016. A widening
    that loosened both ends would be the "quietly disabled gate" this bullet
    is warning about.

  What the bar still catches is unchanged and enormous: tier 1 answering as
  tier 2 scores **0.8094**, 29× the new ceiling, and a collapsed index scores
  **0.0**. `bars.toml`'s hash did exactly its job here — it made the change
  visible, and the visible change is argued in `source` beside the number.
- **The three pending bars were not filled in**, although this run produced
  values for all three (`fuzzy recall_at_5` 0.8094 at `all`, 0.3063 at
  `band=2-4`, 0.6893 at `typo_class=transposition`). A pending bar is filled in
  once, from a baseline that is trusted; a baseline whose frame is under active
  suspicion cannot authorise it. They stay `pending` until cause 2 is settled.

  **Still not filled after the paired run, and now for a measured reason
  rather than a suspicion.** All three are `fuzzy recall_at_5`, and the fuzzy
  tier is the arm the ordering defect hits *hardest* — **+0.001750 at 4.92
  SE**. Filling them from this baseline would pin a number that a confirmed
  defect is holding down by ~0.0018, and when #39 repairs the ordering the
  measured value rises. A window bar set at today's damaged value would then
  **fail on the fix** — which is "an eval that cannot fail ratifies the bug"
  arriving through the bar rather than through the control. They should be
  filled from a run of the repaired system, and #39 is the blocker.

- **`bars.toml` was not edited at all**, deliberately, including its prose. The
  recorded ledger entry carries `bars_sha256
  ae51d05c1f9abdeb9f9f7bfef5d752a56cff492e3f80798d065012bf0268c6a5`, and that
  hash is only useful while the file it names can still be produced. Adding a
  comment explaining why the bars stay pending would have moved it for no
  functional gain, so the explanation lives here instead.
- **Task 14 Steps 3 and 4 are not done**, so nothing gates yet. Step 5 (ADR-0041)
  and Step 6 (the PRD corrections) were independent of the outcome and are done.

## Recorded in passing

`fuzzy recall_at_5` on `band=2-4` is **0.3063**. ADR-0002's gate failed this
band at 27.8% against a 0.75 bar; the rebuilt frame measures it at 30.6% against
the same bar. **The gate's original failure is intact** — three points better on
a differently-drawn sample, and nowhere near the bar it was set. Whatever else
this run leaves open, it does not reopen that.
