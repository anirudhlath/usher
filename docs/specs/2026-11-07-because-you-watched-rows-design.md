# Because You Watched rows that match what a film is like: design

**Status:** Proposed.
**Date:** 2026-11-07.
**Slices:** one. It changes one row provider and adds one scheduled job. Everything
under "Not in this slice" waits until this one has run on real watching.

## What the owner asked for

> I think the because you watched row shows only some relevant results. Since i
> watched blackberry it suggested jobs many items down the row.

> The movies that come to mind that are similar to [War Dogs] is american made or
> lord of war to me. For tom holland spiderman it makes me think about blue beetle
> from dc.

> Other spiderman and marvel movies are fine too but you get the idea

Asked to test plot-only similarity, then shown the results:

> Nvm then your approach seems good enough

The goal is a row that matches on what a film is like (the kind of story, the
protagonist, themes and tone) rather than on words in its description, with the
best matches near the front.

## What is wrong today

`title_neighbors` is the row's only source. It takes the 100 closest embeddings
over `compose_document` (name, credits, overview, tagline, genres, keywords), then
reorders them with keyword and genre overlap and keeps 25.

| Watched | Film | Embedding rank (of ~100k) | Position in today's row |
|---|---|---|---|
| BlackBerry | The Social Network | 3 | 21 |
| BlackBerry | Jobs | 67 | 18 |
| War Dogs | Lord of War | 90 | not in row |
| War Dogs | American Made | 2,256 | not in row, outside the 100 |
| Spider-Man: Homecoming | Blue Beetle | 2,036 | not in row, outside the 100 |

So reordering alone would help BlackBerry and cannot help the other two. War Dogs'
row is led by title-word matches (Mad Dog Coll, True to the Army, War Dog), and 16
of its 25 have under 100 TMDb votes. All 25 of Homecoming's are Spider-Man, other
Marvel or Superman films.

## What was tried

Live runs against production's catalog, read-only, on the three films above plus
The Dark Knight, Arrival, Sinners and Breaking Bad.

- **Embedding plot text only: rejected.** It removes the title-word matches and
  pushes the owner's films further down: Lord of War 18,300, American Made 3,860,
  The Social Network 398, Blue Beetle 8,252.
- **TMDb's `/similar`: rejected.** It offers Psych: The Movie and Hotel St. Pauli
  for War Dogs.
- **The LLM ordering the merged candidates: rejected.** Between runs Lord of War
  went from 2nd to out of the row and Jobs from 1st to 7th. Told to allow at most 5
  franchise films, it returned 20 of 20 for Homecoming.
- **The LLM labelling franchises, with slots reserved in code: rejected.** It
  labelled Alien as Arrival's franchise and Sinister as Sinners', and the reserved
  slots moved those to the front.
- **The LLM naming candidates, fixed rules merging them: chosen.** Below.

## The design

### Three lists per seed

| List | Source | Order |
|---|---|---|
| **Named** | The LLM names 25 titles from the seed's name, year, overview and keywords | The model's |
| **Recommended** | The metadata provider's recommendations for the seed, first page | The provider's |
| **Close** | Top 100 by embedding cosine to the seed, among titles with at least 500 TMDb votes | Cosine |

Named titles are matched to the catalog on lowercased name and year within one,
taking the highest vote count when several match. A name that matches nothing is
dropped. Skeleton titles are not eligible on any list. The seed itself is removed
from all three.

The Named prompt, as tested:

```text
Title: {name} ({year}).
Overview: {overview}
Keywords: {keywords}

Name 25 real films or series a viewer who loved this one would say feel most like
it: the same kind of story, protagonist, themes and tone. Do not pick titles just
because their name shares a word with it. Do not include the title itself. Give the
exact release title in the title field, without the year, and the year in the year
field.
```

The schema is an object with `films`, an array of up to 30 `{title, year}`, at
temperature 0.3.

### The merge

1. Score every title by reciprocal rank fusion over the three lists:
   the sum of `1 / (60 + rank)`, with rank starting at 1.
2. **Merged** is every title on Named or Recommended, ordered by score descending,
   then vote count descending, then id. A title only on Close is never a card. Close
   only raises titles another list also found.
3. The row is up to 20 cards. Odd positions take the next unused title from Named,
   and even positions take the next unused title from Merged. When one list runs out,
   the other fills the rest.

