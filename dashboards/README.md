# Dashboards

Grafana dashboards for Usher, shipped as provisioned JSON so a fresh deploy has
them without clicking. They live with the code that emits the data, so they
version together.

**The stack that renders them is not in this repository.**
[PRD 10](../docs/prd/10-telemetry-and-dashboards.md)'s "Where the stack lives"
puts Grafana, Prometheus, Loki and Tempo in `~/code/observability/`, shared with
Alfred and anything added later, and Usher's only coupling to telemetry is the
two environment variables in `.env.example`. So this directory holds JSON and a
provisioning YAML that the other project's compose file bind-mounts;
**Usher's own `compose.yml` gains nothing.** The two mounts are spelled out in
`provisioning/dashboards.yml`.

| file | dashboard | datasource |
|---|---|---|
| `01-library-and-catalog.json` | 1 — Library & Catalog | Postgres only |
| `02-taste-and-watching.json` | 2 — Taste & Watching | Postgres only |
| `03-pipeline.json` | 3 — Pipeline | Prometheus **and** Postgres |
| `04-performance.json` | 4 — Performance | Prometheus **and** Postgres |
| `05-cost-and-compliance.json` | 5 — Cost & Compliance | Postgres, and one Prometheus panel |
| `alerts/usher.yml` | — (Prometheus rules) | Prometheus |

**Dashboard 6 — Quality evals is specified in PRD 10 and not built.** ⚠️ This
line replaces two contradictory ones: D7 through D10 each appended a row to the
table above and left the previous task's *"dashboards N–6 are not yet built"*
sentence standing beneath it, so this file simultaneously claimed that
dashboards 2, 4 and 5 did not exist and listed two of them. Repaired by D11
rather than left, because the next reader of a table with a stale sentence
under it trusts the sentence.

**`alerts/usher.yml` is in the table because it is the same kind of asset for
the same reason** — written against the instruments in `src/usher/` and
versioned with them, evaluated by a Prometheus this repository does not own. It
needs a `rule_files:` entry and a bind mount in `~/code/observability/`, neither
of which is committed there yet; the file's own header spells both out.
⚠️ **Three stanzas rather than two since D13**: *Disk projection* is the one
rule whose series Usher does not emit and the stack does not yet produce, so
that project's collector also needs a `hostmetrics` receiver. It is written out
above the rule that needs it, and until it lands the rule's `absent()` companion
is what says so out loud rather than reading green.

**Every panel on dashboards 1 and 2 is SQL against the canonical database and
none of them is a Prometheus query.** That is PRD 10's first principle doing the
thing it was written for: *"Most of what is worth knowing about a media catalog
is **not a metric**… The catalog *is* the record."* No metric the collector
holds is a catalog count or a watch state, so reaching for one here would
produce a permanently empty panel. Dashboard 2 makes the same point one level
down: **watch state is a table, not a series**, and where the table cannot
answer the question the panel is not shipped at all.

