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

Dashboards 3–6 are specified in PRD 10 and not yet built.

**Every panel on both dashboards is SQL against the canonical database and none
of them is a Prometheus query.** That is PRD 10's first principle doing the
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
