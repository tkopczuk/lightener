"""Platform for Lightener lights."""

from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.group.light import FORWARDED_ATTRIBUTES, LightGroup
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    ColorMode,
)
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_LIGHTS,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.config_validation import PLATFORM_SCHEMA
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util.color import value_to_brightness

from . import async_migrate_data, async_migrate_entry
from .const import DOMAIN, TYPE_ONOFF
from .util import get_light_type

_LOGGER = logging.getLogger(__name__)

RESTORE_PREFERRED_BRIGHTNESS = "preferred_brightness"

ENTITY_SCHEMA = vol.All(
    vol.DefaultTo({1: 1, 100: 100}),
    {
        vol.All(vol.Coerce(int), vol.Range(min=1, max=100)): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100)
        )
    },
)

LIGHT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITIES): {cv.entity_id: ENTITY_SCHEMA},
        vol.Optional(CONF_FRIENDLY_NAME): cv.string,
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_LIGHTS): cv.schema_with_slug_keys(LIGHT_SCHEMA)}
)


class LightenerRestoreStateData(ExtraStoredData):
    """Extra Lightener state stored across Home Assistant restarts."""

    def __init__(self, preferred_brightness: int | None) -> None:
        """Initialize restore data."""
        self.preferred_brightness = preferred_brightness

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the restore data."""
        return {RESTORE_PREFERRED_BRIGHTNESS: self.preferred_brightness}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up entities for config entries."""
    unique_id = config_entry.entry_id

    await async_migrate_entry(hass, config_entry)

    # The unique id of the light will simply match the config entry ID.
    async_add_entities([LightenerLight(hass, config_entry.data, unique_id)])


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # pylint: disable=unused-argument
) -> None:
    """Set up entities for configuration.yaml entries."""

    lights = []

    for object_id, entity_config in config[CONF_LIGHTS].items():
        data = await async_migrate_data(entity_config, 1)
        data["entity_id"] = object_id

        lights.append(LightenerLight(hass, data))

    async_add_entities(lights)


