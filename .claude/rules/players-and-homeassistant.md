---
paths:
  - "src/usher/adapters/homeassistant/**"
  - "src/usher/adapters/websocket.py"
  - "src/usher/ports/players.py"
  - "src/usher/domain/house.py"
  - "src/usher/domain/recipes.py"
  - "src/usher/domain/screens.py"
  - "src/usher/domain/ways.py"
  - "src/usher/services/screens.py"
  - "src/usher/services/ways.py"
  - "scripts/capture_ha_shape.py"
  - "tests/fakes/house.py"
  - "tests/fakes/home_assistant.py"
  - "tests/fakes/home_assistant_rows.py"
  - "tests/fakes/players.py"
  - "tests/contract/players_contract.py"
  - "tests/fixtures/homeassistant/**"
---

# Players and Home Assistant

Home Assistant owns every area, device and player. Usher holds one websocket
and an in-memory `HouseSnapshot`, and persists nothing about a device. The
behaviour is PRD 07's `## Players`; the design is
`docs/specs/2026-10-06-send-to-screen-design.md`.

## Commands

```bash
# Every test file touching these paths, by name: a `-k` word selects rows and search too.
uv run pytest tests/unit/test_{domain_{house,recipes,screens,ways},services_{screens,ways}}.py \
  tests/unit/test_adapters_{homeassistant_{mapping,session,players},websocket,emby_push}.py \
  tests/unit/test_{players_contract,fixtures_homeassistant,{config,composition}_players}.py \
  tests/unit/test_{api_{screens,ways,problem},ports,outbound_call_sites}.py
# Read-only: key paths and type names, never a value. Capture while a player plays.
set -a; . ./.env; set +a
uv run python scripts/capture_ha_shape.py --listen 30 \
  --merge tests/fixtures/homeassistant/shape.json > /var/tmp/ha-shape.json
```

## Writing to HA

- **Usher writes three things: its two labels into HA's label registry when
  missing (`Usher` and `Usher ignore`, ids `usher` and `usher_ignore`), one of
  them on an entity, and `area_id` on a device.** No service call until slice 2.
- **`config/entity_registry/update`'s `labels` replaces the whole list.** Read
  the entry, change only Usher's two, write the rest back. An entry that comes
  back with no label list is refused before the entry is written; creating a
  missing Usher label in the label registry comes first.
- **Labels are created under one lock, and the id HA answers is checked**, so
  two confirms cannot create `usher` twice or adopt a label under another id.
- **Label and area writes are admin-only in HA.** A non-admin token is refused
  per write with `unauthorized`, which is `PortAuthFailed`.
- **An entity's label or a device's area is done when HA's registry event
  reports it**, not when the command is answered; confirm waits for both with
  `USHER_PLAYERS_CONFIRM_TIMEOUT_SECONDS`. A created label is done when HA's
  answer carries the expected id, since `label_registry_updated` is not
  subscribed.
- **An entity's own `area_id` overrides its device's**, so confirm places only a
  player whose entity has none, and only one in use: an ignored player is never
  placed.
- **The label is written before the area.** An area write HA refuses leaves the
  label, which lists the player under `no_area`; confirming again is safe.

## Reading from HA

- **Subscribe before reading, and number every frame at the one reader.** A
  registry event numbered after the first registry answer forces one more
  read, and further ones coalesce into it. A state event numbered after the
  states answer applies.
- **A row that does not read is dropped and counted at WARNING.** The sync
  never aborts on one.
- **`get_states` outgrows `websockets`' 1 MiB default** in a house with many
  integrations, which closes the socket on every sync. HA's connector sets
  16 MiB.
- **No connection, no house.** `snapshot()` and a write raise why the last
  connection ended (`PortUnavailable` before one has) rather than serve a
  reading that has stopped updating. A crash keeps no reason: its message is
  not the port's.
- **A refused token is not retried.** `auth_invalid` ends the loop until Usher
  restarts, with one ERROR naming `USHER_HA_TOKEN`: the same token never
  succeeds, and HA's `login_attempts_threshold` bans an address that keeps trying.
- **A failed re-read closes the connection**, and the next one syncs from
  scratch. The one warning says Usher closed it, not HA.
- **A session stops for one reason, the first.** A command still in `send`
  when the reader ends is failed at once, not at its timeout.
- **An error names HA's code, never HA's message**, and the token travels only
  in the `auth` frame.

## Fixtures and fakes

- **`tests/fixtures/homeassistant/shape.json` holds key paths and type names,
  never a value.** `tests/fakes/home_assistant_rows.py` renders a
  `HouseSnapshot` with every recorded key, and both directions are checked.
- Synthetic values only: MACs in `02:00:00:…`, IPv4 in `192.0.2.0/24`, and
  entity ids already printed by the spec, or invented and absent from the house.
- **`FakePlayers.reports_writes = False` is how a test holds a write unreported**;
  publish the next house by hand to release it. `refuse` refuses every write,
  `refuse_area` only the area, each raising the `UsherPortError` it holds: that
  is how a half-write is held.
- **A test that dials loopback for real passes `proxy=None`, or clears every
  variable whose name ends in `_proxy`, in any case**, or a proxy in the
  environment answers the dial instead. `websockets` reads `ws`, `wss` and
  `socks` proxies from `urllib.request.getproxies()` beside `http` and `https`.

## Naming

- **`proven`, never `measured`, for a recipe fact.** `guard_prose.py` refuses
  the word in Python prose, and a field its own docstring cannot name drifts.
