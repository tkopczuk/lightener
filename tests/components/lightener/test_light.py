"""Tests for the light platform."""

import asyncio
from unittest.mock import ANY, Mock, patch
from uuid import uuid4

import pytest
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    ColorMode,
)
from homeassistant.components.light import DATA_PROFILES, Profile
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, ServiceRegistry, State
from homeassistant.helpers.restore_state import (
    RestoredExtraData,
    RestoreEntity,
    StoredState,
    async_get as async_get_restore_state,
)
from homeassistant.util import dt as dt_util

from custom_components.lightener.const import TYPE_DIMMABLE, TYPE_ONOFF
from custom_components.lightener.light import (
    LightenerControlledLight,
    LightenerLight,
    async_setup_platform,
    create_brightness_map,
    create_reverse_brightness_map,
    create_reverse_brightness_map_on_off,
    prepare_brightness_config,
    scale_ranged_value_to_int_range,
    translate_config_to_brightness,
)


async def test_turn_on_resilient_to_single_failure(
    hass: HomeAssistant, create_lightener
):
    """Ensure a failure in one entity service call does not cancel other calls."""

    # Create a lightener with two lights
    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {
                "light.test1": {},
                "light.test2": {},
            },
        }
    )

    calls: list[tuple] = []

    # Capture original class method so successful calls can delegate
    orig_async_call = ServiceRegistry.async_call

    async def fake_async_call(self, domain, service, data, blocking=True, context=None):
        calls.append((domain, service, data.get(ATTR_ENTITY_ID)))
        if data.get(ATTR_ENTITY_ID) == "light.test1":
            raise RuntimeError("boom")
        return await orig_async_call(
            self, domain, service, data, blocking=blocking, context=context
        )

    # Patch the class method with autospec so `self` is passed
    with patch.object(
        ServiceRegistry, "async_call", side_effect=fake_async_call, autospec=True
    ):
        await lightener.async_turn_on(brightness=128)
        await hass.async_block_till_done()

    # Both calls were attempted (order not guaranteed due to concurrency)
    attempted = sorted([c[2] for c in calls])
    assert attempted == ["light.test1", "light.test2"]

    # light.test1 failed
    assert hass.states.get("light.test1").state == "off"

    # light.test2 should have ended up on despite light.test1 failing
    assert hass.states.get("light.test2").state == "on"


###########################################################
### LightenerLight class only tests


async def test_lightener_light_properties(hass):
    """Test all the basic properties of the LightenerLight class."""

    config = {"friendly_name": "Living Room"}
    unique_id = str(uuid4())

    lightener = LightenerLight(hass, config, unique_id)

    assert lightener.unique_id == unique_id

    # Name must be empty so it'll be taken from the device
    assert lightener.name is None
    assert lightener.device_info["name"] == "Living Room"

    assert lightener.should_poll is False
    assert lightener.has_entity_name is True

    assert lightener.icon == "mdi:lightbulb-group"


async def test_lightener_light_properties_no_unique_id(hass):
    """Test all the basic properties of the LightenerLight class when no unique id is provided."""

    config = {"friendly_name": "Living Room"}

    lightener = LightenerLight(hass, config)

    assert lightener.unique_id is None
    assert lightener.device_info is None
    assert lightener.name == "Living Room"


async def test_lightener_light_turn_on(hass: HomeAssistant, create_lightener):
    """Test the state changes of the LightenerLight class when turned on."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {
                "light.test1": {},
                "light.test2": {},
            },
        }
    )

    await lightener.async_turn_on()
    await hass.async_block_till_done()

    assert hass.states.get("light.test1").state == "on"
    assert hass.states.get("light.test2").state == "on"


async def test_lightener_light_turn_on_forward(hass: HomeAssistant, create_lightener):  # pylint: disable=unused-argument
    """Test if passed arguments are forwared when turned on."""

    lightener: LightenerLight = await create_lightener()

    with patch.object(ServiceRegistry, "async_call") as async_call_mock:
        await lightener.async_turn_on(
            brightness=50, effect="blink", color_temp_kelvin=3000
        )

    async_call_mock.assert_called_once_with(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {
            ATTR_ENTITY_ID: "light.test1",
            "brightness": 50,
            "effect": "blink",
            "color_temp_kelvin": 3000,
        },
        blocking=True,
        context=ANY,
    )


async def test_lightener_light_turn_on_go_off_if_brightness_0(
    hass: HomeAssistant, create_lightener
):
    """Test that turned on sends brightness 0 if the controlled light is on."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"50": "0"}},
        }
    )

    hass.states.async_set(entity_id="light.test1", new_state="on")

    await lightener.async_turn_on(brightness=1)
    await hass.async_block_till_done()

    assert hass.states.get("light.test1").state == "off"


