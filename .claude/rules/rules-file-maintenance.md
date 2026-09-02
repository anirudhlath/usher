---
paths:
  - ".claude/rules/**"
---

# Maintaining the rules files themselves

Loaded when you edit anything under `.claude/rules/`. `CLAUDE.md` states the
one-line rule — a finding goes in the subsystem file, not in `CLAUDE.md`. This
file is how the loading actually works, and what two splits cost to learn.

**Every number below is re-derivable, which is the whole argument of this file:**

```bash
wc -lc .claude/rules/*.md | sort -n                 # what each trigger loads today
grep -L '^paths:' .claude/rules/*.md                # no frontmatter = loads ALWAYS, at any size
git show <sha>^:.claude/rules/<file>.md | wc -lc    # what it loaded before a split
```

**Token figures here are bytes ÷ 4, an estimate, never `count_tokens`** — one
method, stated, because this section previously carried two unlabelled ones and
they disagreed by 25%. And **sizes here rot within days**: these files are
appended to by ordinary work, so quote a number with the sha it was measured at,
or re-run `wc` before repeating it.

## How `paths:` really matches, read out of the client rather than assumed

Verified 2026-09-01 against Claude Code **2.1.247** by reading the shipped
binary, then reproducing the matcher against the `ignore` npm package (v7).
Every claim below is mechanical, and the failure mode for all of them is silent.

- **The frontmatter key is `paths:`. A file without it loads unconditionally**,
  which is mechanically identical to pasting its contents into `CLAUDE.md`.
  This is the trap the 2026-08-21 split nearly inverted; see below.
- **A trailing `/**` is stripped from every pattern before matching.**
  `"src/usher/api/**"` is matched as `src/usher/api`.
- **Matching is gitignore semantics**, not `picomatch`/`minimatch`, applied to
  the path relative to the repo root. A directory pattern therefore covers
  everything beneath it, which is why the strip above is harmless.
- **A pattern with no `/` matches that name at any depth.** `"alembic.ini"`
  matches a root file, but `"tests/**"` → `tests` would also match a `tests`
  directory nested anywhere. Prefer an anchored pattern when you mean one place.
- **If every pattern is `**` after stripping, the file loads unconditionally** —
  a second way to get the always-on behaviour by accident.
- **Path-scoped rules do not survive compaction.** `CLAUDE.md` and unscoped
  rules are re-injected from disk; a path-scoped file is summarised away and
  reloads only when a matching file is read again. A rule that must hold for a
  whole long session belongs in `CLAUDE.md`, not here.

To check what a session actually loaded, use `/context` (which shows the memory
files in play) rather than `/memory` (which shows where they could come from).

## What the two splits cost, and the rule that came out of them

**The first split, 2026-08-08 (`dbe9206`).** `testing-discipline.md` had
reached **1,729 lines / 116,474 bytes** behind `tests/**` — a trigger that fires
for almost every task in a TDD repo, so the file that loaded most often was also
the largest one. The sweep ledgers and the fixture material moved to triggers
that fire when they are actually wanted, leaving **845 lines** on `tests/**`,
and that is why `mutation-sweeps.md` and `fixtures-and-fakes.md` exist at all.

**And the file that split escaped it repeated the failure, which is the part
worth carrying.** `mutation-sweeps.md` was the destination of that first split
and then grew to **5,077 lines / 339,061 bytes** behind `docs/plans/**` — a
trigger that fires for almost every planning task here, so the same shape
recurred at the same place within one milestone. Split again 2026-08-21, landed
in `308b58e` on the 25th: **3,917 lines of per-task ledger moved to
`mutation-sweep-ledgers.md`, leaving 1,185 lines / 78,283 bytes on the
trigger** — a 77% reduction in what a planning session loads to reach the
mechanics (~85K → ~20K tokens at 4 bytes/token).

**Those two figures do not add back to 5,077, and the gap is exactly the
signposting.** 1,185 + 3,917 = 5,102; the 25 extra are the *"the per-task
ledgers moved out"* section written into `mutation-sweeps.md` at the cut, which
the single file never had. The new file gained 25 more of its own — frontmatter
and an orientation paragraph, outside the 3,917 entirely — so it landed at 3,942
lines on disk. What was checksummed before and after, and what recombines
byte-identical, is the **moved text**; neither file as it stands is a half of
the original. Re-derive all of it with
`git show 308b58e^:.claude/rules/mutation-sweeps.md | wc -lc` and its pair.

**Both halves have moved since, and this file's citations of them rotted first —
the failure this file is about, committed by the file itself.** A size here is a
snapshot of a tree other sessions are appending to *as you read it*: on
2026-09-02 `mutation-sweeps.md` measured **897 lines / ~58K bytes** and
`mutation-sweep-ledgers.md` **4,355 lines**, and both moved several times within
the hour — the byte figure in this sentence was already 286 bytes stale by the
end of it. Treat the sha-anchored figures above as the durable ones and re-run
`wc -lc .claude/rules/*.md` for anything current — never quote this sentence's
numbers second-hand.