Alternating is what keeps feel-alikes up front. A plain fusion puts titles on two
lists first, which for a franchise seed means 8 of 8 Spider-Man and Marvel films
before Kick-Ass at 20. Close-only titles are left out because they are where the
junk is: Superbad under Breaking Bad, Dog under War Dogs. None reached a card in the
tested rows below once they were sorted last, so these rows are unchanged by leaving
them out.

### Results on the tested merge

Two runs each. Between runs the rows shared 18, 20, 16, 20, 18, 19 and 18 of 20
cards.

| Watched | Film | Run 1 | Run 2 |
|---|---|---|---|
| BlackBerry | The Social Network | 1 | 1 |
| BlackBerry | Steve Jobs | 3 | 3 |
| BlackBerry | Jobs | 4 | 4 |
| War Dogs | Lord of War | 6 | 8 |
| War Dogs | American Made | 12 | 12 |
| Homecoming | Kick-Ass | 5 | 11 |
| Homecoming | Shazam! | 7 | 9 |
| Homecoming | Big Hero 6 | 11 | 5 |
| Homecoming | Blue Beetle | not found | not found |

Run 1's rows, first ten cards:

| # | War Dogs | BlackBerry | Homecoming |
|---|---|---|---|
| 1 | GoodFellas | The Social Network | Spider-Man: Far From Home |
| 2 | The Wolf of Wall Street | Pirates of Silicon Valley | The Amazing Spider-Man |
| 3 | Blow | Steve Jobs | Super 8 |
| 4 | American Gangster | Jobs | Spider-Man 2 |
| 5 | American Hustle | The Big Short | Kick-Ass |
| 6 | Lord of War | Air | Spider-Man: Into the Spider-Verse |
| 7 | Casino | Moneyball | Shazam! |
| 8 | A Most Violent Year | Joy | Spider-Man: No Way Home |
| 9 | Scarface | The Founder | Sky High |
| 10 | Training Day | Moxie | The Amazing Spider-Man 2 |

**Known weak spots, accepted for this slice:**

- No list found Blue Beetle, so it is not an acceptance bar.
- American Made appears only on Recommended, which leaves it at 12.
- The back of the row is weaker, with provider-only titles such as Moxie, Jakob the
  Liar and Small Apartments.
- Rows mix films and series.
- The runs used only well-known seeds. How the LLM behaves on an obscure seed is
  untested, and the owner's actual recent watches were not tested either.

### When rows are built

A `ScheduledJob` walks the household's recency window, the same `list_recent` the
provider reads (12 titles), and builds a row for each seed that has none. Each build
costs one LLM call and one metadata call, run one seed at a time. The job runs every
15 minutes and **must make no LLM or metadata call when nothing is due**. Generation
never happens on the request path, per PRD 06.

A row is due when:

- it has never been built;
- it was built by a different recipe, meaning a stored fingerprint of the prompt,
  merge constants and model no longer matches;
- it is more than 30 days old; or
- a previous attempt produced too few cards and that attempt is more than 7 days
  old.

### What gets stored

A new table keyed by seed title, not by user, since the row is the same for anyone
who watched that seed. It holds ordered cards, which lists each card came from, the
build time and the recipe fingerprint, and it can record an attempt that produced
too few cards. It is rebuildable, so it follows `title_neighbors`' treatment in
backups rather than `watch_states`'. `title_neighbors` itself is not touched: the
rebuild replaces its rows wholesale and `GET /titles/{id}/similar` reads it.

Source names stored or reported are neutral (`named`, `recommended`, `close`), since
no source-specific concept may leave its adapter.

### The read path

`BecauseYouWatchedProvider` uses a seed's stored row when it has one, whatever the
LLM and scheduler settings say, and falls back to `title_neighbors` otherwise. Seed
selection, the overlap rule, `_MAX_SEEDS`, scores, titles and reasons do not change.
The existing six-hour row TTL stays, so a newly built row can take up to six hours
to appear unless a deploy restarts the process first.

**Watched films stay in the row and are marked.** Each card for a title the household
has played carries `played`, so the console draws the green bar that Rediscover's
cards already show. No console change is needed. One read covers the whole row rather
than one per card, and the mark applies to stored and fallback rows alike. Watched
state is read when the row is built and never stored with it, because one stored row
serves every household that watched the seed.