class LightenerLight(LightGroup, RestoreEntity):
    """Represents a Lightener light."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_data: MappingProxyType,
        unique_id: str | None = None,
    ) -> None:
        """Initialize the light using the config entry information."""

        ## Add all entities that are managed by this lightened.
        entities: list[LightenerControlledLight] = []
        entity_ids: list[str] = []

        if config_data.get(CONF_ENTITIES) is not None:
            for entity_id, entity_config in config_data[CONF_ENTITIES].items():
                entity_ids.append(entity_id)
                entities.append(
                    LightenerControlledLight(entity_id, entity_config, hass=hass)
                )

        super().__init__(
            unique_id=unique_id,
            name=config_data[CONF_FRIENDLY_NAME] if unique_id is None else None,
            entity_ids=entity_ids,
            mode=None,
        )

        self._attr_has_entity_name = unique_id is not None
        self._attr_is_on = False
        self._attr_brightness = None
        self._is_frozen = False
        self._preferred_brightness = None
        self._pending_child_turn_on: dict[str, dict[str, Any]] = {}
        self._child_correction_unsubs: dict[str, CALLBACK_TYPE] = {}
        self._immediate_child_corrections: set[str] = set()
        self._child_correction_debounce = 1.0

        if self._attr_has_entity_name:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, self.unique_id)},
                name=config_data[CONF_FRIENDLY_NAME],
            )

        self._entities = entities

        _LOGGER.debug(
            "Created lightener `%s`",
            config_data[CONF_FRIENDLY_NAME],
        )

    async def async_added_to_hass(self) -> None:
        """Register listeners."""
        await super().async_added_to_hass()
        await self._async_restore_preferred_brightness()

        @callback
        def async_child_state_changed(event) -> None:
            """Correct child state after a child reports stale target attributes."""
            self._async_maybe_correct_child_state(
                event.data["entity_id"], event.data["new_state"]
            )

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                self._entity_ids,
                async_child_state_changed,
            )
        )

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        """Return extra state data to restore after Home Assistant restarts."""

        return LightenerRestoreStateData(self._preferred_brightness)

    async def _async_restore_preferred_brightness(self) -> None:
        """Restore the last preferred Lightener brightness."""

        brightness = None

        if (last_extra_data := await self.async_get_last_extra_data()) is not None:
            brightness = last_extra_data.as_dict().get(RESTORE_PREFERRED_BRIGHTNESS)

        if brightness is None and (last_state := await self.async_get_last_state()):
            brightness = last_state.attributes.get(ATTR_BRIGHTNESS)

        if (restored_brightness := self._coerce_brightness(brightness)) is not None:
            self._preferred_brightness = restored_brightness

    @staticmethod
    def _coerce_brightness(brightness: Any) -> int | None:
        """Coerce a restored brightness value into Home Assistant's raw range."""

        if brightness is None:
            return None

        try:
            restored_brightness = int(brightness)
        except (TypeError, ValueError):
            return None

        if 0 <= restored_brightness <= 255:
            return restored_brightness

        return None

    @property
    def color_mode(self) -> str:
        """Return the color mode of the light."""

        if not self.is_on:
            return None

        supported_color_modes = self._supported_color_modes()
        color_mode = self._attr_color_mode

        if (
            color_mode
            and color_mode != ColorMode.UNKNOWN
            and color_mode != ColorMode.ONOFF
            and color_mode in supported_color_modes
        ):
            return color_mode

        for fallback_mode in (
            ColorMode.BRIGHTNESS,
            ColorMode.COLOR_TEMP,
            ColorMode.HS,
            ColorMode.RGB,
            ColorMode.RGBW,
            ColorMode.RGBWW,
            ColorMode.WHITE,
            ColorMode.XY,
        ):
            if fallback_mode in supported_color_modes:
                return fallback_mode

        return ColorMode.BRIGHTNESS

    @property
    def supported_color_modes(self) -> set[str] | None:
        """Flag supported color modes."""

        return self._supported_color_modes()

    def _supported_color_modes(self) -> set[str]:
        """Return Lightener-supported color modes without mutating group state."""

        color_modes = set(super().supported_color_modes or set())

        # We support BRIGHNESS if the controlled lights are not on/off only.
        color_modes.discard(ColorMode.ONOFF)

        if not color_modes:
            if (
                self._attr_color_mode
                and self._attr_color_mode != ColorMode.UNKNOWN
                and self._attr_color_mode != ColorMode.ONOFF
            ):
                color_modes.add(self._attr_color_mode)
            else:
                color_modes.add(ColorMode.BRIGHTNESS)

        return color_modes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Forward the turn_on command to all controlled lights."""

        # This is basically a copy of LightGroup::async_turn_on but it has been changed
        # so we can pass different brightness to each light.

        # List all attributes we want to forward.
        data = {
            key: value for key, value in kwargs.items() if key in FORWARDED_ATTRIBUTES
        }

        # Retrieve the brightness being set to the Lightener.
        brightness = kwargs.get(ATTR_BRIGHTNESS)

        # If the brightness is not being set, reuse the last known Lightener level.
        if brightness is None:
            brightness = self._attr_brightness

        if brightness is None:
            brightness = self._preferred_brightness

        self._attr_is_on = True
        if brightness is not None:
            self._attr_brightness = brightness
            self._preferred_brightness = brightness

        _LOGGER.debug(
            "[Turn On] Attempting to set brightness of `%s` to `%s`",
            self.entity_id,
            brightness,
        )

        self._is_frozen = True

        async def _safe_service_call(
            entity: LightenerControlledLight, service: str, entity_data: dict
        ) -> None:
            """Call a service for an entity, logging success and guarding failures."""
            try:
                await self.hass.services.async_call(
                    LIGHT_DOMAIN,
                    service,
                    entity_data,
                    blocking=True,
                    context=self._context,
                )
                _LOGGER.debug(
                    "Service `%s` called for `%s` (%s) with `%s`",
                    service,
                    entity.entity_id,
                    entity.type,
                    entity_data,
                )

                if (
                    service == SERVICE_TURN_ON
                    and self._has_child_correction_target(entity_data)
                ):
                    self._pending_child_turn_on[entity.entity_id] = entity_data.copy()
                    self._immediate_child_corrections.discard(entity.entity_id)
                    self._cancel_child_correction(entity.entity_id)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception(
                    "Service `%s` for `%s` (%s) failed: %s; payload=%s",
                    service,
                    entity.entity_id,
                    entity.type,
                    exc,
                    entity_data,
                )

        try:
            async with asyncio.TaskGroup() as group:
                for entity in self._entities:
                    service = SERVICE_TURN_ON
                    entity_data = data.copy()

                    if brightness is not None:
                        entity_brightness = entity.translate_brightness(brightness)

                        # If the light brightness level is zero, we turn it off instead.
                        if entity_brightness == 0:
                            service = SERVICE_TURN_OFF
                            entity_data = {}
                            self._clear_child_correction(entity.entity_id)

                            # "Transition" is the only additional data allowed with the turn_off service.
                            if ATTR_TRANSITION in data:
                                entity_data[ATTR_TRANSITION] = data[ATTR_TRANSITION]
                        else:
                            # Set the translated brightness level.
                            entity_data[ATTR_BRIGHTNESS] = entity_brightness

                    # Set the proper entity ID.
                    entity_data[ATTR_ENTITY_ID] = entity.entity_id

                    # Submit the service call concurrently, guarded to avoid cancelling siblings on failure.
                    group.create_task(_safe_service_call(entity, service, entity_data))
        finally:
            self._is_frozen = False

        self.async_update_group_state()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off all lights controlled by this Lightener."""
        self._is_frozen = True

        if self._attr_brightness is not None:
            self._preferred_brightness = self._attr_brightness

        try:
            await super().async_turn_off(**kwargs)

            _LOGGER.debug("[Turn Off] Turned off `%s`", self.entity_id)

            self._clear_child_corrections()
            self._attr_is_on = False
            self._attr_brightness = None
        finally:
            self._is_frozen = False

        self.async_update_group_state()
        self.async_write_ha_state()

    def turn_on(self, **kwargs: Any) -> None:
        """Turn the lights controlled by this Lightener on. There is no guarantee that this method is synchronous."""
        self.async_turn_on(**kwargs)

    def turn_off(self, **kwargs: Any) -> None:
        """Turn the lights controlled by this Lightener off. There is no guarantee that this method is synchronous."""
        self.async_turn_off(**kwargs)

    @callback
    def async_update_group_state(self) -> None:
        """Update the Lightener state based on the controlled entities."""

        if self._is_frozen:
            return

        current_is_on = self._attr_is_on
        current_brightness = self._attr_brightness

        # LightGroup refreshes useful child-derived attributes here, including
        # availability, color/effect attributes, modes, and supported features.
        # It also writes is_on and brightness, which Lightener owns independently.
        super().async_update_group_state()

        self._attr_is_on = current_is_on
        self._attr_brightness = current_brightness

        _LOGGER.debug(
            "Setting the brightness of `%s` to `%s`",
            self.entity_id,
            self._attr_brightness,
        )

    @callback
    def _async_maybe_correct_child_state(self, entity_id: str, state) -> None:
        """Schedule correction if a child reports stale target attributes."""

        target_data = self._pending_child_turn_on.get(entity_id)

        if (
            target_data is None
            or state is None
            or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        ):
            return

        if self._child_state_matches_target(state, target_data):
            self._clear_child_correction(entity_id)
            return

        if entity_id not in self._immediate_child_corrections:
            self._immediate_child_corrections.add(entity_id)
            self.hass.async_create_task(
                self._async_correct_child(entity_id, target_data.copy()),
                name=f"Lightener [child correction {entity_id}]",
            )
            return

        self._async_schedule_child_correction(
            entity_id,
            target_data,
            delay=self._child_correction_debounce,
        )

    def _child_state_matches_target(self, state, target_data: dict[str, Any]) -> bool:
        """Return true if a child state already matches the target command."""

        if state.state != STATE_ON:
            return False

        for attr, target_value in target_data.items():
            if attr in (ATTR_ENTITY_ID, ATTR_TRANSITION):
                continue

            current_value = state.attributes.get(attr)

            if current_value is None:
                continue

            if attr == ATTR_BRIGHTNESS:
                if abs(int(current_value) - int(target_value)) > 1:
                    return False
                continue

            if isinstance(current_value, (list, tuple)) or isinstance(
                target_value, (list, tuple)
            ):
                if not isinstance(current_value, (list, tuple)) or not isinstance(
                    target_value, (list, tuple)
                ):
                    return False

                if tuple(current_value) != tuple(target_value):
                    return False
                continue

            if current_value != target_value:
                return False

        return True

    @staticmethod
    def _has_child_correction_target(target_data: dict[str, Any]) -> bool:
        """Return true if turn_on data has state attributes worth correcting."""

        return any(
            attr not in (ATTR_ENTITY_ID, ATTR_TRANSITION) for attr in target_data
        )

    @callback
    def _async_schedule_child_correction(
        self, entity_id: str, target_data: dict[str, Any], delay: float
    ) -> None:
        """Schedule brightness/color correction for a child."""

        self._cancel_child_correction(entity_id)

        async def _async_run_correction(_now) -> None:
            self._child_correction_unsubs.pop(entity_id, None)
            await self._async_correct_child(entity_id, target_data.copy())

        self._child_correction_unsubs[entity_id] = async_call_later(
            self.hass, delay, _async_run_correction
        )

    @callback
    def _cancel_child_correction(self, entity_id: str) -> None:
        """Cancel any scheduled correction for a child."""

        unsub = self._child_correction_unsubs.pop(entity_id, None)
        if unsub is not None:
            unsub()

    @callback
    def _clear_child_correction(self, entity_id: str) -> None:
        """Clear pending correction state for a child."""

        self._pending_child_turn_on.pop(entity_id, None)
        self._immediate_child_corrections.discard(entity_id)
        self._cancel_child_correction(entity_id)

    @callback
    def _clear_child_corrections(self) -> None:
        """Clear all pending child corrections."""

        for entity_id in list(self._child_correction_unsubs):
            self._cancel_child_correction(entity_id)

        self._pending_child_turn_on.clear()
        self._immediate_child_corrections.clear()

    async def _async_correct_child(
        self, entity_id: str, target_data: dict[str, Any]
    ) -> None:
        """Re-apply turn_on data after a child reports a stale state."""

        pending_data = self._pending_child_turn_on.get(entity_id)
        if pending_data != target_data:
            return

        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        if self._child_state_matches_target(state, target_data):
            self._clear_child_correction(entity_id)
            return

        try:
            await self.hass.services.async_call(
                LIGHT_DOMAIN,
                SERVICE_TURN_ON,
                target_data,
                blocking=True,
                context=self._context,
            )
            _LOGGER.debug(
                "Corrected state of `%s` after child state update with `%s`",
                entity_id,
                target_data,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "State correction for `%s` failed: %s; payload=%s",
                entity_id,
                exc,
                target_data,
            )

    @callback
    def async_write_ha_state(self) -> None:
        """Write the state to the state machine."""

        if self._is_frozen:
            return

        _LOGGER.debug(
            "Writing state of `%s` with brightness `%s`",
            self.entity_id,
            self._attr_brightness,
        )

        super().async_write_ha_state()


