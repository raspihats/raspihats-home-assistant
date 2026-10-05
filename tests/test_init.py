"""Setting up, polling and unloading a board."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import SetupBoard
from .fake_bus import FakeBoard, FakeBus

OPTIONS = {
    "poll_interval": 250,
    "watchdog_timeout": 10,
    "safe_on": ["Q1"],
    "safe_hold": ["Q5"],
    "power_on": ["Q0"],
    "inverted_inputs": ["I2"],
}


async def test_setup_creates_device_and_entities(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    entry, _ = await setup_board("DI6acDQ6rly")
    assert entry.state is ConfigEntryState.LOADED

    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert device.identifiers == {("raspihats", "0x60")}
    assert device.manufacturer == "Raspihats"
    assert device.model == "DI6acDQ6rly I2C-HAT"
    assert device.sw_version == "3.1.0"
    assert device.name == "DI6acDQ6rly 0x60"

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    by_domain: dict[str, int] = {}
    for entity in entities:
        by_domain[entity.domain] = by_domain.get(entity.domain, 0) + 1
    assert by_domain == {"switch": 6, "binary_sensor": 6, "sensor": 12}
    assert hass.states.get("switch.di6acdq6rly_0x60_q0").state == STATE_OFF
    assert hass.states.get("binary_sensor.di6acdq6rly_0x60_i0").state == STATE_OFF


@pytest.mark.parametrize(
    ("model", "switches", "inputs"),
    [("DI16ac", 0, 16), ("DQ10rly", 10, 0), ("DQ5rly", 5, 0)],
)
async def test_channel_counts(
    hass: HomeAssistant, setup_board: SetupBoard, model: str, switches: int, inputs: int
) -> None:
    await setup_board(model)
    assert len(hass.states.async_entity_ids("switch")) == switches
    assert len(hass.states.async_entity_ids("binary_sensor")) == inputs


async def test_absent_board_retries(hass: HomeAssistant, bus: FakeBus) -> None:
    entry = await _add_entry(hass, "DI6acDQ6rly", 0x60)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_wrong_board_is_an_error(hass: HomeAssistant, bus: FakeBus) -> None:
    bus.add(0x50, FakeBoard("DQ5rly"))
    entry = await _add_entry(hass, "DQ10rly", 0x50)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def _add_entry(hass: HomeAssistant, model: str, address: int) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain="raspihats",
        unique_id=f"0x{address:02x}",
        data={"board": model, "address": address},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_unload(hass: HomeAssistant, setup_board: SetupBoard) -> None:
    entry, _ = await setup_board()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_no_options_writes_nothing(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    _, board = await setup_board()
    assert board.writes == []


async def test_options_are_applied_once(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    entry, board = await setup_board(options=OPTIONS)
    assert sorted(board.writes) == sorted(
        [
            ("cwdt_ms", 10_000),
            ("safety_value", 0b10),
            ("safety_mask", 0b011111),
            ("power_on_value", 0b1),
            ("di_polarity", 0b100),
        ]
    )
    # The safe state is in place before the watchdog starts counting.
    assert board.writes[-1] == ("cwdt_ms", 10_000)

    board.writes.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert board.writes == []


async def test_old_firmware_skips_missing_registers(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    entry, board = await setup_board(
        options=OPTIONS, firmware=(2, 2, 0), safety_mask=False, polarity=False
    )
    assert entry.state is ConfigEntryState.LOADED
    assert {register for register, _ in board.writes} == {
        "cwdt_ms",
        "safety_value",
        "power_on_value",
    }


async def test_restarted_board_gets_its_settings_back(
    hass: HomeAssistant, setup_board: SetupBoard, poll, caplog: pytest.LogCaptureFixture
) -> None:
    _, board = await setup_board(options=OPTIONS)
    await poll()
    board.writes.clear()

    # A replacement board, fresh from the factory, in the same slot.
    board.safety_value = 0
    board.power_cycle()
    with caplog.at_level(logging.WARNING):
        await poll()
    assert board.writes == [("safety_value", 0b10)]
    assert "restarted" in caplog.text
    assert hass.states.get("switch.di6acdq6rly_0x60_q0").state == STATE_ON


async def test_watchdog_trip_shows_safe_state(
    hass: HomeAssistant, setup_board: SetupBoard, poll, caplog: pytest.LogCaptureFixture
) -> None:
    _, board = await setup_board(options=OPTIONS)
    await poll()
    board.outputs = 0b100001
    await poll()
    assert hass.states.get("switch.di6acdq6rly_0x60_q5").state == STATE_ON

    board.trip_watchdog()
    with caplog.at_level(logging.WARNING):
        await poll()
    assert "watchdog timed out" in caplog.text
    # Q0 dropped to the safe value, Q1 is the one safe-on output, Q5 held.
    assert hass.states.get("switch.di6acdq6rly_0x60_q0").state == STATE_OFF
    assert hass.states.get("switch.di6acdq6rly_0x60_q1").state == STATE_ON
    assert hass.states.get("switch.di6acdq6rly_0x60_q5").state == STATE_ON


@pytest.mark.real_polling
async def test_polling_keeps_its_interval(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    """Real time: the loop polls at the interval, not back-to-back."""
    entry, board = await setup_board("DI16ac", options={"poll_interval": 100})
    reads = board.status_reads
    board.inputs = 1
    await asyncio.sleep(0.55)
    assert 3 <= board.status_reads - reads <= 7
    assert hass.states.get("binary_sensor.di16ac_0x40_i0").state == STATE_ON

    # Unloading stops the loop.
    assert await hass.config_entries.async_unload(entry.entry_id)
    reads = board.status_reads
    await asyncio.sleep(0.3)
    assert board.status_reads == reads


async def test_failed_reapply_is_retried(
    hass: HomeAssistant, setup_board: SetupBoard, poll
) -> None:
    _, board = await setup_board(options=OPTIONS)
    board.writes.clear()
    board.safety_value = 0
    board.power_cycle()

    # The board answers the status read, then drops off mid-way.
    original = board.handle

    def flaky(cmd, data):
        if cmd.name == "DQ_GET_SAFETY_VALUE":
            board.responding = False
        return original(cmd, data)

    board.handle = flaky
    await poll()
    assert board.writes == []

    board.handle = original
    board.responding = True
    await poll()
    assert board.writes == [("safety_value", 0b10)]


async def test_leftover_short_watchdog(
    hass: HomeAssistant,
    bus: FakeBus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A board arriving with a watchdog shorter than a few polls."""
    board = bus.add(0x60, FakeBoard("DI6acDQ6rly"))
    board.cwdt_ms = 200
    with caplog.at_level(logging.WARNING):
        entry = await _add_entry(hass, "DI6acDQ6rly", 0x60)
    assert "too short for polling every 250 ms; polling every 50 ms" in caplog.text
    assert entry.runtime_data.poll_interval.total_seconds() == 0.05
    issue = ir.async_get(hass).async_get_issue(
        "raspihats", f"watchdog_too_short_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders["watchdog"] == "0.2"
    # Nothing was written: the board keeps its period until someone decides.
    assert board.writes == []

    # The settings form shows the period as it is, and will not keep it.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested["watchdog_timeout"] == 0.2
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], suggested
    )
    assert result["errors"] == {"watchdog_timeout": "watchdog_too_short"}

    # Turning it off clears the repair and the fast polling.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**suggested, "watchdog_timeout": 0}
    )
    await hass.async_block_till_done()
    assert board.cwdt_ms == 0
    assert entry.runtime_data.poll_interval.total_seconds() == 0.25
    assert (
        ir.async_get(hass).async_get_issue(
            "raspihats", f"watchdog_too_short_{entry.entry_id}"
        )
        is None
    )


async def test_trip_episode_is_one_warning(
    hass: HomeAssistant,
    setup_board: SetupBoard,
    poll,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, board = await setup_board(options=OPTIONS)
    await poll()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            board.trip_watchdog()
            await poll()
        await poll()
        board.trip_watchdog()
        await poll()
    assert caplog.text.count("watchdog timed out") == 2