async def test_lightener_light_turn_on_keeps_own_state_when_all_children_map_off(
    hass: HomeAssistant, create_lightener
):
    """Test Lightener stays on when its brightness maps every child light off."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {
                "light.test1": {"50": "0"},
                "light.test2": {"50": "0"},
            },
        }
    )

    await lightener.async_turn_on(brightness=1)
    await hass.async_block_till_done()

    assert hass.states.get("light.test1").state == "off"
    assert hass.states.get("light.test2").state == "off"
    assert lightener.is_on is True
    assert lightener.brightness == 1
    assert hass.states.get(lightener.entity_id).state == "on"
    assert hass.states.get(lightener.entity_id).attributes[ATTR_BRIGHTNESS] == 1


async def test_lightener_light_turn_on_without_known_brightness_does_not_default_to_full(
    hass: HomeAssistant, create_lightener
):
    """Test first plain turn_on does not invent a full-brightness level."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "0"}},
        }
    )
    calls: list[tuple[str, dict]] = []

    async def fake_async_call(
        self, domain, service, data, blocking=True, context=None
    ):  # pylint: disable=unused-argument
        calls.append((service, data.copy()))

    with patch.object(
        ServiceRegistry, "async_call", side_effect=fake_async_call, autospec=True
    ):
        await lightener.async_turn_on()
        await hass.async_block_till_done()

    assert calls == [(SERVICE_TURN_ON, {ATTR_ENTITY_ID: "light.test1"})]
    assert lightener.is_on is True
    assert lightener.brightness is None


async def test_lightener_light_turn_on_uses_restored_brightness(
    hass: HomeAssistant, create_lightener
):
    """Test first plain turn_on uses the last restored Lightener brightness."""

    async_get_restore_state(hass).last_states["light.test"] = StoredState(
        State("light.test", "off", {}),
        RestoredExtraData({"preferred_brightness": 178}),
        dt_util.utcnow(),
    )
    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )
    calls: list[tuple[str, dict]] = []

    async def fake_async_call(
        self, domain, service, data, blocking=True, context=None
    ):  # pylint: disable=unused-argument
        calls.append((service, data.copy()))

    with patch.object(
        ServiceRegistry, "async_call", side_effect=fake_async_call, autospec=True
    ):
        await lightener.async_turn_on()
        await hass.async_block_till_done()

    assert isinstance(lightener, RestoreEntity)
    assert lightener.brightness == 178
    assert calls == [
        (SERVICE_TURN_ON, {ATTR_ENTITY_ID: "light.test1", ATTR_BRIGHTNESS: 124})
    ]


async def test_lightener_light_restore_data_keeps_last_brightness_when_off(
    hass: HomeAssistant, create_lightener
):
    """Test restore data keeps preferred brightness after Lightener turns off."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )

    await lightener.async_turn_on(brightness=178)
    await hass.async_block_till_done()

    await lightener.async_turn_off()
    await hass.async_block_till_done()

    assert lightener.brightness is None
    assert lightener.extra_restore_state_data.as_dict() == {
        "preferred_brightness": 178
    }


async def test_lightener_light_service_turn_on_maps_default_profile_brightness(
    hass: HomeAssistant, create_lightener
):
    """Test HA light.turn_on default brightness is mapped to controlled lights."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )
    hass.data[DATA_PROFILES].data[f"{lightener.entity_id}.default"] = Profile(
        f"{lightener.entity_id}.default",
        None,
        None,
        178,
    )

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: lightener.entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(lightener.entity_id).state == "on"
    assert hass.states.get(lightener.entity_id).attributes[ATTR_BRIGHTNESS] == 178
    assert hass.states.get("light.test1").state == "on"
    assert hass.states.get("light.test1").attributes[ATTR_BRIGHTNESS] == 124


async def test_lightener_light_service_turn_on_maps_remembered_brightness(
    hass: HomeAssistant, create_lightener
):
    """Test HA light.turn_on maps Lightener's remembered brightness."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: lightener.entity_id, ATTR_BRIGHTNESS: 178},
        blocking=True,
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: lightener.entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: lightener.entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(lightener.entity_id).state == "on"
    assert hass.states.get(lightener.entity_id).attributes[ATTR_BRIGHTNESS] == 178
    assert hass.states.get("light.test1").state == "on"
    assert hass.states.get("light.test1").attributes[ATTR_BRIGHTNESS] == 124


async def test_lightener_light_service_turn_on_updates_parent_brightness_while_on(
    hass: HomeAssistant, create_lightener
):
    """Test Lightener state stores brightness changes made to the Lightener entity."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: lightener.entity_id, ATTR_BRIGHTNESS: 255},
        blocking=True,
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: lightener.entity_id, ATTR_BRIGHTNESS: 178},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert lightener.brightness == 178
    assert hass.states.get(lightener.entity_id).attributes[ATTR_BRIGHTNESS] == 178
    assert hass.states.get("light.test1").attributes[ATTR_BRIGHTNESS] == 124


