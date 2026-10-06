# Send to a screen: design

**Status:** Proposed.
**Date:** 2026-10-06.
**Companion:** the home panel's Watch screen, being drawn in the Home Panel Design
System project, is this lane's first client. It assumes everything below.

## What the owner asked for

Find a film on the hall wall, and play it on whichever screen in the house the
owner picks, in the right app, from the resume point, with nobody touching a
remote. The decisions, as the owner made them:

1. **Who sends.** The panel now; Alfred by voice and Usher Console next. **The
   goal is anything in the house**, automations included.
2. **Detection.** Automatic, then confirmed once: discovery proposes, the owner
   confirms what is ambiguous, changes after that are tracked on their own.
3. **Choosing a way to play.** The best quality, automatically. **Every option is
   shown, not one level deep**: every screen, device, app and copy, the best one
   marked and pre-selected.
4. **Hands-free.** Wanted. A way that needs a press at the TV is allowed only if
   it says so.
5. **Home Assistant stays the registry.** Usher must not fight HA's entities or
   keep a second copy of them.

## What the house exposes, probed 2026-10-06

One screen, the Living Room.

| Entity | Integration | Area | What HA can do with it |
|---|---|---|---|
| `media_player.living_room_living_room_apple_tv` + `remote.…` | `apple_tv` | Living Room | Reports `app_id`, `app_name`, title, position (it read the app, the episode playing and its position). Opens a URL through `play_media`. Wakes. **No `source_list`**, so HA cannot say which apps are installed |
| `media_player.shield` | `cast` | **none** | Casts a URL. No app launch |
| `media_player.bedroom` | `music_assistant` | Living Room | The same Apple TV, as MA's AirPlay speaker. Model string `Apple TV 4K Gen3` against the other's `Apple TV 4K (gen 3)` |
| `media_player.lg_webos_tv_oled65c3pua_2`, `media_player.shield_2` | `music_assistant` | Living Room | Speakers only |
| `media_player.lg_webos_tv_oled65c3pua` | not `webostv` | — | `unavailable`. **No webOS integration exists**: HA cannot power the LG or switch its input |
| `media_player.playstation_5` | — | Living Room | `unavailable`, `supported_features: 0` |

**Prior art.** The Emby library card in HA already plays on the Apple TV by
calling `media_player.play_media` with `media_content_type: url` and
`infuse://x-callback-url/play?url={stream}&position=`. tvOS asks "Open Infuse?"
every time. That construction moved into Usher in M3 (`wrap_deep_link`); this
spec moves the *sending* here too.

**Usher already knows each copy.** `media_items` stores `container`,
`video_codec`, `audio_codec`, `width`, `height`, `hdr_format`, `audio_channels`
and `runtime_seconds` from ingest, so ranking the ways to play never calls the
source.

## Where it lives, and why

**In Usher, behind a new `Players` port, with Home Assistant as its adapter.**
PRD 00 already says playback "is delegated to a source or a player" and that
Usher is Alfred's "action surface"; this is the player half. The facts about a
file and the facts about a device meet in one tested place, and every client
(the panel, Console, Alfred, and through a thin wrapper HA itself) gets one API.

Two alternatives were rejected. **A custom HA integration** gets automations on
day one, but Console could not use it without HA credentials, the ranking would
be rewritten in HA-side Python away from the facts it ranks, and every change
would deploy by restarting HA. **The panel alone** is fastest to a first slice
and is thrown away the moment Alfred needs it.

This changes PRD 01's shape: Home Assistant was only a consumer, and it becomes
an adapter as well. PRD 01's port list and PRD 07's playback section are amended
in slice 1.

## HA is the registry; Usher is a lens

1. **HA owns every fact about a device**: that it exists, its name, its area,
   power, current app, what is playing. **Usher has no device table.** It holds
   one websocket, keeps registries and states in memory, and recomputes its view
   on every event. A rename or a move in HA shows on the next event; there is
   nothing to sync.
2. **Usher stores only what HA has no concept of**: how to hand an app a URL,
   what a device and app can direct-play, and the ranking. That is code, keyed by
   HA's own identifiers (integration, device model, `app_id`).
3. **Confirming writes to HA, not to Usher.** HA labels carry the decision:
   `usher` on the entity Usher should drive, `usher_ignore` on one it should
   not. A device with no area is given one in HA's device registry. Every other
   dashboard benefits from the same fix.
4. **Usher acts only through HA services.** It never opens its own pyatv or Cast
   connection, which would fight HA's for the same device. Confirmation reads
   HA's state: if HA says the TV is off, it is off.

## Components

