"""Builders for `HouseSnapshot`, and this house as the spec's probe found it.

`tests/fakes/home_assistant_rows.py` renders it in the shape `shape.json` recorded.
"""

from datetime import UTC, datetime

from usher.domain.house import (
    DeviceRole,
    HouseArea,
    HouseDevice,
    HouseEntity,
    HouseSnapshot,
    HouseState,
)

LIVING_ROOM = "living_room"
BEDROOM = "bedroom"
KITCHEN = "kitchen"

APPLE_TV = "media_player.living_room_apple_tv"
APPLE_TV_DEVICE = "d0000000000000000000000000000001"
# Music Assistant's AirPlay player for the same Apple TV: a second entity, never a candidate.
MA_DUPLICATE = "media_player.bedroom"
MA_DEVICE = "d0000000000000000000000000000002"
SHIELD = "media_player.shield"
SHIELD_DEVICE = "d0000000000000000000000000000003"
# Reported by an integration no recipe knows, and unavailable.
LG = "media_player.lg_oled"
LG_DEVICE = "d0000000000000000000000000000004"
PS5 = "media_player.playstation_5"
PS5_DEVICE = "d0000000000000000000000000000005"
# A Cast speaker: the SHIELD's integration, not its model.
NEST = "media_player.kitchen_speaker"
NEST_DEVICE = "d0000000000000000000000000000006"

POSITION_UPDATED_AT = datetime(2026, 10, 6, 19, 0, tzinfo=UTC)


def area(area_id: str, name: str | None = None) -> HouseArea:
    return HouseArea(area_id=area_id, name=name or area_id.replace("_", " ").title())


def device(
    device_id: str,
    *,
    name: str = "Device",
    model: str | None = None,
    manufacturer: str | None = None,
    area_id: str | None = None,
) -> HouseDevice:
    return HouseDevice(
        device_id=device_id, name=name, model=model, manufacturer=manufacturer, area_id=area_id
    )


def entity(
    entity_id: str,
    *,
    integration: str,
    device_id: str | None = None,
    area_id: str | None = None,
    name: str | None = None,
    role: DeviceRole | None = None,
    disabled: bool = False,
) -> HouseEntity:
    return HouseEntity(
        entity_id=entity_id,
        integration=integration,
        device_id=device_id,
        area_id=area_id,
        name=name,
        role=role,
        disabled=disabled,
    )


def state(
    entity_id: str,
    value: str = "off",
    *,
    friendly_name: str | None = None,
    app_id: str | None = None,
    app_name: str | None = None,
    media_title: str | None = None,
    media_duration_seconds: float | None = None,
    media_position_seconds: float | None = None,
    media_position_updated_at: datetime | None = None,
) -> HouseState:
    return HouseState(
        entity_id=entity_id,
        state=value,
        friendly_name=friendly_name,
        app_id=app_id,
        app_name=app_name,
        media_title=media_title,
        media_duration_seconds=media_duration_seconds,
        media_position_seconds=media_position_seconds,
        media_position_updated_at=media_position_updated_at,
    )


def snapshot(
    *,
    areas: tuple[HouseArea, ...] = (),
    devices: tuple[HouseDevice, ...] = (),
    entities: tuple[HouseEntity, ...] = (),
    states: tuple[HouseState, ...] = (),
) -> HouseSnapshot:
    return HouseSnapshot(areas=areas, devices=devices, entities=entities, states=states)


def entity_of(house: HouseSnapshot, entity_id: str) -> HouseEntity:
    found = house.entity(entity_id)
    assert found is not None, f"{entity_id} is not in the snapshot"
    return found


def this_house(
    *, apple_tv: DeviceRole | None = DeviceRole.USE, shield: DeviceRole | None = None
) -> HouseSnapshot:
    """The Living Room as probed, after the owner confirmed the Apple TV."""
    return snapshot(
        areas=(area(BEDROOM), area(KITCHEN), area(LIVING_ROOM)),
        devices=(
            device(
                APPLE_TV_DEVICE,
                name="Living Room Apple TV",
                model="Apple TV 4K (gen 3)",
                manufacturer="Apple",
                area_id=LIVING_ROOM,
            ),
            device(
                MA_DEVICE,
                name="Living Room Apple TV (AirPlay)",
                model="Apple TV 4K Gen3",
                manufacturer="Apple",
                area_id=LIVING_ROOM,
            ),
            device(SHIELD_DEVICE, name="SHIELD", model="SHIELD Android TV", manufacturer="NVIDIA"),
            device(
                LG_DEVICE,
                name="LG OLED",
                model="OLED65C3PUA",
                manufacturer="LG",
                area_id=LIVING_ROOM,
            ),
            device(
                PS5_DEVICE,
                name="PlayStation 5",
                model="PlayStation 5",
                manufacturer="Sony Interactive Entertainment",
                area_id=LIVING_ROOM,
            ),
            device(
                NEST_DEVICE,
                name="Kitchen speaker",
                model="Google Nest Mini",
                manufacturer="Google Inc.",
                area_id=KITCHEN,
            ),
        ),
        entities=(
            entity(APPLE_TV, integration="apple_tv", device_id=APPLE_TV_DEVICE, role=apple_tv),
            entity(MA_DUPLICATE, integration="music_assistant", device_id=MA_DEVICE),
            entity(SHIELD, integration="cast", device_id=SHIELD_DEVICE, role=shield),
            entity(LG, integration="dlna_dmr", device_id=LG_DEVICE),
            entity(PS5, integration="playstation_network", device_id=PS5_DEVICE),
            entity(NEST, integration="cast", device_id=NEST_DEVICE),
        ),
        states=(
            state(
                APPLE_TV,
                "playing",
                friendly_name="Living Room Apple TV",
                app_id="com.example.player",
                app_name="Example Player",
                media_title="Example Episode",
                media_duration_seconds=3600.0,
                media_position_seconds=489.0,
                media_position_updated_at=POSITION_UPDATED_AT,
            ),
            state(MA_DUPLICATE, "idle", friendly_name="Bedroom"),
            state(SHIELD, "off", friendly_name="SHIELD"),
            state(LG, "unavailable", friendly_name="LG OLED"),
            state(PS5, "unavailable", friendly_name="PlayStation 5"),
            state(NEST, "idle", friendly_name="Kitchen speaker"),
        ),
    )
