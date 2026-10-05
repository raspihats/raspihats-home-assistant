"""The interrupt line and the boards' capture queue."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import patch

import gpiod
from gpiod.line import Bias, Edge
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
import pytest

from custom_components.raspihats import irq

from .conftest import SetupBoard, until
from .fake_bus import FakeBoard, FakeBus, FakeLine

IRQ = {"irq_gpio": "gpio21"}

#: The real one; the autouse no_gpio fixture replaces it on the module.
open_line = irq.open_line
I3 = "binary_sensor.di16ac_0x40_i3"


def _record(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Every state the entity takes, in order."""
    seen: list[str] = []

    @callback
    def changed(event: Event[EventStateChangedData]) -> None:
        seen.append(event.data["new_state"].state)

    async_track_state_change_event(hass, entity_id, changed)
    return seen


async def test_armed_on_setup(
    hass: HomeAssistant, setup_board: SetupBoard, gpio: list[FakeLine]
) -> None:
    entry, board = await setup_board("DI16ac", options=IRQ)
    assert board.armed
    assert sorted(board.writes) == [("falling_mask", 0xFFFF), ("rising_mask", 0xFFFF)]
    assert [line.offset for line in gpio] == [21]

    # The masks live in EEPROM: a reload re-arms without writing them again.
    board.writes.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert board.armed
    assert board.writes == []


async def test_pulse_arrives_without_a_poll(
    hass: HomeAssistant, setup_board: SetupBoard, gpio: list[FakeLine]
) -> None:
    _, board = await setup_board("DI16ac", options=IRQ)
    seen = _record(hass, I3)

    # Far shorter than any poll: on and off before anything reads the board.
    board.set_inputs(1 << 3)
    board.set_inputs(0)
    await until(lambda: len(seen) == 2)
    assert seen == [STATE_ON, STATE_OFF]
    assert not board.line_asserted