| Unit | Does | Depends on |
|---|---|---|
| `ports/players.py`, **Players** | Snapshot of areas, devices, entities, labels and states; call a service; wait for a state predicate with a deadline; write a label or an area. No HA vocabulary in the interface | — |
| `adapters/homeassistant/` | One websocket: `auth`, `config/area_registry/list`, `config/device_registry/list`, `config/entity_registry/list`, `subscribe_events` for `state_changed` and the three registry-updated events, `call_service`, `config/entity_registry/update` (labels), `config/device_registry/update` (area). Reconnects with backoff and re-reads the registries on reconnect | `USHER_HA_URL`, `USHER_HA_TOKEN` |
| `domain/screens.py`, **projection** | Pure: snapshot → screens. Recomputed on every event, never stored | snapshot |
| `domain/recipes.py`, **recipes** | Code table keyed by (integration, model family, app). Each recipe states how the URL is handed over, its direct-play profile, how it resumes, whether it is hands-free, its stage predicates, and whether each fact is `measured` or `assumed` | — |
| `services/send.py`, **ranker** | Copies × the screen's devices × matching recipes → every way, ranked | `media_items`, projection, recipes |
| `services/send.py`, **dispatcher** | Runs a way's stages through the port, publishes each one, holds sends in memory | port, playback service, event bus |
| API | `GET /screens`, `POST /screens/devices/{entity_id}/confirm`, `GET /titles/{id}/ways`, `GET /episodes/{id}/ways`, `POST /titles/{id}/send`, `POST /episodes/{id}/send`, `GET /sends/{id}`, and `send.stage` on `/events` | — |

`USHER_PLAYERS_ENABLED=false` is a supported deployment: the routes answer 503
`players_unavailable` and nothing connects to HA.

The existing `POST /titles/{id}/play` and its `deep_link` target are unchanged.
`/ways` is a separate read because `/play` mints credential-bearing tickets, and
showing a list must not.

## The projection

- **A candidate** is a `media_player` entity whose integration has at least one
  recipe. Music Assistant's players have none, so `media_player.bedroom` is never
  a candidate and the duplicate Apple TV needs no special case.
- **A device** is a candidate labelled `usher`. **Unconfirmed** is a candidate
  with neither label. **Ignored** is `usher_ignore`.
- **A screen** is an HA area holding at least one device. A labelled device with
  no area is listed under `NO AREA`, with confirm offering an area.
- Each device in `GET /screens` carries its live state from HA: power, `app_id`
  and app name, what is playing and its position. That is HA's reading, passed
  through rather than reinterpreted.

`POST /screens/devices/{entity_id}/confirm {use: bool, area_id?}` writes the label
(and the area when given) to HA, then returns the projection as it stands after
HA's registry event arrives.

## Ranking, all options visible

`GET /titles/{id}/ways?screen=` (screen optional; omitted means every screen):

1. Read the title's available copies from `media_items`.
2. For each copy × device on the screen × recipe matching that device, test the
   copy against the recipe's profile. The outcome is a **way**, or a
   **can't-play** carrying the reason (`CAN'T · MKV OVER AIRPLAY`,
   `CAN'T · NO DOLBY VISION OVER CAST`).
3. Score each way: resolution, then HDR (Dolby Vision > HDR10+ > HDR10 > SDR,
   counting only what survives the recipe), then audio (lossless object audio >
   lossless > lossy multichannel > stereo), then container fidelity.
4. **Order:** hands-free ways by score, then ways that need a press by score,
   then can't-play entries. Within equal scores, `measured` ranks above
   `assumed`. The first way is `best`.
5. Every way states what it gives up against the best one, in words
   (`1080P · NO DOLBY VISION`), and carries `hands_free`, `measured`, the recipe
   and the copy.

A way's id is stable over (screen, entity, recipe, copy), so a client can send
the way it showed.

## Sending

`POST /titles/{id}/send {way, take_over?}` answers `202 {send_id}` at once, or:

- `409 screen_busy` when the device is playing something else and `take_over`
  is not set. The body says what (`EXAMPLE PLAYER · EXAMPLE EPISODE`).
- `409 way_not_playable` for a can't-play entry.
- `503 players_unavailable` when HA is not answering.

Then the dispatcher runs the way's stages. Each has its own deadline, taken from
the recipe and labelled `assumed` until measured.

| Stage | Action through HA | Confirmed when HA reports |
|---|---|---|
| `WAKING` | `remote.turn_on` or `media_player.turn_on` on the device. CEC brings up the LG | device state is not `off` or `standby` |
| `OPENING` | Mint the ticket (`adapter.stream_targets`), build the recipe's URL, `media_player.play_media` | `app_id` is the recipe's app |
| `ACCEPTING` | Infuse only: `remote.send_command select` once the prompt is up | — (seen at the next stage) |
| `STARTING` | — | `state: playing`, a duration within 2% of the copy's runtime, position near the resume point |
| `PLAYING` | done | — |

Each transition is published as `send.stage {send_id, screen, stage, at, detail}`
on `/events`, so the wall, the phone, Console and Alfred watch one send. `GET
/sends/{id}` serves a client that joins late.

The ticket is minted at `OPENING`, not at request time, so its five-minute life
never races a slow wake.

## Recipes for this house