class LightenerControlledLight:
    """Represents a light entity managed by a LightnerLight."""

    def __init__(
        self: LightenerControlledLight,
        entity_id: str,
        config: dict,
        hass: HomeAssistant,
    ) -> None:
        """Create and instance of this class."""

        self.entity_id = entity_id
        self.hass = hass

        # Get the brightness configuration and prepare it for processing,
        brightness_config = prepare_brightness_config(config.get("brightness", {}))

        # Create the brightness conversion maps (from lightener to entity and from entity to lightener).
        self.levels = create_brightness_map(brightness_config)
        self.to_lightener_levels = create_reverse_brightness_map(
            brightness_config, self.levels
        )
        self.to_lightener_levels_on_off = create_reverse_brightness_map_on_off(
            self.to_lightener_levels
        )

    @property
    def type(self) -> str | None:
        """The entity type."""

        try:
            return get_light_type(self.hass, self.entity_id)
        except HomeAssistantError:
            return None

    def translate_brightness(self, brightness: int) -> int:
        """Calculate the entitiy brightness for the give Lightener brightness level."""

        level = self.levels.get(int(brightness))

        if self.type == TYPE_ONOFF:
            return 0 if level == 0 else 255

        return level

    def translate_brightness_back(self, brightness: int) -> list[int]:
        """Calculate all possible Lightener brightness levels for a give entity brightness."""

        if brightness is None:
            return []

        levels = self.to_lightener_levels.get(int(brightness))

        if self.type == TYPE_ONOFF:
            return self.to_lightener_levels_on_off[int(brightness)]

        return levels


