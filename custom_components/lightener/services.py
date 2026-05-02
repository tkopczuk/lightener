"""Services for the Lightener integration."""

from __future__ import annotations

from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_BRIGHTNESS,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
)
from homeassistant.core import HomeAssistant, ServiceCall, split_entity_id
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.service import verify_domain_control

from .const import DOMAIN, SERVICE_UPDATE_ENTITY_SETTINGS

BRIGHTNESS_CONFIG_SCHEMA = vol.Schema(
    {
        vol.All(vol.Coerce(int), vol.Range(min=1, max=100)): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100)
        )
    }
)

ENTITY_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_BRIGHTNESS): BRIGHTNESS_CONFIG_SCHEMA,
    },
    extra=vol.PREVENT_EXTRA,
)

UPDATE_ENTITY_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(CONF_FRIENDLY_NAME): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional(CONF_ENTITIES): vol.All(
            vol.Length(min=1),
            {cv.entity_id: ENTITY_SETTINGS_SCHEMA},
        ),
    },
    extra=vol.PREVENT_EXTRA,
)


@verify_domain_control(DOMAIN)
async def async_update_entity_settings(call: ServiceCall) -> None:
    """Update the settings of a Lightener entity."""

    hass = call.hass
    target_entity_id = call.data[ATTR_ENTITY_ID]

    if (
        CONF_FRIENDLY_NAME not in call.data
        and CONF_ENTITIES not in call.data
    ):
        raise ServiceValidationError(
            "Service call must include at least one setting to update"
        )

    config_entry = _get_lightener_config_entry(hass, target_entity_id)
    data: dict[str, Any] = dict(config_entry.data)

    if CONF_FRIENDLY_NAME in call.data:
        data[CONF_FRIENDLY_NAME] = call.data[CONF_FRIENDLY_NAME]

    if CONF_ENTITIES in call.data:
        entities = call.data[CONF_ENTITIES]
        _validate_controlled_entities(hass, target_entity_id, entities)
        data[CONF_ENTITIES] = _normalize_entities(entities)

    hass.config_entries.async_update_entry(
        config_entry,
        data=data,
        options=config_entry.options,
    )

    await hass.config_entries.async_reload(config_entry.entry_id)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register Lightener services."""

    if hass.services.has_service(DOMAIN, SERVICE_UPDATE_ENTITY_SETTINGS):
        return

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_ENTITY_SETTINGS,
        async_update_entity_settings,
        schema=UPDATE_ENTITY_SETTINGS_SCHEMA,
    )


def _get_lightener_config_entry(
    hass: HomeAssistant,
    entity_id: str,
) -> ConfigEntry:
    """Return the config entry for a Lightener entity."""

    entity_registry = async_get_entity_registry(hass)
    entity_entry = entity_registry.async_get(entity_id)

    if entity_entry is None or entity_entry.platform != DOMAIN:
        raise ServiceValidationError(
            f"Entity '{entity_id}' is not a Lightener entity"
        )

    if entity_entry.config_entry_id is None:
        raise ServiceValidationError(
            f"Entity '{entity_id}' is not managed by a config entry"
        )

    config_entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)

    if config_entry is None or config_entry.domain != DOMAIN:
        raise ServiceValidationError(
            f"Entity '{entity_id}' is not managed by Lightener"
        )

    return config_entry


def _validate_controlled_entities(
    hass: HomeAssistant,
    target_entity_id: str,
    entities: dict[str, Any],
) -> None:
    """Validate controlled entities before saving settings."""

    entity_registry = async_get_entity_registry(hass)

    for entity_id in entities:
        domain, _ = split_entity_id(entity_id)

        if domain != LIGHT_DOMAIN:
            raise ServiceValidationError(
                f"Controlled entity '{entity_id}' is not a light entity"
            )

        if entity_id == target_entity_id:
            raise ServiceValidationError(
                f"Controlled entity '{entity_id}' cannot be the Lightener entity"
            )

        if (
            hass.states.get(entity_id) is None
            and entity_registry.async_get(entity_id) is None
        ):
            raise ServiceValidationError(
                f"Controlled entity '{entity_id}' does not exist"
            )


def _normalize_entities(entities: dict[str, Any]) -> dict[str, dict[str, dict]]:
    """Normalize service data to the config entry data shape."""

    normalized_entities = {}

    for entity_id, settings in entities.items():
        brightness = settings.get(CONF_BRIGHTNESS, {})
        normalized_entities[entity_id] = {
            CONF_BRIGHTNESS: {
                str(lightener_level): str(entity_level)
                for lightener_level, entity_level in brightness.items()
            }
        }

    return normalized_entities
