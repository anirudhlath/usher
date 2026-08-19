"""The quality-eval harness: what a green test suite cannot judge.

`tests/` proves the code does what the code says. **This package measures
whether the answers are any good**, which is where this project's promises
have actually broken -- ADR-0002's typo gate failed both halves on
2026-08-03, 88% of M8's generated headings were the genre labels the prompt
forbids, and M8's query expansion measured worse than none. Every one of
those was found by a script written for one milestone and never run again.

**Nothing outside this package may import it**, which is the eleventh
import-linter contract rather than a convention. `usher.cli` is the single
exception and is deliberately absent from that contract's sources, exactly
as `usher.composition` is absent from the contracts it composes.

**It never reimplements what it measures.** Every surface drives the real
service through the real composition root. An eval that reimplements the
thing it measures measures itself.

Design: `docs/specs/2026-08-18-usher-quality-evals-design.md`.
"""
