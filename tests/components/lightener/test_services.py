"""Tests for Lightener services."""

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_BRIGHTNESS,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.lightener.const import DOMAIN, SERVICE_UPDATE_ENTITY_SETTINGS


async def test_update_entity_settings_service_updates_entry_and_reloads(
    hass: HomeAssistant,
    create_lightener,
) -> None:
    """Update all settings exposed by the Lightener UI through a service call."""

    lightener = await create_lightener(
        config={
            CONF_FRIENDLY_NAME: "Test",
            CONF_ENTITIES: {
                "light.test1": {"50": "100"},
            },
        }
    )
    config_entry = hass.config_entries.async_entries(DOMAIN)[0]

    await hass.services.async_call(
        DOMAIN,
        SERVICE_UPDATE_ENTITY_SETTINGS,
        {
            ATTR_ENTITY_ID: lightener.entity_id,
            CONF_FRIENDLY_NAME: "Updated Test",
            CONF_ENTITIES: {
                "light.test2": {CONF_BRIGHTNESS: {50: 0}},
                "light.test_onoff": {CONF_BRIGHTNESS: {"25": "100"}},
            },
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert dict(config_entry.data) == {
        CONF_FRIENDLY_NAME: "Updated Test",
        CONF_ENTITIES: {
            "light.test2": {CONF_BRIGHTNESS: {"50": "0"}},
            "light.test_onoff": {CONF_BRIGHTNESS: {"25": "100"}},
        },
    }

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {
            ATTR_ENTITY_ID: lightener.entity_id,
            ATTR_BRIGHTNESS: 255,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get("light.test1").state == "off"
    assert hass.states.get("light.test2").state == "on"
    assert hass.states.get("light.test_onoff").state == "on"


async def test_update_entity_settings_service_accepts_partial_updates(
    hass: HomeAssistant,
    create_lightener,
) -> None:
    """Update one setting without replacing settings that were not supplied."""

    lightener = await create_lightener(
        config={
            CONF_FRIENDLY_NAME: "Test",
            CONF_ENTITIES: {
                "light.test1": {"50": "100"},
            },
        }
    )
    config_entry = hass.config_entries.async_entries(DOMAIN)[0]

    await hass.services.async_call(
        DOMAIN,
        SERVICE_UPDATE_ENTITY_SETTINGS,
        {
            ATTR_ENTITY_ID: lightener.entity_id,
            CONF_FRIENDLY_NAME: "Renamed Test",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert dict(config_entry.data) == {
        CONF_FRIENDLY_NAME: "Renamed Test",
        CONF_ENTITIES: {
            "light.test1": {CONF_BRIGHTNESS: {"50": "100"}},
        },
    }


async def test_update_entity_settings_service_rejects_non_lightener_entity(
    hass: HomeAssistant,
    create_lightener,
) -> None:
    """Reject service calls targeting a light that is not managed by Lightener."""

    await create_lightener()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_UPDATE_ENTITY_SETTINGS,
            {
                ATTR_ENTITY_ID: "light.test1",
                CONF_FRIENDLY_NAME: "Wrong Target",
            },
            blocking=True,
        )


async def test_update_entity_settings_service_rejects_unknown_controlled_entity(
    hass: HomeAssistant,
    create_lightener,
) -> None:
    """Reject controlled lights that do not exist."""

    lightener = await create_lightener()
    config_entry = hass.config_entries.async_entries(DOMAIN)[0]
    original_data = dict(config_entry.data)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_UPDATE_ENTITY_SETTINGS,
            {
                ATTR_ENTITY_ID: lightener.entity_id,
                CONF_ENTITIES: {
                    "light.missing": {CONF_BRIGHTNESS: {"50": "100"}},
                },
            },
            blocking=True,
        )

    assert dict(config_entry.data) == original_data


@pytest.mark.parametrize("controlled_entity_id", ["switch.test", "light.test"])
async def test_update_entity_settings_service_rejects_invalid_controlled_entity(
    hass: HomeAssistant,
    create_lightener,
    controlled_entity_id: str,
) -> None:
    """Reject controlled entities outside the UI-selectable light set."""

    lightener = await create_lightener()
    config_entry = hass.config_entries.async_entries(DOMAIN)[0]
    original_data = dict(config_entry.data)

    if controlled_entity_id == "light.test":
        controlled_entity_id = lightener.entity_id

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_UPDATE_ENTITY_SETTINGS,
            {
                ATTR_ENTITY_ID: lightener.entity_id,
                CONF_ENTITIES: {
                    controlled_entity_id: {CONF_BRIGHTNESS: {"50": "100"}},
                },
            },
            blocking=True,
        )

    assert dict(config_entry.data) == original_data


@pytest.mark.parametrize(
    "service_data",
    [
        {CONF_ENTITIES: {"light.test1": {CONF_BRIGHTNESS: {"0": "50"}}}},
        {CONF_ENTITIES: {"light.test1": {CONF_BRIGHTNESS: {"101": "50"}}}},
        {CONF_ENTITIES: {"light.test1": {CONF_BRIGHTNESS: {"50": "-1"}}}},
        {CONF_ENTITIES: {"light.test1": {CONF_BRIGHTNESS: {"50": "101"}}}},
        {CONF_ENTITIES: {"light.test1": {"unknown": {"50": "100"}}}},
        {"unknown": "value"},
    ],
)
async def test_update_entity_settings_service_rejects_invalid_options(
    hass: HomeAssistant,
    create_lightener,
    service_data: dict[str, Any],
) -> None:
    """Reject unknown options and invalid brightness percentages."""

    lightener = await create_lightener()

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_UPDATE_ENTITY_SETTINGS,
            {
                ATTR_ENTITY_ID: lightener.entity_id,
                **service_data,
            },
            blocking=True,
        )


async def test_update_entity_settings_service_rejects_empty_update(
    hass: HomeAssistant,
    create_lightener,
) -> None:
    """Reject calls that identify a Lightener entity but do not change settings."""

    lightener = await create_lightener()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_UPDATE_ENTITY_SETTINGS,
            {
                ATTR_ENTITY_ID: lightener.entity_id,
            },
            blocking=True,
        )