| Recipe | Handover | Profile | Resume | Hands-free |
|---|---|---|---|---|
| Apple TV 4K · Infuse (`com.firecore.infuse`) | `play_media`, type `url`, `infuse://x-callback-url/play?url=<ticket>&position=` | MKV, MP4 · HEVC, H.264 · Dolby Vision profiles 5 and 8.1, HDR10, HDR10+ · TrueHD Atmos, DTS-HD · up to 4K | `position=` | needs `select` on the prompt, spike 1 |
| Apple TV 4K · AirPlay | `play_media`, type `url`, the ticket itself | MP4, MOV · HEVC, H.264 · HDR10, Dolby Vision in MP4 · AAC, AC-3, E-AC-3 | start position | yes |
| SHIELD · Cast | `play_media` on the `cast` entity, the ticket and its MIME type | MKV, MP4 · HEVC, H.264 · HDR10 · no Dolby Vision · audio unknown | seek once playing | yes |

Every profile fact starts `assumed`. The LG and the PS5 have no recipe: the LG is
reached through CEC from whichever source is woken.

## Failures

- **HA down never takes Usher down.** `/health/ready` does not consult HA.
  Catalog, search and `/play` keep working; `/screens` and `send` answer 503
  `players_unavailable` with `HOME ASSISTANT IS NOT ANSWERING`; `/ways` still
  ranks copies and lists no screens.
- **One send per screen.** A second send supersedes the first, which ends
  `SUPERSEDED`.
- **Never fight the house.** If the device turns to something else mid-send (an
  app opened by hand, an automation turning the TV off), the send ends with what
  happened (`DIDN'T TAKE · SOMEONE OPENED NETFLIX`). It is never retried in a
  loop.
- **Every timeout names its stage and the cause**:
  `DIDN'T TAKE · WAKING · APPLE TV STAYED ASLEEP`,
  `DIDN'T TAKE · ACCEPTING · INFUSE PROMPT NOT ACCEPTED`.
- **Sends live in memory.** They last under a minute. A restart or a deploy
  mid-send closes the event stream and clients fall back to reading HA. Accepted
  and stated rather than persisted.
- **Windows become measured by use.** Each stage's duration is recorded as a
  metric per recipe, which is how a recipe's `assumed` deadlines are replaced.
- New problem codes: `players_unavailable` (503), `screen_busy` (409),
  `way_not_playable` (409). `source_unavailable` (503) is reused for a source
  that cannot mint the ticket.

## Testing

- **The port's contract suite runs against recorded frames from this house**,
  never against the adapter's own expectations. That is the M3 failure: the fake
  Emby implemented the adapter's guess, and the suite passed against a
  write-back that had never worked. Record the registries, a `state_changed`, a
  service call and a label update once, commit them as fixtures, and run the
  suite against both the fake and the recordings.
- **Projection.** Pure tests over snapshots of this house: the MA duplicate, the
  SHIELD with no area, the LG with no integration, unlabelled, labelled and
  ignored entities, a device moved between areas.
- **Ranker.** Tables over every recipe against real `media_items` shapes: a Dolby
  Vision MKV, an HDR10 MP4, an H.264 file with no stream metadata, a copy with
  unknown codecs.
- **Dispatcher.** A scripted fake on a controlled clock: every stage succeeding,
  every stage timing out, supersede, take-over, the device grabbed mid-send, HA
  dropping mid-send.
- **Live acceptance is owner-gated, at the TV.** Ten sends to the Living Room
  from off, from standby and from another app, results posted to the ticket.

## Slices

| Slice | Delivers | Needs |
|---|---|---|
| 0 | The four spikes below | nothing; can run now |
| 1 | HA adapter, projection, `GET /screens`, confirm, `/ways`, PRD 01 and 07 amended | — |
| 2 | `send` for the best proven recipe, stages on `/events`, take-over, supersede | slice 0 |
| 3 | The remaining recipes; windows measured | slice 2 in use |
| 4 | HA `usher.play` (a `rest_command` and script, or a small integration) and an Alfred tool | slice 2 |

After slice 1 the panel draws the real screen picker and every way to play
without playing anything.

### The spikes

1. **Infuse auto-accept.** When does the prompt appear after `play_media`, and
   does `remote.send_command select` take it reliably across ten sends from off,
   standby and another app? If not, the Infuse recipe is `NEEDS ONE PRESS` and
   ranks below the hands-free ways, as agreed.
2. **The wake chain.** Does waking the Apple TV bring the LG up on the right
   input over CEC, and in how long? That is `WAKING`'s first measured deadline.
3. **What Cast on the SHIELD plays.** A 4K HEVC HDR10 MKV, a Dolby Vision file,
   TrueHD audio.
4. **Infuse detection.** Does re-pairing the Apple TV's Companion protocol give
   HA a `source_list`? If not, an app is assumed installed until a send shows
   otherwise, and a failed `OPENING` says `INFUSE MAY NOT BE INSTALLED`.

Optional HA changes, each its own small step and none required: the `webostv`
integration (real TV power and input state, a deadline for the LG itself) and
`androidtv_remote` (app launch on the SHIELD, which would admit an Emby or Kodi
recipe).

## Open questions

- **How the panel reaches Usher.** The panel runs in HA's origin and Usher
  answers on :8100. Direct calls need CORS for that origin. The alternative is a
  proxy through HA. Settled in slice 1's plan.
- **Whose resume point.** Usher resolves the household's default user today. A
  per-person resume is out of scope until Usher has more than one user.
