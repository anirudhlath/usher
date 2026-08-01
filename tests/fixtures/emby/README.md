# Emby fixtures — shape-recorded, value-synthetic

Read [`../README.md`](../README.md) first: it states the licensing rule, the
reserved identifier bands, and the guard that enforces them.

**Every value in these four files is invented**, including the `ProviderIds`,
which until 2026-08-01 carried real TMDb/IMDb/TVDb ids for four real works.
That is a licensing constraint, not a style, and a doubled one: a real Emby
response *embeds TMDb-sourced metadata*, which TMDb's terms forbid
redistributing, and it also identifies a real library and carries real server
and user ids. `src/usher/adapters/emby/mapping.py`'s module docstring says the
same thing from the code's side.

**What is kept, deliberately, because it is Emby's protocol rather than
anyone's data:** field names and nesting; the `Type` vocabulary
(`Movie`/`Series`/`Episode`); `RunTimeTicks` as 100-nanosecond units; the
`.0000000Z` timestamp format; and — most load-bearing — the `VideoRange` /
`ExtendedVideoType` / `ExtendedVideoSubType` token vocabulary. Emby 4.9.5.0
emits `VideoRange ∈ {SDR, DolbyVision, HDR 10}` (with a space) and never
emits `VideoRangeType` or `DvProfile`; `Extended*` carries the **literal
string `"None"`**, not JSON null, so it is always truthy. Those tokens are
what the mapper switches on and were measured live, not guessed. Inventing
them would test nothing.

| File | Shaped to pin |
|---|---|
| `movie_item.json` | the ordinary movie; a Dolby Vision profile-8.1 stream; two audio streams where the **second** is `IsDefault` (so a mapper that took the first would report `aac`/2 instead of `truehd`/8) |
| `series_item.json` | `RunTimeTicks: null`, no `MediaSources` key at all (so a series yields no stream target), `UnplayedItemCount` |
| `episode_item.json` | an episode carrying **its own** provider ids rather than its series' — the payload fact behind "an episode never walks the match ladder" |
| `multi_version_movie.json` | three `MediaSources`, the first transcode-only (`Protocol: Http`, `Size: 0`, `SupportsDirectPlay: false`), so `primary_media_source`'s selection rule has something to select |

`multi_version_movie.json` has been looked for twice against the live server,
over disjoint slices totalling 1,400 movies, and has never met a real
payload — every one carried exactly one `MediaSource`. It stays: another
deployment will have them and the rule is cheap. `CLAUDE.md` records both
searches.

## Regenerating

Never paste a capture in. `scripts/capture_emby_fixture.py` replaces every
leaf with the name of its type, so its output is a shape to diff against, not
a fixture to commit:

```bash
export USHER_EMBY_URL=... USHER_EMBY_USER=... USHER_EMBY_PASSWORD=...
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json
```

Diff that against the committed file and hand-add any missing **key** with an
invented value.

`tests/fakes/emby_server.py` uses these files as templates and overwrites the
identity fields from the seeded `SourceItem`, so the contract suite is not
coupled to the values here — only `tests/unit/test_adapters_emby_mapping.py`
and `tests/unit/test_adapters_emby_playback.py` read them directly.