**Dashboard 2 ships six panels against PRD 10's eight, and the sixth is not a
panel about watching.** M10's D0 audit found three of the eight — watch time by
day and user, taste drift over months, and row effectiveness per `RowProvider` —
have no backing series ([#84](https://github.com/anirudhlath/usher/issues/84),
[#85](https://github.com/anirudhlath/usher/issues/85)), so the file holds five
query panels and one text panel naming the three absences in PRD 10's own words.
The argument for the text panel is that a dashboard whose specification lists
eight and whose JSON holds five reads as a half-finished commit to anyone who
has not read D0's paragraph — and an empty panel would be worse than either,
because on *this* dashboard it looks like a household that watches nothing.

## What the harness checks, and what it cannot

`tests/unit/test_dashboards.py` applies three invariants to every
`dashboards/*.json`: structural validity (including `uid` uniqueness across
files, because Grafana silently overwrites a colliding `uid` with no error
anywhere), Prometheus metric names against PRD 10's catalogue, and Postgres
`table.column` pairs against `Base.metadata`.

⚠️ **The third one is checked only where the SQL writes table names out.** A
SQL alias is not a table name, so `t.name` in `FROM titles t` is invisible to
the scan and a panel written entirely in aliases is checked on nothing. Every
statement in this directory is therefore **unaliased**, and
`test_the_committed_dashboards_are_not_written_in_aliases` is what keeps it
that way.

**Dashboard 2 added a fourth question and a fifth arm.** The fourth is about
what is *not* there:
`test_dashboard_two_ships_no_panel_the_audit_found_unbacked` reads the three
unbacked panel names **out of PRD 10's own D2 paragraph** rather than from a
retyped list, and asserts no committed panel title matches one — in either
direction of containment, so `"Watch time"` is caught as well as the full
spelling. `test_the_absence_panels_three_sentences_are_byte_identical_to_prd_tens`
then pins the text panel's three sentences to the PRD's, so a correction to one
is a red on the other rather than a drift. The fifth arm is invariant 1 for a
**text** panel: it draws no data, so "no target" is its correct shape rather
than an empty rectangle, and what it is exempted into is an assertion that
`options.content` is non-empty.

## The observations — Dashboard 1

Recorded **2026-09-07** against the live catalog `usher_catalog` (Alembic
`m10b`) through the running Grafana 13.1.3 at `127.0.0.1:3000`, datasource uid
`usher-postgres`. Each panel was opened alone at `?viewPanel=<id>` and its own
target was also issued through `/api/ds/query`, which is the same path the
panel takes; the wall times below are that round trip, so they include
Grafana's proxy and not only the database.

Catalog size on the day: **1,276,268 titles, 23,943 `media_items`, 11,516
distinct owned titles.**

### 1 — Titles by enrichment state

`piechart` over `titles.enrichment_state`. The whole catalog and no denominator
caveat: the column is `NOT NULL`.

```sql
SELECT titles.enrichment_state AS state,
       count(*) AS titles_in_state
FROM titles
GROUP BY titles.enrichment_state
ORDER BY count(*) DESC
```

**Returned** 3 rows in 174.4 ms: `skeleton` 1,142,767 · `enriched` 133,447 ·
`stub` 54. Rendered as three slices with the legend carrying all three counts;
`stub` is 0.004% and is a visible sliver only in the legend.

### 2 — Owned vs catalog coverage

`table` over `media_items.available` and `media_items.title_id`.

```sql
SELECT 'catalog titles' AS bucket, count(*) AS rows_counted
FROM titles
UNION ALL
SELECT 'owned titles (matched)', count(DISTINCT media_items.title_id)
FROM media_items
WHERE media_items.available AND media_items.title_id IS NOT NULL
UNION ALL
SELECT 'owned items (available)', count(*)
FROM media_items
WHERE media_items.available
UNION ALL
SELECT 'owned items (unmatched)', count(*)
FROM media_items
WHERE media_items.available AND media_items.title_id IS NULL
```

**Returned** 4 rows in 100.6 ms: catalog titles 1,276,268 · owned titles
(matched) 11,516 · owned items (available) 23,943 · owned items (unmatched)
5,500. The four numbers do not sum to anything: an item is a file, a title is a
work, and 18,443 of the 23,943 available items carry a `title_id` at all.

### 3 — Genre distribution

`barchart`, horizontal, over `GROUP BY unnest(titles.genres)` — the exact facet
collapse `.claude/rules/db-and-sql.md` names.

```sql
SELECT exploded.genre AS genre,
       count(*) AS titles_with_genre
FROM (
    SELECT unnest(titles.genres) AS genre
    FROM titles
    WHERE cardinality(titles.genres) > 0
) AS exploded
GROUP BY exploded.genre
ORDER BY count(*) DESC
LIMIT 25
```

**Returned** 25 rows in 254.2 ms: Drama 394,562 · Documentary 253,026 · Comedy
233,884 · Action 81,134 · Romance 78,130 · … · Western 9,158. **The column is
not a denominator** — `titles.genres` is non-empty on 1,157,046 rows and a
title with three genres is counted three times, so these sum past the catalog.

### 4 — Decade distribution

`barchart`, vertical, over `titles.year`.

```sql
SELECT ((titles.year / 10) * 10)::text AS decade,
       count(*) AS titles_in_decade
FROM titles
WHERE titles.year IS NOT NULL AND titles.year >= 1870
GROUP BY (titles.year / 10) * 10
ORDER BY (titles.year / 10) * 10
```

**Returned** 15 rows in 133.6 ms: 1890 → 18, 1900 → 184, 1910 → 12,971 …
2000 → 162,663, **2010 → 332,491** (the mode), 2020 → 248,183, 2030 → 16.
`titles.year` is non-null on 1,128,000 of 1,276,268 rows. **The `>= 1870` bound
excludes exactly one row**, which carries `year = 1`; without it the axis opens
on a `0` decade holding that single title and every real bar is compressed.
`::text` is deliberate: a numeric x-field makes `barchart` draw a continuous
axis rather than one bar per decade.

### 5 — Language distribution

`barchart`, horizontal, over `titles.original_language`.

```sql
SELECT titles.original_language AS language,
       count(*) AS titles_in_language
FROM titles
WHERE titles.original_language IS NOT NULL
GROUP BY titles.original_language
ORDER BY count(*) DESC
LIMIT 20
```

**Returned** 20 rows in 120.6 ms: en 64,131 · fr 8,451 · es 5,457 · it 5,230 ·
de 4,962 · ja 4,726 · … · pl 1,072.

⚠️ **Denominator caveat, and this is the panel that proves it.**
`titles.original_language` is non-null on **exactly 133,447** rows — the
`enriched` count to the row — so this panel sits on the enriched tier and not
on the catalog. **133,447 of 1,276,268 is 10.5%**; over the owned library the
same tier is **9,784 of 11,516, 85.0%**. Both numbers have to travel together:
the second is what bounds any panel drawn over the shelf, and quoting only the
first understates it eight-fold.

### 6 — Runtime distribution

`barchart`, vertical, over `titles.runtime_minutes` in ten-minute buckets.

```sql
SELECT least((titles.runtime_minutes / 10) * 10, 240)::text AS runtime_bucket_minutes,
       count(*) AS titles_in_bucket
FROM titles
WHERE titles.runtime_minutes IS NOT NULL AND titles.runtime_minutes > 0
GROUP BY least((titles.runtime_minutes / 10) * 10, 240)
ORDER BY least((titles.runtime_minutes / 10) * 10, 240)
```

**Returned** 25 rows in 131.3 ms: 0 → 11,437 · 10 → 10,838 · 20 → 31,931 · …
· **90 → 123,430** (the mode, the 90–99 minute feature) · 100 → 58,091 · … ·
**240 → 4,360**, which is the `least(…, 240)` overflow bucket and reads as
"240 minutes and over" rather than as a spike at four hours. `runtime_minutes`
is non-null on 715,395 of 1,276,268 rows.

### 7 — Quality ladder by decade

`table` over `media_items` joined to `titles`. This is the panel a reader
expects to be missing and it is not: `media_items` carries `container`,
`video_codec`, `audio_codec`, `width`, `height`, `hdr_format`,
`audio_channels` and `file_size_bytes`, all filled from Emby's `MediaStreams`.

```sql
SELECT ((titles.year / 10) * 10)::text AS decade,
       count(*) AS probed_items,
       count(*) FILTER (WHERE media_items.width >= 3840) AS uhd_items,
       round(100.0 * count(*) FILTER (WHERE media_items.width >= 3840) / count(*), 1) AS uhd_pct,
       count(*) FILTER (WHERE media_items.hdr_format IS NOT NULL) AS hdr_items,
       round(100.0 * count(*) FILTER (WHERE media_items.hdr_format IS NOT NULL) / count(*), 1) AS hdr_pct,
       round(100.0 * count(*) FILTER (WHERE media_items.video_codec IN ('hevc', 'av1')) / count(*), 1) AS hevc_or_av1_pct
FROM media_items
JOIN titles ON titles.id = media_items.title_id
WHERE media_items.available
  AND media_items.video_codec IS NOT NULL
  AND titles.year IS NOT NULL
  AND titles.year >= 1870
GROUP BY (titles.year / 10) * 10
ORDER BY (titles.year / 10) * 10
```

**Returned** 12 rows in 78.3 ms — **the 4K share by decade, with its row
count**:

| decade | probed_items | uhd_items | uhd_pct | hdr_items | hdr_pct | hevc_or_av1_pct |
|---|---|---|---|---|---|---|
| 1910 | 9 | 0 | 0.0 | 0 | 0.0 | 0.0 |
| 1920 | 9 | 0 | 0.0 | 0 | 0.0 | 11.1 |
| 1930 | 63 | 2 | 3.2 | 2 | 3.2 | 3.2 |
| 1940 | 72 | 0 | 0.0 | 1 | 1.4 | 1.4 |
| 1950 | 89 | 3 | 3.4 | 3 | 3.4 | 6.7 |
| 1960 | 102 | 4 | 3.9 | 6 | 5.9 | 10.8 |
| 1970 | 243 | 28 | 11.5 | 31 | 12.8 | 16.5 |
| 1980 | 1,929 | 89 | 4.6 | 87 | 4.5 | 9.1 |
| 1990 | 2,220 | 111 | 5.0 | 65 | 2.9 | 7.3 |
| 2000 | 1,303 | 157 | 12.0 | 61 | 4.7 | 17.7 |
| 2010 | 2,554 | 468 | 18.3 | 167 | 6.5 | 20.5 |
| 2020 | 6,488 | 2,015 | 31.1 | 1,132 | 17.4 | 36.0 |

Row count **15,081 probed items across the twelve decades**, against 20,857
items carrying a `video_codec` at all — the difference is items whose title has
no `year`.

⚠️ **The share is of probed items and never of the table**, and the reason is
structural rather than a rounding choice. `HdrFormat` has no SDR member
(`domain/enums.py`: HDR10, DV, HLG), so `hdr_format IS NULL` means *"SDR **or**
never probed"* and never *"SDR"*. 22,239 of 23,943 rows are null, and 3,021 of
those carry no `container` either — 3,007 of them series-level rows with no file
behind them. Catalog-wide on the day: 20,857 probed, 3,171 at 3,840 pixels or
wider, 1,704 with an `hdr_format` (HDR10 974, DV 706, HLG 24). The Dolby Vision
translation is 706 live rows, not a fixture.

### 8 — Franchise completeness (the want-list)

`table` over `titles.collection_id` joined to `collections`, listing the
missing entries — which is what makes it a want-list rather than a percentage.

```sql
WITH owned AS (
    SELECT DISTINCT media_items.title_id AS title_id
    FROM media_items
    WHERE media_items.available
      AND media_items.title_id IS NOT NULL
      AND media_items.episode_id IS NULL
)
SELECT collections.name AS franchise,
       count(*) AS entries,
       count(owned.title_id) AS owned_entries,
       count(*) - count(owned.title_id) AS missing_entries,
       string_agg(
           titles.name || coalesce(' (' || titles.year || ')', ''),
           ', ' ORDER BY titles.year NULLS LAST, titles.name
       ) FILTER (WHERE owned.title_id IS NULL) AS missing
FROM titles
JOIN collections ON collections.id = titles.collection_id
LEFT JOIN owned ON owned.title_id = titles.id
GROUP BY collections.id, collections.name
HAVING count(owned.title_id) > 0 AND count(owned.title_id) < count(*)
ORDER BY count(*) - count(owned.title_id) DESC, collections.name
LIMIT 50
```

**Returned** 50 rows in 67.4 ms, led by The Bowery Boys Collection (48 entries,
1 owned, 47 missing), Doraemon Movies (45 / 3 / 42) and Lupin the Third
Collection (34 / 1 / 33).

**One named franchise with its listed missing entries**, which is this panel's
whole claim — the **James Bond Collection**, 26 entries, 13 owned, 13 missing:

> Dr. No (1962), From Russia with Love (1963), You Only Live Twice (1967), On
> Her Majesty's Secret Service (1969), Diamonds Are Forever (1971), Live and
> Let Die (1973), The Man with the Golden Gun (1974), The Spy Who Loved Me
> (1977), Moonraker (1979), Tomorrow Never Dies (1997), Skyfall (2012), Spectre
> (2015), No Time to Die (2021)

⚠️ **Denominator caveat.** `titles.collection_id` arrives from TMDb enrichment —
13,381 titles carry one, over 5,298 collections — so the panel is bounded by
the enriched tier and **a franchise whose other entries were never enriched
reads as complete**. The `HAVING` clause is what keeps that from being
invisible: a collection with zero owned entries is not a want-list, it is a
franchise nobody has started.

### 9 — Most-represented directors and actors

`table` over `credits` joined to `people`, scoped to the owned library.

```sql
WITH owned AS (
    SELECT DISTINCT media_items.title_id AS title_id
    FROM media_items
    WHERE media_items.available AND media_items.title_id IS NOT NULL
),
directors AS (
    SELECT people.name AS person,
           count(DISTINCT credits.title_id) AS owned_titles
    FROM credits
    JOIN owned ON owned.title_id = credits.title_id
    JOIN people ON people.id = credits.person_id
    WHERE credits.kind = 'crew' AND credits.job = 'Director'
    GROUP BY people.id, people.name
    ORDER BY count(DISTINCT credits.title_id) DESC, people.name
    LIMIT 15
),
actors AS (
    SELECT people.name AS person,
           count(DISTINCT credits.title_id) AS owned_titles
    FROM credits
    JOIN owned ON owned.title_id = credits.title_id
    JOIN people ON people.id = credits.person_id
    WHERE credits.kind = 'cast'
    GROUP BY people.id, people.name
    ORDER BY count(DISTINCT credits.title_id) DESC, people.name
    LIMIT 15
)
SELECT 'director' AS role, directors.person AS person, directors.owned_titles AS owned_titles
FROM directors
UNION ALL
SELECT 'actor', actors.person, actors.owned_titles
FROM actors
ORDER BY role DESC, owned_titles DESC, person
```

**Returned** 30 rows in 824.0 ms — the slowest panel on the dashboard, and the
only one over 500 ms. Directors: Woody Allen 20 · Clint Eastwood 15 · Tom Clegg
13 · Vince McMahon 13 · Barry Levinson 12. Actors: Frank Welker 74 · Jim
Cummings 44 · Samuel L. Jackson 37 · Tom Hanks 37 · Keanu Reeves 34.

**One target rather than two, and that was a repair.** Written as two targets
the panel rendered as two frames behind a frame selector, so the actors half was
one click away and nobody reading the dashboard would see it. The `UNION ALL`
with a `role` column is one frame.

The scope is the owned library on purpose. `credits` holds 2,910,957 rows
catalog-wide (cast 2,530,801, crew 380,156, of which `job = 'Director'` is
143,888) over 896,079 people, but all of it arrives from TMDb enrichment — the
10.5% tier. Over the shelf the same tier is 85.0%, and **that is the figure
that bounds this panel.** Catalog-wide the answer is a different one and worth
recording as the rejected alternative: Jesús Franco 105, Lesley Selander 101,
Joseph Kane 100.

### 10 — Library growth per week

`timeseries` (bars) over `media_items.added_at`.

```sql
SELECT date_trunc('week', media_items.added_at) AS time,
       count(*) AS items_added
FROM media_items
WHERE media_items.available
  AND media_items.added_at IS NOT NULL
  AND $__timeFilter(media_items.added_at)
GROUP BY date_trunc('week', media_items.added_at)
ORDER BY date_trunc('week', media_items.added_at)
```

`$__timeFilter` is Grafana's macro and is expanded server-side. **The query as
issued**, read back from the response's own `executedQueryString` over the
dashboard's default `now-8y` range:

```sql
  AND media_items.added_at BETWEEN '2018-09-09T23:33:52.643Z' AND '2026-09-07T23:33:52.643Z'
```

**Returned** 256 weekly buckets in 13.0 ms. `added_at` spans **2019-02-12 to
2026-09-02**; the recent tail is 2026-08-03 → 182, 2026-08-10 → 2,179,
2026-08-17 → 3,606, 2026-08-24 → 3,393, 2026-08-31 → 1,845, with the only
earlier comparable week being 2023-08-28 at 1,879.

**`added_at IS NOT NULL` is written out even though nothing needs it today.**
0 of 23,943 rows are null, so the filter is structural rather than live — the
next source to report no `DateCreated` would empty part of the curve without
raising anything.

### 11 — Unmatched review queue depth

`stat` over the partial index `ix_media_items_unmatched (source_id) WHERE
title_id IS NULL`.

```sql
SELECT sources.name AS source,
       count(*) AS unmatched
FROM media_items
JOIN sources ON sources.id = media_items.source_id
WHERE media_items.title_id IS NULL
GROUP BY sources.id, sources.name
ORDER BY count(*) DESC
```

**Returned** 1 row in 5.7 ms: `Shared Emby` → **5,500**. That is the depth.

**The depth and the wall time at that depth, against PRD 10's `OFFSET` caveat.**
Dashboard 3's section says `list_unmatched` pages by `OFFSET`, *"measured at
43.7 ms at offset 0 against 388.9 ms at offset 1,126,574"*. That figure does not
describe this deployment and cannot: offset 1,126,574 is two hundred times the
whole unmatched queue, so a page taken there returns nothing. Re-measured
2026-09-07 at the real depth, `EXPLAIN (ANALYZE, BUFFERS)` on
`pgvector/pgvector:pg17`, `LIMIT 50`:

| query | plan | execution |
|---|---|---|
| the panel — `count(*) WHERE title_id IS NULL` | index-only over `ix_media_items_unmatched` | **0.32 ms** (1.32 ms cold) |
| `OFFSET 0` | Index Scan 5,500 rows → top-N heapsort, 51 kB | 2.93 ms |
| `OFFSET 5450` | Index Scan 5,500 rows → quicksort, 1,606 kB | 2.88 ms |

**The offset's cost is invisible at this depth and the sort is the whole
expense**, exactly as `db/repositories/media_item.py`'s own measurement
predicts: `ix_media_items_unmatched` is `(source_id) WHERE title_id IS NULL` and
carries neither `added_at` nor `id`, so every page is a sort over the entire
unmatched population. **None of that reaches this panel**, which is a count and
not a page — the OFFSET caveat belongs to the review queue's pager and is
recorded here so nobody transplants it onto the number above.

The panel is deliberately uncoloured. Grafana's default thresholds would render
5,500 red, and no bar has been set for this queue: a colour would be an alarm
nobody declared.

## The observations — Dashboard 2

Recorded **2026-09-07** against the live catalog `usher_catalog` (Alembic
`m10b`) through the running Grafana 13.1.3 at `127.0.0.1:3000`, datasource uid
`usher-postgres`, provisioned out of this directory into folder `Usher`. Each
panel was opened alone at `?viewPanel=<id>` in a headless Chromium and read off
the rendering, **and** its own target was issued through `/api/ds/query`, which
is the same path the panel takes. The wall times are that round trip warm — the
third of three calls — so they include Grafana's proxy and not only the
database; where the cold first call differed materially it is given too.

**The household is the single row in `users`, `name = 'default'`**, and every
figure below is that household's. Its watch state is **16,782 `watch_states`
rows**: 142 carry `played`, 139 carry a `last_played_at`, 80 carry
`play_count > 1`, and `SUM(play_count)` is 347. Library on the day: 23,943
`media_items` (23,665 of them carrying a `runtime_seconds`), 11,516 distinct
owned titles, 1,276,268 catalog titles.

⚠️ **Every one of the 16,782 rows arrived from a walk rather than from a push,
and that is the single fact behind three of the five panels' caveats.** A walk
lists what exists and writes a row for it; it cannot report a runtime, and it
cannot report a play it did not observe. So `watch_states.runtime_seconds` is
null on **all 16,782 rows**, and 16,585 of them carry `played = false`,
`play_count = 0` **and** `position_seconds = 0` — a row that exists and says
nothing.

### 1 — Completion rate

`stat` over `watch_states.played`, joined to `users` so the panel names the
household rather than assuming one.

```sql
SELECT users.name AS household,
       count(*) AS watch_state_rows,
       count(*) FILTER (WHERE watch_states.played) AS played_rows,
       round(100.0 * count(*) FILTER (WHERE watch_states.played) / count(*), 2) AS completion_rate_pct,
       count(*) FILTER (
           WHERE watch_states.played
              OR watch_states.play_count > 0
              OR watch_states.position_seconds > 0
       ) AS engaged_rows,
       round(
           100.0 * count(*) FILTER (WHERE watch_states.played)
           / nullif(count(*) FILTER (
                 WHERE watch_states.played
                    OR watch_states.play_count > 0
                    OR watch_states.position_seconds > 0
             ), 0),
           2
       ) AS completion_rate_engaged_pct
FROM watch_states
JOIN users ON users.id = watch_states.user_id
GROUP BY users.id, users.name
ORDER BY users.name
```

**Returned** 1 row in 13.3 ms: household `default`, 16,782 watch-state rows,
142 played, **0.85%**, 197 engaged rows, **72.08%**. Rendered as two stats side
by side, the field-name regex `/_pct$/` keeping the three count columns off the
panel while leaving them in the frame for the tooltip.

⚠️ **Two denominators, and the one the task specifies is mostly a fact about
the ingest path.** `count(*) FILTER (WHERE played) / count(*)` over
`watch_states` divides by the library, because the walk wrote a row for nearly
everything it saw — 16,585 of the 16,782 are the zero rows above. **0.85% is
what that produces, and read alone it says this household finishes almost
nothing.** Over the 197 rows carrying any engagement at all it is **72.08%**,
eighty-five times the first, and both numbers ship on the panel because either
one alone is a different claim. The second is not the "true" figure either: it
is the completion rate *of things this household started*, which is the
question a reader of "completion rate" usually means.

### 2 — Abandonment cliff

`barchart` over `position_seconds / runtime_seconds` for `played = false`, in
eleven ten-point buckets, **with the denominator coalesced in the order the
panel description states**.

```sql
WITH abandoned AS (
    SELECT watch_states.position_seconds AS position_seconds,
           coalesce(
               watch_states.runtime_seconds,
               (SELECT max(media_items.runtime_seconds)
                FROM media_items
                WHERE media_items.title_id = watch_states.title_id
                  AND media_items.available),
               (SELECT titles.runtime_minutes * 60
                FROM titles
                WHERE titles.id = watch_states.title_id)
           ) AS runtime_seconds
    FROM watch_states
    WHERE NOT watch_states.played
      AND watch_states.position_seconds > 0
      AND watch_states.title_id IS NOT NULL
)
SELECT CASE
           WHEN deciles.decile_start >= 100 THEN '100%+'
           ELSE deciles.decile_start::text || '-' || (deciles.decile_start + 9)::text || '%'
       END AS stopped_at,
       count(abandoned.position_seconds) AS watch_states_abandoned
FROM generate_series(0, 100, 10) AS deciles(decile_start)
LEFT JOIN abandoned
       ON abandoned.runtime_seconds > 0
      AND least(floor(100.0 * abandoned.position_seconds / abandoned.runtime_seconds / 10) * 10, 100)
          = deciles.decile_start
GROUP BY deciles.decile_start
ORDER BY deciles.decile_start
```

**Returned** 11 rows in 27.0 ms (50.5 ms cold) over **55 abandoned states**:
0–9% → 11 · 10–19% → 8 · 20–29% → 2 · 30–39% → 8 · 40–49% → 7 · 50–59% → 6 ·
60–69% → 6 · 70–79% → 4 · 80–89% → 2 · **90–99% → 0** · 100%+ → 1. The cliff is
at the front: **19 of the 55, 34.5%, stop inside the first fifth** — and the
single largest bucket is the first.

🔴 **Which fallback supplied the denominator, and for what share of rows — the
observation this panel is worthless without.** The three sources in the
coalesce are `watch_states.runtime_seconds`, then `media_items.runtime_seconds`,
then `titles.runtime_minutes * 60`, and on this household:

| source | covers | supplied |
|---|---|---|
| `watch_states.runtime_seconds` (`db/models/watch.py`, nullable by ADR-0014) | **0 of 55** | 0 |
| `media_items.runtime_seconds` | 55 of 55 | **55 of 55 — 100%** |
| `titles.runtime_minutes × 60` | 55 of 55 | 0 (never reached) |

**Every bar above is drawn on `media_items.runtime_seconds`, the second entry,
and not one row on the column the panel names.** The first is null across the
whole table because these states came from a walk; the third would have covered
all 55 too and was never reached.

⚠️ **The second and third are not interchangeable, which is why the share is
recorded rather than the mere fact of a fallback.** Over these 55 rows the two
disagree on **54 of 55**, by a mean absolute 136 s and a maximum of 4,306 s,
and **2 of the 55 land in a different ten-point bucket** depending on which is
used. A run of this panel on a household whose items lack `runtime_seconds`
would therefore be plotting a *different population*, not a noisier version of
this one.

All 55 rows are title-scoped (`title_id IS NOT NULL`, `episode_id IS NULL`), so
the `media_items` sub-select needs no episode arm today; the `title_id IS NOT
NULL` predicate is written out anyway, because an episode-scoped abandoned
state would otherwise silently take a `NULL` denominator and vanish.

**The empty 90–99% bucket is emitted on purpose.** `generate_series(0, 100, 10)`
left-joined to the data is what makes it a zero-height bar with a `0` label
rather than a missing bar — without it the axis runs 80–89% straight into
100%+, and a reader counts nine buckets where there are ten.

**The x-axis labels were a repair.** At `w: 9` the eleven horizontal labels
overlapped into `0-9%10-19%20-29%…` — legible in the frame, unreadable on the
panel. `xTickLabelRotation: -45` fixed it; the alternative, shortening the
labels to their bucket start, would have made `0%` the name of the 0–9% bucket.

### 3 — When each item was last played

`barchart` over `date_part('hour', watch_states.last_played_at)`.

```sql
SELECT hours.hour::text AS hour_utc,
       count(watch_states.id) AS items_last_played
FROM generate_series(0, 23) AS hours(hour)
LEFT JOIN watch_states
       ON watch_states.last_played_at IS NOT NULL
      AND date_part('hour', watch_states.last_played_at) = hours.hour
GROUP BY hours.hour
ORDER BY hours.hour
```

**Returned** 24 rows in 4.5 ms over the **139 dated rows**: the mode is 02:00
with 19, then 04:00 with 17, 05:00 with 13, 06:00 with 11; 12:00 and 14:00 are
**0** and are drawn as zero bars for the same reason the 90–99% bucket is.

**This panel ships retitled and the retitle is the whole finding.** PRD 10
specifies a *time-of-day heatmap*; `last_played_at` is one timestamp per row,
overwritten by every later play, so the only question the column can answer is
*when each item was last played* — never *when this household watches*. 80 of
the 139 dated rows carry `play_count > 1`, so at least that many earlier
timestamps have already been erased.

⚠️ **The hour is UTC and the timestamps two panels over are not, on the same
dashboard.** `date_part` runs server-side in the database session's timezone,
which is `Etc/UTC` here, so these 24 buckets are UTC; Grafana converts a
`timestamptz` *field* to the browser's zone, so panel 4's `last_played` column
renders at UTC−5 in the same screenshot. Directly observed: the top rewatch row
holds `2026-07-30 08:12:53+00` and renders `2026-07-30 03:12:53`. **The 02:00
UTC mode is 21:00 local**, which is a plausible evening and not the 2 a.m. the
axis appears to claim. The panel description says so; no `AT TIME ZONE` is
written into the SQL because the zone would then be a deployment fact committed
to git.

### 4 — Rewatches

`table` over `watch_states.play_count > 1`, labelled from `titles` for
title-scoped rows and from `episodes` joined to its series for episode-scoped
ones.

```sql
WITH episode_labels AS (
    SELECT episodes.id AS episode_id,
           titles.name || ' — S' || episodes.season_number || 'E' || episodes.episode_number AS label
    FROM episodes
    JOIN titles ON titles.id = episodes.title_id
)
SELECT coalesce(titles.name, episode_labels.label) AS item,
       titles.year AS year,
       watch_states.play_count AS plays,
       watch_states.last_played_at AS last_played
FROM watch_states
LEFT JOIN titles ON titles.id = watch_states.title_id
LEFT JOIN episode_labels ON episode_labels.episode_id = watch_states.episode_id
WHERE watch_states.play_count > 1
ORDER BY watch_states.play_count DESC, watch_states.last_played_at DESC NULLS LAST
LIMIT 50
```

**Returned** 50 rows in 7.5 ms — the `LIMIT` binds, because **80 rows** carry
`play_count > 1`. Led by Backrooms (2026) at 13 plays, Obsession (2025) at 11,
The Twilight Saga: Eclipse (2010) at 10, then Interstellar, The Housemaid and
Dhurandhar at 9. 78 of the 80 are title-scoped and 2 are episodes, which render
through the `episode_labels` CTE as `Silo — S3E7` and `Silo — S3E5`; their
`year` column is null, because a `Title`'s year is not an episode's.

**`play_count` is trustworthy here and `last_played_at` is not.** The 80 rows
account for 288 of the household's 347 plays, and every one of them has had at
least one earlier date overwritten — which is the same fact that makes "watch
time by day and user" unbackable, seen from the one angle where it is harmless.

### 5 — Longest unwatched

`table` over `media_items.added_at` for owned titles nobody has played, oldest
first, one row per title.

```sql
SELECT titles.name AS title,
       titles.year AS year,
       min(media_items.added_at) AS added_at,
       (now()::date - min(media_items.added_at)::date) AS days_in_library
FROM media_items
JOIN titles ON titles.id = media_items.title_id
WHERE media_items.available
  AND media_items.added_at IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM watch_states
      WHERE watch_states.title_id = media_items.title_id
        AND (watch_states.played OR watch_states.play_count > 0)
  )
GROUP BY titles.id, titles.name, titles.year
ORDER BY min(media_items.added_at)
LIMIT 50
```

**Returned** 50 rows in 71.4 ms (144.3 ms cold) — the `LIMIT` binds hard, because
**11,380 of the household's 11,516 distinct owned titles** qualify. Oldest:
Wicked City (1987), added 2019-02-12, **2,765 days** in the library; then Cyber
City Oedo 808, Project A-Ko and Overlord at 2,500 days each, all added
2019-11-04.

🔴 **The join is not the one PRD 10 originally specified, and the difference is
166-fold.** *"`media_items.added_at` with no `watch_states` row"* is the wrong
predicate against a walk-fed deployment: re-measured here, it returns **110** of
the 18,443 owned matched items, because the walk writes a row for nearly
everything. The predicate that answers the panel's own English — *never
played*, i.e. no row **or** a row with `play_count = 0 AND NOT played`, which is
the `NOT EXISTS` above — returns **18,279**. D0's audit corrected this in PRD 10
and the panel ships the corrected form; the count differs from the 11,380 above
only because that one is per *title* and this one is per *item*.

`added_at IS NOT NULL` is written out for the same structural reason Dashboard
1's growth panel writes it: 0 of 23,943 available rows are null today, and the
next source to report no `DateCreated` would silently drop titles out of a
want-list rather than raise anything.

### 6 — Three panels PRD 10 specifies that this dashboard does not ship

`text`, markdown, no datasource and no target — the one panel on either
dashboard exempted from invariant 1's target rule, and the reason the exemption
carries a non-empty-`content` assertion instead.

It carries **three sentences quoted from PRD 10's Dashboard 2 paragraph, byte
for byte**, one per absence, each naming the panel, its reason and its issue
number: watch time by day and user (#84), taste drift as genre affinity over
months (#84), and row effectiveness per `RowProvider` (#85). Duplication was
chosen over a link deliberately, on PRD 10's own rule for label vocabularies —
the reader of the artefact must not have to hold the document — and the cost of
that choice is
`test_the_absence_panels_three_sentences_are_byte_identical_to_prd_tens`, which
goes red the day either copy is corrected without the other.

**Rendered** and read back off the panel: all three bullets present, in the
PRD's order, with `#84`, `#84` and `#85` legible and both issue URLs live. The
`⚠️` and the `⏳` survive the round trip through JSON and Grafana's markdown
renderer.

## Two things a reader of this file should not assume

- **`~/code/observability/.env` had `USHER_POSTGRES_DB=usher` on 2026-09-07**,
  a database that no longer exists — the catalog is `usher_catalog`. The
  running Grafana held the correct value only in its process environment, so
  the first `docker compose up -d` after that rename would have taken every
  Postgres panel on every Usher dashboard down with `SQLSTATE 3D000`. Observed
  directly while provisioning this dashboard, and repaired in that file.
  `compose.yml`'s own `${USHER_POSTGRES_DB:-usher}` default is still stale and
  is not this repository's to fix.
- **The wall times above include Grafana's proxy**, so they are upper bounds on
  the database's work and not measurements of it. Where the distinction
  mattered — panel 11 — the `EXPLAIN (ANALYZE)` numbers are given separately.

---

# Dashboard 3 — Pipeline

Ten panels, and the mixed one: Prometheus for rates and depths, Postgres for
the queue's and the reconciler's own breakdowns. Dashboard 1's principle
("the catalog *is* the record") does not decide this dashboard on its own,
because half of what a pipeline does leaves no row behind — a completed job is
**deleted**, and a request that TMDb refused exists only as a counter.

## The three panels whose series is not what the title implies

Two of these are the deliverable and the third was found while measuring.

**1. "Queue depth by priority" is Postgres, and the gauge beside it is a
different panel.** PRD 10 says twice that `usher.jobs.queued` is labelled
`kind`: *"`JobQueue.depth()` counts pending rows per kind, which is what 'which
lane is backed up' asks"*, and then *"M5 introduces demand promotion and the
label stays `kind`: a priority band needs a second `GROUP BY` on `JobQueue`…
The panel reads `jobs` directly."* Verified at this HEAD rather than inherited:
`telemetry.py`'s `_observations` emits `Observation(count, {"kind": kind})` and
nothing else, and `QueueSnapshot` holds two `Mapping[str, int]`s keyed by
`JobKind.value`. **So the pair is two panels, two datasources, two titles.** A
single panel titled *"by priority"* over that gauge renders perfectly and
answers a different question, which is the failure
`test_the_queue_depth_panels_are_two_panels_over_two_datasources` closes.

**2. "Push connection uptime" plots delivery, and its `source` label is an
operator-typed name.** `PushSnapshot.delivering`, not `connected`, because
*"a gauge fed by the socket's state would read 1 for the failure ADR-0004
measured"*. The half nobody had written down is the label: `api/lanes.py`'s
`push_snapshots` builds the reader as
`{self._names[source_id]: PushSnapshot(...)}` over `self._open_adapters`, so
the label is `"Shared Emby"` and never a UUID — **observed as exactly that
string on the live series**, not read off the source. And a source with no
open adapter is simply absent from that comprehension, while
`telemetry.py`'s `_push_observations` returns `[]` with no reader at all:
**a disabled source, or one that does not support push, produces no
observation rather than a zero.** That is the sentence D11's *"Push down"*
alert is written against, and it decides the condition:
`usher_source_push_connected_ratio == 0` for 15 m, never `absent(...)`, which
would page about every source nobody configured.

**3. `usher.jobs.parked` saturates at 1,000, and this was not in the task.**
`composition.py`'s `QueueGauges.refresh` builds the parked counts from
`await queue.parked(limit=1000)` and tallies the returned rows in Python
rather than asking the database for a count, so the gauge is a **floor** on
the parked population. Measured 2026-09-07: the gauge read `enrich` 890 +
`index` 110 = **exactly 1,000**, while

```sql
SELECT kind, count(*) FROM jobs WHERE status = 'parked' GROUP BY kind
```

returned `enrich` 1,891 + `index` 110 = **2,001**. The 1,000 the gauge sees are
the 1,000 most recently updated (`_PARKED` orders `updated_at DESC, id DESC`),
which is why the `index` half is exact and the `enrich` half is not. The panel
keeps PRD 10's series and its own title and **says so in its description**
rather than being renamed to match a wrong number; the repair is a
`parked_depth()` on the queue port beside `depth()`, which is a change to
`ports/`, `db/` and `composition.py` and not to this directory.

## Two more things measured here that a panel author needs

**`$__rate_interval` empties every rate panel on this stack.** Grafana derives
it from the datasource's configured scrape interval, and a Prometheus fed by
remote write from an OTel collector has none — so it resolved to `1m15s`
against a 60 s push interval, which is one sample, and `rate()` over one sample
is empty. Observed directly: `executedQueryString` read
`rate(usher_enrichment_latency_seconds_count[1m15s])` and the response carried
zero frames. **Every rate window on this dashboard is the literal `[5m]`**, so
the file does not depend on an operator having set `timeInterval`.

**The exporter puts the unit in the name.** Every gauge Usher registers carries
`unit="1"` and every histogram `unit="s"`, and the OTel Prometheus exporter
appends that unit ahead of the aggregation suffix. So the name a panel must be
written in is `usher_jobs_queued_ratio`, not `usher_jobs_queued`, and
`usher_enrichment_latency_seconds_bucket`, not
`usher_enrichment_latency_bucket`. D6's normaliser stripped only
`_bucket`/`_count`/`_sum`/`_total` and would have graded **every** Prometheus
target on this dashboard as a metric PRD 10 does not document; it could not
have found this, because dashboard 1 has no Prometheus panel and D6's synthetic
control was written in a spelling the exporter does not produce.
`normalise_metric` now strips one unit as well, and
`test_the_prometheus_normaliser_strips_the_exporters_unit_suffix` pins twelve
names read off `/api/v1/label/__name__/values` on the running stack.

## The observations

Recorded **2026-09-07** through the running Grafana 13.1.3 at
`127.0.0.1:3000` (served from the sub-path `/grafana/`, so a panel's own URL is
`/grafana/d/usher-pipeline/…?viewPanel=<id>`), against Prometheus
`prom/prometheus:v3.13.2` and the live catalog `usher_catalog` (Alembic
`m10b`). Each panel was opened alone at `?viewPanel=<id>&kiosk` in a headless
Chrome 151 over CDP and screenshotted, and **every one of its committed targets
was also issued through `/api/ds/query`**, which is the same path the panel
takes; the wall times below are that round trip and include Grafana's proxy.

⚠️ **The bind mounts `provisioning/dashboards.yml` describes are not
present on this host's Grafana.** `/etc/grafana/provisioning/dashboards` does
not exist in the container and `/var/lib/grafana/dashboards/usher` is an empty
directory inside the `grafana-data` volume rather than this repository. So the
observation loaded the committed JSON through `POST /api/dashboards/db`
instead. That is a gap in `~/code/observability/compose.yml`, not in this file,
and it is recorded because a reader of the provisioning YAML would otherwise
assume the mechanism is wired.

### How the traffic was generated, and what was *not* run

Three panels needed traffic. **No app was started for any of it.** The
operator's own deployment (`usher-usher-1`, running since 2026-09-02 with
`USHER_PUSH_ENABLED=true` and `USHER_WORKER_ENABLED=true`) is already exporting
every series on this dashboard, so the observation is a read of a running
system plus HTTP requests to its public API:

- **The job, enrichment and TMDb panels**: 130 `GET /titles/{id}` requests
  against skeleton titles carrying a `tmdb_id`, which is M5's read-through
  path — `services/titles.py` promotes the title it answers to
  `JobPriority.DEMAND`. The deployment's own worker then drained the queue.
  Cost: **+129 enrichments and +130 TMDb requests**, all of them work the
  catalog wanted.
- **The Emby latency panel**: four `GET /admin/sources/{id}/status` calls,
  which is one `verify()` each — `authenticate`, `verify`, `verify_policy`,
  `verify_public` and nothing that walks.
- 🔴 **The push panels needed no run at all, and that is the finding.**
  `.claude/rules/api-telemetry-and-lanes.md` warns that starting the shipped
  app against a real source is itself an unbounded walk, because the push
  lane's reconnect gap-closer calls `reconcile(source, DELTA, adapter)`. The
  task specified a bounded scratch source for this reason. **It was not
  needed**: the running deployment publishes both push series continuously, so
  the observation is a read and **no lane was pointed at the operator's Emby
  library by this task.** No scratch source was created, and no `usher sync`,
  `usher work` or `usher push` was run.

⚠️ **One live fact bounds the queue panels, and it is why the depth had to be
generated rather than waited for.** Every owned skeleton title in this catalog
already carries a *parked* `enrich` job, and `_ENQUEUE`'s
`WHERE jobs.status <> 'parked'` refuses to revive one — *"a title TMDb has
permanently refused is not revived by being scrolled past"*. Measured: of 40
owned skeleton titles opened, **0** produced a pending job. The 130 that
worked were skeletons with no `jobs` row at all.

### 1 — Queue depth by priority band

`table`, Postgres, over `jobs.priority` and `jobs.status`.

```sql
SELECT coalesce(band.name, 'off-rung ' || pending.priority::text) AS priority_band,
       coalesce(band.priority, pending.priority) AS priority,
       coalesce(pending.pending, 0) AS pending
FROM (VALUES ('DEMAND', 100), ('VISIBLE', 80), ('NEW', 50), ('BACKFILL', 20))
         AS band(name, priority)
FULL OUTER JOIN (
    SELECT jobs.priority AS priority, count(*) AS pending
    FROM jobs
    WHERE jobs.status = 'pending'
    GROUP BY jobs.priority
) AS pending ON pending.priority = band.priority
ORDER BY coalesce(band.priority, pending.priority) DESC
```

**Returned** 4 rows in 11.6 ms. Observed twice, deliberately:

| when | DEMAND 100 | VISIBLE 80 | NEW 50 | BACKFILL 20 |
|---|---|---|---|---|
| 19:38:43, immediately after 60 read-throughs | **61** | 0 | 0 | 0 |
| 19:54:06, after the worker drained | 0 | 0 | 0 | 0 |

**The four-rung spine is the panel's whole structural claim.** A bare
`GROUP BY jobs.priority` returns *nothing* for an empty queue, and an empty
table is indistinguishable from a panel whose datasource is misconfigured — the
failure `tests/unit/test_dashboards.py` exists for. With the spine, the second
row of the table above is legible as "the queue is empty" at a glance.
`jobs.priority` is a free `integer` with a `CHECK (priority >= 0 AND
priority <= 100)` and promotion moves it with `GREATEST`, so the `FULL OUTER
JOIN` and the `'off-rung '` label are what keep a value off the four rungs
visible instead of silently dropped. **No off-rung value exists today**: the
whole `jobs` table holds 20, 50 and 80 only.

### 2 — Queue depth by kind

`timeseries`, Prometheus.

```
sum by (kind) (usher_jobs_queued_ratio)
```

**Returned** 9 frames × 361 points in 7.6 ms — one per `JobKind`, because
`PostgresJobQueue.depth()` fills `dict.fromkeys(JobKind, 0)` before applying
the `GROUP BY`, so an empty kind reports 0 rather than dropping its series.
Over the six-hour window only `enrich` was ever non-zero (5 points, max 1);
the 61-deep moment above is **not** in this series, and that is worth
understanding rather than explaining away: the reader is a snapshot the worker
refreshes *after* each pass, and the drain finished between two refreshes.
The gauge is stale and never wrong, which is exactly what
`register_queue_gauges`' docstring claims for it — and the reason the panel
beside it reads the table.

### 3 — Parked jobs by kind

`timeseries`, Prometheus.

```
sum by (kind) (usher_jobs_parked_ratio)
```

**Returned** 9 frames × 361 points in 9.9 ms, flat for the whole window:
`enrich` **890**, `index` **110**, every other kind 0. Against the table at the
same moment: `enrich` **1,891**, `index` **110**. See "the three panels" above
— this series saturates at 1,000 and the panel says so.

### 4 — Enrichment throughput and p50/p99

`timeseries`, Prometheus, three targets, all split on `outcome`.

```
sum by (outcome) (rate(usher_enrichment_latency_seconds_count[5m]))
histogram_quantile(0.5,  sum by (outcome, le) (rate(usher_enrichment_latency_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (outcome, le) (rate(usher_enrichment_latency_seconds_bucket[5m])))
```

**Returned** 2 frames per target in 5.4 / 8.7 / 8.0 ms. Peak throughput
**0.417 enrichments/s** for `enriched` against **0.0167/s** for `failed`;
lifetime counts moved from 122/77 to **251 enriched / 82 failed** over the
observation. **p50 2.50 s and p99 4.95 s — identical for both outcomes**, and
that is a real limit of the instrument rather than a coincidence: at this
histogram's bucket widths a success and a failure land in the same two buckets,
so the panel separates the two populations' *rates* far better than their
*latencies*.

⚠️ **The label is `outcome` and there is no demand/background split on this
series.** `enrich.py`: *"Labelled `outcome` rather than PRD 10's original
`trigger`: nothing in M4 enriches on demand… while a failure's latency and a
success's are genuinely different populations."* M5 shipped demand promotion
and the label did not move — every sample above came from a `JobPriority.DEMAND`
read-through, and the series still says only `enriched` or `failed`. Splitting
this panel the way D12 wants needs a second label on the histogram, not a
different query.

### 5 — Promotion latency against the 5 s read-through target

`table`, Postgres, over `jobs` at `VISIBLE` or above, with a red threshold at
5 s.

```sql
SELECT jobs.kind AS kind,
       jobs.priority AS priority,
       jobs.status AS status,
       round(extract(epoch FROM (jobs.updated_at - jobs.created_at))::numeric, 3)
           AS seconds_since_promotion,
       jobs.key AS title_id,
       jobs.traceparent AS traceparent
FROM jobs
WHERE jobs.priority >= 80
ORDER BY jobs.updated_at DESC
LIMIT 50
```

**Returned** 24 rows in 5.5 ms. Twenty-three of them are the M5 `VISIBLE`
promotions this deployment already had, and **every one of them reached its
terminal state inside the target — 2.483 s to 4.959 s, none over 5 s — and
every one of them terminated as `parked`.** Speed is not success, and this
panel is the one place that is visible.

The twenty-fourth row is one this observation produced: a `DEMAND` (100)
promotion that retried four times against *"title … conflicts with an existing
title"* and parked at **286.099 s**, rendered red. That is the panel doing its
job.

⚠️ **Its population is promotions that did not complete.** A completed job's
row is `DELETE`d — `JobStatus` has no `DONE` member, for the reason
`domain/jobs.py` gives — so the 129 read-throughs that *succeeded* during this
observation left nothing here at all. Read it as the miss list; it is a floor
on the misses and never a distribution over all promotions. The `traceparent`
column is what `current_traceparent()` put on the row at enqueue, and it is
what makes the panel a join to the requesting span rather than an estimate.

### 6 — Sync run outcomes and duration

`table`, Postgres, over `sync_runs`.

```sql
SELECT sync_runs.kind AS kind,
       sync_runs.status AS status,
       count(*) AS runs,
       sum(sync_runs.items_seen) AS items_seen,
       sum(sync_runs.items_unmatched) AS items_unmatched,
       round(avg(extract(epoch FROM
           (sync_runs.finished_at - sync_runs.started_at)))::numeric, 1)
           AS avg_seconds,
       round(max(extract(epoch FROM
           (sync_runs.finished_at - sync_runs.started_at)))::numeric, 1)
           AS max_seconds
FROM sync_runs
GROUP BY sync_runs.kind, sync_runs.status
ORDER BY sync_runs.kind, sync_runs.status
```

**Returned** 7 rows in 4.7 ms, over the 119 rows `sync_runs` holds:

| kind | status | runs | items_seen | items_unmatched | avg_seconds | max_seconds |
|---|---|---|---|---|---|---|
| delta | completed | 66 | 29,914 | 5,846 | 3.9 | 66.2 |
| delta | failed | 1 | 0 | 0 | 0.1 | 0.1 |
| full | completed | 1 | 60 | 0 | 9.6 | 9.6 |
| full | failed | 1 | 120 | 0 | 19.3 | 19.3 |
| watch_state | completed | 27 | 1,194,855 | 1,168,679 | 17,855.6 | 481,988.4 |
| watch_state | failed | 20 | 3,231,000 | 3,223,036 | 5,310.6 | 30,342.2 |
| watch_state | running | 3 | 615,000 | 614,760 | *null* | *null* |

**The outcome breakdown is Postgres and not the histogram, and that is a
measurement rather than a preference.** `usher.sync.run.duration` does carry a
`status` label, but the histogram holds only what the *current* process
recorded: on the day it was one series, `kind="delta" status="completed"`,
count 15. Every failure above predates that process. **Three runs are still
`running`** — started 2026-08-19 and never finished — so their `finished_at` is
NULL and their duration columns are NULL rather than zero; an `avg` that
coalesced them to 0 would have reported those three as instant.

**No `$__timeFilter`, deliberately.** The table is 119 rows — this
deployment's entire recorded history — and any window short enough to be an
operational default hides exactly the failed and never-finished runs the panel
exists to show. The dashboard's time picker therefore does not move this panel,
which the description says out loud.

### 7 — Emby request latency by op

`timeseries`, Prometheus, two targets.

```
histogram_quantile(0.95, sum by (op, le) (rate(usher_source_request_duration_seconds_bucket[5m])))
sum by (op) (usher_source_request_duration_seconds_sum) / sum by (op) (usher_source_request_duration_seconds_count)
```

**Returned** 7 frames per target in 12.4 / 6.7 ms. Lifetime means, which are
the readable number here: `get_watch_state` **0.255 s**, `get_item`
**0.423 s**, `list` **1.080 s**, `authenticate` **1.339 s** (down from 3.528 s
as the four probes below diluted it), `verify_policy` **0.144 s**, `verify`
**0.145 s**, `verify_public` **0.478 s**. The p95 target was non-empty only
where the window held requests — 10 points for `authenticate`, 5 each for the
three `verify_*` ops, 4 for `get_item`, **0 for `list` and
`get_watch_state`**.

**Two targets on purpose, and that asymmetry is the reason.** This deployment
talks to Emby only while a sync, a watch-state pass or a push lane is running,
so an idle window is the ordinary case rather than an outage — a panel that
carried only the p95 would be blank most of the time and a reader could not
tell that from a broken datasource. The lifetime mean is always present and is
what the axis is scaled by.

### 8 — Push connection uptime

`timeseries`, Prometheus, two targets, left axis soft-capped at 1.

```
max by (source) (usher_source_push_connected_ratio)
max by (source) (usher_source_push_reconnects_total)
```

**Returned** 1 frame each in 4.6 / 5.5 ms: `Shared Emby · delivering` **0** for
every one of 361 points, `Shared Emby · reconnects (cumulative)` **11**.

**The label rendered in the legend is `Shared Emby`** — the operator-typed
source name, from `self._names[source_id]`, exactly as
`api/lanes.py::push_snapshots` builds it. There is **one** series and the
`sources` table holds **one** row, so this observation does not on its own
demonstrate the absence half; what does is the code path
(`_push_observations` returns `[]` with no reader, and the comprehension omits
any source with no open adapter) plus the live shape below.

🔴 **A second fact, observed rather than looked for: this series should not
exist right now.** `GET /health/ready` on the same process reports
`"lanes": {"push": [], "worker": true}` — **no running push lane** — while the
gauge has gone on publishing `0` every 60 s for the whole window. That is the
defect M10's S10 fixed: before it, a lane that hit `PushSupervisor.run`'s
failure ceiling had its task complete and nothing popped it, so the
`SourceAdapter` stayed in `self._open_adapters` and `push_snapshots()` kept
reading it. The running image was built **2026-08-26** and predates the repair.
On this HEAD the same state would produce *no series* — which is the honest
behaviour, and the reason D11's alert is
`usher_source_push_connected_ratio == 0` and never `absent(...)`: a source with
no lane is invisible here, so an `absent()` alert would fire for every source
nobody configured and stay silent for the one whose lane died holding a socket.

### 9 — Push events applied, by kind

`timeseries`, Prometheus.

```
sum by (kind) (usher_source_push_events_total)
```

**Returned** 4 frames × 361 points in 5.4 ms, flat for the whole window:
`item_added` **94**, `watch_state_changed` **74**, `item_updated` **70**,
`item_removed` **17**.

**The cumulative counter is plotted rather than its rate**, which is what makes
this panel answer PRD 10's question — *"separates 'the lane is up' from 'the
lane is doing anything'"* — in the state the deployment is actually in. Four
flat lines say "this lane has delivered 255 events in its life and none of them
recently"; a `rate()` would say `0` and look identical to a source that has
never delivered anything at all. The `source` label is the same operator-typed
name panel 8 carries, and a source with no open lane produces no series here
either.

### 10 — TMDb requests/sec against the ceiling, with 429 count

`timeseries`, Prometheus, three targets.

```
sum(rate(usher_provider_requests_total{provider="tmdb"}[5m]))
sum(usher_provider_requests_total{provider="tmdb", status="429"}) or vector(0)
vector(30)
```

**Returned** 1 frame each in 5.1 / 5.5 / 5.2 ms. Peak **0.417 req/s** against
a ceiling of **30**, so 1.4% of the budget at the busiest minute this
observation produced; **429 count 0**; lifetime totals `status="200"` **255**
and `status="404"` **3**, with no `status="error"` row — this deployment has
not had a TMDb transport failure.

⚠️ **PRD 10's "~40 ceiling" is not a figure this deployment has at any
spelling.** `Settings.tmdb_requests_per_second` defaults to **30.0**,
`.env.example` sets `USHER_TMDB_REQUESTS_PER_SECOND=30`, and the running
container sets the variable at all. The ceiling series is 30 and PRD 10's
sentence has been corrected in the same commit.

**Two spellings here are load-bearing.** The rate target selects on `provider`
only: `status` is the HTTP status code as a string
(`adapters/tmdb/client.py`, `status = str(response.status_code)`) **or the
literal `"error"`** for a transport failure that never reached a status line,
and both are recorded from a `finally` — *"a denominator that omitted the
failures would read low exactly during an outage."* And `or vector(0)` is what
makes "no 429s" render as a zero instead of an empty series, at the stated cost
that a metric which stopped existing entirely would read the same; the
requests/sec series beside it is what distinguishes those two.

**The ceiling is a named series and not a threshold line, and that was a
repair.** As a threshold Grafana drew it against the *right* axis's scale, so a
red line labelled nothing appeared at 0.14 req/s on a left axis that had
auto-scaled to 0.46 — a panel telling a reader the ceiling was a third of a
request per second. `vector(30)` carries its own legend entry, names the
setting it comes from, and pulls the axis to the ceiling so headroom is the
readable quantity.

## ⚠️ Two Dashboard 3 panels plot a mean, not a quantile, and the reason is an instrument

`Enrichment throughput and mean latency` and `Emby request mean latency by op`
were written as `histogram_quantile` panels and were changed at integration,
because `test_no_committed_panel_takes_a_quantile_over_a_histogram_still_on_the_sdk_defaults`
(D9's) caught them.

`usher.enrichment.latency` and `usher.source.request.duration` both still take
the OTel SDK's **default second-scale boundaries** — `configure_metrics`
installs no `View` and neither declares an
`explicit_bucket_boundaries_advisory`. D1 measured what that does: over a real
export, `histogram_quantile(0.5)` answered a flat **2.5000 s** against a
sample's true **35.20 ms**. A quantile over those buckets is not a loss of
resolution, it is a loss of the measurement, and it plots a plausible number
rather than failing.

`rate(_sum) / rate(_count)` is true whatever the boundaries are, so these two
plot a mean until the instruments carry a ladder. **A mean has no p99**, so the
enrichment panel's second quantile target was dropped rather than converted.

Fixing the instruments is the better repair and is not done here: D1's ladder
was derived from ADR-0002's and ADR-0031's measured distributions, and neither
of these two instruments has a measured distribution to derive one from. Issue
filed; the panels are honest in the meantime.

## 5 — Cost & Compliance (`05-cost-and-compliance.json`, uid `usher-cost-compliance`)

Eight panels: three compliance, three spend, one freshness, one disk.

🔴 **Panel 1–3 are a licence term, not a metric, and they are the panels that do
not get trimmed.** Every other panel here is a blind spot when it breaks; these
are TMDb's six-month cache ceiling. `tests/unit/test_no_third_party_data.py`
enforces the *redistribution* half of that licence mechanically; **nothing
enforces the retention half**, and these three plus their threshold are the
whole of it.

They read `raw_payloads.fetched_at` and deliberately not `titles.enriched_at`,
which records when Usher enriched a title and does not move when a cached
payload is re-read (ADR-0016). The ceiling is spelled `interval '6 months'` and
never `'180 days'` — the two are never equal, and 180 days matches *more* rows,
so it over-reports a breach that has not happened. `<` and never `<=`: TMDb's
term is *no more than* six months.

**Three statements rather than one**, because `count(*)` reads every row and
folding them together costs the other two their index — measured separately at
0.48 / 4.46 ms against one `Parallel Seq Scan` combined.

**Observed 2026-09-07 on the live catalog: 133,631 cached payloads, oldest
`fetched_at` 2026-08-11 21:40:59+00 against a ceiling of 2026-03-11, 0 past it,
share 0.** The obligation is being met. That was the first time anyone looked.

**Panels 4–5, spend.** Both return **zero rows on this deployment** — `llm_calls`
and `curated_rows` are genuinely empty — which is a real state and not a panel
fault. ⚠️ **A dashboard reading $0.00 is not evidence of a free deployment**:
`cost_usd` defaults to `0`, honest for a local model and *wrong* for a hosted
one an operator forgot to price. The mitigation is that the token counts are
exact, so spend is recomputable after the fact, which is why panel 4 plots
tokens beside money. Panel 5's join is one join wide and its index is partial
(`WHERE generation_id IS NOT NULL`), so which population it reads depends on
`USHER_QUERY_EXPANSION_ENABLED`.

**Panel 6, embedding compute — a mean, not a quantile.** Same reason as
Dashboard 3's two: `usher.embedding.duration` declares no bucket advisory, so a
quantile over the SDK defaults answers a flat plausible number (issue #86).
Series verified present: `usher_embedding_duration_seconds_count` = 141.

**Panel 8, disk.** 🔴 **Plotted against measured free disk, never against PRD
08's resource table**, which carries its own warning that nothing reads it and
no policy derives from it — and which M9's Track 2 treated as a budget, deriving
a 2.0 GB ceiling and **withdrawing a design** measured at 2.702 GB against a
number with no forcing function (ADR-0036). A number nothing enforces is not a
threshold. This panel is where D13's alert is drawn from, and **the window is the
same in both**: a 7-day least-squares fit extrapolated 14 days.

⚠️ **The free-disk denominator is not in this panel and cannot be.** The title
says *headroom against measured free disk* and the query returns
`pg_total_relation_size` per table — the numerator. There is no portable SQL for
a filesystem's free space, so the denominator is a Prometheus series,
`system_filesystem_usage_bytes{state="free"}`, and the pairing is the panel.
Measured 2026-09-11 on this deployment: `pg_database_size('usher_catalog')`
**8,384,394,931 B (7,996 MB)** at 1,276,268 titles with 133,576 enriched,
against **677,427,249,152 B free** on `/`. ⚠️ PRD 08's resource envelope records
**5,025,650,355 B (4,793 MB)** for the same database at `m09c` on 2026-08-12 —
**+3.36 GB in thirty days against +3,901 titles**, so the growth followed
`m09d`, `m09e` and a full re-embed rather than the catalog. That row is dated
and denominated and this task did not rewrite it; it is flagged for whoever
owns it.

⚠️ **It reads `pg_class` and so names no Usher table**, which means invariant 3
has nothing of ours to grade. That is the one exemption in
`test_the_committed_dashboards_are_not_written_in_aliases`, and it is asserted
**by name and by count** rather than merely granted — otherwise the exemption
becomes how every later panel escapes the check.

# Alerts — `alerts/usher.yml` and `alerts/grafana/usher.yml`

PRD 10's `## Alerts` table names seven rules and opens *"Kept few, so they mean
something."* **All seven are here, in two files and two engines, as eight
rules.** Six are Prometheus rules in `alerts/usher.yml` — **Ingest stalled**,
**Jobs parking** and **Push down** from D11, **Enrichment SLA missed** and
**Provider degraded** from D12, and **Disk projection**'s two halves from D13.
Two are Grafana-managed Postgres rules in `alerts/grafana/usher.yml` — **Cost
anomaly**, which has no series to name at all, and *Disk projection*'s
**database-growth** half, which has an exact query and no metric. Nothing is
owed any more;
`tests/unit/test_alerts.py` held that debt as an `xfail(strict=True)` naming
which task owed which, and **D13 removed the marker** rather than leaving it to
XPASS-strict — which is the failure a strict xfail is for, and the mechanism
that made the last task come back and close the ledger.

⚠️ **D14 was told to be that day and was not.** Its acceptance reads *"D11's
bidirectional name check is now green, seven rules against PRD 10's seven
rows"*, which assumed it went last. Measured at the milestone HEAD D14 rebased
onto — `97d851a`, 2026-09-11 — D12 had landed its two and `m10/D13` still had
nothing committed on it. So the marker stays with *Cost anomaly* struck from
the ledger it names, and it is D13's to remove.

**Nothing in this repository evaluates these rules.** The same asymmetry the
dashboards have, one step further: the shared Prometheus in
`~/code/observability/` has **no `rule_files:` entry and no mount for this
directory**, and its Grafana has no alerting provisioning, so committing either
file arms nothing. Each file's own header spells out the stanzas that would.
Every firing below was produced by a throwaway engine against real data — a
throwaway Prometheus reading the shared one over `remote_read` for the five,
and a throwaway Grafana over a throwaway Postgres for *Cost anomaly* — real
data, real rule engine, shared stack untouched.

## 🔴 Every metric name here carries a segment the OTel name does not

An alert naming a metric nobody stores does not fail and does not draw an empty
rectangle. It evaluates to an empty vector, which is **what healthy looks
like** — loaded, green and permanently silent. That is strictly worse than D8's
version of the same bug, where the cost was a blank panel somebody eventually
looks at.

The collector appends the instrument's *unit* before the aggregation suffix.
Read off this host's Prometheus on **2026-09-11**
(`/api/v1/label/__name__/values`, 76 `usher_`/`http_` names):

| PRD 10 declares | the collector stores | why |
|---|---|---|
| `usher.jobs.queued` | `usher_jobs_queued_ratio` | gauge, `unit="1"` |
| `usher.jobs.parked` | `usher_jobs_parked_ratio` | gauge, `unit="1"` |
| `usher.source.push.connected` | `usher_source_push_connected_ratio` | gauge, `unit="1"` |
| `usher.jobs.duration` | `usher_jobs_duration_seconds_count` | histogram, `unit="s"` |

**`unit="1"` becomes `_ratio` on a gauge and on nothing else.** On a counter it
is dropped in favour of `_total` (`usher.source.push.reconnects` →
`usher_source_push_reconnects_total`); on a histogram it is dropped entirely
(`usher.search.results` → `usher_search_results_bucket`). Nothing about an
instrument's name says which of the three it is, so
`test_every_rule_is_written_in_the_spelling_prometheus_stores` derives the
stored spelling from the `create_*` call itself, and
`test_the_stored_spelling_derivation_matches_this_hosts_prometheus` pins that
derivation against the names above.

⚠️ **The catalogue check cannot see this failure.** `usher_jobs_queued`
normalises into PRD 10's metric table perfectly. Measured by planting exactly
that spelling into the committed rule: the catalogue case stayed **green** and
only the spelling case went red.

⚠️ **The declaration walk is an AST walk over a docstring-stripped tree, not a
text scan.** `telemetry.py` spells `create_observable_gauge("usher.jobs.queued",
callbacks=[other])` twice in prose while arguing about the SDK discarding a
second registration. A `str.find` scan reports **43** declarations against the
real **41**, and one of the two phantoms supplies the name with no `unit=` — so
it would derive `usher_jobs_queued` as the stored spelling and the check meant
to catch that spelling would be the thing that accepted it.

## 🔴 `unless`, never `and … == 0` — the zero-versus-absence rule, and it cuts both ways

*Ingest stalled* is *"queue depth rising for 30 min with zero completions"*, and
its two halves have **opposite shapes**:

- **Depth is always nine series.** `PostgresJobQueue.depth` fills
  `dict.fromkeys(JobKind, 0)` before returning, with its own comment giving the
  reason: *"A GROUP BY returns only non-empty kinds, and a gauge that stops
  reporting a series is indistinguishable from one reporting zero."*
- **Completions are only the kinds that have settled something.**
  `usher.jobs.duration` is *recorded*, so a lane that has never run a job has no
  `kind` of its own on that side at all.

`and` is a set intersection, so it keeps only the kinds present on **both**
sides — dropping exactly the lanes that have never settled a job, which is not an
edge of "ingest stalled" but the worst case of it. Measured 2026-09-11 against
this host's Prometheus:

| spelling | `kind`s it can reach on the live deployment |
|---|---|
| `and … == 0` | 5 — derive, enrich, index, match, watch_history |
| `unless … > 0` | 9 — those, plus **bootstrap, curate, sync, watch_writeback** |

**On a fresh deployment the same comparison inverts the alert.** With 25
`curate` jobs queued against a running worker that cannot claim them
(`USHER_LLM_ENABLED=false` leaves `CURATE` out of `composition.worker_kinds` —
M4's *"a queue that grows forever"*), the two full expressions answered:

```
committed  (unless … > 0):  {kind="curate"} = 27.84     <- the stalled lane
task text  (and … == 0):    no data                     <- silent
```

The `and` spelling was not merely late; on that deployment it reported
**nothing at all**, because the only lane that was stalled was the only lane
with no completions series.

**Push down takes the same distinction and decides it the other way**, which is
why they are worth reading together. There a reported **0** is the incident and
an **absent** series is not: `api/lanes.py`'s `push_snapshots()` iterates
`self._open_adapters` and a lane only opens for an *enabled* source, so no
series means nobody is running a lane for that source — a configuration state an
operator chose. PRD 10's qualifier *"on a source that supports it"* therefore
needs no join against `sources.supports_push`; the series is already scoped to
those sources by construction, and an `absent()` arm would page somebody for
having parked a server that is being rebuilt.

## 🔴 Two windows of thirty minutes is sixty, and the rule fires on neither

The depth half was written `increase(usher_jobs_queued_ratio[30m]) > 0` with
`for: 30m` — PRD 10's duration in both places, each correct read alone. **That
pair is unsatisfiable for a queue that rises once and then stops moving**, which
is the commonest stall there is: the rise sits inside a `[30m]` range vector for
exactly thirty minutes and then leaves it, so the condition is true for at most
as long as `for:` demands, and fires on a knife edge one evaluation wide.

Watched live on 2026-09-11 against 25 `curate` jobs and a worker that could not
claim them, depth flat at 25 the whole time, sampled every two minutes:

```
increase(depth[30m])       42.5 30.8 28.5 27.5 … 25.7 25.6 25.6 -> 0.0
min_over_time(depth[30m])     0    0    0    0 …    0    0    0 ->  25
```

The alert went `pending` at **17:09:58Z** and back to `inactive` at
**17:39:34Z** — one evaluation before its own `for: 30m` would have fired it. It
was not a slow alert; it was a silent one.

`min_over_time(…) > 0` reads the same thirty minutes the other way — *"this lane
has had work waiting at every sample for thirty minutes"* — and becomes true at
exactly the moment the last empty sample leaves the window, which is PRD 10's
*"for 30 min"* said properly. The patience lives in the range vector; `for:` is
then a blip guard and nothing more.

`test_no_decaying_window_is_as_long_as_the_for_that_waits_on_it` is the guard,
and it is **for D12–D14 more than for D11**: *Provider degraded* is a `rate()`
over a window and *Cost anomaly* a daily comparison, and both invite the same
shape, because the PRD sentence names one duration and there are two places to
put it. It exempts `min_over_time` and the instant selectors by name — their
answer *persists*, so a long `for:` under them is patience rather than a knife
edge.

⚠️ **The bare `usher_jobs_queued_ratio > 0` conjunct is a freshness guard, not a
redundancy.** `min_over_time` answers from whatever samples are in the window,
so a process that exported a backlog and then died keeps satisfying it for half
an hour. An instant vector selector needs a sample inside Prometheus's 5-minute
staleness window, which is what makes the rule say "there is a queue **now**".
Pinned by the promtool case below: a series that stops at t=40m pages at neither
50m nor 70m. Paging *"curate is stalled"* at a process that is gone is a wrong
page, and PRD 10 has no exporter-down alert to catch it.

## `increase()` over a gauge is right for *Jobs parking*, and `delta()` is not

Prometheus 3.13.2 emits an info-level warning on that rule — *"metric might not
be a counter, name does not end in `_total`/`_sum`/`_count`/`_bucket`"* — and the
documentation points a gauge at `delta()`. So promtool was asked what each
actually computes rather than the warning being obeyed (`promtool test rules`,
1 min interval, `[15m]`, 2026-09-11):

| the gauge over the window | `increase()` | `delta()` |
|---|---|---|
| `0 → 100 → 0` — filled, then a worker drained it | **0** | −107.14 |
| `0` rising by 10 to `140` — a lane backing up | **150** | — |
| `890 → 0 → 1` — an operator cleared the backlog, then one new job parked | **1.071** | −952.50 |

`increase()` answers the question in all three: a drained queue is **0**, so no
false page; a rise is the rise; and a cleared-then-reparked backlog still reports
the one new park, because the reset correction credits the drop rather than
carrying it. `delta()` is wrong in all three. The warning is about the name, not
about this arithmetic.

## ⚠️ The parked gauge saturates at 1,000, so the live deployment cannot fire *Jobs parking*

`composition.QueueGauges.refresh` builds the parked counts from
`await queue.parked(limit=1000)` and tallies the returned rows in Python, so the
gauge is a **floor** on the parked population, never a count of it. D8 measured
the cap from the panel side; measured again from the alert side on 2026-09-11:

| | |
|---|---|
| `sum(usher_jobs_parked_ratio)` on the live instance | **1000** |
| `SELECT kind, count(*) FROM jobs WHERE status='parked'` on `usher_catalog` | **enrich 1891 + index 110 = 2001** |

D8 read **1,890 + 110 = 2,000** from the same table on 2026-09-07. **One more
enrich job has parked since and the gauge has not moved by one**, so on that
instance this alert has already missed a real park and will miss every future one
until the parked population drops back under the cap. The cap is in
`composition.py` and not in the rule, so the rule is written for the series that
exists and the gap is recorded here rather than worked around.

## ⚠️ The queue gauges do not exist unless a worker lane is running

`register_queue_gauges` is called **inside** the worker lane — `api/lanes.py`'s
`_work_lane` and `cli.py`'s `work`, nowhere else — and `telemetry._observations`
returns `[]` with no reader, deliberately: *"a fabricated zero is the one value
that makes the alert quietly wrong."*

So `USHER_WORKER_ENABLED=false` publishes **no depth series at all** and *Ingest
stalled* cannot fire against such a process. D11's task text proposed firing it
by enqueueing against `USHER_WORKER_ENABLED=false` and waiting out the window;
that recipe cannot work. The recipe below replaces it with a **running** worker
that settles nothing because the lane's handler was never registered — the same
operator-visible condition, and the one the gauge can see.

## ⚠️ A silent push channel is given up after ~7.5 minutes, and *Push down* waits 15

Measured while firing this rule, and it narrows what the alert covers. Against a
stub that upgrades and delivers nothing — ADR-0004's own failure — the shipped
defaults give the lane up long before the alert's window elapses:

```
D11 Stub Emby's push channel failed (5/5): /embywebsocket delivered no message
in 90s (ceiling 90s); treating the channel as dead
D11 Stub Emby's push channel failed 5 times in a row; marking it unavailable and
leaving this source to the nightly reconcile
```

`push_max_consecutive_failures` (5) × `push_stale_after_seconds` (90 s) ≈ **7.5
minutes**, against `for: 15m`. The lane finishes, `LaneSupervisor.refresh`
releases its adapter (M10's S10), `push_snapshots()` stops reporting it, and the
alert correctly stops firing — **absence is not an incident, and here it means
Usher has already handed the source to the nightly walk.** Watched live: that
source's alert went from `pending` to gone without ever reaching `firing`.

So *Push down* covers the case where the channel **stays up and goes quiet** — a
source delivering intermittently, a proxy holding the socket open — and not the
case where the lane gives up. Both are correct; only the first is an incident
PRD 08 has no other answer for. The firing below uses
`USHER_PUSH_STALE_AFTER_SECONDS=1200` to hold a silent socket open past the
alert's own window, which **is** the condition rather than a way around the rule.

## 🔴 The seventh alert has no series, and its file must not be in the other file's glob

PRD 10's *Cost anomaly* is *"Daily LLM spend > 3x trailing 7-day median"*, and
there is nothing in Prometheus to write it against. That is a refusal with a
sentence behind it, not a gap: PRD 10's own first principle puts LLM spend on
Postgres — *"`llm_calls` is the record — so there is no `usher.llm.*` series at
all"* — and `telemetry.py`'s register records the mechanical half, that an OTel
observable callback runs on the metric reader's background thread while every
database call here is a coroutine on asyncpg. So the rule is SQL, evaluated by
Grafana against the same datasource Dashboard 5's spend panels use.

**It is in `alerts/grafana/`, one directory down, and that is load-bearing.**
`alerts/usher.yml`'s header tells an operator to mount `dashboards/alerts` at
`/etc/prometheus/rules` with `rule_files: [/etc/prometheus/rules/*.yml]`. That
glob is not recursive and Prometheus unmarshals rule files **strictly**, so a
Grafana provisioning file beside `usher.yml` is read as a rule file and rejected
— and the cost is not one ignored file. Measured 2026-09-11 on
`prom/prometheus:v3.13.2`, with the committed Grafana file copied to
`/rules/grafana.yml`:

```
promtool check rules /rules/*.yml     # committed layout
  Checking /rules/usher.yml
    SUCCESS: 7 rules found            # exit 0

promtool check rules /rules/*.yml     # Grafana file as a sibling
  Checking /rules/grafana.yml
    FAILED: yaml: unmarshal errors:
      field apiVersion not found in type rulefmt.RuleGroups
      field orgId      not found in type rulefmt.RuleGroup
      field folder     not found in type rulefmt.RuleGroup
      field uid        not found in type rulefmt.Rule
      field title      not found in type rulefmt.Rule
      field condition  not found in type rulefmt.Rule
      field noDataState not found in type rulefmt.Rule
      field execErrState not found in type rulefmt.Rule
      field data       not found in type rulefmt.Rule
  Checking /rules/usher.yml
    SUCCESS: 7 rules found            # exit 1
```

(The real output prefixes each line with a line number into `grafana.yml`;
they are elided here because a line number into a file this document does not
own is a citation that rots — #82's lesson, applied to a transcript.)

⚠️ **Re-run at D13's HEAD and it still holds, with two numbers moved.** D14
measured **5** rules because D13 had not landed; the committed layout now serves
**7**, and the sibling layout now reports **17** unmarshal errors rather than 9,
because the Grafana file has gained a second rule group with its own `orgId`,
`folder`, `uid`, `title`, `condition`, `noDataState`, `execErrState` and `data`.
The conclusion is unchanged and the numbers are re-derived rather than
inherited, which is the point of re-running it.

🔴 **And the server is worse than the linter.** A Prometheus started against the
committed layout serves all seven rules on `/api/v1/rules`; started against the
mixed directory it **exits 2 before opening a port**, with
`Error loading rule file patterns from config`. Not "six rules instead of
seven" and not "a warning in the log" — no Prometheus at all, so D11's three,
D12's two and D13's two go with it.
`test_the_postgres_rule_is_not_in_the_directory_prometheus_globs` is the guard,
and it asserts the Prometheus directory's glob holds exactly `usher.yml` rather
than asserting the Grafana file is absent — the second spelling passes for an
empty directory.

✅ **D13 landed in the same split, and its halves went where this paragraph
said they would.** Its free-space arm and its `absent()` guard are in
`alerts/usher.yml`; its database-growth arm is the `usher-disk` group of this
file. Two things about D13's plan were wrong and are corrected where they are
used: `usher_disk_free_bytes` is a name **no producer on this host emits**, and
*"a linear fit over `pg_database_size`"* is not expressible in SQL at all
because Postgres keeps no size history. `alert_names()` spanning both files is
what lets one alert name cover three rules in two engines, so the bidirectional
check needed nothing new — that part of the prediction held exactly.

## The cost-anomaly statement, and the six decisions in it

D14's task text calls out four properties of this query — the eight-day window,
the median, the floor, and staying in `numeric`. Two more were forced by
writing it: the day series has to be **generated** rather than grouped, and the
day boundary has to be **spelled UTC**. All six are below.

The statement lives in the rule and is executed by
`tests/integration/test_cost_anomaly_query.py` against a real
`pgvector/pgvector:pg17`, read out of the YAML rather than retyped. **Every arm
there also executes a planted variant and asserts the two disagree**, because
each of these decisions is one token wide and reads correct alone.

**Eight calendar days, seven of them judged.** The trailing median excludes
today, so the window holds seven complete days *plus* the partial one being
judged. A seven-day window including today compares today against a median it
is a member of, which is a bar that moves toward whatever fired it.

⚠️ **A median is robust to dropping its lowest value, which is what makes this
hard to pin** — and the obvious fixture (an ascending week) cannot see the
window narrow at all. The committed case seeds the *oldest* in-window day as the
**largest**, so narrowing the window by a day drops the median from `4N` to
`3N`; and it seeds a real row on the day before the window, so widening it by a
day does the same. Today spends `11N`, under the correct bar of `12N` and over
both mutants' `9N`, so **both** a narrowed and a widened window page on a night
the committed statement correctly ignores.

**A median, not a mean**, as PRD 10 specifies, and the reason is this
deployment's shape: one generation per household per night, so a single failed
night at $0 and a single re-run at 2x are both ordinary and a mean carries both
into the bar.

⚠️ **The fixture the plan prescribed for this ratifies the mean.** D14's task
text says to seed *"one zero day and one double day"* — over `[0, N, N, N, N,
N, 2N]` the mean is `7N/7 = N` and the median is `N`, exactly equal, so that
week cannot tell the two statistics apart any better than a flat one can. The
committed case seeds `[0, 0, N, N, N, N, 6N]`: median `N`, mean `10N/7`, and a
night at `3.5N` pages under the first and is silent under the second.

🔴 **The trailing days are *generated*, not grouped, so a silent night is a
zero and not an absence.** A plain `GROUP BY day` over `llm_calls` produces no
row at all for a night with no calls, so a deployment that curates three nights
a week would take its median over three nonzero days and never notice the week
it ran every night. The statement generates the eight-day calendar from its own
bounds and `LEFT JOIN`s the ledger onto it. That is D11's zero-versus-absence
rule, one datasource over, and it is also what fixes the trailing set's
cardinality at **seven** — which the median's spelling depends on, below.

🔴 **The comparison stays in `numeric`, and `percentile_cont` is a `float8`
cast arriving without anyone writing one.** PostgreSQL has no `numeric`
overload of it. Measured on PostgreSQL 17.10, 2026-09-11:

```sql
SELECT pg_typeof(percentile_cont(0.5) WITHIN GROUP (ORDER BY cost_usd)),  -- double precision
       pg_typeof(percentile_disc(0.5) WITHIN GROUP (ORDER BY cost_usd))   -- numeric
FROM llm_calls;
```

So the statement the task text supplies — which spells `percentile_cont` in the
same breath as *"the comparison stays in `numeric`"* — is not the statement that
shipped. `percentile_disc` is `WITHIN GROUP (ORDER BY anyelement) -> anyelement`
and, over a set whose size is fixed at seven and therefore odd, returns the same
element `percentile_cont` would.

⚠️ **And the float is not academic at the boundary, which is the only place a
threshold alert ever is.** At a trailing median of exactly `0.14500000` and a
today of exactly `0.43500000`, `3 * 0.145` is `0.43499999999999994` in binary
floating point and `0.435` in `numeric`. PRD 10's condition is *greater than*
three times, so the honest answer is silence and the float spelling **pages**.
Both spellings report `spend_ratio = 3.0000`; only `fired` differs, which makes
a float firing indistinguishable from a real one unless somebody knows to look.

**`round(..., 8)` on the diagnostic columns turns out to be a type guard as
well as a renderer**, which was found by planting the swap rather than by
reasoning about it: `round(double precision, integer)` does not exist in
PostgreSQL, so `percentile_disc` -> `percentile_cont` does not quietly start
comparing floats — the statement raises
`UndefinedFunctionError: function round(double precision, integer) does not
exist` and Grafana's `execErrState: Error` carries it to a human. That is a
better failure than a wrong answer and it is worth not tidying away.

🔴 **The floor: `0.02` USD in a day, and without it this alert is useless on
most deployments.** A trailing median of `0` makes `3 x median` zero and any
spend at all greater than it — and that is the default state, because
`llm_price_in_per_mtok` and `llm_price_out_per_mtok` both default to
`Decimal(0)`, which `src/usher/config.py` calls *"the honest value for a local
model and the wrong one for a hosted model an operator forgot to price"*. This
host's LLM is a local vLLM. So without a floor, the day an operator finally
fills those two settings in is the day this alert starts paging and never
stops.

`0.02` sits between one ordinary night and a tripled one, and both ends are
measured rather than chosen: one generation per household per night cost exactly
**`0.01658700`** in the 2026-08-07 live verification at `3`/`15` USD per Mtok,
so `3x` of it is `0.04976100`. ⚠️ **It is a spend, not a ratio**, so a
deployment whose ordinary night is larger has to raise it; the rule's
description says so and names the two settings.

**UTC days, spelled out.** `date_trunc('day', <timestamptz>)` truncates in the
*session's* time zone, and nothing in this repository sets Grafana's — the
datasource inherits whatever the server was started with. The integration case
runs the statement under `Pacific/Kiritimati` (+14) and `Pacific/Midway` (−11)
and asserts the answer is byte-identical, then runs the unqualified spelling
under the same zones and asserts it is **not**. On the committed fixture the
unqualified one does not merely re-bucket a label: it flips `fired` from 1 to 0.

**The window bound is on the raw `timestamptz` column**, never on the truncated
expression, so `ix_llm_calls_at` still serves it. `EXPLAIN` at 4,000 seeded
rows, one every six hours, so the eight-day window selects 32 rows whatever the
table holds (measured 2026-09-11, PostgreSQL 17.10):

| seeded rows | plan chosen | chosen | next best | ratio |
|---|---|---|---|---|
| 100   | `Seq Scan`   | 46.42 | 46.42  | **1.00** |
| 300   | `Index Scan` | 50.64 | 54.42  | **1.07** |
| 1,000 | `Index Scan` | 50.77 | 83.92  | **1.65** |
| 2,000 | `Index Scan` | 50.77 | 124.92 | 2.46 |
| 4,000 | `Index Scan` | 50.77 | 208.80 | 4.11 |

At 300 rows the planner already picks the index — on a margin of **1.07**, which
is a tie-break wearing a measurement's clothes. At 1,000 it is still under the
2.0 `A_DECISIVE_MARGIN` calls decided. The case seeds 4,000. ⚠️ The whole-plan
ratio understates the index because both plans carry the same ~42 of CTE-scan
cost for the calendar and the aggregate; the scan node alone reads `9.01`
against `166.16`, **18.4x**.
`Index Cond: (at >= ((date_trunc('day', (now() AT TIME ZONE 'UTC')) - '7 days'
::interval) AT TIME ZONE 'UTC'))`, with nothing left over to `Filter`.

## ⚠️ Three things Grafana does with a table frame, each of which decides whether this rule works at all

All three measured against `grafana/grafana:13.1.3` on 2026-09-11, with the
committed file mounted at `/etc/grafana/provisioning/alerting`.

🔴 **Every numeric column becomes a series the condition judges; every string
column becomes a label on it.** The condition here is a `> 0` threshold, so a
second numeric column would be a second series that clears it on every
evaluation — `days_in_window` is `8` by construction — and this alert would fire
forever with a page naming no anomaly. That is the Postgres-side twin of the
empty-vector failure the Prometheus header opens with, failing loudly instead of
silently, and an operator's response to it is to turn the alert off. Hence
`::text` on all six diagnostics and on nothing else. The firing below is one
instance carrying six labels, which is that behaviour confirmed rather than
assumed.

⚠️ **The price of that is that the numbers are series identity.** Grafana keys
an alert instance on its label set, so the moment `today_spend_usd` changes —
which is the moment a new `llm_calls` row lands — the old instance stops being
returned and a **new** one starts at `Pending`. Measured: one evaluation after
the trailing week was raised for the resolve below, this rule reported
`{'alerting': 1, 'normal': 1}` — the `spend_ratio=4.0785` instance still
Alerting because its series had merely vanished, and a fresh `spend_ratio=1.0000`
instance Normal beside it. A day whose spend keeps growing therefore
re-instantiates rather than updates, and an operator sees a resolve and a
re-fire rather than one page that moves.

**The trade is taken deliberately**, because the alternative is a page with no
numbers on it: Grafana's annotation templates can only reach a SQL frame's
values *through* `$labels`, so a diagnostic that is not a label is not on the
page. D14's acceptance is that *"the firing records the `cost_usd` values it
was computed from, because a firing whose inputs were not written down cannot
be distinguished from a threshold that was lowered"* — and what bounds the
churn is the ledger's own shape, one row per generation per household per
night rather than one per scrape.

🔴 **`relativeTimeRange: {from: 0, to: 0}` is rejected, and a rejected file
provisions *no* rules rather than one bad one.** The statement carries its own
window in SQL and reads no `$__timeFilter`, so the obvious spelling for "this
rule has no time range" is zero-width — and Grafana answers
`[alerting.alert-rule.invalidRelativeTime] Invalid alert rule query LEDGER:
invalid relative time range [From: 0s, To: 0s]`, logs `Failed to provision
alerting`, and loads nothing. It is set to the evaluation interval instead.
**This was caught by provisioning the committed file into a real Grafana and
not by any test here**, which is the argument for the firing being part of the
task rather than a formality.

**`noDataState: OK`, `execErrState: Error`.** The statement is a `SELECT` over
CTEs with no top-level row source, so it returns exactly one row whatever
`llm_calls` holds — on this deployment, where that table is genuinely **0 rows**,
`fired=0, today_spend_usd=0.00000000, trailing_median_usd=0.00000000,
days_with_spend=0`. `NoData` is therefore unreachable through the data and is
set for the other way it arrives: a datasource that cannot be reached. A
deployment that has never curated has not had a cost anomaly. An **error** is
decided the other way, because a statement whose answer nobody has is not the
same as an answer of "no anomaly", and is something an operator can fix.

## 🔴 *Disk projection* names a series nothing on this host produces

**Two alerts here have no metric and they fail differently.** *Cost anomaly*
names none, because PRD 10's first principle puts LLM spend on Postgres and
`llm_calls` is the record — so it is a `SELECT`, and a `SELECT` that names
something absent *raises*. *Disk projection* names one, because a filesystem's
free space really is a metric, just not one anybody here is producing — so its
rule **parses, loads and evaluates to an empty vector**, which is
indistinguishable from healthy. That is the failure mode this whole document
opens with, and this is the only rule in the file that was one wrong word away
from it.

Measured 2026-09-11 against the shared Prometheus
(`/api/v1/label/__name__/values`): **93 metric names, and not one is a disk, a
filesystem or a node series.** Two reasons, both deliberate and both in the
stack's own files:

- `prometheus/prometheus.yml` has **no `scrape_configs` at all** — *"the
  collector is the one front door to this stack, and a scrape target that is
  not asked for is a series name nobody chose"*;
- `otel-collector/config.yaml` has three receivers (`otlp`, two `filelog`) and
  **no `hostmetrics`**.

So the rule needs a third wiring stanza beside the two this file's header
already carries, and it is written out above the rule. ⚠️ **A node exporter was
the other candidate and is refused**: it is a scrape target, and that Prometheus
scrapes nothing by design, so wiring one in reopens a decision the stack made
instead of using it. `hostmetrics` enters through the front door that exists and
rides the same `prometheusremotewrite` exporter every other name in that
Prometheus came through.

### The name is `system_filesystem_usage_bytes`, and free space is a *label*

Measured rather than assumed, because this is precisely where D11 lost a round.
The stack's own collector image (`otel/opentelemetry-collector-contrib:0.158.0`)
was run with a `hostmetrics` receiver against a throwaway Prometheus on
2026-09-11, and what arrived was:

| | |
|---|---|
| name | `system_filesystem_usage_bytes` (`system.filesystem.usage`, `unit="By"` → `_bytes`) |
| labels | `mountpoint`, `device`, `type`, `mode`, **`state`** |
| `state` values | `free`, `used`, `reserved` — **three series of one name per mountpoint** |
| `/` on this host | used 1,317,918,756,864 · free 677,411,958,784 · reserved 653,676,544 |

🔴 **`usher_disk_free_bytes` — the name D13's task text supplies — is produced by
nothing, anywhere.** It is not a mis-spelling of a stored name the way D11's four
were; no producer on this host or in the stack emits it. A rule written on it
parses, loads, evaluates empty and reads *healthy forever*, and it is the
planted control in
`test_the_disk_rule_is_grounded_in_a_measured_series_and_not_in_the_resource_table`.

🔴 **And `state="free"` is not optional.** Without it the expression projects
all three series independently, and **the rule fires if any of them trends
below zero** — so a *falling* `used` pages you. `used` falls every time
something is cleaned up: a `VACUUM FULL`, a log rotation, a `docker image
prune`. The selector is the difference between one question and three, and
`promtool` case 3 below is that arm — a falling `used` and `reserved` beside a
rising `free`, which must produce nothing.

### Three rules for one alert, and the second exists to make the first falsifiable

All three are named *Disk projection* — two Prometheus rules here and a
Postgres-datasource rule in `alerts/grafana/usher.yml`. A rule PRD 10's table
does not name falsifies *"kept few, so they mean something"*, and the
bidirectional name check grades the two name sets against each other through
`alert_names()`, which unions both files. `promtool check rules` accepts the
same alert name twice in one group — **SUCCESS: 7 rules found** — and Grafana
accepts it across two groups, which the throwaway run below confirms rather than
assumes.

They answer three different questions: **whether** a filesystem is filling (the
projection), **whether the alert can see anything at all** (the `absent()`
guard), and **which consumer** is filling it (the Postgres half). The second is
this section's subject.

The second rule is `absent(system_filesystem_usage_bytes{state="free"})`, and the
reason is one query. Both Prometheus arms were evaluated **read-only against the
shared Prometheus** on 2026-09-11:

```
predict_linear(system_filesystem_usage_bytes{state="free"}[7d], 14*86400) < 0
  -> []          <- no alerts. This is what "healthy" looks like.
absent(system_filesystem_usage_bytes{state="free"})
  -> {state="free"} = 1     <- "the disk alert is blind"
```

The first line is D11's failure reproduced on purpose. The second is the repair:
one hour of no samples and an operator is told the projection cannot see
anything, rather than being told nothing for as long as the file is installed.

⚠️ **`absent()` here and deliberately not in *Push down*, and the two are worth
reading together.** There an absent series is a source nobody is running a lane
for — a configuration state an operator chose — so an `absent()` arm would page
somebody for having parked a server. Here there is no such state: a deployment
either produces filesystem facts or has an unwatched disk. The distinction is
not *does the series exist* but **is its absence a choice**.

It carries `severity: warning` rather than `page`. A blind alert is an
operations defect and wants a working day; waking somebody because a receiver
was never configured is the wrong page, and paging for it would train an
operator to silence the pair.

`test_every_rule_carries_a_window_a_severity_and_a_description_naming_its_series_and_panel`
gained an arm for it: an `absent()` result carries **only the selector's equality
matchers**, so `{{ $labels.mountpoint }}` on such a rule renders empty and the
page names no subject. Proved by planting exactly that — it dies on
*"`absent()` carries only its own equality matchers ['state'] … ['mountpoint']
renders empty"*.

### The Postgres half, three refutations, and the file D14 built for it

PRD 10's condition is *"Postgres **or image cache** on track to fill within 14
days"*, and D13's task text asks for a companion **Postgres-datasource** rule on
*"the growth of `pg_database_size`"*. Three things about that were wrong, and
the third stopped being wrong while this task was running.

1. **`pg_database_size` is a function, not a series, and it keeps no history.**
   There is no `postgres_exporter` in the stack — the 93 names hold nothing
   `pg_`-prefixed — and a linear fit needs a past. Postgres does not store one.
   So *"a linear fit over `pg_database_size`"* is not expressible in SQL at all,
   and this is the instruction that had to be taken as far as it goes rather
   than followed.
2. **A second rule under a different name is caught by D11's own check.** PRD 10
   names seven alerts; an eighth falsifies the sentence the table opens with,
   and `test_every_alert_prd_10_names_exists_…` grades *"named by no PRD 10
   row"*. So the growth rule carries the **same** alert name, in a different
   engine — `alert_names()` unions the two files, and the union is still seven.
3. ~~**A Postgres-datasource rule cannot live in this repository.**~~ True when
   D13 started and false by the time it landed: **D14 built
   `alerts/grafana/usher.yml`**, one directory below the glob, for *Cost
   anomaly* — which has no series for the same reason and a different one. The
   growth rule is its second group.

#### What is measurable, since `pg_database_size` has no past

Rows do. Seven relations carry a creation timestamp, and each one's current
`pg_total_relation_size / count(*)` prices the rows created in the trailing
week. That is an **estimate of what those rows cost**, labelled as one;
`pg_database_size` itself is exact and is the denominator.

Run against the live catalog on 2026-09-11, the committed statement answers:

| | |
|---|---|
| `database_bytes` (exact) | **8,384,394,931** |
| `measured_table_bytes` (the seven) | **6,830,292,992** — **81.5%** of it |
| `added_bytes_7d` | **2,585,865** |
| `projected_bytes_14d` | **5,171,731** |
| `projected_pct_of_database` | **0.0617** |
| `fired` | **0** |

⚠️ **Two of the ten largest relations are invisible to it and that is stated
rather than worked around.** `title_search_names` (682 MB) and `images`
(455 MB) carry no creation timestamp, so their growth reaches only the
denominator. The other 81.5% is what the statement can see.

⚠️ **One seq scan per relation, because only one of the seven time columns is
indexed** — `ix_raw_payloads_fetched_at`. Measured on the live catalog over
1.27M titles, 2.9M credits, 3.3M neighbours and 896k people: **1.080 s cold,
483.6 ms warm**. At `interval: 10m` the cold figure is a 0.18% duty cycle. That
is the price of the signal and it is recorded rather than assumed; the repair,
if it ever matters, is an index on `titles.created_at` and not a shorter window.

🔴 **The threshold is the database against itself.** *"The next fortnight adds
more than everything this database currently holds"* — self-calibrating, and it
needs no operator to have told it how big the disk is, which is exactly what
`08-operations.md`'s resource envelope cannot be asked for (ADR-0036). A
**bootstrap fires it on purpose**: a deployment filling an empty catalog really
is on track to add more than it holds, and the operator provisioning its disk
is who should be told.

**`severity: warning`, not `page`, and the asymmetry with the free-space half is
the point.** That one answers *whether* a filesystem is filling and is the page.
This one answers *which consumer*, and a page for the diagnostic half would
train an operator to silence the pair. Read them together: **both firing means
Postgres is the consumer; the free-space rule alone means something else on that
filesystem is** — most likely the image cache, which has no eviction and grows
with browse coverage.

⚠️ **`for: 10m` here and `for: 1h` there, and the `for:` follows the signal
rather than the alert name.** The free-space half is a least-squares fit over a
*scraped gauge*, where a single mis-sampled point moves the answer and an hour
buys a fit that has seen more samples. This half is a `count(*)` over
*committed rows*: no sampling noise, monotone over the window, changing by at
most the ingest rate between evaluations. A long `for:` here is a delay with
nothing to learn in it — *Cost anomaly*'s own argument, and it applies for the
same reason. One extra interval buys a second opinion from the same statement,
so a half-committed bulk load cannot page on its own.

**Exactly one numeric column, the rest `::text`** — D14's finding, unchanged:
Grafana turns every numeric column of a table frame into a series the `> 0`
threshold judges, so a stray second number fires the alert forever. Confirmed
against a real PostgreSQL rather than assumed — `format_type` over the
statement's own output reads `fired integer` and five `text`.

### The image cache half: bounded per image, unbounded over time

`08-operations.md`'s *Image cache* row said *"capped by a configurable LRU
ceiling"* until 2026-08-14, when `d6d62ec` corrected it — **one day after this
task was drafted**, so that half of D13's first acceptance bullet was already
done at HEAD. What the row still lacked was the actual bound, the growth driver
and three of the four settings, and D13 supplies those from a measurement:

| the cache on this deployment, 2026-09-11 | |
|---|---|
| bytes | **146,056,327** |
| files | **1,116** over **828** distinct images |
| rungs per image | 609 at one · 155 at two · 59 at three · 5 at four · **none at five** |
| mean per stored rung | **127.8 KiB** |
| browse coverage | 828 distinct images cached for a **1,276,268**-title catalog — **0.06%** |

The "none at five" row is `IMAGE_LADDER = (154, 342, 780, 1280)` showing up in
the data: the ladder bounds the cache at four entries an image *by
construction*, which is what `services/images.py` claims and what nobody had
checked. The unboundedness is the other axis — **nothing evicts and the catalog
grows**, so the size is *images browsed × up to four rungs × their bytes* and
**the growth driver is browse coverage, not catalog size**. Extrapolating
today's mix (1.35 rungs an image, 127.8 KiB a rung) to **one image per title**
gives **~225 GB**, and all four rungs **~668 GB**, against 631 GiB free on this
host. Both are floors as well as extrapolations: a title has a poster *and* a
backdrop, so one image per title is the conservative end. Labelled as such —
and the reason a 14-day projection over this directory is a real question
rather than a formality.

There is no eviction method to configure: `DiskImageBlobStore` has `get`, `put`,
`_forget_other_media_types` and `_path`. The four real settings are
`image_cache_dir`, `image_max_bytes` (a **per-image** 5 MiB refusal, never a
cache cap), `image_fetch_timeout_seconds` and `image_cdn_base_url`.

### No threshold comes from PRD 08's resource envelope, and a test says so

That table's own header records that nothing reads it, no host enforces it and
no policy derives from it; M9's Track 2 derived a 2.0 GB ceiling from one row,
measured a design at 2.702 GB and **withdrew the design** (ADR-0036). So
`test_the_disk_rule_is_grounded_in_a_measured_series_and_not_in_the_resource_table`
parses **63 byte figures** out of that table — in both the decimal and the
binary reading of every ambiguous unit — and forbids any of them appearing as a
literal in any `expr` in the file.

⚠️ **The positive control the task text names does not land at this HEAD.** It
proposes planting `8589934592` (the old `~8 GB` row); that row now reads
*"`~8–12 GB` described a database this project no longer has"*, so `8 GB` is no
longer a standalone figure and the plant would pass. The controls used instead
are `5025650355` — the measured baseline, the figure most likely to be promoted
from a measurement into a threshold — and `2147483648`, ADR-0036's withdrawn
ceiling. Both die on
*"a rule in this file carries a byte literal that is a figure from PRD 08's
resource envelope"*.

The baseline **is** quoted, in the rule's description, as *what the database was
on a date*: 5,025,650,355 B (4,793 MB) at 1,272,367 titles with 130,647 enriched
on 2026-08-12, and 8,384,394,931 B (7,996 MB) at 1,276,268 titles with 133,576
enriched on 2026-09-11. The scan reads `expr` and never the annotations, because
a scan that could not tell those apart would forbid saying the number at all.

### 7 days predicting 14, and `predict_linear` decays out of its own window

**+3.36 GB in thirty days against +3,901 titles** is the argument for the long
window: this deployment's growth followed `m09d`, `m09e` and a full re-embed,
not the catalog. An hour extrapolated to a fortnight would page on every
`VACUUM`.

`predict_linear` was added to
`test_no_decaying_window_is_as_long_as_the_for_that_waits_on_it`'s list, and it
belongs there for the stated reason rather than by family resemblance: a
least-squares fit over `[7d]` is tilted by a one-off step for exactly seven days
and then not at all, which is the same knife edge `increase` has. `for: 1h`
against `[7d]` passes; `for: 7d` planted against it dies on *"is not shorter than
the [7d] window its own decaying condition lives in"*.

⚠️ **A migration that transiently doubles a table fires this rule, and the
recovery was measured rather than assumed.** `08-operations.md` prices `m09d` at
+637 MB transient on `credits` (794 → 1,431 MB, settling at 740 only after a
`VACUUM FULL` the migration does not run). Driven through `promtool` as a 637 MB
drop over one hour onto a 5 GB filesystem that then goes flat:

| t after the drop | `predict_linear(free[7d], 14d)` |
|---|---|
| 2 h | **−102.81 GB** — firing |
| 4 h | −30.40 GB |
| 6 h | −12.38 GB |
| 8 h | −5.44 GB |
| 10 h | −2.07 GB |
| 12 h | **−0.18 GB** — still firing |
| 16 h | **+1.75 GB** — resolved |
| 24 h | +3.17 GB |

So the entry that belongs in [`upgrade.md`](../docs/runbooks/upgrade.md) is
**silence for the migration window and roughly twelve to sixteen hours after
it**, not for the seven days the range vector is long. The figure is for that
drop's shape on that much free space and scales with both. **Widening the
window is not the repair** — widening it is how an alert stops seeing the thing
it is for.

⚠️ **There is nothing to silence it with, so the step was not written.** The
shared stack has **no Alertmanager**, `prometheus/prometheus.yml` has no
`alerting:` block, and Grafana provisions no alert rules — checked, not assumed.
Nothing routes anything in this file, which is the same asymmetry this section
opens with about `rule_files:`. A silence procedure against an Alertmanager that
does not exist is a runbook step nobody can run, and this project has a rule
about those. `upgrade.md` is named here as where it goes; until then a
migration's firing is visible on Prometheus's own `/alerts` page and nowhere
else.

### Fired once, on a real filesystem, without lowering anything

🔴 **A firing whose recipe is "we lowered the number until it fired" is recorded
as not having fired.** Nothing about the rule was changed for this: the same
committed expression, the same `[7d]`, the same `14 * 86400`, the same `for: 1h`.
What changed was the disk.

- **The filesystem**: a **256 MiB ext4 image** (`fallocate -l 256M`, `mkfs.ext4`)
  mounted on a loop device at `/var/tmp/d13/scratchmnt`. Usable **241,081,344 B**,
  free at t₀ **223,188,992 B**. Nothing of this host's real storage was
  involved beyond the image file.
- **The rate**: exactly **1 MiB every 60 s**, `dd` + `sync`, logged with the
  wall clock and the filesystem's own `df` after each write. The first three
  deltas are 1,048,576 B each, so the rate is the measurement and not the
  intention.
- **The negative control, running at the same time**: this host's real `/` —
  1.996 TB, 677 GB free — was reported by the same collector under the same
  rule, because the rule carries no `mountpoint` selector.

What happened, in UTC:

| | |
|---|---|
| `pending` | **18:37:58.747Z**, the first evaluation after the first negative fit |
| fill | 1 MiB/min, **62 writes**, free 223,188,992 → 158,175,232 B (exactly 62 MiB) |
| **`firing`** | **19:37:58.747Z** — `activeAt` + `for: 1h` to the millisecond |
| `$value` | **−20,029,396,686** — the free bytes the fit predicts at the 14-day horizon |
| labels | `mountpoint=/var/tmp/d13/scratchmnt`, `device=/dev/loop0`, `type=ext4`, `mode=rw`, `state=free`, `severity=page` |
| the page | *"/var/tmp/d13/scratchmnt is projected to fill within 14 days"* |
| freed | **19:39:18Z**, all 62 blocks deleted, free back to 223,186,944 B |
| **resolved** | by **20:08:55Z** — 29 min 37 s later, last firing sample **−44,596,514** |

🔴 **The resolve was produced by giving the filesystem its space back, not by
editing the rule** — the same care D11 took in making the silent Emby stub
*deliver* rather than stopping it. And it took half an hour rather than an
instant, which is the `[7d]` window doing what it is for: one minute of
recovery does not erase an hour of decline from a least-squares fit, and the
alert stays up until the trend really has turned.

### ⚠️ The rule is noisy until its own window has filled, and that is measured

The same live run produced a **false positive on this host's real `/`**, and it
is recorded here rather than tuned away because it is the most useful thing the
run produced.

`predict_linear` answers from whatever samples are inside `[7d]`, however few.
The throwaway Prometheus had been running half an hour, so the "seven-day
trend" for `/` was thirty minutes of samples — and one **−4,033 MB step at
18:45:17Z** (this host runs vLLM, Docker and a dozen other containers) was the
entire trend in it:

```
18:44:17  677,174,931,456
18:45:17  673,141,497,856   -4,033 MB   <- one step
18:46:17  672,667,385,856     -474 MB
...       flat to within a few MB
```

`/` is 1.996 TB with 677 GB free and is in no danger whatever. The rule
predicted **−3.67 TB** at the 14-day horizon, went `pending` at **18:45:28Z**
and **reached `firing` at 19:45:28Z** — so this is a false page and not merely a
false pending, and it is recorded as one.

**It then cleared itself, and the clock on that is the whole of the argument.**
As flat samples accumulated in the window the prediction climbed
−3.67 TB → −3.44 TB → −2.22 TB → −1.42 TB → −51.7 GB → −11.6 GB, and the alert
was **gone by 19:46:53Z** — 85 seconds after it fired, and 61 minutes after the
step that caused it. It needed roughly eighty minutes of samples in a seven-day
window before one −4,033 MB step stopped being the whole trend.

🔴 **No data-sufficiency conjunct was added, and that is a decision rather than
an omission.** The obvious repairs — `and x offset 1d`,
`count_over_time(x[7d]) > N`, the node-exporter mixin's *"and it is already
below 40% free"* — each need a number nobody here has measured, and a number
picked so that a demonstration passes is exactly the thing the firing recipe
below exists to refuse. In steady state the arithmetic is unremarkable: a
−4,033 MB step against 677 GB free, spread over a full seven days, moves the
14-day extrapolation by ~8 GB and the rule stays silent. The mis-firing window
is the first days after the receiver is wired, it is bounded, and the operator
living through it is by construction the person who just cleared the `absent()`
page. The measurement is here for whoever decides to encode a guard.

### The database-growth half, fired through a throwaway Grafana

Not Prometheus, so it was fired the way D14 fired *Cost anomaly*: a throwaway
**Grafana** (`grafana/grafana:13.1.3`) with `dashboards/alerts/grafana` mounted
at `/etc/grafana/provisioning/alerting` and a provisioned `usher-postgres`
datasource, reading a throwaway **PostgreSQL** (`pgvector/pgvector:pg17`)
migrated to head with `alembic upgrade head` against **its own** database. The
shared Grafana on `127.0.0.1:3000` and the shared `usher-postgres-1` were not
touched; the only thing either was asked for was a `SELECT`.

Both rules provisioned from the one file — `usher-cost-anomaly` in group
`usher-cost` and `usher-disk-projection-postgres` in `usher-disk` — which is
what says two Grafana rules may share an alert **name** as long as they are in
different groups. PRD 10's table still has one *Disk projection* row and
`alert_names()` still unions to seven.

🔴 **The fixture is a bootstrap, because that is the rule's own firing
condition and not a lowered number.** Nothing about the rule was changed for
this: the same statement, the same `2 ×`, the same `> 0` threshold, the same
`for: 10m`. **60,000 `titles` rows were inserted with the schema's own
`created_at DEFAULT now()`** — a real bootstrap in miniature — so a database
that was 9,516,723 B empty became 135,468,723 B of which **everything** had been
created inside the trailing week.

| | |
|---|---|
| `Pending` | **20:09:50Z**, the first evaluation after the seed |
| **`Alerting`** | **20:19:50Z** — `activeAt` + `for: 10m` exactly |
| `$value` | **1** — the `fired` column, and the comparison that decided is the one in `numeric` |
| labels | `database_bytes=135468723`, `measured_table_bytes=126197760`, `added_bytes_7d=126001152`, `projected_bytes_14d=252002304`, `projected_pct_of_database=186.0225`, `severity=warning` |
| the page | *"Postgres is on track to add 252002304 B in 14 days against the 135468723 B it holds"* |

**The resolve was produced by letting the growth leave the window, not by
deleting the rows.** 59,000 of the 60,000 rows had their `created_at` moved to
30 days ago — which is the state this same database is in a month later, with
every byte still present. `added_bytes_7d` fell 126,001,152 → **3,860,070** and
`projected_pct_of_database` 186.0225 → **3.2023**, against a database that had
*grown* to 241,079,987 B in the meantime. Resolved at **20:39:50Z**.

⚠️ **D14's series-identity churn, reproduced independently.** Grafana keys an
alert instance on its label set, and every number this statement returns is a
label — so the moment the numbers changed, the old instance stopped being
returned rather than being updated. Watched at 20:30:11Z: the 186.0225 instance
still `Alerting` because its series had merely vanished, and a fresh 3.2023
instance `Normal` beside it. It took **one further evaluation interval** — the
20:39:50Z pass — for the stale one to be dropped and the rule to read
`inactive`. That is the price D14 records for putting the diagnostics in labels,
measured here at ten minutes, and it is the reason `interval: 10m` is also the
churn quantum.

### Fire and resolve, without waiting a fortnight

`promtool test rules` drives both rules through their real `for: 1h` at the real
durations, including the three negatives no live run produces on demand. All
**SUCCESS** on 2026-09-11:

- A filesystem losing **1 MiB a minute** does **not** fire at 59m and fires at
  61m, with the full rendered page compared field by field.
- A filesystem whose free space is **rising** never fires, at 61m or at 179m.
- 🔴 **`used` and `reserved` rising are not selected.** The negative control for
  `state="free"`: the same mountpoint reporting a falling `used` and `reserved`
  beside a *rising* `free` produces no alert at all.
- The absence arm fires at 61m and not at 59m on an instance exporting
  `usher_jobs_queued_ratio` and no filesystem series whatever.
- The absence arm **resolves** the moment a filesystem starts reporting — a
  series absent for 70 minutes and then present is not firing at 80m.
- The migration table above: −102.81 GB at 2 h, −0.18 GB at 12 h, **+1.75 GB at
  16 h**.

```bash
docker cp dashboards/alerts/usher.yml <throwaway-prom>:/tmp/d13/usher.yml
docker exec <throwaway-prom> promtool check rules /tmp/d13/usher.yml
docker exec -w /tmp/d13 <throwaway-prom> promtool test rules fire-and-resolve.yml
```

## The firings

Each rule was put through `pending → firing → resolved` against real data on
**2026-09-11** — the Prometheus rules by a throwaway Prometheus reading the
shared one over `remote_read`, and the two Grafana rules by a throwaway Grafana
reading a throwaway Postgres. **In no case was the shared stack touched**:
`~/code/observability/`'s Prometheus still has no `rule_files:` entry and its
Grafana no alerting provisioning; D13's disk series came from a *second*
throwaway collector, not from the shared one. Times are UTC; `instance` is the
exporting process's `service.instance.id`, and the two Postgres rules have none
because their series is a table.

🔴 **One row below is a false positive and is in the table on purpose.** The
free-space rule was run without a `mountpoint` selector, so this host's real `/`
was graded by it at the same time as the scratch filesystem — and it fired.
Recording it beside the deliberate firing is the only way the deliberate one
means anything.

| rule | fired | labels | `$value` | resolved |
|---|---|---|---|---|
| Jobs parking | 17:15:58 (active 17:10:58 + `for: 5m`) | `kind=bootstrap`, `instance=5a6032ac…` | 1.069 | 17:25:02, when the park left the `[15m]` window |
| Push down | 17:43:13 (active 17:28:13 + `for: 15m`) | `source=D11 Silent Channel`, `instance=4a433056…` | 0 | 18:08:25, when the channel started delivering |
| Ingest stalled | 17:48:13 (active 17:43:13 + `for: 5m`) | `kind=curate`, `instance=5a6032ac…` | 25 | 17:49:09, 40 s after the queue was drained |
| Provider degraded (D12) | 18:48:44 (active 18:38:43 + `for: 10m`) | `provider=tmdb` | 0.2000 | 19:07:48, 4m00s after the fault lifted — one `[5m]` window |
| Enrichment SLA missed (D12) | 18:53:45 (active 18:38:43 + `for: 15m`) | `trigger=demand` | 7.475 | 19:08:59, 5m11s after the fault lifted |
| Cost anomaly | 18:57:50 (active 18:47:50 + `for: 10m`) | `today_spend_usd=0.07324200`, `trailing_median_usd=0.01795800`, `spend_ratio=4.0785`, `floor_usd=0.02000000`, `days_in_window=8`, `days_with_spend=8` | 1 | 19:17:50, two evaluations after the condition went false — see below |
| Disk projection, free space (D13) | 19:37:58 (active 18:37:58 + `for: 1h`) | `mountpoint=/var/tmp/d13/scratchmnt`, `device=/dev/loop0`, `type=ext4`, `state=free` | −20,029,396,686 | by 20:08:55, 29m37s after the 62 MiB were deleted |
| Disk projection, free space — **false positive** (D13) | 19:45:28 (active 18:45:28 + `for: 1h`) | `mountpoint=/`, `type=btrfs`, `state=free` | −11,621,552,887 | 19:46:53, 85 s later, once the window held ~80 min of samples |
| Disk projection, database growth (D13) | 20:19:50 (active 20:09:50 + `for: 10m`) | `database_bytes=135468723`, `added_bytes_7d=126001152`, `projected_bytes_14d=252002304`, `projected_pct_of_database=186.0225` | 1 | 20:39:50, two evaluations after the growth left the window — see the churn note above |

**Jobs parking** — one `bootstrap` job enqueued with the key `d11-not-a-phase`.
`services/handlers.py`'s `_bootstrap_phase` raises `PortDataMalformed`, which is
the arm in `services/jobs.py` that sets the `usher.job.parked` span attribute and
calls `_fail(retryable=False)`. The row landed `status='parked'` with
`last_error` naming the seven valid phases, and the gauge moved 0 → 1 on the next
worker pass. It resolved on its own once the park fell out of the `[15m]`
window, which is the rule saying *"nothing new has parked"* rather than
*"nothing is parked"* — the distinction the absence of a threshold buys.

**Push down** — a stub Emby on loopback that authenticates, accepts the
`/embywebsocket` upgrade and **never sends a frame**. At the firing:
`usher_source_push_reconnects_total` read **0**, so the socket was the original
one, held open for the whole fifteen minutes; `ss` showed the pair of
`ESTABLISHED` sockets between the two processes; `/health/ready` listed the lane
under `push` with an empty `crashed_sources`; and the gauge had sixteen
consecutive samples of `0`. ⚠️ **This ran against a stub and never against the
operator's Emby** — `.claude/rules/api-telemetry-and-lanes.md` records that a
push lane's reconnect gap-closer issues a full `DELTA` reconcile, 1,126,789 items
against the measured household. The stub's own lane logged the refusal
(*"no item sync has ever completed for this source, so the reconnect delta would
walk its entire library"*), which is that guard working. **The resolve was
produced by making the stub deliver**, not by stopping it: a firing produced by
stopping the source proves the wrong thing, and so would a resolve.

**Ingest stalled** — 25 `curate` jobs enqueued against a running worker with
`USHER_LLM_ENABLED=false`, so `CURATE` is absent from `worker_kinds`, the jobs
are never claimed, and `usher_jobs_duration_seconds_count{kind="curate"}` never
comes into existence. That is the alert's headline case and the one `and … == 0`
cannot see. `$value` is **25**, an honest job count matching the 25 pending rows,
because `min_over_time` reports a depth where `increase()` reported an
extrapolated 27.84 for the same queue.

### Fire and resolve, without waiting an hour

`promtool test rules` drives the committed file through both transitions at the
real `for:` durations, including the two negatives no live run produces on
demand. Rules copied into the container because Prometheus is docker-net-only:

```bash
docker cp dashboards/alerts/usher.yml observability-prometheus-1:/tmp/usher.yml
docker exec observability-prometheus-1 promtool check rules /tmp/usher.yml
docker exec -w /tmp observability-prometheus-1 promtool test rules fire-and-resolve.yml
```

The cases that matter, all **SUCCESS** on 2026-09-11:

- *Ingest stalled* does not fire at 34m and fires at 40m on `kind="curate"`,
  which has **no completions series at all**, while `kind="enrich"` beside it —
  same depth, settling steadily — never fires.
- The freshness conjunct: a lane whose series **stops being exported** at t=40m
  pages at neither 50m nor 70m, though `min_over_time` still answers 25.
- *Jobs parking* does not fire at 9m, fires at 12m, and **resolves at 40m**.
- *Push down* does not fire at 14m, fires at 20m, and **resolves at 60m** once
  the gauge reads 1.
- The absence arm: an instance exporting **no push series at all** never fires
  *Push down*, at 20m or at 60m. An `absent()` arm would fire for the whole hour.

**Provider degraded** (D12) — the real `TmdbClient` driven through
`httpx.MockTransport` against an upstream answering **10 % 429 and 10 % a
transport drop** that never reaches a status line. Both arms go through
`client.py`'s `finally`, which is what makes the transport failure *counted*
rather than absent. At the firing, the numerator and denominator were recorded
**separately** rather than inferred from the ratio:

| series | rate |
|---|---|
| `…{status="200"}` | 4.9116 /s |
| `…{status="429"}` | 0.6124 /s |
| `…{status="error"}` | 0.6124 /s |
| **numerator** `sum by (provider) (rate(…{status=~"429\|5..\|error"}[5m]))` | **1.2252 /s** |
| **denominator** `sum by (provider) (rate(…[5m]))` | **6.1409 /s** |
| ratio | **0.19959** |

🔴 **The `error`-in-both decision, observed rather than asserted.** `error`
appears in the numerator (1.2252 = 0.6124 + 0.6124) *and* in the denominator
(6.1409 = 4.9116 + 0.6124 + 0.6124). The same instant with `error` dropped from
the numerator reads **0.09948** — exactly half, because the two failure arms ran
at equal rates. That is the mild case. The severe one is a *total* transport
outage, where the numerator would be 0 and the denominator the error count, so
the ratio reads a healthy **0 %** at the worst possible moment;
`promtool` drives exactly that (test 2 below) and the rule fires at **100 %**.
PRD 10 states only the denominator half of this — *"a denominator that omitted
the failures would read low exactly during an outage"* — and the numerator half
follows from the same sentence without being in it.

**The resolve was produced by fixing the fault, not by stopping the load.** The
driver kept both lanes running at full rate for another fifteen minutes with the
delay removed and the upstream answering 200; a resolve produced by stopping the
traffic proves nothing, the same way D11's push resolve had to come from the
stub *delivering* rather than from the stub going away. Provider degraded
cleared at **19:07:48**, four minutes after the fault lifted at 19:03:48 — one
`[5m]` rate window draining — and Enrichment SLA missed at **19:08:59**, its
`$value` visibly decaying through **6.2017 s** at 19:07:48 as fast enrichments
displaced slow ones inside the window.

**Enrichment SLA missed** (D12) — `EnrichService` driven through its real
`_apply` with a provider stub sleeping **6 s**, at `JobPriority.DEMAND`. 🔴 **A
second lane ran at `BACKFILL` the whole time at 50 ms**, because an alert fired
by making *all* enrichment slow proves the quantile works and not the scoping,
and the scoping is the whole content of this task. At the firing, with both
lanes live:

| `trigger` | p99 over `[5m]` |
|---|---|
| `demand` | **7.4750 s** — over the SLA, fires |
| `background` | **0.0995 s** — under it, silent |

and the fired alert carries `trigger="demand"` in its own label set. Before
D12 this alert could not be written at all: read off this host's Prometheus on
2026-09-11, `usher_enrichment_latency_seconds_bucket` carried exactly
`instance`, `job`, `le`, `otel_scope_name`, `outcome` — **PRD 10 specified an
alert against a `trigger` dimension the shipped series did not carry, and its
own correction bullet three hundred lines above the table recorded why.**

⚠️ **Both `$value`s are bucket interpolations, and both match
`lo + (hi − lo) × q` exactly** — 7.4750 = 5 + 2.5 × 0.99, 0.0995 =
0.05 + 0.05 × 0.99. That is the honest reading of any `histogram_quantile`, and
it is why `usher.enrichment.latency` gained an
`explicit_bucket_boundaries_advisory` in the same task. ⚠️ **The advisory's
justification is narrower than "the rule could not fire".** 5 s is *also* a
boundary of the SDK's default ladder, so under the defaults the comparison
still discriminates — a deployment with 99 % of enrichments inside 5 s reads
**4.95 s** and stays silent, one where more than 1 % cross reads **9.95 s** and
pages. What the defaults cannot do is report a *latency*: a healthy 100 ms
deployment reads a p99 of 4.95 s, fifty milliseconds from an SLA it is nowhere
near, with no resolution on either side of the threshold to watch a drift
approach it.

**Cost anomaly** — the one that is not Prometheus, so it was fired through a
throwaway **Grafana** (`grafana/grafana:13.1.3`) reading a throwaway Postgres,
with `dashboards/alerts/grafana` mounted at
`/etc/grafana/provisioning/alerting` and a provisioned `usher-postgres`
datasource. The shared Grafana on `127.0.0.1:3000` was not touched.

🔴 **The firing records the `cost_usd` values it was computed from**, because a
firing whose inputs were not written down cannot be told apart from a threshold
somebody lowered. The database was a clone of the 1.27M-title seed catalog at
head `m10c`, with `USHER_LLM_PRICE_IN_PER_MTOK=3` and
`USHER_LLM_PRICE_OUT_PER_MTOK=15` — the pair the 2026-08-07 live verification
used — and `USHER_LLM_BASE_URL` on the local vLLM (`gemma-4-26b-a4b`). Four
**real** `usher curate` generations were run in one evening against a trailing
week seeded at one generation a night:

| `at` (UTC) | `tokens_in` | `tokens_out` | `cost_usd` |
|---|---|---|---|
| 2026-09-04 … 09-10, 12:00 (7 seeded rows) | 4881 | 221 | `0.01795800` each |
| 2026-09-11 18:46:10 | 4881 | 221 | `0.01795800` |
| 2026-09-11 18:46:29 | 4881 | 247 | `0.01834800` |
| 2026-09-11 18:46:31 | 4881 | 257 | `0.01849800` |
| 2026-09-11 18:46:34 | 4881 | 253 | `0.01843800` |

Today `0.01795800 + 0.01834800 + 0.01849800 + 0.01843800 = 0.07324200`; the
trailing median `0.01795800`; the ratio `4.0785`; the bar `3 x 0.01795800 =
0.05387400`; the floor `0.02000000`, cleared three times over so it is not what
decided. The four generations' own costs reconcile exactly:
`(4881x3 + 221x15) / 1e6 = 0.017958` and so on, which is the 2026-08-07
eight-decimal reconciliation reproduced on a different model.

**The rendered page**, which is what an operator actually gets — read off a
second firing at **19:26:30Z** on the same fixture, because the first firing's
summary was the spelling this one replaced (below):

> **LLM spend today is 0.07324200 USD against a trailing 7-day median of
> 0.01795800; ratio 4.0785**

**The resolve was produced by making the week catch up with the night, not by
deleting the night.** Money already spent cannot be un-spent, and an alert that
resolved because somebody removed its evidence would prove the wrong thing —
this is the same care D11 took in making the silent Emby stub *deliver* rather
than stopping it. Three more generations were added to each of the seven
trailing days, with the token counts and costs of today's second, third and
fourth, so every day in the window became the identical four-generation night:
trailing median `0.07324200`, ratio `1.0000`, `fired = 0`. That is also what
tomorrow does on its own, when today joins the trailing week.

⚠️ **It resolved two evaluations late, and that is the label-as-identity
property above, observed.** The condition went false at the **19:07:50**
evaluation — but under a *different* label set (`spend_ratio=1.0000`,
`trailing_median_usd=0.07324200`), so Grafana saw a brand-new `Normal` series
appear beside a firing one that had merely stopped being returned:
`{'alerting': 1, 'normal': 1}`. It resolved at **19:17:50**, when the missing
series aged out, leaving `{'normal': 1}` and an empty
`/api/alertmanager/grafana/api/v2/alerts`. So the ten-minute `for:` is not the
only latency in this rule; a resolve costs the missing-series grace as well,
and an operator reading the timestamps should expect it.

⚠️ **Three things this firing caught that no test here could**, which is the
argument for the live run being part of the task rather than a formality.

1. The rule as first written carried `relativeTimeRange: {from: 0, to: 0}` —
   correct in intent, since the statement has no time range of its own — and
   Grafana **refused the whole provisioning file** for it, loading no rules at
   all.
2. The string-columns-become-labels conversion the `::text` casts depend on was
   a *reading* of Grafana's behaviour until this run showed **one** instance
   carrying exactly the six diagnostic columns as labels and `value = 1e+00`
   from the seventh.
3. 🔴 The summary's first spelling was
   `{{ $labels.spend_ratio }}x the trailing 7-day median (…)`, which reads
   perfectly here and renders **"LLM spend today is undefined (zero trailing
   median)x the trailing 7-day median"** on this rule's *other* reachable
   firing — a zero trailing median above the floor, where `spend_ratio` is a
   sentence rather than a number. The two numbers lead now and the ratio
   follows as its own clause.
   `test_the_cost_anomaly_summary_survives_an_undefined_ratio` guards the
   adjacency; the rendered page quoted above is from the re-firing that
   confirmed the new spelling.

## 🔴 `for:` longer than its own `rate()` window — the D11 guard, narrowed by measurement

D11 measured that `increase(depth[30m])` with `for: 30m` is unsatisfiable and
generalised it to *every* decaying call:
`test_no_decaying_window_is_as_long_as_the_for_that_waits_on_it` asserted
`for: < window` for all of them, and its own docstring said the case was *"for
D12–D14 more than for D11"*. **Both of D12's rules fail that assertion and both
demonstrably fire**, so the generalisation was too wide and D12 narrowed it
rather than working around it.

The knife edge is not a property of the function. It is a property of whether
the signal underneath is *regenerated while the fault lasts*:

- a **gauge** is a level — it steps once and freezes, so the step leaves the
  range vector exactly `[W]` later and the condition is true for at most W.
  D11's queue depth is this, and its measurement stands unchanged;
- a **counter** (or a histogram's bucket series) is fed by *every event*, so
  under a fault lasting D the condition holds for about D + W and a `for:`
  longer than W is patience rather than a race.

Measured twice, live and synthetically. Live, against this host's data: both
rules went `pending` within ~90 s of the fault starting and fired at exactly
`activeAt + for:` — Provider degraded at **18:48:44** (`for: 10m` over `[5m]`)
and Enrichment SLA missed at **18:53:45** (`for: 15m` over `[5m]`).
Synthetically, `promtool test rules` drives the same two through both
transitions plus the negatives no live run produces on demand. The guard now
grades a decaying window only when its operand is a **gauge**, derived from
`create_observable_gauge` declarations rather than listed, and it still bites:
moving *Jobs parking* to `for: 20m` over its `[15m]` gauge window fails on its
own `E ` line.

### D12's `promtool` cases, all SUCCESS on 2026-09-11

1. *Provider degraded* is silent at 9m, **fires at 16m**, still firing at 60m —
   `for: 10m` over `[5m]`, sustained 10 % 429.
2. **A total transport outage fires at 100 %** — only `status="error"` moving.
   With `error` in the denominator alone this reads 0 % and never pages.
3. **A 404 storm never fires**, at 16m or 60m: a title TMDb does not have is the
   enrichment lane's ordinary business, which is why the numerator names `429`
   rather than a `4..` class.
4. *Provider degraded* **resolves by 40m** once the 429s stop.
5. *Enrichment SLA missed* is silent at 14m, **fires at 21m** with
   `trigger="demand"` and `$value` 7.475s, still firing at 60m.
6. 🔴 **Slow `background` enrichment never fires**, at 21m or 60m. Without
   `{trigger="demand"}` on the selector this case fires — which is what an alert
   written before the label existed would have done.

⚠️ **The 5 % threshold is provisional and is labelled as one in the rule
itself.** The only live run this project has is M9's S3 crawl — 130,334 requests
over 1.98 h, **no 429 and one 400** — so the observed degraded rate at the one
load ever measured is `0/130,334 = 0 %`, and the single non-2xx is a code this
numerator deliberately excludes. That argues 5 % is *safe*, not that it is
*right*: nothing here knows what fraction of 429s TMDb returns under sustained
load, because this deployment has never seen one. The re-measure is named — the
first real 429 this deployment sees. The 20 % above is a *stub* answering on
demand and is not evidence about TMDb.
