# ADR-0010 — `watch_states.title_id` is `ON DELETE RESTRICT`, not `CASCADE`

**Status:** Accepted

## Context

The M1 schema (Task 8) originally gave `watch_states.title_id` the same
shape every other child-of-`Title` foreign key gets by default:
`ON DELETE CASCADE`. `media_items.title_id` is `ON DELETE SET NULL` for a
deliberate, already-documented reason — an unmatched `MediaItem` is a
legitimate state, sitting in a review queue
([02](../02-data-model.md)) — but `watch_states.title_id`
was not given the same scrutiny in the first pass.

[02](../02-data-model.md) already establishes *why* Usher owns its
own UUIDv7 identity rather than using a provider ID: upstream identifiers
"get merged, split, and re-pointed," and merging two `Title`s that turn out
to be the same film ingested twice (once directly, once via a provider ID)
"becomes a repointing operation rather than a primary-key rewrite cascading
through watch state." M4's four-tier matcher (
[09](../09-roadmap.md)) is expected to produce exactly these duplicates.

A repointing merge is, concretely: repoint every `media_items`/
`watch_states` row that references the losing `Title` onto the winning
`Title`, then delete the loser. Under `CASCADE`, any bug in that repoint
step — an off-by-one in which id is the winner, a partial failure between
the repoint and the delete, a future refactor that reorders the two steps —
silently deletes the watch history of everyone who owned the losing
`Title`, with no error and no trace. The row is just gone.

## Decision

`watch_states.title_id` is `ON DELETE RESTRICT`. Deleting a `Title` that
still has any `watch_states` row pointing at it fails loudly, at the
`DELETE`, with a foreign key violation — verified directly against a real
Postgres. A correct merge must explicitly `UPDATE watch_states SET
title_id = :winner WHERE title_id = :loser` (repointing) before the
`DELETE` can succeed at all.

`media_items.title_id` stays `SET NULL`. The two look parallel — both are
"child references a `Title`" foreign keys — but protect opposite things:
an unmatched `MediaItem` is worth keeping regardless of its `Title` link
(review queue), so losing that link should just clear it, not block
anything. A `WatchState` *is* the thing worth keeping; losing its `Title`
link has no such benign reading, so it must not be able to happen
silently.

## Consequences

**Gained:** a Title merge that forgets to repoint `watch_states` — or gets
the direction backwards, or crashes partway through — fails at the
`DELETE` with a clear constraint-violation error instead of quietly
discarding watch history. This is the failure mode the UUIDv7-identity
design in [02](../02-data-model.md) exists to make possible to
get right; `RESTRICT` is what turns "possible to get right" into "hard to
get wrong silently."

**Given up:** a `Title` delete (merge or otherwise) is now two statements
instead of one wherever `watch_states` might reference it — the repoint,
then the delete — never a single cascading `DELETE`. This is accepted: the
whole point is that those two steps must both happen, deliberately, in the
right order, and `RESTRICT` is what enforces that they do.

**Follow-on:** `ix_watch_states_title_id` was added alongside this change.
`uq_watch_states_user_title` leads with `user_id`, so it cannot serve a
lookup keyed on `title_id` alone — and every `RESTRICT`-checked `DELETE`
against `titles` now runs exactly that lookup against `watch_states`. Without
a dedicated index, every Title delete (including every merge) would seq-scan
`watch_states` to evaluate the constraint.

## Evidence

Verified directly against a real `pgvector/pgvector:pg17` container (17.10):
a `titles` row with one dependent `sources`/`users`/`watch_states` row each,
then an attempted delete of the `Title`:

```sql
DELETE FROM titles WHERE id = '00000000-0000-7000-8000-000000000002';
-- ERROR:  update or delete on table "titles" violates foreign key
-- constraint "fk_watch_states_title_id_titles" on table "watch_states"
-- DETAIL:  Key (id)=(00000000-0000-7000-8000-000000000002) is still
-- referenced from table "watch_states".
```

The `DELETE` above only succeeds once the referencing `watch_states` row is
first repointed or removed — exactly the "repointing operation" [02](../02-data-model.md)
requires. `media_items.title_id`'s `SET NULL` (unchanged by this ADR) is
confirmed by the schema itself —
`\d+ titles` lists `fk_media_items_title_id_titles ... ON DELETE SET NULL`
against `fk_watch_states_title_id_titles ... ON DELETE RESTRICT` on the same
table, both from the same migration.