async def test_lightener_light_service_turn_on_preserves_adaptive_brightness(
    hass: HomeAssistant, create_lightener
):
    """Test an external turn_on brightness adaptation updates Lightener state."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: lightener.entity_id, ATTR_BRIGHTNESS: 255},
        blocking=True,
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {
            ATTR_ENTITY_ID: lightener.entity_id,
            ATTR_BRIGHTNESS_PCT: 40,
            ATTR_TRANSITION: 1,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert lightener.brightness == 102
    assert hass.states.get(lightener.entity_id).attributes[ATTR_BRIGHTNESS] == 102
    assert hass.states.get("light.test1").attributes[ATTR_BRIGHTNESS] == 71


async def test_lightener_light_turn_on_preserves_intercepted_adaptive_brightness(
    hass: HomeAssistant, create_lightener
):
    """Test Adaptive Lighting-style intercepted kwargs update Lightener state."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )

    await lightener.async_turn_on(brightness=255)
    await hass.async_block_till_done()

    await lightener.async_turn_on(brightness=102, transition=1)
    await hass.async_block_till_done()

    assert lightener.brightness == 102
    assert hass.states.get(lightener.entity_id).attributes[ATTR_BRIGHTNESS] == 102
    assert hass.states.get("light.test1").attributes[ATTR_BRIGHTNESS] == 71


async def test_lightener_light_turn_on_writes_parent_state_once_without_child_updates(
    hass: HomeAssistant, create_lightener
):
    """Test turn_on writes one parent state update when children do not update."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )

    async def fake_async_call(
        self, domain, service, data, blocking=True, context=None
    ):  # pylint: disable=unused-argument
        return None

    with patch.object(
        ServiceRegistry, "async_call", side_effect=fake_async_call, autospec=True
    ), patch.object(
        lightener, "async_write_ha_state", wraps=lightener.async_write_ha_state
    ) as async_write_ha_state:
        await lightener.async_turn_on(brightness=178)
        await hass.async_block_till_done()

    assert async_write_ha_state.call_count == 1
    assert lightener.brightness == 178
    assert hass.states.get(lightener.entity_id).attributes[ATTR_BRIGHTNESS] == 178


async def test_lightener_light_turn_on_corrects_stale_child_brightness(
    hass: HomeAssistant, create_lightener
):
    """Test child state changes drive stale brightness correction."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )
    calls: list[dict] = []

    async def fake_async_call(
        self, domain, service, data, blocking=True, context=None
    ):  # pylint: disable=unused-argument
        calls.append(data.copy())

        if (
            domain == LIGHT_DOMAIN
            and service == SERVICE_TURN_ON
            and data[ATTR_ENTITY_ID] == "light.test1"
            and len(calls) == 2
        ):
            hass.states.async_set(
                "light.test1",
                "on",
                attributes={ATTR_BRIGHTNESS: data[ATTR_BRIGHTNESS]},
            )

    with patch.object(
        ServiceRegistry, "async_call", side_effect=fake_async_call, autospec=True
    ):
        await lightener.async_turn_on(brightness=178)
        await hass.async_block_till_done()

        assert calls == [{ATTR_ENTITY_ID: "light.test1", ATTR_BRIGHTNESS: 124}]

        hass.states.async_set(
            "light.test1", "on", attributes={ATTR_BRIGHTNESS: 39}
        )
        await hass.async_block_till_done()

    assert calls == [
        {ATTR_ENTITY_ID: "light.test1", ATTR_BRIGHTNESS: 124},
        {ATTR_ENTITY_ID: "light.test1", ATTR_BRIGHTNESS: 124},
    ]
    assert hass.states.get("light.test1").attributes[ATTR_BRIGHTNESS] == 124


