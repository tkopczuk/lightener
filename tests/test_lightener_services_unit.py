"""Unit tests for Lightener service helpers."""

import pytest
import voluptuous as vol
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_BRIGHTNESS,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
)

from custom_components.lightener.services import (
    UPDATE_ENTITY_SETTINGS_SCHEMA,
    _normalize_entities,
)


def test_update_entity_settings_schema_accepts_valid_settings() -> None:
    """Validate service settings and coerce brightness percentages to integers."""

    result = UPDATE_ENTITY_SETTINGS_SCHEMA(
        {
            ATTR_ENTITY_ID: "light.test",
            CONF_FRIENDLY_NAME: "Living Room",
            CONF_ENTITIES: {
                "light.test1": {CONF_BRIGHTNESS: {"50": "0"}},
            },
        }
    )

    assert result == {
        ATTR_ENTITY_ID: "light.test",
        CONF_FRIENDLY_NAME: "Living Room",
        CONF_ENTITIES: {
            "light.test1": {CONF_BRIGHTNESS: {50: 0}},
        },
    }


@pytest.mark.parametrize(
    "payload",
    [
        {CONF_ENTITIES: {"light.test1": {CONF_BRIGHTNESS: {"0": "50"}}}},
        {CONF_ENTITIES: {"light.test1": {CONF_BRIGHTNESS: {"101": "50"}}}},
        {CONF_ENTITIES: {"light.test1": {CONF_BRIGHTNESS: {"50": "-1"}}}},
        {CONF_ENTITIES: {"light.test1": {CONF_BRIGHTNESS: {"50": "101"}}}},
        {CONF_ENTITIES: {"light.test1": {"unknown": {"50": "100"}}}},
        {CONF_ENTITIES: {}},
        {"unknown": "value"},
    ],
)
def test_update_entity_settings_schema_rejects_invalid_settings(payload) -> None:
    """Reject invalid service options before the handler runs."""

    with pytest.raises(vol.Invalid):
        UPDATE_ENTITY_SETTINGS_SCHEMA(
            {
                ATTR_ENTITY_ID: "light.test",
                **payload,
            }
        )


def test_normalize_entities_uses_config_entry_shape() -> None:
    """Normalize service data to match config flow storage."""

    assert _normalize_entities(
        {
            "light.test1": {CONF_BRIGHTNESS: {50: 0}},
            "light.test2": {},
        }
    ) == {
        "light.test1": {CONF_BRIGHTNESS: {"50": "0"}},
        "light.test2": {CONF_BRIGHTNESS: {}},
    }
