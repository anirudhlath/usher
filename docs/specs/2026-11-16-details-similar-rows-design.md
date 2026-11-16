# The details page's "Similar titles" from the Because You Watched row: design

**Status:** Proposed.
**Date:** 2026-11-16.
**Slices:** one. It changes one route, adds one kind of queued job, and changes one
section of the console's details page.
**Builds on:** [Because You Watched rows](2026-11-07-because-you-watched-rows-design.md),
merged as PR #115. That design's three lists, merge, storage and rebuild rules are
used here unchanged.

## What the owner asked for

> I also want the row in the details page fixed. I mean really it should use the same
> algorithm for that. Additionally the thumbnail for the similar items on that row
> doesnt load on demand.

Asked what a title with no stored row should show, and offered "the old list straight
away, the new row built in the background and swapped in a few seconds later":

> Yes that's okay

Shown the whole change played back, including the three guesses listed under
"Who decided what":

> This is good

The goal: the "Similar titles" section on a title's page shows the same row the home
screen would show for that title, and its cards have posters.

## What is wrong today

**The list.** `GET /titles/{id}/similar` reads only `title_neighbors`, the older
word-matching list. On prod, War Dogs' section is led by Mad Dog Coll, while its home
row puts Lord of War sixth and American Made tenth. The home row has passed the
owner's bars; the details page never got it.

**The posters.** Every card in the section is an empty poster. `SimilarTitleResponse`
carries `id, kind, name, year, score` and no artwork, so the console's `SimilarSection`
has no image id to give `PosterCard`. The images exist: all ten of War Dogs'
neighbours have rows in `images`. Home cards work because `RowCard` carries `artwork`.

## The design

### What the section shows

One list of cards, picked in this order:

1. **The title's stored row**, when `seed_rows` holds cards for it: those cards, in
   stored order, up to 20. This is the same record the home screen reads, so a title
   shows the same cards in the same order in both places.
2. **Otherwise the old list**: the title's `title_neighbors`, in stored rank order,
   up to 20. Today the route stops at 10; 20 is what the home screen's own fallback
   reads for the same title, so the two places still agree.

Every card, from either list, carries what a home card carries: poster, watched mark,
owned mark. Watched titles stay in the list with the green bar, as on the home screen.
A card whose title the catalog no longer holds is dropped, as on the home screen.

### Building a row when a page is opened

When the section is read and the title's record is **due**, Usher queues a build for
that title and answers straight away with whatever the section shows now. "Due" is the
existing `is_due` rule, unchanged:

- no record at all, or
- cards over 30 days old, or built under another recipe, or
- a failed attempt over 7 days old (a newer failed attempt holds the build back).

So a title nobody has opened shows the old list on first view; a title whose stored
row has aged shows the stored row while the rebuild runs; and a title that could not
produce a good row keeps the old list and is not retried for 7 days.

A build is queued only when all of these hold:

- the deployment has a model (`USHER_LLM_ENABLED`); without one, the section is the
  stored row if one exists, else the old list, and nothing is queued;
- the title is enriched. An unenriched title has no overview for the model to read.
  Opening its page already asks for enrichment at the front of the queue, enrichment
  finishing already tells the console the title changed, and the console already
  re-reads the section on that event, so the build is queued on that second read.

**The build is a queued job, not a scheduled one.** A new job kind, keyed on the title
id, at the priority a page open gets (`DEMAND`), run one at a time. The queue already
drops a second request for the same title, retries a failed job, and parks one that
keeps failing. The handler:

1. reads the title; if it is gone, the job is done;
2. re-checks `is_due`; if another build got there first, the job is done;
3. calls the existing `SeedRowService.build`, which stores the row or the failed
   attempt, exactly as the home screen's job does;
4. tells clients the title changed (`title.updated` for that title id).

**An outage defers the build; it never parks it.** A model or provider outage raises
out of `build` with nothing stored, so the record stays due. Left to the queue's
ordinary failure path, the job would park after five attempts (a few minutes), a
parked job is never re-queued by asking again, and that title could never build. So
the handler turns any `UsherPortError` that `build` lets out into `JobDeferred` with
a five-minute wait, the way the sync handler waits out a busy source: no attempt is
spent, the job tries again every five minutes, and it builds when the model is back.
No event is sent until a build stores something; the section keeps what it had.

The home screen's `seed_rows.build` job is untouched. It and this job write the same
records under the same rules. If both reach one title at the same moment, both build
and the later save wins. That costs one extra completion and is not guarded against.

### How the swap reaches the page

The console already listens for `title.updated` on the open title and re-reads the
section. Nothing new is needed on that path: the handler's event makes the section
re-read, and the re-read finds the stored row.

The response says whether a build is waiting or running, so the page can say so.
`building` is true exactly when a build job for this title is in the queue after
this read: the record is due, the deployment has a model, and the title is enriched.
It is true on the read that queued the job and on every later read until the job
stores something. It is false for an unenriched title, which shows the old list with
no marker until enrichment finishes and the section is re-read.