async def test_lightener_light_debounces_repeated_child_corrections(
    hass: HomeAssistant, create_lightener
):
    """Test first stale child correction is immediate and later ones debounce."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"100": "70"}},
        }
    )
    lightener._child_correction_debounce = 0.05  # pylint: disable=protected-access
    calls: list[dict] = []

    async def fake_async_call(
        self, domain, service, data, blocking=True, context=None
    ):  # pylint: disable=unused-argument
        calls.append(data.copy())

    with patch.object(
        ServiceRegistry, "async_call", side_effect=fake_async_call, autospec=True
    ):
        await lightener.async_turn_on(brightness=178)
        await hass.async_block_till_done()

        hass.states.async_set(
            "light.test1", "on", attributes={ATTR_BRIGHTNESS: 39}
        )
        await hass.async_block_till_done()
        assert len(calls) == 2

        hass.states.async_set(
            "light.test1", "on", attributes={ATTR_BRIGHTNESS: 40}
        )
        await asyncio.sleep(0)
        assert len(calls) == 2

        await asyncio.sleep(0.06)
        await hass.async_block_till_done()

    assert calls == [
        {ATTR_ENTITY_ID: "light.test1", ATTR_BRIGHTNESS: 124},
        {ATTR_ENTITY_ID: "light.test1", ATTR_BRIGHTNESS: 124},
        {ATTR_ENTITY_ID: "light.test1", ATTR_BRIGHTNESS: 124},
    ]


async def test_lightener_light_child_correction_reapplies_color(
    hass: HomeAssistant, create_lightener
):
    """Test stale child color is corrected with the full turn_on payload."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test_temp": {}},
        }
    )
    calls: list[dict] = []

    async def fake_async_call(
        self, domain, service, data, blocking=True, context=None
    ):  # pylint: disable=unused-argument
        calls.append(data.copy())

    with patch.object(
        ServiceRegistry, "async_call", side_effect=fake_async_call, autospec=True
    ):
        await lightener.async_turn_on(
            brightness=178,
            rgb_color=(10, 20, 30),
        )
        await hass.async_block_till_done()

        assert calls == [
            {
                ATTR_ENTITY_ID: "light.test_temp",
                ATTR_BRIGHTNESS: 178,
                ATTR_RGB_COLOR: (10, 20, 30),
            }
        ]

        hass.states.async_set(
            "light.test_temp",
            "on",
            attributes={
                ATTR_BRIGHTNESS: 178,
                ATTR_RGB_COLOR: (1, 2, 3),
            },
        )
        await hass.async_block_till_done()

    assert calls == [
        {
            ATTR_ENTITY_ID: "light.test_temp",
            ATTR_BRIGHTNESS: 178,
            ATTR_RGB_COLOR: (10, 20, 30),
        },
        {
            ATTR_ENTITY_ID: "light.test_temp",
            ATTR_BRIGHTNESS: 178,
            ATTR_RGB_COLOR: (10, 20, 30),
        },
    ]


async def test_lightener_light_turn_on_translate_brightness(
    hass: HomeAssistant, create_lightener
):
    """Test that turned on sends brightness 0 if the controlled light is on."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"50": "0"}},
        }
    )
    hass.states.async_set(entity_id="light.test1", new_state="on")

    await lightener.async_turn_on(brightness=192)
    await hass.async_block_till_done()

    assert hass.states.get("light.test1").state == "on"
    assert hass.states.get("light.test1").attributes["brightness"] == 129


async def test_lightener_light_child_changes_do_not_replace_own_brightness(
    hass: HomeAssistant, create_lightener
):
    """Test child light changes do not replace Lightener's own brightness."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"50": "0"}},
        }
    )

    await lightener.async_turn_on(brightness=192)
    await hass.async_block_till_done()

    hass.states.async_set(
        entity_id="light.test1", new_state="on", attributes={ATTR_BRIGHTNESS: 1}
    )

    lightener.async_update_group_state()

    assert lightener.is_on is True
    assert lightener.brightness == 192


async def test_lightener_light_child_turning_on_does_not_turn_parent_on(
    hass: HomeAssistant, create_lightener
):
    """Test external child changes do not turn on the independent Lightener."""

    lightener: LightenerLight = await create_lightener()

    hass.states.async_set(
        entity_id="light.test1", new_state="on", attributes={ATTR_BRIGHTNESS: 200}
    )

    lightener.async_update_group_state()

    assert lightener.is_on is False
    assert lightener.brightness is None


async def test_lightener_light_turn_on_go_off_if_brightness_0_transition(
    hass: HomeAssistant, create_lightener
):
    """Test that turned on sends brightness 0 if the controlled light is on."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"50": "0"}},
        }
    )

    hass.states.async_set(entity_id="light.test1", new_state="on")

    with patch.object(ServiceRegistry, "async_call") as async_call_mock:
        await lightener.async_turn_on(brightness=1, transition=10)

    async_call_mock.assert_called_once_with(
        LIGHT_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "light.test1", ATTR_TRANSITION: 10},
        blocking=True,
        context=ANY,
    )


async def test_lightener_light_color_mode_xy(hass: HomeAssistant, create_lightener):
    """Test Lightener inherits child color mode while it is on."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {}},
        }
    )

    hass.states.async_set(
        entity_id="light.test1",
        new_state="on",
        attributes={"color_mode": ColorMode.XY},
    )
    lightener._attr_is_on = True  # pylint: disable=protected-access

    lightener.async_update_group_state()

    assert hass.states.get("light.test1").attributes["color_mode"] == ColorMode.XY

    assert lightener.color_mode == ColorMode.XY
    assert lightener.supported_color_modes == {ColorMode.XY}


