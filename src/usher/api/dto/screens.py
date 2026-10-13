"""Response shapes for `GET /screens` and confirm, and the body confirm takes."""

from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictBool

from usher.domain.house import DeviceRole, HouseArea, HouseState
from usher.domain.recipes import Recipe
from usher.domain.screens import HouseView, Screen, ScreenDevice

__all__ = [
    "AreaResponse",
    "ConfirmRequest",
    "DeviceResponse",
    "PlayerStateResponse",
    "RecipeResponse",
    "ScreenResponse",
    "ScreensResponse",
]


class RecipeResponse(BaseModel):
    """One way a player can be handed a title. `proven` is false until a send shows it."""

    key: str
    name: str
    hands_free: bool
    proven: bool

    @classmethod
    def of(cls, recipe: Recipe) -> Self:
        return cls(
            key=recipe.key, name=recipe.name, hands_free=recipe.hands_free, proven=recipe.proven
        )


class PlayerStateResponse(BaseModel):
    """Home Assistant's live reading of a player, passed through rather than reinterpreted."""

    state: str
    app_id: str | None
    app_name: str | None
    media_title: str | None
    media_duration_seconds: float | None
    media_position_seconds: float | None
    media_position_updated_at: AwareDatetime | None

    @classmethod
    def of(cls, state: HouseState) -> Self:
        return cls(
            state=state.state,
            app_id=state.app_id,
            app_name=state.app_name,
            media_title=state.media_title,
            media_duration_seconds=state.media_duration_seconds,
            media_position_seconds=state.media_position_seconds,
            media_position_updated_at=state.media_position_updated_at,
        )


class DeviceResponse(BaseModel):
    """A player Usher could play on. `state` is null while HA reports none for it."""

    entity_id: str
    name: str
    model: str | None
    area_id: str | None
    role: DeviceRole | None
    recipes: list[RecipeResponse]
    state: PlayerStateResponse | None

    @classmethod
    def of(cls, device: ScreenDevice) -> Self:
        return cls(
            entity_id=device.entity_id,
            name=device.name,
            model=device.model,
            area_id=device.area_id,
            role=device.role,
            recipes=[RecipeResponse.of(recipe) for recipe in device.recipes],
            state=PlayerStateResponse.of(device.state) if device.state is not None else None,
        )


class ScreenResponse(BaseModel):
    area_id: str
    name: str
    devices: list[DeviceResponse]

    @classmethod
    def of(cls, screen: Screen) -> Self:
        return cls(
            area_id=screen.area_id,
            name=screen.name,
            devices=[DeviceResponse.of(device) for device in screen.devices],
        )


class AreaResponse(BaseModel):
    area_id: str
    name: str

    @classmethod
    def of(cls, area: HouseArea) -> Self:
        return cls(area_id=area.area_id, name=area.name)


class ScreensResponse(BaseModel):
    """Every candidate in exactly one of four places, and every area to place one in."""

    screens: list[ScreenResponse]
    unconfirmed: list[DeviceResponse]
    no_area: list[DeviceResponse]
    ignored: list[DeviceResponse]
    areas: list[AreaResponse]

    @classmethod
    def of(cls, view: HouseView) -> Self:
        return cls(
            screens=[ScreenResponse.of(screen) for screen in view.screens],
            unconfirmed=[DeviceResponse.of(device) for device in view.unconfirmed],
            no_area=[DeviceResponse.of(device) for device in view.no_area],
            ignored=[DeviceResponse.of(device) for device in view.ignored],
            areas=[AreaResponse.of(area) for area in view.areas],
        )


class ConfirmRequest(BaseModel):
    """Use or ignore the player; `area_id` places one in use that is in no area."""

    model_config = ConfigDict(extra="forbid")

    use: StrictBool
    area_id: str | None = None
