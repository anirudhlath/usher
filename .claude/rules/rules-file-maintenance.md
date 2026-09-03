---
paths:
  - ".claude/rules/**"
---

# Maintaining the rules files themselves

`CLAUDE.md` states the rule: a finding goes in the subsystem file, and it is the
*rule*, not the story. This file is how the loading works and what the size cap
is for.

## The cap, and why splitting is usually the wrong answer

**No rules file may exceed 200 lines.** At 400 it is broken, not merely large.

The instinct when a file gets big is to split it, and this repository tried that
twice — `testing-discipline.md` in 2026-08-08 (`dbe9206`), `mutation-sweeps.md`
in 2026-08-21 (`308b58e`, moving 3,917 lines of per-task ledger into a
`mutation-sweep-ledgers.md` whose trigger was itself). Both worked as
arithmetic. Neither fixed anything:

- **`testing-discipline.md` regrew.** It left the split at 845 lines and
  measured 1,331 three weeks later, without anyone deciding to. The trigger that
  made it grow (`tests/**`, the most-fired path here) was unchanged, so the
  pressure was too. **A split is a reset, not a fix.**
- **The ledger file was deleted outright on 2026-09-02**, all 4,355 lines. Every
  line the 2026-08-21 split had carefully *preserved* turned out to be history
  nobody would read — the split had been an elaborate way of not deciding to
  delete it.

So: **cut first, and only split if two genuinely different subjects are left.**
Interleaving is evidence the file is one subject — count alternating runs before
believing a proposed seam. `search-and-embeddings.md` was proposed for a
lexical/vector split in 2026-09-01 and refused on exactly that test: thirteen
alternating runs, and six of the twelve boundaries had no blank line before
them, so both halves would have begun mid-argument.

**The trap that makes a split actively harmful:** a rules file is conditional
*only if it carries `paths:`*. One without the key loads **unconditionally**, so
moving bulk into a new file with no frontmatter promotes it from
sometimes-loaded to always-loaded. Check the frontmatter before believing a
split reduced anything; the failure is silent and points the wrong way.

## What to keep, and what git already has

Keep the rule. Delete how it was found. A session needs to know what not to get
wrong — not the sample size, the refuted hypothesis, the milestone, or the date.

Delete on sight: measurement narratives, sweep ledgers, before/after tables, row
counts, latency percentiles, "measured rather than assumed" framing, and any
"the general form is…" paragraph restating the rule directly above it. Keep a
number only when someone would otherwise re-litigate a settled decision, and
then keep the number rather than its derivation.

**Prefer a pointer to the source over a copy of it.** The 2026-09-02 audit found
the pattern repeatedly: a module docstring or an ADR had been corrected while
the rules file that paraphrased it had not. `adapters/search/postgres.py`
carried its own `ef_search` retraction while `search-and-embeddings.md` still
asserted the refuted claim 1,600 lines away from its own counter-evidence. The
copy rots; the original does not.

## How `paths:` really matches

Verified 2026-09-01 against Claude Code 2.1.247 by reading the shipped binary,
then reproducing the matcher against the `ignore` npm package (v7). Every claim
is mechanical and every failure mode is silent.

- **The key is `paths:`; a file without it loads unconditionally.**
- **A trailing `/**` is stripped before matching**, so `"src/usher/api/**"` is
  matched as `src/usher/api`.
- **Matching is gitignore semantics** — not `picomatch`/`minimatch` — against
  the path relative to the repo root, so a directory pattern covers everything
  beneath it.
- **A pattern with no `/` matches that name at any depth.** Prefer an anchored
  pattern when you mean one place.
- **If every pattern is `**` after stripping, the file loads unconditionally** —
  a second way to get always-on behaviour by accident.
- **Path-scoped rules do not survive compaction.** `CLAUDE.md` and unscoped
  rules are re-injected from disk; a path-scoped file is summarised away and
  reloads only when a matching file is read again. A rule that must hold for a
  whole long session belongs in `CLAUDE.md`.

Use `/context` to see what a session actually loaded, not `/memory` (which shows
where memory could come from).

## Citing, and measuring

**Quote a distinctive sentence, never a line number, when citing into another
file.** A line number is exact when written and points at unrelated prose after
one insert above it — and cannot survive a move at all. A quoted phrase is
greppable and self-routing. This file carried three line-number citations that
were all exact at `c68c768` and all wrong a day later.

Sizes rot within hours while other sessions append. Re-derive rather than quote:

```bash
wc -l .claude/rules/*.md | sort -n            # what each trigger loads today
grep -L '^paths:' .claude/rules/*.md          # no frontmatter = loads ALWAYS
```