def translate_config_to_brightness(config: dict) -> dict:
    """Create a copy of config converting the 0-100 range to 1-255.

    Convert the values to integers since the original values are strings.
    """

    return {
        value_to_brightness((1, 100), int(k)): 0
        if int(v) == 0
        else value_to_brightness((1, 100), int(v))
        for k, v in config.items()
    }


def prepare_brightness_config(config: dict) -> dict:
    """Convert the brightness configuration to a list of tuples and sorts it by the lightener level.

    Also add the default 0 and 255 levels if they are not present.
    """

    config = translate_config_to_brightness(config)

    # Zero must always be zero.
    config[0] = 0

    # If the maximum level is not present, add it.
    config.setdefault(255, 255)

    # Transform the dictionary into a list of tuples and sort it by the lightener level.
    config = sorted(config.items())

    return config


def create_brightness_map(config: list) -> dict:
    """Create a mapping of lightener levels to entity levels."""

    brightness_map = {0: 0}

    for i in range(1, len(config)):
        start, end = config[i - 1][0], config[i][0]
        start_value, end_value = config[i - 1][1], config[i][1]
        for j in range(start + 1, end + 1):
            brightness_map[j] = scale_ranged_value_to_int_range(
                (start, end), (start_value, end_value), j
            )

    return brightness_map