### When things fail

The home screen never waits on a build, and in every case below a seed with no
stored row shows today's row.

| Case | Behaviour |
|---|---|
| `USHER_LLM_ENABLED=false` or scheduler off | No rows are built. Stored rows keep showing |
| LLM or metadata provider unreachable, or answering 429 or 5xx | The walk stops and nothing is recorded. A later run tries again under the scheduler's failure spacing |
| Provider has no record of the seed, or the seed has no provider id | Recommended is empty and the build continues |
| LLM output truncated or unusable for this seed | The attempt is recorded, so the 7-day rule applies, and the walk moves to the next seed |
| Fewer than 8 cards after matching | No row is stored. The attempt is recorded, so the 7-day rule applies |
| A rebuild fails or falls short on a seed that has a row | The existing row stays |
| A carded title is later deleted | `hydrate` drops it, as it does now |

This slice has no off switch of its own. Reverting the change is the way back.

## Constraints this has to respect

- `complete_json` hard-codes `temperature: 0.8`, and every result above was at 0.3.
  The port needs a per-call temperature, and curation stays at 0.8.
- `LLMPurpose` is a closed vocabulary. The new call adds a member and updates PRD 10
  in the same change, and goes through `services/llm_ledger.py`.
- Metadata recommendations are a new source-neutral method on the metadata port, with
  fixtures captured the way `fixtures-and-fakes.md` describes.
- Close needs a query that returns 100 rows under its vote filter. Check the plan
  against `db-and-sql.md` rather than assuming `nearest_for` can be reused.
- vLLM is shared with production Alfred, so builds stay sequential.
- PRD 06 and PRD 10 change in the same commit as the behaviour they describe.

## Acceptance

Unit tests pin the merge's order on cases whose premises they assert, per the rule
that a membership assertion is not an ordering test.

Then a live run on production's data after the deploy, twice per seed, with these
bars recorded before the run. The bars name seeds outside the household's window, so
an operator command builds one named seed through the same code the job uses. The run
writes only to the new table. A failed bar stops the work and goes to the owner, who
decides between fixing forward and reverting.

| Watched | Bar, in both runs | Today |
|---|---|---|
| BlackBerry | The Social Network, Steve Jobs and Jobs all in the top 6 | 21, 19, 18 |
| War Dogs | Lord of War in the top 10, American Made in the top 15 | neither in row |
| War Dogs | None of Mad Dog Coll, True to the Army, War Dog: A Soldier's Best Friend in the row | all three in row |
| Homecoming | At least 3 of Kick-Ass, Shazam!, Big Hero 6, Sky High, Chronicle in the top 12 | none in row |

There is no bar on the number of franchise films, since the owner said they are fine.
The tested runs held 10 and 13 of 20 Spider-Man and Marvel films, against 23 of 25
today.

The last check is the owner's: read the rows built from their real recent watches on
the actual home screen. A row that fails there is a failure even with every bar met.

## Not in this slice

- The title page's "More like this" and `GET /titles/{id}/similar`.
- Other row providers.
- An LLM-written description of each film as a richer embedding. It is untested and
  might do better than the plot text, but it means reprocessing the catalog.
- Tuning the back of the row, or a separate slot for Recommended.

## Left to the builder

- Table and column names, and whether cards are rows or an array.
- How the job answers `last_done()`, given that it may do nothing on most ticks.
- How the repository reaches `RowContext`, within that class's rule that a field
  arrives with the provider that reads it.
- How an operator builds and prints one named seed's row. There must be a way, since
  the live acceptance run needs it, and extending `usher similar` is the obvious one.
- The telemetry that separates a stored row from a fallback on the home screen. An
  operator must be able to tell them apart.

## Who decided what

**The owner:** the combined approach over plot-only similarity, that other
Spider-Man and Marvel films are fine in the row, and that films the household has
already watched stay in the row, marked as watched.

**The designer, not yet ruled on by the owner:**

- The LLM names candidates and fixed rules merge them, replacing the LLM ordering the
  owner was first shown.
- Close-only titles are never cards.
- No franchise-count bar and no separate off switch.
- The numbers: 500 votes for Close, 8 cards to store a row, a 15-minute period, 30
  days to rebuild, 7 days to retry.
- Acceptance runs after the deploy rather than on a copy of production.
