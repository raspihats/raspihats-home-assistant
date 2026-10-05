"""Fixtures for the Raspihats tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from raspihats.i2c_hats import I2CHat

from custom_components.raspihats.const import DOMAIN
from custom_components.raspihats.irq import LineError

from .fake_bus import FakeBoard, FakeBus, FakeLine


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture(autouse=True)
def no_poll_loop(request: pytest.FixtureRequest) -> Generator[None]:
    """Let the tests decide when polls happen, via the poll fixture.

    Tests marked real_polling keep the polling loop, in real time.
    """
    if request.node.get_closest_marker("real_polling"):
        yield
        return

    async def idle(self: Any) -> None:
        """Poll nothing."""

    with patch(
        "custom_components.raspihats.coordinator.RaspihatsCoordinator.async_poll", idle
    ):
        yield


@pytest.fixture(autouse=True)
def no_gpio() -> Generator[None]:
    """No GPIO, unless a test asks for the gpio fixture."""

    def unavailable(offset: int) -> None:
        raise LineError("no GPIO in tests")

    with patch("custom_components.raspihats.irq.open_line", unavailable):
        yield


@pytest.fixture
def gpio(bus: FakeBus, no_gpio: None) -> Generator[list[FakeLine]]:
    """Simulated header GPIOs; the list holds the lines requested."""
    opened: list[FakeLine] = []

    def open_line(offset: int) -> FakeLine:
        line = FakeLine(bus, offset)
        opened.append(line)
        return line

    with patch("custom_components.raspihats.irq.open_line", open_line):
        yield opened


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Wait for something the event loop does on its own, like serving a GPIO."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


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
