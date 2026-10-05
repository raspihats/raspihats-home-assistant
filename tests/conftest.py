"""Fixtures for the Raspihats tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator
from typing import Any

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from raspihats.i2c_hats import I2CHat

from custom_components.raspihats.const import DOMAIN

from .fake_bus import FakeBoard, FakeBus


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture
def bus() -> Generator[FakeBus]:
    """Put a simulated bus under the raspihats library."""
    fake = FakeBus()
    previous = I2CHat._i2c_bus
    I2CHat._i2c_bus = fake
    yield fake
    I2CHat._i2c_bus = previous


type SetupBoard = Callable[..., Awaitable[tuple[MockConfigEntry, FakeBoard]]]


@pytest.fixture
def setup_board(hass: HomeAssistant, bus: FakeBus) -> SetupBoard:
    """Put a board on the bus and set up an entry for it."""

    async def setup(
        model: str = "DI6acDQ6rly",
        address: int | None = None,
        options: dict[str, Any] | None = None,
        **board_args: Any,
    ) -> tuple[MockConfigEntry, FakeBoard]:
        if address is None:
            address = {"DI16ac": 0x40, "DQ10rly": 0x50, "DQ5rly": 0x50}.get(model, 0x60)
        board = bus.add(address, FakeBoard(model, **board_args))
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=f"0x{address:02x}",
            title=f"{model} 0x{address:02X}",
            data={"board": model, "address": address},
            options=options or {},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry, board

    return setup


@pytest.fixture
def poll(hass: HomeAssistant) -> Callable[[], Awaitable[None]]:
    """Run one poll of every loaded board, as the polling loop would."""

    async def poll_once() -> None:
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    return poll_once
