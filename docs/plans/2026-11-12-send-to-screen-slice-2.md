# Send to a screen, slice 2: the send — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /titles/{id}/send` and `POST /episodes/{id}/send` play a chosen way on a screen through Home Assistant, hands-free, and every stage of the send is visible on `/events` and at `GET /sends/{id}`.

**Architecture:** The `Players` port gains two acts, `wake` and `play_url`, which the Home Assistant adapter makes as service calls. A request-scoped `SendService` turns a way id into an order (checks, the copy's source URL, the resume point, a link that mints a fresh ticket each time it is called). A process-wide `SendDispatcher` on `app.state.sends` runs the order's stages as one task per screen, holds sends in memory, and publishes `send.stage`. Nothing is persisted and there is no migration.

**Tech Stack:** Python 3.13, FastAPI, pydantic, OpenTelemetry metrics, pytest; the console's TypeScript problem table.

**Spec:** `docs/specs/2026-10-06-send-to-screen-design.md` (`## Sending`, `## Failures`, `## Testing`, `## Slices`). Read it with the departures below: the spec predates the spikes at the TV, and `.claude/rules/players-and-homeassistant.md`'s `## At the TV` holds what they found.

### Departures from the spec, each from a spike at the TV

| Spec says | This plan does | Why |
|---|---|---|
| `WAKING` is confirmed when the device state is not `off` or `standby` | `WAKING` wakes the player, then hands the link over every `retry_seconds` until HA accepts it | HA's reading does not change on the wake, and a sleeping Apple TV refuses the handover |
| `ACCEPTING` sends `select` at Infuse's prompt | No such stage | Infuse opens a handed-over link with no prompt and no press |
| `OPENING` is confirmed when `app_id` is the recipe's | Confirmed by a reading that differs from the one at the handover and shows the recipe's app | Infuse may already be in front, playing something else |
| `STARTING` checks the position is near the resume point | Checks `playing` and the duration only | The position has not been read at the TV right after a start |
| A device turning to something else ends the send at once | The send ends at its stage's deadline, naming what the player shows then | HA reported another app for half a second in the middle of a send that then played |
| `send` for the best proven recipe; every recipe is unproven | The Infuse recipe becomes `proven`, and only a proven recipe is sent through | Fourteen sends at the TV played through it; AirPlay and Cast have never been sent |
| The LG is woken over CEC and its input checked | Nothing reads or drives the LG | The owner ruled it out: Usher talks only to the Apple TV |

## Global Constraints

- Usher acts only through Home Assistant's services, over the one websocket `HomeAssistantPlayers` already holds. It never opens its own connection to a device.
- The port's interface carries no Home Assistant vocabulary: `wake` and `play_url`, never `turn_on`, `play_media` or `call_service`.
- A ticket URL, the link wrapping it and a source URL are credentials. None may reach a log line, an exception message, an event payload, a response body or a `Send`.
- The ticket is minted when the link is handed over, never when the request is answered.
- Once HA accepts a handover, that send never hands over again. No stage is retried in a loop.
- One send per screen: a second send to a screen supersedes the first, which ends `superseded`.
- Sends live in memory only. No table, no migration, no setting.
- Home Assistant down never takes Usher down: `/health/ready` does not consult it, and `/play` and `/ways` keep answering.
- With `USHER_PLAYERS_ENABLED=false` the three new routes answer `503 players_unavailable` and nothing connects.
- Every printed line is uppercase and joined with ` · ` (U+00B7 between spaces), as `/ways`' refusals are.
- `proven`, never `measured`, for a recipe fact; `guard_prose.py` refuses the word in Python prose.
- Test values are synthetic: entity ids the spec already prints or invented ones, TMDb ids at or above 90,000,000, IMDb ids in `tt99…`.
- Ports are `abc.ABC`. `services/` imports only `domain/`, `ports/` and other services. Domain models are frozen.
- The PRD changes in the same commit as the behaviour. The gate (`CLAUDE.md`, `### The gate`) is green before every commit; Task 7 also runs `npm run verify`, `npm run e2e` and `npm run e2e:visual` from `web/`.
- Narrow runs while iterating: `uv run pytest <the task's test files> -p no:randomly`. The whole gate once per task, before its commit.

## Review Focus

1. **A dispatcher task that raises something unexpected.** A person expects the send to end `didnt_take` and the screen to take the next send, never a send stuck in `waking` forever. Pinned in Task 4, `test_a_crash_ends_the_send_and_frees_the_screen`.
2. **The link reaching somewhere it is read.** A person expects no ticket in `/events`, `GET /sends/{id}` or the log. Pinned in Task 2 (`test_a_refused_handover_names_the_code_and_never_the_url`) and Task 4 (`test_the_link_reaches_the_player_and_nothing_else`).
3. **A double tap: the same way sent twice within a second.** A person expects one film to start, the first send to read `superseded`, and both ids to answer at `GET /sends/{id}`. Pinned in Task 4, `test_a_second_send_to_the_screen_supersedes_the_first`.
4. **The player vanishing from HA's states mid-send** (the integration reloads, the entity goes `unavailable` or is removed). A person expects `DIDN'T TAKE` naming the player as off, not an `AttributeError` on a missing reading. Pinned in Task 3 (`showing` over `None`) and Task 4 (`test_a_player_that_vanishes_reads_as_turned_off`).
5. **Infuse already playing something else when the send lands.** A person expects `playing` only once the new item plays, not the instant the old one reports its position. Pinned in Task 3 (`started` on the 2% boundary) and Task 4 (`test_the_old_item_still_playing_is_not_the_send_playing`).

---

### Task 1: A recipe carries its windows, and Infuse is proven

**Files:**
- Modify: `src/usher/domain/recipes.py`
- Modify: `docs/prd/07-client-api.md` (`## Players`, the sentence ending "every recipe is `proven: false` until a send has shown it")
- Test: `tests/unit/test_domain_recipes.py`, `tests/unit/test_domain_ways.py`

**Interfaces:**
- Produces: `Windows(waking_seconds: float, retry_seconds: float, opening_seconds: float, starting_seconds: float)`, a frozen slotted dataclass; `Recipe.windows: Windows`; the Infuse recipe with `proven=True`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_domain_recipes.py
def test_the_infuse_recipe_is_proven_and_no_other_is() -> None:
    assert [recipe.key for recipe in RECIPES if recipe.proven] == ["apple_tv.infuse"]

def test_infuse_waits_as_long_as_the_spike_showed_a_wake_takes() -> None:
    windows = next(r for r in RECIPES if r.key == "apple_tv.infuse").windows
    assert windows == Windows(
        waking_seconds=45.0, retry_seconds=2.0, opening_seconds=20.0, starting_seconds=30.0
    )

def test_every_window_is_positive_and_a_retry_fits_inside_the_wake() -> None:
    for recipe in RECIPES:
        w = recipe.windows
        assert min(w.waking_seconds, w.retry_seconds, w.opening_seconds, w.starting_seconds) > 0
        assert w.retry_seconds < w.waking_seconds

# tests/unit/test_domain_ways.py -- an MP4 both Apple TV recipes play, at an equal score
def test_the_proven_recipe_is_best_where_two_score_alike() -> None:
    ways = rank([living_room], [mp4_hevc_aac_copy])
    assert ways[0].quality.score == ways[1].quality.score          # the premise
    assert [way.recipe.key for way in ways[:2]] == ["apple_tv.infuse", "apple_tv.airplay"]
    assert ways[0].best
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/unit/test_domain_recipes.py tests/unit/test_domain_ways.py -p no:randomly`; expected: `ImportError: Windows`, and the ranking case failing with AirPlay first (its key sorts first).

- [ ] **Step 3: Add `Windows` and `Recipe.windows`; set Infuse `proven=True`.** AirPlay and Cast take the same four values and stay `proven=False`. `Windows`' docstring says each value is assumed until the stage metric of Task 4 shows otherwise, and that the wake's 45 s covers the 30 s a wake from off took at the TV. Replace the Infuse recipe's `# Spike 1 …` comment with one line saying the sends at the TV proved the handover and the hands-free claim.

- [ ] **Step 4: Repair every case a proven Infuse makes false.** Cases in `test_domain_ways.py`, `test_services_ways.py`, `test_api_ways.py` and `test_api_screens.py` may pin `proven: false` for Infuse or an order that depended on the tie. Repair the premise, not the assertion. The console's fixtures are hand-written and stay as they are.

- [ ] **Step 5: PRD 07.** Replace the sentence with: "On an equal score a `proven` recipe ranks first. Infuse on the Apple TV 4K is proven; every other recipe is `proven: false` until a send has shown it."

- [ ] **Step 6: Gate, then commit** — `recipes: a recipe carries its windows, and Infuse on the Apple TV is proven`

---

### Task 2: The port wakes a player and hands it a URL, on both arms

**Files:**
- Modify: `src/usher/ports/players.py`, `src/usher/adapters/homeassistant/players.py`
- Modify: `tests/fakes/players.py`, `tests/fakes/home_assistant.py`, `tests/contract/players_contract.py`
- Modify: `.claude/rules/players-and-homeassistant.md` (`## Writing to HA`: the bullet ending "No service call until slice 2." now names the two calls; add one bullet saying a handover's URL is a credential and an error names HA's code only. Displace, do not grow: the file is at its target)
- Test: `tests/unit/test_adapters_homeassistant_players.py`, `tests/unit/test_players_contract.py`

**Interfaces:**
- Produces, on `Players`:
  - `async def wake(self, entity_id: str) -> None` — ask the player to wake. Returns when the registry has taken the request, which is not when the player is awake.
  - `async def play_url(self, entity_id: str, url: str) -> None` — hand the player a URL to open. `PortDataMalformed` when the registry refuses, which is what a sleeping player looks like; `PortUnavailable` with no connection or no answer in time; `PortAuthFailed` for the credential. The URL appears in no error.
- Produces, on `FakePlayers`: `calls: list[tuple[str, str, str]]` (`("wake", entity_id, "")`, `("play_url", entity_id, url)`); `refuse_plays: list[UsherPortError]`, each `play_url` popping and raising the first; `on_play: Callable[[str, str], None] | None`, run after an accepted `play_url`; `drop() -> None`, which leaves no house and wakes every waiter. `refuse` refuses `wake` and `play_url` as it refuses every write. Both new methods `await asyncio.sleep(0)` first.
- Produces, on `FakeHomeAssistant`: a `call_service` handler answering success; `service_calls`, a property over the existing `commands` answering each `call_service` frame without its `id`; `refuse_services: list[str]`, each `call_service` popping the first code and answering `_refusal(ident, code)`. The existing `refused["call_service"]` still refuses every one.
- Produces, on `PlayersHarness`, three abstract methods both arms implement: `service_calls() -> list[tuple[str, str]]` (`("wake" | "play_url", entity_id)`, the HA arm mapping `turn_on` and `play_media` to those two words), `refuse_next_handover() -> None`, and `async lose_the_house() -> None` (the fake's `drop()`; the HA arm drops its connection and waits until `snapshot()` raises).

- [ ] **Step 1: Write the failing contract cases** in `PlayersContract`. `PLAYER` is `APPLE_TV` from `tests/fakes/house.py`:

```python
async def test_a_wake_reaches_the_registry(self, harness) -> None:
    await harness.players.wake(PLAYER)
    assert harness.service_calls() == [("wake", PLAYER)]

async def test_a_handover_reaches_the_registry(self, harness) -> None:
    await harness.players.play_url(PLAYER, URL)
    assert harness.service_calls() == [("play_url", PLAYER)]

async def test_a_refused_handover_is_malformed_and_a_later_one_lands(self, harness) -> None:
    harness.refuse_next_handover()
    with pytest.raises(UsherPortError) as caught:
        await harness.players.play_url(PLAYER, URL)
    assert isinstance(caught.value, PortDataMalformed)
    await harness.players.play_url(PLAYER, URL)
    assert harness.service_calls() == [("play_url", PLAYER), ("play_url", PLAYER)]

async def test_with_no_house_neither_act_is_made(self, harness) -> None:
    await harness.lose_the_house()
    for act in (harness.players.wake(PLAYER), harness.players.play_url(PLAYER, URL)):
        with pytest.raises(PortUnavailable):
            await act
    assert harness.service_calls() == []
```

A refused handover counts as a call the registry saw on both arms, so the third case's two entries are the refused one and the accepted one.

- [ ] **Step 2: Write the failing adapter cases** in `tests/unit/test_adapters_homeassistant_players.py`:

```python
async def test_wake_asks_the_media_player_to_turn_on(...) -> None:
    await players.wake(PLAYER)
    assert server.service_calls == [{
        "type": "call_service", "domain": "media_player", "service": "turn_on",
        "target": {"entity_id": PLAYER},
    }]

async def test_play_url_hands_the_url_over_as_a_url(...) -> None:
    await players.play_url(PLAYER, URL)
    assert server.service_calls == [{
        "type": "call_service", "domain": "media_player", "service": "play_media",
        "service_data": {"media_content_type": "url", "media_content_id": URL},
        "target": {"entity_id": PLAYER},
    }]

async def test_a_refused_handover_names_the_code_and_never_the_url(...) -> None:
    server.refuse_services.append("home_assistant_error")
    with pytest.raises(PortDataMalformed) as caught:
        await players.play_url(PLAYER, URL)
    assert str(caught.value) == "Home Assistant refused call_service: home_assistant_error"
    chain = [caught.value, caught.value.__cause__, caught.value.__context__]
    assert all(URL not in repr(link) and "TICKET" not in repr(link) for link in chain)
    assert server.service_calls[0]["service_data"]["media_content_id"] == URL   # the premise: it was sent
```

`URL` is `"infuse://x-callback-url/play?url=https%3A%2F%2Fusher.test%2Fstream%2FTICKET%2FName"`. No log sink here: the adapter logs nothing on a refused command, so an empty sink would prove nothing. The log is pinned in Task 4, where a line is written.

- [ ] **Step 3: Run and see them fail** — `uv run pytest tests/unit/test_players_contract.py tests/unit/test_adapters_homeassistant_players.py -p no:randomly`; expected: `TypeError: Can't instantiate abstract class`.

- [ ] **Step 4: Implement** the two abstract methods, the fake's and the fake server's additions, and `HomeAssistantPlayers.wake`/`play_url` as `self._live().command("call_service", …)` with the frames above. No log line of its own.

- [ ] **Step 5: Gate, then commit** — `players: the port wakes a player and hands it a URL, and an error never carries the URL`

---

### Task 3: A send, its stages, and what a reading says about it

**Files:**
- Create: `src/usher/domain/sends.py`
- Modify: `.claude/rules/players-and-homeassistant.md` (`paths:` gains `src/usher/domain/sends.py` and `src/usher/services/send.py`; its test command gains `test_domain_sends.py`, `test_services_send{,_dispatcher}.py` and `test_api_send.py`) and `CLAUDE.md`'s rules table row for that file, to match
- Test: `tests/unit/test_domain_sends.py`

**Interfaces:**
- Consumes: `HouseState` (`domain/house.py`), `Recipe` (`domain/recipes.py`).
- Produces:

```python
class SendStage(StrEnum):
    WAKING = "waking"; OPENING = "opening"; STARTING = "starting"
    PLAYING = "playing"; DIDNT_TAKE = "didnt_take"; SUPERSEDED = "superseded"

FINISHED: Final = frozenset({SendStage.PLAYING, SendStage.DIDNT_TAKE, SendStage.SUPERSEDED})

@dataclass(frozen=True, slots=True)
class StageMark:
    stage: SendStage
    at: datetime
    detail: str | None = None

@dataclass(frozen=True, slots=True)
class Send:
    id: uuid.UUID
    title_id: uuid.UUID            # an episode's series: the events' filter key
    episode_id: uuid.UUID | None
    way_id: uuid.UUID
    area_id: str
    screen_name: str
    entity_id: str
    device_name: str
    recipe_key: str
    marks: tuple[StageMark, ...]   # never empty; the last is where the send stands
    failed_stage: SendStage | None = None

    @property
    def stage(self) -> SendStage: ...          # marks[-1].stage
    @property
    def detail(self) -> str | None: ...        # marks[-1].detail
    @property
    def finished(self) -> bool: ...            # stage in FINISHED
    def moved(self, stage: SendStage, at: datetime, *, detail: str | None = None,
              failed_stage: SendStage | None = None) -> "Send": ...

def busy_with(state: HouseState | None) -> str | None
def opened(now: HouseState | None, *, at_handover: HouseState | None, app_id: str | None) -> bool
def started(now: HouseState | None, *, at_handover: HouseState | None, app_id: str | None,
            runtime_seconds: int | None) -> bool
def showing(state: HouseState | None, *, device_name: str, recipe: Recipe) -> str
def didnt_take(stage: SendStage, cause: str) -> str

STAYED_ASLEEP: Final = "{device} STAYED ASLEEP"
MAY_NOT_BE_INSTALLED: Final = "{app} MAY NOT BE INSTALLED"
NOT_ANSWERING: Final = "HOME ASSISTANT IS NOT ANSWERING"
USHER_FAILED: Final = "USHER FAILED"
```

The rules the signatures do not settle:

- `busy_with`: `None` unless `state.state == "playing"`; then the app name and the media title that are present, uppercased and joined with ` · `; `"SOMETHING ELSE"` when neither is.
- `opened`: a reading that is not `None`, differs from `at_handover`, and whose `app_id` is the recipe's. A recipe with `app_id=None` asks only that the reading differs.
- `started`: `opened`, and `state == "playing"`, and the reading's duration within 2% of `runtime_seconds`. A duration or a runtime that is unknown agrees.
- `showing`, first match: no reading, or a state of `off`, `standby` or `unavailable` → `"{DEVICE} TURNED OFF"`; an `app_id` that is not the recipe's, with an app name → `"SOMEONE OPENED {APP NAME}"`; otherwise `"{RECIPE NAME} DID NOT START PLAYING"`.
- `didnt_take(SendStage.WAKING, "X")` is `"DIDN'T TAKE · WAKING · X"`.
- `moved` refuses (`ValueError`) a move from a finished send, and a `failed_stage` on anything but `DIDNT_TAKE`.

- [ ] **Step 1: Write the failing tests** — one case per rule above, plus these, whose values are the point:

```python
def test_a_duration_two_percent_under_the_runtime_agrees_and_one_second_more_does_not() -> None:
    at = _state(state="playing", app_id=INFUSE, media_duration_seconds=1_000.0)
    def now(d: float) -> HouseState: return _state(state="playing", app_id=INFUSE, media_duration_seconds=d)
    assert started(now(7_056.0), at_handover=at, app_id=INFUSE, runtime_seconds=7_200)
    assert not started(now(7_055.0), at_handover=at, app_id=INFUSE, runtime_seconds=7_200)
    assert started(now(7_344.0), at_handover=at, app_id=INFUSE, runtime_seconds=7_200)
    assert not started(now(7_345.0), at_handover=at, app_id=INFUSE, runtime_seconds=7_200)

def test_the_reading_at_the_handover_is_never_the_send_playing() -> None:
    at = _state(state="playing", app_id=INFUSE, media_duration_seconds=7_200.0)
    assert not started(at, at_handover=at, app_id=INFUSE, runtime_seconds=7_200)

def test_a_paused_player_is_not_busy_and_a_playing_one_says_what_with() -> None:
    assert busy_with(_state(state="paused", app_name="Netflix")) is None
    assert busy_with(None) is None
    assert busy_with(_state(state="playing", app_name="Netflix", media_title="Example Episode")) \
        == "NETFLIX · EXAMPLE EPISODE"
    assert busy_with(_state(state="playing")) == "SOMETHING ELSE"

def test_what_a_player_shows_at_a_deadline() -> None:
    kw = {"device_name": "Living Room Apple TV", "recipe": INFUSE_RECIPE}
    assert showing(None, **kw) == "LIVING ROOM APPLE TV TURNED OFF"
    assert showing(_state(state="standby"), **kw) == "LIVING ROOM APPLE TV TURNED OFF"
    assert showing(_state(state="idle", app_id="com.netflix.Netflix", app_name="Netflix"), **kw) \
        == "SOMEONE OPENED NETFLIX"
    assert showing(_state(state="paused", app_id=INFUSE, app_name="Infuse"), **kw) \
        == "INFUSE DID NOT START PLAYING"
```

- [ ] **Step 2: Run and see them fail** — `uv run pytest tests/unit/test_domain_sends.py -p no:randomly`; expected: `ModuleNotFoundError`.
- [ ] **Step 3: Implement `domain/sends.py`.**
- [ ] **Step 4: Gate, then commit** — `sends: a send's stages, and what a player's reading says about each`

---

### Task 4: The dispatcher runs a send's stages and publishes each

**Files:**
- Create: `src/usher/services/send.py`
- Modify: `src/usher/ports/events.py` (`ClientEventKind.SEND_STAGE = "send.stage"`), `src/usher/api/dto/events.py` (`SseEventKind.SEND_STAGE`, `_WIRE`)
- Modify: `docs/prd/07-client-api.md` (`## Streaming updates (SSE)`: a `send.stage` row — payload "Send id, screen, stage, failed stage, time, printed detail", client action "Show the send's progress"; and one sentence under the table: a `send.stage` is scoped to the title sent, an episode's to its series), `docs/prd/10-telemetry-and-dashboards.md` (the metric row below)
- Test: `tests/unit/test_services_send_dispatcher.py`, `tests/unit/test_api_dto_events.py`

**Interfaces:**
- Consumes: `Players.wake`/`play_url`/`wait_until`/`snapshot` (Task 2), `FakePlayers` (Task 2), everything in `domain/sends.py` (Task 3), `Way` (`domain/ways.py`), `EventPublisher`, `new_id` (`domain/ids.py`).
- Produces:

```python
@dataclass(frozen=True, slots=True)
class SendOrder:
    title_id: uuid.UUID
    episode_id: uuid.UUID | None
    way: Way
    link: Callable[[], str]     # a fresh ticket inside on every call; never stored, never logged

class SendDispatcher:
    def __init__(self, players: Players, events: EventPublisher, *,
                 clock: Callable[[], datetime] = ...,          # aware UTC now
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 keep: int = 32) -> None
    def start(self, order: SendOrder) -> Send     # synchronous; the send at WAKING
    def get(self, send_id: uuid.UUID) -> Send | None
    async def aclose(self) -> None
```

- Metric: histogram `usher.send.stage.duration`, unit `s`, attributes `usher.send.recipe` (the recipe key), `usher.send.stage` (`waking`, `opening`, `starting`) and `usher.send.outcome` (`confirmed`, `timed_out`, `unavailable`, `superseded`, `failed`). One point per stage a send leaves. PRD 10's catalogue row says: emitted by the dispatcher when a send leaves a stage, and is what a recipe's windows are read from.
- Event: `ClientEvent(kind=SEND_STAGE, title_id=send.title_id, episode_id=send.episode_id, data={"send_id": str, "screen": area_id, "stage": str, "failed_stage": str | None, "at": ISO-8601, "detail": str | None})`.

**How a send runs** (the algorithm the tests and signatures do not fix):

```
start(order):   no await anywhere in it
    cancel the task running on order.way.screen.area_id, if any
    record a Send at WAKING under new_id(); create its task; return the Send
    (the task publishes WAKING as its first act, so an event never precedes the 202)

task:
  WAKING    players.wake(entity)                     a PortDataMalformed here is ignored
            began = monotonic()
            loop:
                at_handover = the player's reading in players.snapshot()
                try: players.play_url(entity, order.link()); break
                except PortDataMalformed:
                    if monotonic() - began + retry_seconds > waking_seconds:
                        end DIDNT_TAKE at WAKING, STAYED_ASLEEP
                    sleep(retry_seconds)
  OPENING   players.wait_until(opened(...), timeout=opening_seconds)
            on its PortUnavailable -> the deadline rule below, MAY_NOT_BE_INSTALLED
  STARTING  players.wait_until(started(...), timeout=starting_seconds)
            on its PortUnavailable -> the deadline rule below, showing(...)
  PLAYING   end

deadline rule: read players.snapshot(). If it raises, the cause is NOT_ANSWERING;
               otherwise the cause given, with showing() read from that snapshot.
any other UsherPortError from wake, play_url or snapshot: end DIDNT_TAKE at the
               current stage, NOT_ANSWERING
CancelledError: if cancelled by a newer send, end SUPERSEDED and publish it;
               if by aclose(), end nothing and publish nothing. Re-raise either way.
any other exception: logger.exception without the order, end DIDNT_TAKE at the
               current stage, USHER_FAILED
finally:       free the screen if this task still holds it
```

The loop's bound stands on its boundary: with `waking_seconds=6` and `retry_seconds=2` handovers happen at 0, 2, 4 and 6 s, four in all. `get` answers a running send and the last `keep` finished ones; a running send is never dropped to make room.

- [ ] **Step 1: Write the failing tests.** One fixture builds the house (`media_player.living_room_living_room_apple_tv`, named `Living Room Apple TV`, in `Living Room`, labelled to use), a `Way` through the Infuse recipe with `windows=Windows(waking_seconds=6.0, retry_seconds=2.0, opening_seconds=0.05, starting_seconds=0.05)` and a copy of `runtime_seconds=7_200`, a recording publisher, and a `sleep` that advances the fake `monotonic` by what it was asked to sleep. `LINK` is Task 2's `URL`; `order.link` counts its calls. Helpers `infuse(...)`/`netflix(...)` publish a reading for the player.

```python
async def test_every_stage_in_order_and_one_handover() -> None:
    players.on_play = lambda *_: infuse(state="paused", media_duration_seconds=7_200.0)
    send = dispatcher.start(order)
    assert send.stage is SendStage.WAKING
    await settle()                                   # until the send reaches STARTING
    infuse(state="playing", media_duration_seconds=7_200.0)
    await finished(send.id)
    assert [e.data["stage"] for e in published] == ["waking", "opening", "starting", "playing"]
    assert players.calls == [("wake", PLAYER, ""), ("play_url", PLAYER, LINK)]
    assert {e.kind for e in published} == {ClientEventKind.SEND_STAGE}
    assert {e.title_id for e in published} == {order.title_id}
    assert set(published[0].data) == {"send_id", "screen", "stage", "failed_stage", "at", "detail"}
    assert published[0].data["screen"] == "living_room"

async def test_a_sleeping_player_is_asked_again_with_a_fresh_ticket_each_time() -> None:
    players.refuse_plays = [PortDataMalformed("refused")] * 2
    ...
    assert [call[0] for call in players.calls] == ["wake", "play_url", "play_url", "play_url"]
    assert slept == [2.0, 2.0]
    assert links_minted == 3

async def test_a_player_that_stays_asleep_ends_the_send_at_the_last_handover_inside_the_window() -> None:
    players.refuse_plays = [PortDataMalformed("refused")] * 10
    ...
    assert len([c for c in players.calls if c[0] == "play_url"]) == 4      # 0, 2, 4 and 6 s
    assert (send.stage, send.failed_stage) == (SendStage.DIDNT_TAKE, SendStage.WAKING)
    assert send.detail == "DIDN'T TAKE · WAKING · LIVING ROOM APPLE TV STAYED ASLEEP"

async def test_an_app_that_never_opens_is_not_handed_the_link_again() -> None:
    ...                                              # accepted, and no reading follows
    assert send.detail == "DIDN'T TAKE · OPENING · INFUSE MAY NOT BE INSTALLED"
    assert len([c for c in players.calls if c[0] == "play_url"]) == 1

@pytest.mark.parametrize(("reading", "cause"), [
    (netflix_idle, "SOMEONE OPENED NETFLIX"),
    (standby, "LIVING ROOM APPLE TV TURNED OFF"),
    (infuse_paused, "INFUSE DID NOT START PLAYING"),
])
async def test_a_start_that_never_comes_names_what_the_player_shows(reading, cause) -> None:
    ...                                              # opened by an Infuse reading, then `reading`
    assert send.detail == f"DIDN'T TAKE · STARTING · {cause}"

async def test_a_player_that_vanishes_reads_as_turned_off() -> None:
    ...                                              # opened, then house.without_state(PLAYER)
    assert send.detail == "DIDN'T TAKE · STARTING · LIVING ROOM APPLE TV TURNED OFF"

async def test_the_old_item_still_playing_is_not_the_send_playing() -> None:
    infuse(state="playing", media_duration_seconds=1_000.0)          # before the send
    send = dispatcher.start(order); await settle()
    infuse(state="playing", media_duration_seconds=1_000.0, media_position_seconds=61.0)
    await settle()
    assert dispatcher.get(send.id).stage is SendStage.STARTING       # opened, not started
    infuse(state="playing", media_duration_seconds=7_150.0)
    await finished(send.id)
    assert dispatcher.get(send.id).stage is SendStage.PLAYING

async def test_a_second_send_to_the_screen_supersedes_the_first() -> None:
    first = dispatcher.start(order); await settle()                  # parked in OPENING
    calls_before = list(players.calls)
    second = dispatcher.start(order); await settle()
    assert dispatcher.get(first.id).stage is SendStage.SUPERSEDED
    assert dispatcher.get(first.id).failed_stage is None
    assert players.calls[len(calls_before):] == [("wake", PLAYER, ""), ("play_url", PLAYER, LINK)]
    assert not dispatcher.get(second.id).finished
    assert "superseded" in [e.data["stage"] for e in published if e.data["send_id"] == str(first.id)]

async def test_a_send_to_another_screen_supersedes_nothing() -> None: ...

async def test_home_assistant_dropping_mid_send_says_so() -> None:
    ...                                              # parked in OPENING, then players.drop()
    assert send.detail == "DIDN'T TAKE · OPENING · HOME ASSISTANT IS NOT ANSWERING"

async def test_a_handover_home_assistant_cannot_take_ends_the_send_at_waking() -> None:
    players.refuse_plays = [PortUnavailable("Home Assistant is not answering")]
    ...
    assert send.detail == "DIDN'T TAKE · WAKING · HOME ASSISTANT IS NOT ANSWERING"
    assert slept == []

async def test_a_crash_ends_the_send_and_frees_the_screen() -> None:
    crashing = replace(order, link=_raises(RuntimeError("boom")))
    send = dispatcher.start(crashing); await finished(send.id)
    assert dispatcher.get(send.id).detail == "DIDN'T TAKE · WAKING · USHER FAILED"
    nxt = dispatcher.start(order); await settle()
    assert not dispatcher.get(nxt.id).finished and dispatcher.get(nxt.id).stage is not SendStage.WAKING

async def test_the_link_reaches_the_player_and_nothing_else() -> None:
    ...           # through a DEBUG sink: a whole send, then one whose on_play raises RuntimeError
    assert all(LINK not in json.dumps(e.data) for e in published)
    assert all(LINK not in repr(dispatcher.get(i)) for i in (played.id, crashed.id))
    assert sink != []                                                # the premise: the crash was logged
    assert all(LINK not in line and "TICKET" not in line for line in sink)
    assert players.calls.count(("play_url", PLAYER, LINK)) == 2      # the premise: it was sent, twice

async def test_finished_sends_are_kept_to_the_bound_and_a_running_one_is_never_dropped() -> None:
    ...                                              # keep=2: three finished and one running
    assert [dispatcher.get(i) is not None for i in ids] == [False, True, True, True]

async def test_closing_cancels_a_running_send_and_publishes_nothing_more() -> None: ...

async def test_each_stage_a_send_leaves_records_its_duration() -> None:
    ...                                              # InMemoryMetricReader, as test_telemetry_*.py do
    assert points == {("apple_tv.infuse", "waking", "confirmed"),
                      ("apple_tv.infuse", "opening", "confirmed"),
                      ("apple_tv.infuse", "starting", "confirmed")}
```

In `tests/unit/test_api_dto_events.py`, add `"send.stage"` to the pinned set of wire names.

- [ ] **Step 2: Run and see them fail** — `uv run pytest tests/unit/test_services_send_dispatcher.py tests/unit/test_api_dto_events.py -p no:randomly`; expected: `ModuleNotFoundError: usher.services.send`.
- [ ] **Step 3: Implement** the event kind and its wire name, then `SendOrder` and `SendDispatcher` as above.
- [ ] **Step 4: PRD 07 and PRD 10**, as listed under Files. `tests/unit/test_telemetry_metric_names.py` pins the catalogue against the code in both directions.
- [ ] **Step 5: Gate, then commit** — `send: the dispatcher runs a send's stages, one send to a screen, each stage on /events`

---

### Task 5: From a way's id to an order

**Files:**
- Modify: `src/usher/services/send.py` (beside the dispatcher), `src/usher/services/playback.py`
- Test: `tests/unit/test_services_send.py`, `tests/unit/test_services_playback.py`

**Interfaces:**
- Consumes: `SendDispatcher.start`, `SendOrder` (Task 4); `busy_with` (Task 3); `project`, `rank`; `PlayersUnavailable` and `PLAYERS_OFF` (`services/screens.py`); `title_name`, `episode_name` (`services/playback.py`); `WatchStateRepository.get_for_title`/`get_for_episode`; `wrap_deep_link` (`ports/source.py`).
- Produces:

```python
# services/playback.py
class PlaybackService:
    async def direct_url(self, copy: MediaItem) -> str | None
    # The copy's source URL as its source streams it, or None when the source cannot
    # answer or offers no direct target. A credential: for a caller that mints a ticket.

# services/send.py
class UnknownWay(LookupError): ...
class WayNotPlayable(Exception):      # .detail: str, safe to show
class ScreenBusy(Exception):          # .detail: str, safe to show
class SourceUnavailable(Exception):   # .detail: str, safe to show

def build_link(handover: Handover, ticket_url: str, *, position_seconds: int) -> str

class SendService:
    def __init__(self, players: Players | None, dispatcher: SendDispatcher | None,
                 media_items: MediaItemRepository, playback: PlaybackService,
                 watch_states: WatchStateRepository,
                 mint: Callable[[str, str], str]) -> None
    async def send_title(self, title: Title, *, way_id: uuid.UUID, take_over: bool,
                         user_id: uuid.UUID) -> Send
    async def send_episode(self, series: Title | None, episode: Episode, *, way_id: uuid.UUID,
                           take_over: bool, user_id: uuid.UUID) -> Send
```

The order of the checks, each ending the request before the next begins:

1. No players or no dispatcher → `PlayersUnavailable(PLAYERS_OFF, retryable=False)`. A house that cannot be read → `PlayersUnavailable` in the port's words, as `ScreensService._reading` does it.
2. Rank the item's copies on every screen. No way with this id → `UnknownWay`.
3. A way with a refusal → `WayNotPlayable(f"CAN'T · {way.refusal}")`.
4. A recipe that is not proven → `WayNotPlayable(f"CAN'T SEND · {RECIPE NAME} IS NOT PROVEN")`.
5. `busy_with(way.device.state)` and no `take_over` → `ScreenBusy` with that text.
6. `playback.direct_url(way.file)` is `None` → `SourceUnavailable("could not reach the source holding this item")`.
7. The resume point: the user's watch state for the item, when it is not played and its position is above zero; else 0.
8. `dispatcher.start(SendOrder(...))`, whose `link` calls `mint(source_url, name)` and then `build_link`. For an episode, `title_id` is `episode.title_id` and the name is `episode_name(series, episode)`.

`build_link(Handover.INFUSE_CALLBACK, ticket, position_seconds=p)` is `wrap_deep_link(ticket)`, with `&position={p}` appended when `p > 0`. Any other handover raises `NotImplementedError` naming it.

- [ ] **Step 1: Write the failing tests** — one per numbered check, asserting the exception, its `detail`, and that nothing later ran (`playback.asked == []` for checks 1–5, `players.calls == []` for 1–6), plus:

```python
def test_a_resume_point_rides_the_link_and_zero_does_not() -> None:
    ticket = "https://usher.test/stream/TICKET/Name"
    assert build_link(Handover.INFUSE_CALLBACK, ticket, position_seconds=0) == wrap_deep_link(ticket)
    assert build_link(Handover.INFUSE_CALLBACK, ticket, position_seconds=1234) \
        == wrap_deep_link(ticket) + "&position=1234"

def test_every_proven_recipe_has_a_link_to_build() -> None:
    proven = [recipe for recipe in RECIPES if recipe.proven]
    assert proven                                                    # the premise
    for recipe in proven:
        assert build_link(recipe.handover, "https://usher.test/stream/T/N", position_seconds=0)

async def test_the_ticket_is_minted_at_the_handover_and_not_at_the_request() -> None:
    send = await service.send_title(title, way_id=way.id, take_over=False, user_id=USER)
    assert minted == []                                              # answered, nothing minted
    await settle()
    assert minted == [(SOURCE_URL, title_name(title))]
    assert players.calls[-1] == ("play_url", PLAYER, build_link(Handover.INFUSE_CALLBACK, TICKET, position_seconds=0))

@pytest.mark.parametrize(("watch", "suffix"), [
    (None, ""), (unplayed_at(1234), "&position=1234"), (played_at(1234), ""), (unplayed_at(0), ""),
])
async def test_the_resume_point_is_the_users_unless_played(watch, suffix) -> None: ...

async def test_a_busy_screen_is_taken_over_only_when_asked() -> None:
    netflix_playing()
    with pytest.raises(ScreenBusy) as caught:
        await service.send_title(title, way_id=way.id, take_over=False, user_id=USER)
    assert caught.value.detail == "NETFLIX · EXAMPLE EPISODE"
    send = await service.send_title(title, way_id=way.id, take_over=True, user_id=USER)
    assert send.stage is SendStage.WAKING

async def test_an_episode_is_scoped_to_its_series_and_named_as_an_episode() -> None:
    send = await service.send_episode(series, episode, way_id=way.id, take_over=False, user_id=USER)
    assert (send.title_id, send.episode_id) == (series.id, episode.id)
    await settle()
    assert minted[0][1] == episode_name(series, episode)

# tests/unit/test_services_playback.py
async def test_a_copys_direct_url_is_its_sources_own() -> None: ...       # == the adapter's direct target url
async def test_a_source_that_cannot_answer_has_no_direct_url() -> None: ...   # None, adapter closed
```

- [ ] **Step 2: Run and see them fail** — `uv run pytest tests/unit/test_services_send.py tests/unit/test_services_playback.py -p no:randomly`.
- [ ] **Step 3: Implement** `PlaybackService.direct_url` over the existing `_copy_targets`, then the exceptions, `build_link` and `SendService`.
- [ ] **Step 4: Gate, then commit** — `send: a way's id becomes an order, and the ticket is minted at the handover`

---

### Task 6: The routes

**Files:**
- Create: `src/usher/api/dto/sends.py`, `src/usher/api/routers/sends.py`
- Modify: `src/usher/api/dto/problem.py` (`SCREEN_BUSY = "screen_busy"`, `WAY_NOT_PLAYABLE = "way_not_playable"`), `src/usher/api/deps.py`, `src/usher/api/app.py`, `src/usher/api/routers/screens.py` (its `_unavailable` becomes the public `players_unavailable`, which the new router imports)
- Modify: `docs/prd/07-client-api.md`, `docs/prd/01-architecture.md` where it lists the players' components
- Test: `tests/unit/test_api_send.py`, and the vocabulary cases in `tests/unit/test_api_problem.py` and `tests/unit/test_api_problem_vocabulary.py`

**Interfaces:**
- Consumes: `SendService` and its four exceptions (Task 5), `SendDispatcher` (Task 4), `Send` (Task 3), `PlayersUnavailable`.
- Produces:

```python
# api/dto/sends.py
class SendRequest(BaseModel):
    way: uuid.UUID
    take_over: bool = False

class SendStageResponse(BaseModel):
    stage: SendStage
    at: AwareDatetime
    detail: str | None

class SendResponse(BaseModel):
    send_id: uuid.UUID
    title_id: uuid.UUID
    episode_id: uuid.UUID | None
    way_id: uuid.UUID
    screen: str                    # the area id
    screen_name: str
    entity_id: str
    recipe: str                    # the recipe key
    stage: SendStage
    failed_stage: SendStage | None
    detail: str | None
    stages: list[SendStageResponse]
    @classmethod
    def of(cls, send: Send) -> Self: ...

# api/deps.py
def get_ticket_minter(request: Request, cipher: TicketCipherDep) -> Callable[[str, str], str]
TicketMinterDep = Annotated[Callable[[str, str], str], Depends(get_ticket_minter)]
def get_send_dispatcher(request: Request) -> SendDispatcher | None     # app.state.sends
def get_send_service(...) -> SendService
SendServiceDep, SendDispatcherDep
```

`get_ticket_minter` is the closure `get_playback_service` builds today, moved out so both services take it; `get_playback_service` takes `TicketMinterDep`. The lifespan sets `app.state.sends = SendDispatcher(players, events)` when players are on and `None` when off, and awaits its `aclose()` before `close_players()`. `get_send_dispatcher` raises the same `RuntimeError` `get_players` does when the attribute was never set.

| Route | Answers |
|---|---|
| `POST /titles/{title_id}/send` · `POST /episodes/{episode_id}/send` | `202 SendResponse` · `404 not_found` (no such title or episode) · `422 validation_failed` (the body, or `no way has this id`) · `409 way_not_playable` · `409 screen_busy` · `503 players_unavailable` (with `Retry-After` when retryable) · `503 source_unavailable` |
| `GET /sends/{send_id}` | `200 SendResponse` · `404 not_found` (`send not found`: unknown, dropped from the last 32, or sent before a restart) · `503 players_unavailable` while players are off |

Both `POST`s take `DefaultUserIdDep` for the resume point. Tag: `screens`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_api_send.py`, on `test_api_ways.py`'s fixtures (app built with `push_enabled=False, worker_enabled=False`, `FakePlayers` on `app.state.players`, a `SendDispatcher` over it on `app.state.sends`):

```python
async def test_a_send_is_accepted_and_readable() -> None:
    answer = await client.post(f"/api/v1/titles/{TITLE}/send", json={"way": str(way_id)})
    assert answer.status_code == 202
    body = answer.json()
    assert body["stage"] == "waking" and body["failed_stage"] is None
    assert body["screen"] == "living_room" and body["recipe"] == "apple_tv.infuse"
    read = await client.get(f"/api/v1/sends/{body['send_id']}")
    assert read.status_code == 200 and read.json()["send_id"] == body["send_id"]

@pytest.mark.parametrize(("arrange", "status", "code", "detail"), [
    (unknown_title, 404, "not_found", "title not found"),
    (unknown_way, 422, "validation_failed", "no way has this id"),
    (mkv_over_airplay, 409, "way_not_playable", "CAN'T · MKV OVER AIRPLAY"),
    (mp4_over_airplay, 409, "way_not_playable", "CAN'T SEND · AIRPLAY IS NOT PROVEN"),
    (netflix_playing, 409, "screen_busy", "NETFLIX · EXAMPLE EPISODE"),
    (source_down, 503, "source_unavailable", "could not reach the source holding this item"),
])
async def test_a_send_that_is_not_accepted_says_why(arrange, status, code, detail) -> None: ...

async def test_players_off_is_503_with_no_retry_after_and_an_unread_house_has_one() -> None: ...
async def test_take_over_plays_over_what_is_playing() -> None: ...            # 202
async def test_an_episode_send_is_accepted() -> None: ...                     # 202, episode_id set
async def test_a_send_nobody_made_is_404() -> None: ...                       # code not_found
async def test_a_finished_send_lists_every_stage_it_passed() -> None:
    ...                                                                        # driven to playing
    assert [mark["stage"] for mark in read.json()["stages"]] == ["waking", "opening", "starting", "playing"]

def test_the_vocabulary_is_ten_and_each_new_code_has_one_status() -> None:
    assert len(ProblemCode) == 10
```

The existing vocabulary and OpenAPI scans (`test_api_problem_vocabulary.py`, `test_api_openapi.py`, `test_api_dto.py`) decide the rest: every declared failure is a `ProblemResponse`, every response model's name ends `Response`.

- [ ] **Step 2: Run and see them fail** — `uv run pytest tests/unit/test_api_send.py -p no:randomly`; expected: 404s from an unrouted path.
- [ ] **Step 3: Implement** the DTOs, the deps, the lifespan lines and the router; include it in the app where the screens and ways routers are.
- [ ] **Step 4: PRD 07.**
  - `### Player routes`: the two rows of the table above.
  - `#### The vocabulary`: "**Ten members, closed.**", with rows `screen_busy | 409 | the player is playing something and the send did not ask to take over; \`detail\` says what` and `way_not_playable | 409 | the way cannot play this copy, or its recipe is not proven`.
  - `## Players`, new bullets stating: what a send does stage by stage (wake, hand over until taken, the app in front, playing); that it is answered `202` at once and followed on `/events` or at `GET /sends/{id}`; `take_over`; one send per screen and `superseded`; `didnt_take` with `failed_stage` and the printed `detail`; that the last 32 finished sends are kept in memory and a restart forgets them; that only a proven recipe is sent through; that the resume point is the default user's; that the ticket is minted at the handover.
  - The bullet beginning "**With players off or HA unreachable**" gains the three routes.
- [ ] **Step 5: Gate, then commit** — `api: POST /titles/{id}/send, POST /episodes/{id}/send and GET /sends/{id}, with screen_busy and way_not_playable`

---

### Task 7: The console knows the ninth and tenth codes

**Files:**
- Regenerate: `web/src/api/schema.d.ts` (never by hand: `guard-generated.sh` refuses it)
- Modify: `web/src/api/problem.ts`, `web/src/api/problem.test.ts`, `web/src/test/fixtures/problems.ts`, `web/src/design-system/components/feedback/Problem.tsx` and its test, and any prose or case that says "eight"
- Modify: `docs/plans/progress.md` and `docs/prd/README.md` (this plan's rows: Tasks 1–7 landed, Task 8 waiting on the owner)

**Interfaces:**
- Consumes: the OpenAPI document of Task 6.
- Produces, in `RECOVERY`:

```ts
screen_busy: { status: 409, scale: 'inline', rendered: true, retryable: false, recovery: { kind: 'none' } },
way_not_playable: { status: 409, scale: 'inline', rendered: true, retryable: false,
                    recovery: { kind: 'other-copies', label: 'See other copies' } },
```

No console surface sends anything in this slice: the home panel is the first client, and the entries exist so the closed table still compiles.

- [ ] **Step 1: Regenerate the schema from a file.** Dump the app's OpenAPI document to `/var/tmp/usher-openapi.json` the way `tests/unit/test_api_openapi.py` builds its app, then run `gen:types`' own `openapi-typescript` invocation (same package pins, from `web/package.json`) on that file. Confirm `git diff --stat web/src/api/schema.d.ts` shows only the send routes, the three send models, the two codes and `send.stage`.
- [ ] **Step 2: See the typecheck fail** — `npm run typecheck` from `web/`; expected: `RECOVERY` is missing `screen_busy` and `way_not_playable`.
- [ ] **Step 3: Add the two entries, their fixtures and their cases**, following how `players_unavailable` was added (`git log --oneline -S players_unavailable -- web/src` finds that commit).
- [ ] **Step 4: Both gates** — the Python gate, then from `web/`: `npm run verify`, `npm run e2e -- --trace off`, `npm run e2e:visual -- --trace off`. A changed screenshot is expected only where the problem gallery lists every code.
- [ ] **Step 5: Commit** — `console: screen_busy and way_not_playable join the problem table`

---

### Task 8: Live acceptance at the TV — owner-gated

**This task wakes a real television and plays on it. It does not start until the owner says so, and the owner is at the TV for it.**

**Files:**
- Modify: `src/usher/domain/recipes.py` only if a window proved too short; `tests/fakes/home_assistant.py` only to pin the refusal code HA really sends; `docs/plans/progress.md` and `docs/prd/README.md` (Task 8's row)

- [ ] **Step 1: Run the branch beside production**, never on it: from this worktree, `python -m usher` on port 8101 against the dev catalog, with `USHER_PLAYERS_ENABLED=true` and the Home Assistant settings from `~/.config/usher/ha-live.env`, `USHER_PUSH_ENABLED=false`, `USHER_WORKER_ENABLED=false`. Drive it from a throwaway script under `/var/tmp/sts-slice-2/`, which prints no ticket, token or host. The script posts to an address the Apple TV resolves, never `127.0.0.1` or `localhost`, because the link's host is the POST's host.
- [ ] **Step 2: Twelve sends to the Living Room**, reading each back from `GET /sends/{id}` until it finishes:
  - three from off (the TV off, the Apple TV asleep), noting for each whether Home Assistant answers `turn_on` or `play_media` after more than 10 s, which ends the send at `waking` while the film may still open;
  - three from the Apple TV's home screen;
  - one while another app plays, with no `take_over`: expect `409 screen_busy` naming the app and what plays;
  - the same again with `take_over`;
  - two sends two seconds apart: expect the first `superseded` and one film playing;
  - one episode with a resume point: expect it to start there;
  - one that plays past five minutes and then seeks: the ticket expires after 300 s;
  - one posted while the screen's player is `unavailable`.
- [ ] **Step 3: Record**, per send: every stage's time, the final stage and detail, and what the owner saw. From the log, the code HA answers a handover to a sleeping Apple TV with; make `FakeHomeAssistant`'s scripted refusal in Task 2's cases that code.
- [ ] **Step 4: Bars.** Every send from off and from the home screen ends `playing` with nobody touching a remote; no send reports `playing` while something else is on the screen; no send reports `didnt_take` while the film plays. A bar that fails is a finding to fix before the merge, not a note.
- [ ] **Step 5: Post the table of results on the pull request**, the numbers themselves, and flip Task 8's status cells.

---

## What this slice leaves

- AirPlay and Cast are ranked and never sent; slice 3 proves and sends them, and reads the windows off `usher.send.stage.duration`.
- No console surface sends. The home panel's Watch screen is the first client.
- Copies whose facts are identical are still indistinguishable in `/ways`.
- Any item already playing in the recipe's app with a runtime within 2% of the sent copy's reads `playing` at the first new reading, since nothing in HA's reading tells the old playback from the new. That is the same film sent again, and also most consecutive episodes of a series sent under `take_over`.
- Home Assistant's own `usher.play` and Alfred's tool are slice 4.