async def test_lightener_light_color_mode_onoff(hass: HomeAssistant, create_lightener):
    """Test that Lightener keeps its color mode to BRIGHTNESS with an ONOFF controlled light."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test_onoff": {}},
        }
    )

    hass.states.async_set(
        entity_id="light.test_onoff",
        new_state="on",
        attributes={"color_mode": ColorMode.ONOFF},
    )

    await lightener.async_turn_on(brightness=1)
    await hass.async_block_till_done()

    assert lightener.color_mode == ColorMode.BRIGHTNESS
    assert lightener.supported_color_modes == {ColorMode.BRIGHTNESS}

    assert (
        hass.states.get("light.test_onoff").attributes["color_mode"] == ColorMode.ONOFF
    )

    # Assert that the color_mode goes to null when the light is turned off
    await lightener.async_turn_off()
    await hass.async_block_till_done()

    assert lightener.color_mode is None
    assert lightener.supported_color_modes == {ColorMode.BRIGHTNESS}


async def test_lightener_light_color_mode_unknown(
    hass: HomeAssistant, create_lightener
):
    """Test Lightener uses a supported mode when child color_mode is UNKNOWN."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test_temp": {}},
        }
    )

    hass.states.async_set(
        entity_id="light.test_temp",
        new_state="on",
        attributes={"color_mode": ColorMode.UNKNOWN},
    )

    await lightener.async_turn_on(brightness=1)
    await hass.async_block_till_done()

    assert lightener.color_mode == ColorMode.COLOR_TEMP

    assert (
        hass.states.get("light.test_temp").attributes["color_mode"] == ColorMode.UNKNOWN
    )

    # Assert that the color_mode goes to null when the light is turned off
    await lightener.async_turn_off()
    await hass.async_block_till_done()

    assert lightener.color_mode is None


async def test_lightener_light_color_mode_unknown_uses_supported_child_mode(
    hass: HomeAssistant, create_lightener
):
    """Test unknown child color mode does not force unsupported brightness mode."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.color_temp_only": {}},
        }
    )

    hass.states.async_set(
        entity_id="light.color_temp_only",
        new_state="off",
        attributes={ATTR_SUPPORTED_COLOR_MODES: [ColorMode.COLOR_TEMP]},
    )
    lightener.async_update_group_state()
    lightener._attr_is_on = True  # pylint: disable=protected-access

    assert lightener.supported_color_modes == {ColorMode.COLOR_TEMP}
    assert lightener.color_mode == ColorMode.COLOR_TEMP


async def test_lightener_light_async_update_group_state(
    hass: HomeAssistant, create_lightener
):
    """Test child updates preserve Lightener's own state and brightness."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"50": "0"}},
        }
    )

    lightener._attr_brightness = 150  # pylint: disable=protected-access
    lightener._attr_is_on = True  # pylint: disable=protected-access

    hass.states.async_set(
        entity_id="light.test1", new_state="on", attributes={"color_temp_kelvin": 3000}
    )

    lightener.async_update_group_state()

    assert lightener.is_on is True
    assert lightener.color_temp_kelvin == 3000

    assert lightener.brightness == 150

    hass.states.async_set(
        entity_id="light.test1", new_state="on", attributes={"brightness": 255}
    )

    lightener.async_update_group_state()

    assert lightener.brightness == 150

    hass.states.async_set(
        entity_id="light.test1", new_state="on", attributes={"brightness": 1}
    )

    lightener.async_update_group_state()

    assert lightener.brightness == 150

    hass.states.async_set(
        entity_id="light.test1", new_state="on", attributes={"brightness": 0}
    )

    lightener.async_update_group_state()

    assert lightener.is_on is True
    assert lightener.brightness == 150


async def test_lightener_light_async_update_group_state_zero(
    hass: HomeAssistant, create_lightener
):
    """Test zero child brightness does not replace Lightener brightness."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {}},
        }
    )

    lightener._attr_brightness = 150  # pylint: disable=protected-access

    hass.states.async_set(
        entity_id="light.test1", new_state="on", attributes={"brightness": 0}
    )

    lightener.async_update_group_state()

    assert lightener.brightness == 150


async def test_lightener_light_async_update_group_state_unavailable(
    hass: HomeAssistant, create_lightener
):
    """Test unavailable children do not replace Lightener brightness."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"50": "0"}, "light.I_DONT_EXIST": {}},
        }
    )

    lightener._attr_brightness = 150  # pylint: disable=protected-access

    hass.states.async_set(
        entity_id="light.test1", new_state="on", attributes={"brightness": 1}
    )

    lightener.async_update_group_state()

    assert lightener.brightness == 150


