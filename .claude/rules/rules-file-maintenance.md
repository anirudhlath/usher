---
paths:
  - ".claude/rules/**"
---

# Maintaining the rules files themselves

Loaded when you edit anything under `.claude/rules/`. `CLAUDE.md` states the
one-line rule — a finding goes in the subsystem file, not in `CLAUDE.md`. This
file is how the loading actually works, and what two splits cost to learn.

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

**The first split, 2026-08-21's predecessor.** `testing-discipline.md` had
reached **1,728 lines** behind `tests/**` — a trigger that fires for almost
every task in a TDD repo, so the file that loaded most often was also the
largest one. The sweep ledgers and the fixture material moved to triggers that
fire when they are actually wanted, which is why `mutation-sweeps.md` and
`fixtures-and-fakes.md` exist at all.

**And the file that split escaped it repeated the failure, which is the part
worth carrying.** `mutation-sweeps.md` was the destination of that first split
and then grew to **5,077 lines / 339,061 bytes** behind `docs/plans/**` — a
trigger that fires for almost every planning task here, so the same shape
recurred at the same place within one milestone. Split again 2026-08-21:
**1,185 lines / 78,283 bytes stay on the trigger, 3,917 lines of per-task
ledger moved to `mutation-sweep-ledgers.md`**, a 77% reduction in what a
planning session loads to reach the mechanics (~85K → ~20K tokens, estimated at
4 bytes/token, not measured with `count_tokens`). Both halves were checksummed
before and after and recombine byte-identical to the original.

**The trap that split exposed, because it nearly inverted the fix:** a rules
file is *conditional only if it carries `paths:`*. One without the key loads
**unconditionally**, so moving 3,917 lines into a new file with no frontmatter
would have promoted them from sometimes-loaded to always-loaded. The ledger
file's trigger is therefore itself — it loads when you append to it, and is
opened deliberately otherwise. **Check the frontmatter before believing a
split reduced anything**; the failure is silent and points the wrong way.

## And one split that was measured and declined (2026-09-01)

`search-and-embeddings.md` is **1,987 lines / 39,519 tokens** — the largest
path-triggered file here, and 15% more lines than `testing-discipline.md` had
(1,728) when its size forced the first split. It was proposed for the same
treatment, on the obvious axis: lexical/RRF material to a `search-and-ranking`
file, vector material to an `embeddings-and-vectors` file. **The split was
measured and refused, and the measurement is the point.**

Classifying every line from 9 to 1987 onto that axis yields **thirteen
alternating runs**, not two — the topics are entangled because the subsystem is
(RRF exists to fuse a lexical lane with a semantic one). Two consequences
killed it:

- **Cross-boundary references break silently.** Line 691 opens the S4 section
  with *"Every figure above it was taken on a synthetic corpus or a ~10k
  population"* — the epistemic frame for everything that follows, and every one
  of those figures would have moved to the other file.
- **Six of the twelve boundaries have no blank line before them**, so both
  halves would begin mid-argument, and one embedded finding (the `ts_rank_cd`
  lexical result at 1326–1342) is the refutation step *inside* the credits
  ablation and cannot be lifted out without breaking the argument.

**The general rule: interleaving is evidence that the file is one subject, not
two.** A count of alternating runs is the cheap test — the 2026-08-21 split that
worked had exactly one seam. Re-propose this one only with a different axis, and
measure the run count before believing it.

`services/similar.py` triggering **both** this file and `rows-and-genome.md`
(~52K tokens together) was examined at the same time and is also deliberate:
the HTML comment at the top of `rows-and-genome.md` records that it was added
on 2026-08-12 precisely so that anyone editing the blend or re-adding a signal
would load the genome findings. Removing it to save tokens re-opens the failure
that comment was written to close.