⚠️ **And the first split has already been undone by ordinary work, which is the
strongest evidence this file has for its own thesis.** `testing-discipline.md`
left `dbe9206` at **845 lines**; it measured **1,331** on 2026-09-02, so it has
regrown 58% of the way back to the 1,729 that forced the split — in three weeks,
without anyone deciding to. **A split is not a fix, it is a reset**: the trigger
that made the file grow (`tests/**`, this repository's most-fired path) is
unchanged, so the same pressure is still on it. Re-measure before assuming the
2026-08-08 split is still buying anything, and expect the next one to be needed
on the same file rather than on a new one.

**The trap that split exposed, because it nearly inverted the fix:** a rules
file is *conditional only if it carries `paths:`*. One without the key loads
**unconditionally**, so moving 3,917 lines into a new file with no frontmatter
would have promoted them from sometimes-loaded to always-loaded. The ledger
file's trigger is therefore itself — it loads when you append to it, and is
opened deliberately otherwise. **Check the frontmatter before believing a
split reduced anything**; the failure is silent and points the wrong way.

## And one split that was measured and declined (2026-09-01)

`search-and-embeddings.md` is the largest file on a *working* trigger — the
ledger file is bigger but loads only when you open it. It measured **1,987 lines
/ 126,638 bytes at `c68c768`**, the state the classification below was done
against, and has been north of 2,000 lines and ~130 KB (~32K tokens) at every
measurement since: **about a fifth more lines than `testing-discipline.md`
carried at `dbe9206^` (1,729)** when its size forced the first split. It was
proposed for the same treatment, on the obvious axis: lexical/RRF
material to a `search-and-ranking` file, vector material to an
`embeddings-and-vectors` file. **The split was measured and refused, and the
measurement is the point.**

Classifying every line of the body — 9 to 1,987, which was the whole file at
`c68c768`; it has been appended to since and the new lines are unclassified —
onto that axis yields **thirteen alternating runs**, not two. The topics are
entangled because the subsystem is (RRF exists to fuse a lexical lane with a
semantic one). Two consequences killed it:

- **Cross-boundary references break silently.** The `M9 Task S4` section opens
  by saying every figure above it was *"taken on a synthetic corpus or a ~10k
  population"* — the epistemic frame for everything that follows, and every one
  of those figures would have moved to the other file.
- **Six of the twelve boundaries have no blank line before them**, so both
  halves would begin mid-argument. And one embedded finding — the paragraph
  beginning *"The escape hatch this file proposed one paragraph earlier"*, whose
  `ts_rank_cd` top three for *Bill Murray* are three documentaries **about** him
  rather than films he is in — is the refutation step *inside* the credits
  ablation and cannot be lifted out without breaking the argument.

**The general rule: interleaving is evidence that the file is one subject, not
two.** A count of alternating runs is the cheap test — the 2026-08-21 split that
worked had exactly one seam. Re-propose this one only with a different axis, and
measure the run count before believing it.

**Quote a sentence, never a line number, when you cite into another rules
file — a citation that was verified when written is not one that stays true.**
This section carried three of them: *9 to 1987* for the classification, *691*
for the S4 frame, *1326–1342* for the `ts_rank_cd` result. Every one was exact
against `c68c768`, and every one was pointing at unrelated prose a day later,
because other sessions appended above them — the same rot this file records
about its own byte counts, in the form that fails **silently**. A distinctive
quoted sentence is greppable and survives an insert above it. Anchor a *size* to
a sha for the same reason.

**And a line number cannot survive a *move* at all, which is the sharper case.**
`tests/integration/test_playback_leaks.py` still cites *"the false green
`.claude/rules/mutation-sweeps.md:561` names"*. That finding moved to
`api-telemetry-and-lanes.md` on 2026-09-01, leaving a forwarding note behind, so
561 now lands inside an unrelated M8 sweep entry — while
`grep -rn 'sink == \[\]' .claude/rules/` finds the finding *and* the forwarding
note in one step. Cite the phrase and let grep do the routing; note this
paragraph names no line in that test file, because it moved while this was
being written.

`services/similar.py` triggering **both** `search-and-embeddings.md` and
`rows-and-genome.md` (over 40K tokens together, 2026-09-02 — and *not* this
file, whose trigger is `.claude/rules/**`) was examined at the same time
and is also
deliberate: the HTML comment at the top of `rows-and-genome.md` records that it
was added on 2026-08-12 precisely so that anyone editing the blend or re-adding
a signal would load the genome findings. Removing it to save tokens re-opens the
failure that comment was written to close.