async def test_lightener_light_async_update_group_state_no_match_no_change(
    hass: HomeAssistant, create_lightener
):
    """Test child brightness combinations do not replace Lightener brightness."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"50": "0"}, "light.test2": {"10": "100"}},
        }
    )

    def test(test1: int, test2: int):
        lightener._attr_brightness = 150  # pylint: disable=protected-access

        hass.states.async_set(
            entity_id="light.test1", new_state="on", attributes={"brightness": test1}
        )

        hass.states.async_set(
            entity_id="light.test2", new_state="on", attributes={"brightness": test2}
        )

        lightener.async_update_group_state()

        assert lightener.brightness == 150

    test(0, 29)
    test(1, 255)
    test(129, 1)
    test(1, 254)
    test(1, 1)
    test(1, None)


@pytest.mark.parametrize(
    "test1, current",
    [
        (0, 10),
        (0, 20),
        (128, 20),
        (255, 200),
        (255, 255),
    ],
)
async def test_lightener_light_async_update_group_state_preserves_current_brightness(
    test1, current, hass: HomeAssistant, create_lightener
):
    """Test child updates preserve Lightener's current brightness."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test1": {"50": "0", "60": "100"}},
        }
    )

    lightener._attr_brightness = current  # pylint: disable=protected-access
    lightener._preferred_brightness = current  # pylint: disable=protected-access

    hass.states.async_set(
        entity_id="light.test1", new_state="on", attributes={"brightness": test1}
    )

    lightener.async_update_group_state()

    assert lightener.brightness == current


