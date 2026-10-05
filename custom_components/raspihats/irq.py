"""The input boards' shared interrupt line.

An armed DI board pulls its interrupt GPIO low while its capture queue holds
edges. The line is open drain and wired-OR across the stack, so several
boards can share one GPIO, and it is a level, not an edge: a capture stored
while the line is already low makes no new falling edge. So every wake-up
drains all the boards on the line, and keeps draining until the line reads
high again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from glob import glob
import logging
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

_LOGGER = logging.getLogger(__name__)

CONSUMER = "home-assistant-raspihats"

#: GPIO controller labels of the 40-pin header, by SoC. Matched by label
#: because the gpiochip numbering differs between models and kernels (the
#: Raspberry Pi 5 header was gpiochip4 for a while).
HEADER_LABELS = (
    "pinctrl-bcm2835",
    "pinctrl-bcm2836",
    "pinctrl-bcm2837",
    "pinctrl-bcm2711",
    "pinctrl-rp1",
)

#: Drain rounds per wake-up before giving up on a line that stays low.
MAX_ROUNDS = 8

type Drain = Callable[[], Awaitable[None]]


class LineError(Exception):
    """The GPIO could not be requested."""


class GpioLine:
    """One header GPIO, requested as an input through libgpiod."""

    def __init__(self, request: Any, offset: int, active: Any) -> None:
        """Wrap a granted line request; ``active`` is gpiod's Value.ACTIVE."""
        self._request = request
        self._offset = offset
        self._active = active

    @property
    def fd(self) -> int:
        """File descriptor that turns readable when an edge arrives."""
        return self._request.fd

    def asserted(self) -> bool:
        """Return whether a board is holding the line low right now."""
        return self._request.get_value(self._offset) == self._active

    def clear_events(self) -> None:
        """Consume the pending edge events; only the level matters."""
        self._request.read_edge_events()

    def release(self) -> None:
        """Give the GPIO back."""
        self._request.release()


def open_line(offset: int) -> GpioLine:
    """Request a header GPIO as the interrupt input. Blocking."""
    try:
        import gpiod
        from gpiod.line import Bias, Edge, Value
    except ImportError as err:
        raise LineError(f"libgpiod is not available: {err}") from err

    path = _header_chip(gpiod)
    settings = gpiod.LineSettings(
        edge_detection=Edge.BOTH,
        bias=Bias.PULL_UP,
        # Active low, so "active" means "a board is asserting it".
        active_low=True,
    )
    try:
        request = gpiod.request_lines(
            path, consumer=CONSUMER, config={offset: settings}
        )
    except OSError as err:
        raise LineError(f"cannot request GPIO{offset} on {path}: {err}") from err
    return GpioLine(request, offset, Value.ACTIVE)


def _header_chip(gpiod: Any) -> str:
    chips = []
    for path in sorted(glob("/dev/gpiochip*")):
        try:
            with gpiod.Chip(path) as chip:
                chips.append((path, chip.get_info().label))
        except OSError:
            continue
    for label in HEADER_LABELS:
        for path, found in chips:
            if found == label:
                return path
    if not chips:
        raise LineError("no /dev/gpiochip device is available to Home Assistant")
    seen = ", ".join(f"{path} ({label})" for path, label in chips)
    raise LineError(f"no Raspberry Pi header GPIO controller among {seen}")


class IrqLine:
    """Watches one GPIO and drains the boards wired to it."""

    def __init__(self, hass: HomeAssistant, offset: int, line: GpioLine) -> None:
        """Initialize; nothing is watched until start()."""
        self._hass = hass
        self.offset = offset
        self._line = line
        self.drains: list[Drain] = []
        self._task: asyncio.Task[None] | None = None
        self._stuck = False
        self._stopped = False

    @callback
    def start(self) -> None:
        """Watch the line, and serve it at once if it is already low."""
        self._hass.loop.add_reader(self._line.fd, self._on_edge)
        self._serve()

    @callback
    def stop(self) -> None:
        """Stop watching and release the GPIO."""
        if self._stopped:
            return
        self._stopped = True
        self._hass.loop.remove_reader(self._line.fd)
        if self._task is not None:
            self._task.cancel()
        self._line.release()

    @callback
    def _on_edge(self) -> None:
        try:
            self._line.clear_events()
        except OSError as err:
            _LOGGER.debug("GPIO%d: reading edge events failed: %s", self.offset, err)
        self._serve()

    @callback
    def _serve(self) -> None:
        # One drain loop at a time. An edge during it needs nothing more:
        # the loop reads the level again after every round. (Checked with
        # done() because the task starts eagerly and can finish before it is
        # even stored.)
        if self._task is None or self._task.done():
            self._task = self._hass.async_create_background_task(
                self._drain_until_released(), f"raspihats GPIO{self.offset} interrupt"
            )

    async def _drain_until_released(self) -> None:
        for _ in range(MAX_ROUNDS):
            if not self._line.asserted():
                self._stuck = False
                return
            for drain in tuple(self.drains):
                await drain()
        if self._line.asserted() and not self._stuck:
            # The polls keep reading the boards set up here, so nothing is
            # lost; their inputs are just no faster than the polling.
            self._stuck = True
            _LOGGER.warning(
                "GPIO%d stays low after draining every board on it. A board "
                "that is not set up in Home Assistant may be holding it, or "
                "something else is wired to that pin",
                self.offset,
            )


_LINES: HassKey[dict[int, IrqLine]] = HassKey("raspihats_irq_lines")
_LOCK: HassKey[asyncio.Lock] = HassKey("raspihats_irq_lock")


async def async_attach(hass: HomeAssistant, offset: int, drain: Drain) -> CALLBACK_TYPE:
    """Have ``drain`` called whenever GPIO ``offset`` asserts.

    Boards on the same GPIO share one watcher. Returns the callback that
    detaches again; the GPIO is released with its last board.

    Raises:
        LineError: The GPIO cannot be used.

    """
    if _LINES not in hass.data:
        hass.data[_LINES] = {}
        hass.data[_LOCK] = asyncio.Lock()

        @callback
        def stop_all(event: Event) -> None:
            for line in hass.data[_LINES].values():
                line.stop()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_all)

    lines = hass.data[_LINES]
    async with hass.data[_LOCK]:
        line = lines.get(offset)
        if line is None:
            gpio = await hass.async_add_executor_job(open_line, offset)
            line = lines[offset] = IrqLine(hass, offset, gpio)
            line.start()
        line.drains.append(drain)

    @callback
    def detach() -> None:
        line.drains.remove(drain)
        if not line.drains:
            lines.pop(offset, None)
            line.stop()

    return detach