def create_reverse_brightness_map(config: list, lightener_levels: dict) -> dict:
    """Create a map with all entity level (from 0 to 255) to all possible lightener levels at each entity level.

    There can be multiple lightener levels for a single entity level.
    """

    # Initialize with all levels from 0 to 255.
    reverse_brightness_map = {i: [] for i in range(256)}

    # Initialize entries with all lightener levels (it goes from 0 to 255)
    for k, v in lightener_levels.items():
        reverse_brightness_map[v].append(k)

    # Now fill the gaps in the map by looping though the configured entity ranges
    for i in range(1, len(config)):
        start, end = config[i - 1][0], config[i][0]
        start_value, end_value = config[i - 1][1], config[i][1]

        # If there is an entity range to be covered
        if start_value != end_value:
            order = 1 if start_value < end_value else -1

            # Loop through the entity range
            for j in range(start_value, end_value + order, order):
                entity_level = scale_ranged_value_to_int_range(
                    (start_value, end_value), (start, end), j
                )
                # If the entry is not yet present for into that level, add it.
                if entity_level not in reverse_brightness_map[j]:
                    reverse_brightness_map[j].append(entity_level)

    return reverse_brightness_map


def create_reverse_brightness_map_on_off(reverse_map: dict) -> dict:
    """Create a reversed map dedicated to on/off lights."""

    # Build the "on" state out of all levels which are not in the "off" state.
    on_levels = [i for i in range(1, 256) if i not in reverse_map[0]]

    # The "on" levels are possible for all non-zero levels.
    reverse_map_on_off = dict.fromkeys(range(1, 256), on_levels)

    # The "off" matches the normal reverse map.
    reverse_map_on_off[0] = reverse_map[0]

    return reverse_map_on_off


def scale_ranged_value_to_int_range(
    source_range: tuple[float, float],
    target_range: tuple[float, float],
    value: float,
) -> int:
    """Scale a value from one range to another and return an integer."""

    # Unpack the original and target ranges
    (a, b) = source_range
    (c, d) = target_range

    # Calculate the conversion
    y = c + ((value - a) * (d - c)) / (b - a)
    return round(y)