async def test_lightener_light_async_update_group_state_onoff(
    hass: HomeAssistant, create_lightener
):
    """Test ONOFF children still produce brightness color mode while on."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {"light.test_onoff": {}},
        }
    )

    lightener._attr_is_on = True  # pylint: disable=protected-access

    hass.states.async_set(
        entity_id="light.test_onoff",
        new_state="on",
        attributes={"color_mode": ColorMode.ONOFF},
    )

    lightener.async_update_group_state()

    assert lightener.color_mode == ColorMode.BRIGHTNESS
    assert lightener.supported_color_modes == {ColorMode.BRIGHTNESS}


###########################################################
### LightenerControlledLight class only tests


async def test_lightener_light_entity_properties(hass):
    """Test all the basic properties of the LightenerLight class."""

    light = LightenerControlledLight("light.test1", {"brightness": {"10": "20"}}, hass)

    assert light.entity_id == "light.test1"


async def test_lightener_light_entity_calculated_levels(hass):
    """Test the calculation of brigthness levels."""

    light = LightenerControlledLight(
        "light.test1",
        {
            "brightness": {
                "10": "100",
            }
        },
        hass,
    )

    assert light.levels[0] == 0
    assert light.levels[13] == 128
    assert light.levels[25] == 245
    assert light.levels[26] == 255
    assert light.levels[27] == 255
    assert light.levels[100] == 255
    assert light.levels[255] == 255

    light = LightenerControlledLight(
        "light.test1",
        {
            "brightness": {
                "100": "0",  # Test the ordering
                "10": "10",
                "50": "100",
            }
        },
        hass,
    )

    assert light.levels[0] == 0
    assert light.levels[15] == 15
    assert light.levels[26] == 26
    assert light.levels[27] == 28
    assert light.levels[128] == 255
    assert light.levels[129] == 253
    assert light.levels[255] == 0


async def test_lightener_light_entity_calculated_to_lightner_levels(hass):
    """Test the calculation of brigthness levels."""

    light = LightenerControlledLight(
        "light.test1",
        {
            "brightness": {
                "10": "100"  # 26: 255
            }
        },
        hass,
    )

    assert light.to_lightener_levels[0] == [0]
    assert light.to_lightener_levels[26] == [3]
    assert light.to_lightener_levels[253] == [26]
    assert light.to_lightener_levels[254] == [26]
    assert light.to_lightener_levels[255] == list(range(26, 256))

    light = LightenerControlledLight(
        "light.test1",
        {
            "brightness": {
                "100": "0",  # Test the ordering
                "10": "10",
                "50": "100",
            }
        },
        hass,
    )

    assert light.to_lightener_levels[0] == [0, 255]
    assert light.to_lightener_levels[26] == [26, 242]
    assert light.to_lightener_levels[255] == [128]

    assert light.to_lightener_levels[3] == [3, 254]
    assert light.to_lightener_levels[10] == [10, 250]


@pytest.mark.parametrize(
    "entity_id, expected_type",
    [
        ("light.test1", TYPE_DIMMABLE),
        ("light.test_onoff", TYPE_ONOFF),
    ],
)
async def test_lightener_light_entity_type(entity_id, expected_type, hass):
    """Test translate_brightness_back with float values."""

    light = LightenerControlledLight(
        entity_id,
        {},
        hass,
    )

    assert light.type is expected_type


@pytest.mark.parametrize(
    "lightener_level, light_level",
    [
        (0, 0),
        (1, 10),
        (26, 255),
        (39, 122),
        (255, 0),
    ],
)
async def test_lightener_light_entity_translate_brightness_dimmable(
    lightener_level, light_level, hass
):
    """Test translate_brightness_back with float values."""

    light = LightenerControlledLight(
        "light.test1",
        {
            "brightness": {
                "10": "100",
                "20": "0",
                "100": "0",
            }
        },
        hass,
    )

    assert light.translate_brightness(lightener_level) == light_level


@pytest.mark.parametrize(
    "lightener_level, light_level",
    [
        (0, 0),
        (1, 255),
        (26, 255),
        (39, 255),
        (255, 0),
    ],
)
async def test_lightener_light_entity_translate_brightness_dimmable_onoff(
    lightener_level, light_level, hass
):
    """Test translate_brightness_back with float values."""

    light = LightenerControlledLight(
        "light.test_onoff",
        {"brightness": {"10": "100", "20": "0", "100": "0"}},
        hass,
    )

    assert light.translate_brightness(lightener_level) == light_level


async def test_lightener_light_entity_translate_brightness_float(hass):
    """Test translate_brightness_back with float values."""

    light = LightenerControlledLight(
        "light.test1",
        {
            "brightness": {
                "10": "100"  # 26: 255
            }
        },
        hass,
    )

    assert light.translate_brightness(2.9) == 20


async def test_lightener_light_entity_translate_brightness_back_float(hass):
    """Test translate_brightness_back with float values."""

    light = LightenerControlledLight(
        "light.test1",
        {
            "brightness": {
                "10": "100"  # 26: 255
            }
        },
        hass,
    )

    assert light.translate_brightness_back(25.9) == [3]


###########################################################
### Other


async def test_async_setup_platform(hass):
    """Test for platform setup."""

    # pylint: disable=W0212

    async_add_entities_mock = Mock()

    config = {
        "platform": "lightener",
        "lights": {
            "lightener_1": {
                "friendly_name": "Lightener 1",
                "entities": {"light.test1": {10: 100}},
            },
            "lightener_2": {
                "friendly_name": "Lightener 2",
                "entities": {"light.test2": {100: 10}},
            },
        },
    }

    await async_setup_platform(hass, config, async_add_entities_mock)

    assert async_add_entities_mock.call_count == 1

    created_lights: list = async_add_entities_mock.call_args.args[0]

    assert len(created_lights) == 2

    light: LightenerLight = created_lights[0]

    assert isinstance(light, LightenerLight)
    assert light.name == "Lightener 1"
    assert len(light._entities) == 1

    controlled_light: LightenerControlledLight = light._entities[0]

    assert isinstance(controlled_light, LightenerControlledLight)
    assert controlled_light.entity_id == "light.test1"
    assert controlled_light.levels[26] == 255

    light: LightenerLight = created_lights[1]

    assert isinstance(light, LightenerLight)
    assert light.name == "Lightener 2"
    assert len(light._entities) == 1

    controlled_light: LightenerControlledLight = light._entities[0]

    assert isinstance(controlled_light, LightenerControlledLight)
    assert light.extra_state_attributes["entity_id"][0] == "light.test2"
    assert controlled_light.entity_id == "light.test2"
    assert controlled_light.levels[255] == 26


@pytest.mark.parametrize(
    "config, expected_result",
    [
        # Normal configuration
        (
            {
                "10": "50",
                "20": "0",
                "30": "100",
            },
            {
                26: 128,
                51: 0,
                76: 255,
            },
        ),
        # Empty configuration
        ({}, {}),
        # Zero values
        ({"10": "0"}, {26: 0}),
        # 100% values
        ({"100": "100"}, {255: 255}),
    ],
)
def test_translate_config_to_brightness(config, expected_result):
    """Test the translate_config_to_brightness function."""

    assert translate_config_to_brightness(config) == expected_result


@pytest.mark.parametrize(
    "config, expected_result",
    [
        # Normal configuration
        (
            {
                "10": "50",
                "20": "0",
                "30": "100",
            },
            [
                (0, 0),
                (26, 128),
                (51, 0),
                (76, 255),
                (255, 255),
            ],
        ),
        # Empty configuration
        (
            {},
            [
                (0, 0),
                (255, 255),
            ],
        ),
        # 100% values
        (
            {
                "1": "100",
                "100": "50",
            },
            [
                (0, 0),
                (3, 255),
                (255, 128),
            ],
        ),
    ],
)
def test_prepare_brightness_config(config, expected_result):
    """Test the prepare_brightness_config function."""
    assert prepare_brightness_config(config) == expected_result


@pytest.mark.parametrize(
    "lightener_level, expected_entity_level",
    [
        (0, 0),
        (10, 30),
        (40, 0),
        (80, 90),
        (255, 255),
        (5, 15),
        (25, 15),
        (60, 45),
    ],
)
def test_create_brightness_map(lightener_level, expected_entity_level):
    """Test the create_brightness_map function."""

    config = [
        (0, 0),
        (10, 30),
        (40, 0),
        (80, 90),
        (255, 255),
    ]
    brigtness_map = create_brightness_map(config)

    assert brigtness_map[lightener_level] == expected_entity_level

    # Check if the length is correct
    assert len(brigtness_map) == 256


@pytest.mark.parametrize(
    "entity_level, expected_lightener_level_list",
    [
        (0, [0, 40]),
        (15, [5, 25, 47]),
        (30, [10, 53]),
        (90, [80]),
        (255, [255]),
    ],
)
def test_create_reverse_brightness_map(entity_level, expected_lightener_level_list):
    """Test the create_reverse_brightness_map function."""

    config = [
        (0, 0),
        (10, 30),
        (40, 0),
        (80, 90),
        (255, 255),
    ]

    levels = create_brightness_map(config)
    reverse_brightness_map = create_reverse_brightness_map(config, levels)

    assert reverse_brightness_map[entity_level] == expected_lightener_level_list

    # Check if the length is correct
    assert len(reverse_brightness_map) == 256


def test_create_reverse_brightness_map_on_off():
    """Test the create_reverse_brightness_map function."""

    config = [
        (0, 0),
        (10, 30),
        (40, 0),
        (80, 90),
        (255, 255),
    ]

    levels = create_brightness_map(config)
    reverse_brightness_map = create_reverse_brightness_map(config, levels)
    reverse_brightness_map_on_off = create_reverse_brightness_map_on_off(
        reverse_brightness_map
    )

    # Expected off is a list with 0 and 40
    expected_lightener_level_list_off = [0, 40]

    # Expected on is a list that goes from 1 to 255, except 40
    expected_lightener_level_list_on = list(range(1, 40)) + list(range(41, 256))

    assert reverse_brightness_map_on_off[0] == expected_lightener_level_list_off

    assert reverse_brightness_map_on_off[1] == expected_lightener_level_list_on
    assert reverse_brightness_map_on_off[10] == expected_lightener_level_list_on
    assert reverse_brightness_map_on_off[40] == expected_lightener_level_list_on
    assert reverse_brightness_map_on_off[45] == expected_lightener_level_list_on
    assert reverse_brightness_map_on_off[254] == expected_lightener_level_list_on
    assert reverse_brightness_map_on_off[255] == expected_lightener_level_list_on

    # Check if the length is correct
    assert len(reverse_brightness_map) == 256


@pytest.mark.parametrize(
    "source_range, value, target_range, expected_result",
    [
        # Positive order
        ((1, 255), 1, (1, 100), 1),
        ((1, 255), 255, (1, 100), 100),
        ((1, 255), 128, (1, 100), 50),
        # Low target range
        ((1, 255), 2, (1, 10), 1),
        ((1, 255), 15, (1, 10), 1),
        ((1, 255), 16, (1, 10), 2),
        ((1, 255), 25, (1, 10), 2),
        # Negative target order
        ((1, 255), 1, (255, 1), 255),
        ((1, 255), 255, (255, 1), 1),
        ((1, 255), 128, (255, 1), 128),
        # Negative source order
        ((255, 1), 1, (1, 100), 100),
        ((255, 1), 255, (1, 100), 1),
        ((255, 1), 26, (1, 100), 90),
    ],
)
def test_scale_ranged_value_to_int_range(
    source_range, value, target_range, expected_result
):
    """Test the scale_ranged_value_to_int_range function."""

    assert (
        scale_ranged_value_to_int_range(source_range, target_range, value)
        == expected_result
    )


# Issues


async def test_lightener_issue_41(hass: HomeAssistant, create_lightener):
    """Test the state changes of the LightenerLight class when turned on."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {
                "light.test1": {},
                "light.test2": {50: 0},
            },
        }
    )

    await lightener.async_turn_on(brightness=30)
    await hass.async_block_till_done()
    assert lightener.brightness == 30

    await lightener.async_turn_off()
    await hass.async_block_till_done()
    assert lightener.brightness is None
    assert hass.states.get("light.test1").state == "off"
    assert hass.states.get("light.test2").state == "off"

    await lightener.async_turn_on()
    await hass.async_block_till_done()
    assert lightener.brightness == 30

    assert hass.states.get("light.test1").state == "on"
    assert hass.states.get("light.test1").attributes["brightness"] == 30
    assert hass.states.get("light.test2").state == "off"


async def test_lightener_issue_97(hass: HomeAssistant, create_lightener):
    """Test the state changes of the LightenerLight class when turned on."""

    lightener: LightenerLight = await create_lightener(
        config={
            "friendly_name": "Test",
            "entities": {
                "light.test1": {50: 100},
                "light.test_onoff": {50: 0},
            },
        }
    )

    await lightener.async_turn_on(brightness=129)  # 51% of 255
    await hass.async_block_till_done()
    assert lightener.brightness == 129
    assert hass.states.get("light.test").attributes["brightness"] == 129

    assert hass.states.get("light.test1").state == "on"
    assert hass.states.get("light.test_onoff").state == "on"

    await lightener.async_turn_on(brightness=200)
    await hass.async_block_till_done()
    assert lightener.brightness == 200
    assert hass.states.get("light.test").attributes["brightness"] == 200

    assert hass.states.get("light.test1").state == "on"
    assert hass.states.get("light.test_onoff").state == "on"

    assert hass.states.get("light.test1").attributes["brightness"] == 255
