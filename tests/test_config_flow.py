"""The config and options flows."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from raspihats.i2c_hats import I2CHat

from custom_components.raspihats.const import DOMAIN

from .conftest import SetupBoard
from .fake_bus import FakeBoard, FakeBus


@pytest.fixture
def no_setup() -> Generator[None]:
    """Create entries without setting them up."""
    with patch("custom_components.raspihats.async_setup_entry", return_value=True):
        yield


async def _pick(hass: HomeAssistant, model: str, address: str) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"board": model}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "address"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"address": address}
    )


@pytest.mark.usefixtures("no_setup")
async def test_add_board(hass: HomeAssistant, bus: FakeBus) -> None:
    bus.add(0x53, FakeBoard("DQ10rly"))
    result = await _pick(hass, "DQ10rly", "0x53")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "DQ10rly 0x53"
    assert result["data"] == {"board": "DQ10rly", "address": 0x53}
    assert result["result"].unique_id == "0x53"


async def test_address_choices_follow_the_model(
    hass: HomeAssistant, bus: FakeBus
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"board": "DI16ac"}
    )
    selector = result["data_schema"].schema["address"]
    assert selector.config["options"][0] == "0x40"
    assert selector.config["options"][-1] == "0x4F"


@pytest.mark.usefixtures("no_setup")
async def test_wrong_board(hass: HomeAssistant, bus: FakeBus) -> None:
    bus.add(0x50, FakeBoard("DQ5rly"))
    result = await _pick(hass, "DQ10rly", "0x50")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_board"}
    assert result["description_placeholders"]["found"] == "DQ5rly I2C-HAT"

    # The user corrects the address without starting over.
    bus.add(0x51, FakeBoard("DQ10rly"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"address": "0x51"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_nothing_at_the_address(hass: HomeAssistant, bus: FakeBus) -> None:
    result = await _pick(hass, "DQ10rly", "0x50")
    assert result["errors"] == {"base": "no_response"}


async def test_i2c_not_enabled(hass: HomeAssistant) -> None:
    previous = I2CHat._i2c_bus
    I2CHat._i2c_bus = None
    try:
        with patch(
            "raspihats.i2c_hats._base.smbus2.SMBus",
            side_effect=FileNotFoundError(2, "No such file or directory"),
        ):
            result = await _pick(hass, "DQ10rly", "0x50")
    finally:
        I2CHat._i2c_bus = previous
    assert result["errors"] == {"base": "bus_unavailable"}


async def test_already_configured(hass: HomeAssistant, setup_board: SetupBoard) -> None:
    await setup_board("DQ10rly", address=0x50)
    result = await _pick(hass, "DQ10rly", "0x50")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _suggested(result: dict) -> dict:
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }


async def test_options_start_from_the_board(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    entry, board = await setup_board("DI6acDQ6rly")
    board.cwdt_ms = 5000
    board.safety_value = 0b000010
    board.safety_mask = 0b011111
    board.power_on_value = 0b000001

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert _suggested(result) == {
        "poll_interval": 250,
        "watchdog_timeout": 5,
        "safe_on": ["Q1"],
        "safe_hold": ["Q5"],
        "power_on": ["Q0"],
        "inverted_inputs": [],
    }

    # Saving what was shown leaves the board's EEPROM alone.
    board.writes.clear()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _suggested(result)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert board.writes == []


async def test_options_change_the_board(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    entry, board = await setup_board("DI6acDQ6rly")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Nothing ticked: the form leaves the lists out entirely.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"poll_interval": 500, "watchdog_timeout": 10}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "poll_interval": 500,
        "watchdog_timeout": 10,
        "safe_on": [],
        "safe_hold": [],
        "power_on": [],
        "inverted_inputs": [],
    }
    await hass.async_block_till_done()
    # The entry reloaded and wrote the only value that changed.
    assert board.writes == [("cwdt_ms", 10_000)]
    assert entry.runtime_data.poll_interval.total_seconds() == 0.5


async def test_watchdog_has_to_cover_a_few_polls(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    entry, _ = await setup_board("DQ5rly")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"poll_interval": 1000, "watchdog_timeout": 3}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"watchdog_timeout": "watchdog_too_short"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"poll_interval": 1000, "watchdog_timeout": 4}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_options_follow_the_firmware(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    entry, _ = await setup_board(
        "DI6acDQ6rly", firmware=(2, 2, 0), safety_mask=False, polarity=False
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = {str(key) for key in result["data_schema"].schema}
    assert fields == {"poll_interval", "watchdog_timeout", "safe_on", "power_on"}


async def test_input_board_options(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    entry, _ = await setup_board("DI16ac")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = {str(key) for key in result["data_schema"].schema}
    assert fields == {"poll_interval", "inverted_inputs"}


async def test_options_need_the_board_online(
    hass: HomeAssistant, setup_board: SetupBoard, poll
) -> None:
    entry, board = await setup_board("DQ5rly")
    board.responding = False
    await poll()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"


async def test_options_board_drops_off_while_reading(
    hass: HomeAssistant, setup_board: SetupBoard
) -> None:
    entry, board = await setup_board("DQ5rly")
    original = board.handle

    def drop(cmd, data):
        if cmd.name == "CWDT_GET_PERIOD":
            board.responding = False
        return original(cmd, data)

    board.handle = drop
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