async def test_without_the_gpio_polls_still_catch_pulses(
    hass: HomeAssistant,
    setup_board: SetupBoard,
    poll,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        _, board = await setup_board("DI16ac", options=IRQ)
    assert "GPIO21 is not available" in caplog.text
    assert board.armed

    seen = _record(hass, I3)
    board.set_inputs(1 << 3)
    board.set_inputs(0)
    await poll()
    assert seen == [STATE_ON, STATE_OFF]


async def test_off_means_polling_only(
    hass: HomeAssistant, setup_board: SetupBoard, gpio: list[FakeLine]
) -> None:
    _, board = await setup_board("DI16ac", options={"irq_gpio": "off"})
    assert not board.armed
    assert board.writes == []
    assert gpio == []


async def test_one_line_serves_the_stack(
    hass: HomeAssistant, setup_board: SetupBoard, gpio: list[FakeLine]
) -> None:
    inputs, di16 = await setup_board("DI16ac", options=IRQ)
    mixed, di6 = await setup_board("DI6acDQ6rly", options=IRQ)
    assert len(gpio) == 1

    di6.set_inputs(0b100)
    await until(
        lambda: hass.states.get("binary_sensor.di6acdq6rly_0x60_i2").state == STATE_ON
    )
    di16.set_inputs(1)
    await until(
        lambda: hass.states.get("binary_sensor.di16ac_0x40_i0").state == STATE_ON
    )

    # The GPIO is released with the last board on it, and unloading disarms.
    assert await hass.config_entries.async_unload(inputs.entry_id)
    assert not di16.armed
    assert not gpio[0].released
    assert await hass.config_entries.async_unload(mixed.entry_id)
    assert not di6.armed
    assert gpio[0].released


async def test_rearmed_after_watchdog_trip_and_restart(
    hass: HomeAssistant, setup_board: SetupBoard, gpio: list[FakeLine], poll
) -> None:
    _, board = await setup_board("DI6acDQ6rly", options=IRQ)
    await poll()

    board.trip_watchdog()
    assert not board.armed
    await poll()
    assert board.armed

    board.power_cycle()
    await poll()
    assert board.armed

    board.set_inputs(1)
    await until(
        lambda: hass.states.get("binary_sensor.di6acdq6rly_0x60_i0").state == STATE_ON
    )


async def test_line_held_by_an_unknown_board(
    hass: HomeAssistant,
    bus: FakeBus,
    setup_board: SetupBoard,
    gpio: list[FakeLine],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A second input board, armed by something else and never drained here.
    stranger = bus.add(0x41, FakeBoard("DI16ac"))
    stranger.rising_mask = stranger.falling_mask = 0xFFFF
    stranger.armed = True
    stranger.set_inputs(1)

    with caplog.at_level(logging.WARNING):
        await setup_board("DI16ac", options=IRQ)
        await until(lambda: "GPIO21 stays low" in caplog.text)


async def test_overflow_is_reported(
    hass: HomeAssistant,
    setup_board: SetupBoard,
    poll,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, board = await setup_board("DI16ac", options=IRQ)
    for _ in range(100):
        board.set_inputs(1)
        board.set_inputs(0)
    board.set_inputs(1)
    with caplog.at_level(logging.WARNING):
        await poll()
    assert "dropped its oldest captured edges" in caplog.text
    assert hass.states.get("binary_sensor.di16ac_0x40_i0").state == STATE_ON


OLD = {"firmware": (2, 1, 2), "irq_enable": False, "polarity": False}


async def test_2x_armed_by_masks_alone(
    hass: HomeAssistant, setup_board: SetupBoard, gpio: list[FakeLine]
) -> None:
    entry, board = await setup_board("DI16ac", options=IRQ, **OLD)
    assert board.armed
    assert board.rising_mask == board.falling_mask == 0xFFFF
    # Volatile on 2.x: nothing goes to EEPROM.
    assert board.writes == []

    seen = _record(hass, I3)
    board.set_inputs(1 << 3)
    board.set_inputs(0)
    await until(lambda: len(seen) == 2)
    assert seen == [STATE_ON, STATE_OFF]

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert board.rising_mask == board.falling_mask == 0
    assert not board.queue
    assert gpio[0].released


async def test_2x_input_read_does_not_strand_captures(
    hass: HomeAssistant, setup_board: SetupBoard, gpio: list[FakeLine], poll
) -> None:
    _, board = await setup_board("DI16ac", options=IRQ, **OLD)
    seen = _record(hass, I3)

    # An edge lands, and a poll reads the inputs before the interrupt is
    # served. On 2.x that read releases the line with the capture queued.
    board.set_inputs(1 << 3)
    await poll()
    assert seen == [STATE_ON]
    assert not board.queue

    # The queue was drained, so the next edge asserts the line again.
    board.set_inputs(0)
    await until(lambda: len(seen) == 2)
    assert seen == [STATE_ON, STATE_OFF]


async def test_2x_rearmed_after_power_loss(
    hass: HomeAssistant, setup_board: SetupBoard, gpio: list[FakeLine], poll
) -> None:
    _, board = await setup_board("DI16ac", options=IRQ, **OLD)
    await poll()
    board.power_cycle()
    assert not board.armed
    await poll()
    assert board.armed
    board.set_inputs(1)
    await until(
        lambda: hass.states.get("binary_sensor.di16ac_0x40_i0").state == STATE_ON
    )


class _Chip:
    """gpiod.Chip, for a machine with the given controllers."""

    labels: ClassVar[dict[str, str]] = {}

    def __init__(self, path: str) -> None:
        if path not in self.labels:
            raise PermissionError(13, "Permission denied")
        self.path = path

    def __enter__(self) -> _Chip:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def get_info(self) -> SimpleNamespace:
        return SimpleNamespace(label=self.labels[self.path])


def _gpiod(labels: dict[str, str]) -> Any:
    """Just enough of gpiod to look for the header controller."""
    _Chip.labels = labels
    return SimpleNamespace(Chip=_Chip)


def _requests(error: OSError | None = None) -> Any:
    def request_lines(path: str, consumer: str, config: dict) -> SimpleNamespace:
        if error is not None:
            raise error
        return SimpleNamespace(path=path, consumer=consumer, config=config)

    return request_lines


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        # Raspberry Pi 5: the RP1 controller, wherever it is numbered.
        (
            {
                "/dev/gpiochip0": "gpio-brcmstb@107d508500",
                "/dev/gpiochip4": "pinctrl-rp1",
            },
            "/dev/gpiochip4",
        ),
        ({"/dev/gpiochip0": "pinctrl-bcm2711"}, "/dev/gpiochip0"),
    ],
)
def test_header_controller_is_found_by_label(
    labels: dict[str, str], expected: str
) -> None:
    gpiod = _gpiod(labels)
    with patch.object(irq, "glob", return_value=sorted(labels)):
        assert irq._header_chip(gpiod) == expected


def test_no_header_controller() -> None:
    gpiod = _gpiod({"/dev/gpiochip0": "INT34C5:00"})
    with (
        patch.object(irq, "glob", return_value=["/dev/gpiochip0"]),
        pytest.raises(irq.LineError, match="no Raspberry Pi header"),
    ):
        irq._header_chip(gpiod)
    with (
        patch.object(irq, "glob", return_value=[]),
        pytest.raises(irq.LineError, match="no /dev/gpiochip device"),
    ):
        irq._header_chip(gpiod)


def test_line_requested_as_pulled_up_input() -> None:
    _Chip.labels = {"/dev/gpiochip0": "pinctrl-bcm2711"}
    with (
        patch.object(gpiod, "Chip", _Chip),
        patch.object(gpiod, "request_lines", _requests()),
        patch.object(irq, "glob", return_value=["/dev/gpiochip0"]),
    ):
        line = open_line(21)
    request = line._request
    assert request.path == "/dev/gpiochip0"
    settings = request.config[21]
    assert settings.active_low is True
    assert settings.bias is Bias.PULL_UP
    assert settings.edge_detection is Edge.BOTH


def test_busy_line_is_a_line_error() -> None:
    _Chip.labels = {"/dev/gpiochip0": "pinctrl-bcm2711"}
    with (
        patch.object(gpiod, "Chip", _Chip),
        patch.object(gpiod, "request_lines", _requests(OSError(16, "Device busy"))),
        patch.object(irq, "glob", return_value=["/dev/gpiochip0"]),
        pytest.raises(irq.LineError, match="cannot request GPIO21"),
    ):
        open_line(21)
