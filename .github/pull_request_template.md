## What changed, and why

<!-- One or two sentences. The commit body carries the argument. -->

## The gate

<!--
Checkboxes are right *here* — this is a form a human fills in. They are the
thing the plan format forbids in a `docs/plans/` file, and that distinction is
deliberate; please don't "fix" it.
-->

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy src tests`
- [ ] `uv run lint-imports`
- [ ] `uv run pytest`
- [ ] `npm run verify` from `web/` — only if this touches the console

## Two questions this project's reviews keep asking

**Which test fails without this change, and was it seen red?**

<!-- Name the case. "The suite passes" is not the answer to this question. -->

**Which claim in `docs/prd/` does this invalidate?**

<!--
"None" is a fine answer when it is true. If it isn't, the PRD moves in this
same PR — see CLAUDE.md's "Keep the PRD current".
-->
