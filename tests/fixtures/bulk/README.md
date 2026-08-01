# Bulk-dataset fixtures — synthetic rows in the real file formats

Read [`../README.md`](../README.md) first: it states the licensing rule, the
reserved identifier bands, and the fact that these files previously carried
real IMDb rows under a note claiming they did not.

**Every value in these four files is invented.** No id, title, year, runtime,
rating or vote count here describes a real work. What is preserved is the
*format* — and each row exists to pin one thing about it.

## `title.basics.slice.tsv`

Ten lines: a header and nine data rows, five of which survive the filter. The
file has no trailing newline, matching the shape the adapter reads.

| Line | Row | Pins |
|---|---|---|
| 0 | header | `parse_basics_row` returns `None` for it rather than raising |
| 1 | `short` | `titleType` filtering — only the four types that map to `TitleKind` are kept |
| 2 | `"A Quoted Synthetic Title"` | **the whole reason this project does not use `csv`** — see below |
| 3 | a `movie` | the ordinary path, and the join target for `title.ratings` |
| 4 | a `tvSeries` | `endYear` is kept; the comma-separated `genres` field splits into three |
| 5 | a `tvEpisode` | dropped — `TitleKind` is movie/series only |
| 6 | `isAdult=1` | dropped, per PRD 04 |
| 7 | a `videoGame` | dropped |
| 8 | a `tvMiniSeries` | maps onto `TitleKind.SERIES` |
| 9 | a `tvMovie`, every optional column `\N` | maps onto `TitleKind.MOVIE`; `\N` becomes `None`, never the literal two characters |

**The quoted row is load-bearing and its shape matters.** IMDb's TSVs have no
quoting mechanism at all, and a title field may both open *and* close with a
literal `"` — 21 such titles in the first 553,395 rows of the real
`title.basics.tsv.gz`, measured directly (`CLAUDE.md` names the specimen).
`csv.reader`'s default `QUOTE_MINIMAL` silently strips both quotes off such a
field, which is why `usher.adapters.bulk.imdb` parses with `line.split("\t")`.

The invented replacement had to keep that exact shape to keep testing it:
verified before committing that `csv.reader` strips `"A Quoted Synthetic
Title"` down to `A Quoted Synthetic Title` while `str.split("\t")` does not.
A title with *interior* quotes (`A "Quoted" Title`) would **not** work —
`csv.reader` only treats `"` as a quote character at the start of a field, so
such a row survives both parsers and pins nothing.

The other format properties the tests depend on, all preserved: tab
separation, the header row, `\N` as the null sentinel, the movie/series
`kind` split, and line ordering (`position` is a line offset, so resuming
from line 5 must yield exactly the last two kept rows).

## `title.ratings.slice.tsv`

Header plus three rows. Two join onto basics rows; the third (`tt99000090`)
has no basics row, so a rating for a title the catalog does not hold is
exercised. Ratings are on IMDb's own 0–10 scale because
`Title.community_rating` is `Field(ge=0, le=10)` and the schema mirrors that
as a CHECK — the scale is the contract, the numbers are invented.

## `movie_ids.slice.jsonl` / `tv_series_ids.slice.jsonl`

TMDb's *public daily id export* format: one JSON object per line, no wrapping
array. Four movie rows and two series rows, shaped to pin the export's two
real asymmetries — the TV export spells the name `original_name` and omits
`adult` entirely, so a parser that read `original_title` would raise on every
TV row — plus a row with no `popularity` (which must default to `0.0`, never
`None`: the column is `NOT NULL` and a crawl queue ordered by `NULL` has no
ordering) and an `adult: true` row.

Both files carry id `90000045`. That is deliberate: TMDb's movie and series
id spaces overlap on 26,968 ids, which is what ADR-0011 exists for.

## Regenerating

Don't. These are hand-written, and the real dumps must never be committed —
`USHER_BULK_DATA_DIR` defaults to `data/bulk`, which is inside `.gitignore`.
To change one, edit it and run

```bash
uv run pytest tests/unit/test_adapters_bulk_imdb.py \
              tests/unit/test_adapters_bulk_tmdb_ids.py \
              tests/integration/test_bootstrap_end_to_end.py
```

Several assertions depend on line offsets and on the order of the kept rows.