**The page does not depend on the event.** The console's rule is that it must be
correct if no event ever arrives (`web/CONVENTIONS.md`), and the event can go
missing: the stream drops, or the worker runs in another process. So while a read
says `building`, the page also re-reads the section every 5 seconds, for at most 60
seconds from the first such read. A re-read that says `building: false` ends it. At
60 seconds the re-reads stop and the marker goes, whatever the last answer was; the
page keeps the list it has, and the next visit starts again.

While `building` is true and the 60 seconds are not up, the section shows a small
"updating" marker beside its heading, in the slot and style the "stale" marker uses
today.

### The response

`GET /titles/{id}/similar` keeps its path and its `404` for an unknown title. Its body
becomes:

| Field | Meaning |
|---|---|
| `cards` | The list above. Each card is the home screen's card shape (`RowCardResponse`). |
| `source` | `row` when the cards come from the stored row, `neighbors` when they come from the old list. |
| `building` | Whether a build for this title is waiting or running, as defined above. |

`neighbors`, `score`, `computed_at` and `stale` go. The console is the only client of
this route; it shows none of the scores, and the two staleness notes described the old
table.

An empty `cards` is `200`, as an empty `neighbors` is today.

The route now needs the household's user id for watched marks; it takes it the way
the home route does (`DefaultUserIdDep`).

### The console

`SimilarSection` renders `cards` through `PosterCard` with the card's artwork and
watched mark, the same way a home row does. Its states become:

- cards: the row, with the "updating" marker while a build is being waited on;
- no cards, and a build is being waited on: a quiet "finding similar titles" line;
- no cards otherwise (including after the 60 seconds): one plain line saying there
  is nothing similar to show;
- the read failed: today's error block, unchanged.

The "stale" marker and the "Fixed at ten" and "never computed" notes are removed.

## Constraints this has to respect

- **The shared model.** Builds run one at a time on the queue. With the scheduled job
  that is at most two seed-row completions at once, inside what the local model is
  known to take.
- **Layering.** The route reads through a service; the queue is reached through
  `JobQueue`. Import-linter's contracts hold.
- **A kind the worker claims must have a handler, and the reverse.** The new kind is
  registered exactly where `CURATE` is: only with a model.
- **Stored rows are shared.** Watched marks are read per household at request time and
  never stored, as on the home screen.
- **The PRD moves with the code.** Sections 01 (the job kind's row in the concurrency
  table), 05 (similarity), 06 (when stored rows are built) and 07 (the route) change
  in the same commits.
- **The console's generated API types and cross-language guards** are regenerated, not
  hand-edited.

## Acceptance

On prod, after the deploy, with the model up:

1. **Same row in both places.** For BlackBerry, War Dogs and Spider-Man: Homecoming,
   the details page's cards are the stored row's cards in the stored order.
2. **Posters.** Every card whose title has a servable poster shows it, on a stored
   row and on the old list.
3. **First view of an unbuilt title.** Pick an enriched film with no record. Its page
   shows the old list with posters at once and the "updating" marker; the section
   swaps to the built row without a reload. Record the seconds from open to swap.
4. **Second view.** The same page, reopened, shows the stored row at once, with no
   marker and no new completion in the LLM ledger.
5. **A title that builds too few cards** keeps the old list, shows no marker on
   reopening, and is not retried.
6. **The owner reads** two or three details pages of their choosing and says whether
   the section is right.

An outage is not staged on prod. The deferral, the 60-second stop and the
no-event path are proven by tests: a build whose model raises leaves the job pending
with no attempt spent; a page that receives no event still swaps on a re-read; a page
whose build never finishes drops the marker at 60 seconds and stops asking.

## Not in this slice

- Any change to the home screen rows or to how seeds are chosen.
- Building rows ahead of time for titles nobody has opened.
- A "why this title" reason on cards.
- Removing `title_neighbors` or its daily rebuild. It stays as the fallback here and
  on the home screen.
- Dropping Because You Watched rows from the row cache on a watch write (offered
  earlier, still open).

## Left to the builder

- The job kind's name and the service and port names.
- Whether the section's cards are hydrated by reusing the row hydration or by a small
  service of its own, as long as the three reads (catalog, ownership, artwork) stay
  one statement each.
- The exact wording of the "updating" marker and the two empty-state lines, in the
  console's existing voice.
- Test placement across the four console test layers.

## Who decided what

**The owner:**

- The details page uses the same algorithm as the home row.
- Posters must load.
- A title with no stored row shows the old list first and swaps to the built row.
- Approved as played back: 20 cards rather than 10; watched titles stay, with the
  green bar; the "stale" and "fixed at ten" notes go.

**My calls, not yet seen by the owner:**

- The build is a queued job at page-open priority, one at a time, rather than a
  scheduled sweep.
- A build waits for the title to be enriched.
- The route keeps its path and changes its body; the old fields are removed rather
  than kept alongside.
- A small "updating" marker shows while a build is under way.
- The swap rides the existing `title.updated` event, backed by a re-read every 5
  seconds for at most 60 seconds, because the console's own rule is that no screen
  may depend on an event arriving.
- A build that hits a model or provider outage waits and retries every five minutes
  until it succeeds, instead of parking.
- An unenriched title shows no "updating" marker until its build is actually queued.
- The old list, where it is shown, is up to 20 long, as on the home screen.
- With no model configured, nothing is queued and the old list stays.
