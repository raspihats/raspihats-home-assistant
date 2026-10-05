"""Switches, binary sensors and counters."""

from __future__ import annotations

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest

from .conftest import SetupBoard

RELAY = "switch.dq10rly_0x50_q3"


async def _switch(hass: HomeAssistant, service: str, entity_id: str = RELAY) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_switch_drives_the_relay(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    _, board = await setup_board("DQ10rly")

    await _switch(hass, SERVICE_TURN_ON)
    assert board.outputs == 0b1000
    assert hass.states.get(RELAY).state == STATE_ON

    await _switch(hass, SERVICE_TURN_OFF)
    assert board.outputs == 0
    assert hass.states.get(RELAY).state == STATE_OFF


async def test_switch_follows_the_board(
    hass: HomeAssistant, setup_board: SetupBoard, poll
) -> None:
    _, board = await setup_board("DQ10rly")
    board.outputs = 0b1000
    await poll()
    assert hass.states.get(RELAY).state == STATE_ON


async def test_failed_write_raises(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    _, board = await setup_board("DQ10rly")
    board.responding = False
    with pytest.raises(HomeAssistantError, match="Could not switch Q3"):
        await _switch(hass, SERVICE_TURN_ON)


async def test_board_offline_and_back(
    hass: HomeAssistant, setup_board: SetupBoard, poll
) -> None:
    _, board = await setup_board("DQ10rly")
    board.responding = False
    await poll()
    assert hass.states.get(RELAY).state == STATE_UNAVAILABLE

    board.responding = True
    board.outputs = 0b1000
    await poll()
    assert hass.states.get(RELAY).state == STATE_ON


async def test_inputs(hass: HomeAssistant, setup_board: SetupBoard, poll) -> None:
    _, board = await setup_board("DI16ac")
    board.inputs = 1 << 15
    await poll()
    assert hass.states.get("binary_sensor.di16ac_0x40_i15").state == STATE_ON
    assert hass.states.get("binary_sensor.di16ac_0x40_i0").state == STATE_OFF


async def test_counters_are_off_until_enabled(
    hass: HomeAssistant, setup_board: SetupBoard, poll
) -> None:
    entry, board = await setup_board("DI16ac")
    registry = er.async_get(hass)
    rising = "sensor.di16ac_0x40_i0_rising_edges"
    falling = "sensor.di16ac_0x40_i0_falling_edges"
    assert (
        registry.async_get(rising).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    )
    assert hass.states.get(rising) is None

    registry.async_update_entity(rising, disabled_by=None)
    registry.async_update_entity(falling, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    board.rising[0] = 41
    board.falling[0] = 40
    await poll()
    assert hass.states.get(rising).state == "41"
    assert hass.states.get(falling).state == "40"
    assert hass.states.get(rising).attributes["state_class"] == "total_increasing"

    # Power loss restarts the board's counters from zero.
    board.power_cycle()
    await poll()
    assert hass.states.get(rising).state == "0"
