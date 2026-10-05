"""Polling coordinator for one Raspihats I2C-HAT."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from raspihats.protocol import StatusWordBits

from .board import (
    Board,
    BoardError,
    BoardIdentity,
    BoardSettings,
    BoardState,
    CounterKey,
    WrongBoard,
    labels_to_mask,
)
from .const import (
    CONF_BOARD,
    CONF_INVERTED_INPUTS,
    CONF_IRQ_GPIO,
    CONF_POLL_INTERVAL,
    CONF_POWER_ON,
    CONF_SAFE_HOLD,
    CONF_SAFE_ON,
    CONF_WATCHDOG_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    IRQ_GPIOS,
    MANUFACTURER,
    MIN_POLL_INTERVAL,
    MIN_WATCHDOG_TIMEOUT,
    PRODUCT_URL,
    WATCHDOG_POLLS,
)

_LOGGER = logging.getLogger(__name__)

_RESTARTED = (
    StatusWordBits.POR_RESET.value
    | StatusWordBits.SOFT_RESET.value
    | StatusWordBits.IWD_RESET.value
)
_WATCHDOG_TRIPPED = StatusWordBits.CWDT_TIMEOUT.value
_QUEUE_OVERFLOWED = StatusWordBits.DI_IRQ_CAPTURE_QUEUE_FULL.value

type RaspihatsConfigEntry = ConfigEntry[RaspihatsCoordinator]


class RaspihatsCoordinator(DataUpdateCoordinator[BoardState]):
    """Polls one board and carries the writes to it."""

    config_entry: RaspihatsConfigEntry
    identity: BoardIdentity

    def __init__(self, hass: HomeAssistant, entry: RaspihatsConfigEntry) -> None:
        """Initialize the coordinator."""
        # No update_interval: async_poll() does the scheduling, see there.
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            always_update=False,
        )
        self.poll_interval = timedelta(
            milliseconds=entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )
        self.board = Board(entry.data[CONF_ADDRESS], entry.data[CONF_BOARD])
        #: GPIO of the interrupt line the options ask for, None for off.
        self.irq_gpio = IRQ_GPIOS.get(entry.options.get(CONF_IRQ_GPIO, ""))
        #: Why the interrupt GPIO could not be used, for diagnostics.
        self.irq_line_error: str | None = None
        self._counters: set[CounterKey] = set()
        self._writes = 0
        self._polled = False
        self._tripping = False
        self._settings_due = False
        self._irq_due = False
        # Polls and interrupt drains both read the capture queue; one at a
        # time keeps the edges in the order the board captured them.
        self._io = asyncio.Lock()

    @property
    def uses_irq(self) -> bool:
        """Whether the board's capture queue is armed and read."""
        return self.irq_gpio is not None and self.identity.has_irq

    @property
    def device_info(self) -> DeviceInfo:
        """The device every entity of this board belongs to."""
        model = self.identity.model
        return DeviceInfo(
            identifiers={(DOMAIN, str(self.config_entry.unique_id))},
            manufacturer=MANUFACTURER,
            model=f"{model} I2C-HAT",
            model_id=model,
            name=self.config_entry.title,
            sw_version=self.identity.firmware,
            configuration_url=PRODUCT_URL.format(slug=model.lower()),
        )

    async def _async_setup(self) -> None:
        """Identify the board and bring its settings in line with the options."""
        try:
            self.identity = await self.hass.async_add_executor_job(self.board.open)
            await self._async_apply_settings()
            if self.uses_irq:
                await self._async_arm()
            self._check_watchdog()
        except WrongBoard as err:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="wrong_board",
                translation_placeholders={
                    "address": f"0x{self.board.address:02X}",
                    "board": self.board.model,
                    "found": err.found,
                },
            ) from err
        except BoardError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="not_responding",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_poll(self) -> None:
        """Poll the board for as long as the entry is loaded.

        DataUpdateCoordinator schedules its next refresh from the clock
        rounded down to the second, so an interval under a second would run
        back-to-back refreshes for most of every second. A plain loop keeps
        the interval honest.
        """
        interval = self.poll_interval.total_seconds()
        while True:
            await asyncio.sleep(interval)
            await self.async_refresh()

    async def _async_update_data(self) -> BoardState:
        """Read the board."""
        writes = self._writes
        try:
            async with self._io:
                state = await self.hass.async_add_executor_job(
                    self.board.read_state, tuple(self._counters), self.uses_irq
                )
                if self._polled and state.status & _RESTARTED:
                    self.logger.warning("%s restarted (power loss or reset)", self.name)
                    # Possibly a replacement board, with factory settings. The
                    # status bit is gone once read, so remember until done.
                    self._settings_due = True
                if self._polled and state.status & (_RESTARTED | _WATCHDOG_TRIPPED):
                    # Both leave the capture queue disarmed.
                    self._irq_due = True
                if self._settings_due:
                    await self._async_apply_settings()
                    self._settings_due = False
                if self._irq_due:
                    if self.uses_irq:
                        await self._async_arm()
                    self._irq_due = False
        except BoardError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="not_responding",
                translation_placeholders={"error": str(err)},
            ) from err

        tripped = bool(state.status & _WATCHDOG_TRIPPED) and bool(self.identity.outputs)
        if tripped and not self._tripping:
            # The bit survives until read, so on the first poll it records a
            # trip from before Home Assistant (re)connected - a restart of
            # Home Assistant itself, typically. A trip on every poll is one
            # episode, worth one warning.
            self.logger.log(
                logging.WARNING if self._polled else logging.INFO,
                "%s: the communication watchdog timed out and the outputs went "
                "to their safe state",
                self.name,
            )
        self._tripping = tripped
        if state.status & _QUEUE_OVERFLOWED and self.uses_irq:
            self.logger.warning(
                "%s: inputs changed faster than they were read and the board "
                "dropped its oldest captured edges; the input states are "
                "current again",
                self.name,
            )
        self._polled = True
        self._replay(state.captures)

        if writes != self._writes and self.data is not None:
            # A write overlapped this read, so its output states may predate
            # the write; keep the ones the write left behind.
            state = replace(state, outputs=self.data.outputs)
        return state

    async def async_drain(self) -> None:
        """Read the captured edges; called when the interrupt line asserts."""
        if self.data is None:
            return
        try:
            async with self._io:
                captures = await self.hass.async_add_executor_job(
                    self.board.drain_captures
                )
        except BoardError as err:
            # The next poll reports the board as unavailable if it stays so.
            self.logger.debug("%s: reading captured edges failed: %s", self.name, err)
            return
        self.logger.debug(
            "%s: %d edge(s) read on the interrupt line", self.name, len(captures)
        )
        self._replay(captures)

    async def async_disarm(self) -> None:
        """Release the interrupt line, so no unserved board holds it low."""
        if not self.uses_irq:
            return
        try:
            async with self._io:
                await self.hass.async_add_executor_job(self.board.disarm_irq)
        except BoardError as err:
            self.logger.debug("%s: disarming failed: %s", self.name, err)

    @callback
    def _replay(self, captures: Sequence[int]) -> None:
        """Show every captured edge in order.

        A pulse shorter than a poll arrives as two captures, so it reaches
        Home Assistant as on and then off rather than not at all.
        """
        for inputs in captures:
            if self.data is None or inputs == self.data.inputs:
                continue
            self.data = replace(self.data, inputs=inputs)
            self.async_update_listeners()

    async def _async_arm(self) -> None:
        changed = await self.hass.async_add_executor_job(self.board.arm_irq)
        if changed:
            self.logger.info("%s: wrote %s", self.name, ", ".join(changed))

    @callback
    def _check_watchdog(self) -> None:
        """Keep a watchdog the options did not set fed, and say it is short.

        A board can arrive with a watchdog period set by a script or a test
        rig. Shorter than a few polls, it trips between them and the outputs
        keep dropping to their safe state. The options flow never allows
        that, so it is reported as a repair, and until it is dealt with the
        polling speeds up to keep the watchdog fed.
        """
        entry = self.config_entry
        issue_id = f"watchdog_too_short_{entry.entry_id}"
        watchdog_ms = self.board.settings.watchdog_ms or 0
        poll_ms = self.poll_interval.total_seconds() * 1000
        needed_ms = max(MIN_WATCHDOG_TIMEOUT * 1000, WATCHDOG_POLLS * poll_ms)
        if not watchdog_ms or watchdog_ms >= needed_ms:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        fed_ms = max(MIN_POLL_INTERVAL, watchdog_ms // WATCHDOG_POLLS)
        if fed_ms < poll_ms:
            self.poll_interval = timedelta(milliseconds=fed_ms)
        self.logger.warning(
            "%s: the board's watchdog is set to %s ms, too short for polling "
            "every %d ms; polling every %d ms to keep it fed. Set the watchdog "
            "timeout in the board's settings",
            self.name,
            watchdog_ms,
            poll_ms,
            self.poll_interval.total_seconds() * 1000,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="watchdog_too_short",
            translation_placeholders={
                "board": entry.title,
                "watchdog": f"{watchdog_ms / 1000:g}",
                "minimum": f"{needed_ms / 1000:g}",
            },
        )

    async def _async_apply_settings(self) -> None:
        desired = settings_from_options(self.identity, self.config_entry.options)
        changed = await self.hass.async_add_executor_job(
            self.board.apply_settings, desired
        )
        if changed:
            self.logger.info("%s: wrote %s", self.name, ", ".join(changed))

    async def async_write_output(self, index: int, value: bool) -> None:
        """Switch one output and publish the new state without waiting a poll."""
        self._writes += 1
        try:
            await self.hass.async_add_executor_job(
                self.board.write_output, index, value
            )
        except BoardError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={
                    "channel": self.identity.outputs[index],
                    "error": str(err),
                },
            ) from err
        finally:
            self._writes += 1

        outputs = self.data.outputs or 0
        if value:
            outputs |= 1 << index
        else:
            outputs &= ~(1 << index)
        self.async_set_updated_data(replace(self.data, outputs=outputs))

    @callback
    def async_track_counter(self, key: CounterKey) -> CALLBACK_TYPE:
        """Include a counter in the polls until the returned callback runs.

        Counters cost a frame each, so only the ones an enabled entity shows
        are read.
        """
        self._counters.add(key)
        return lambda: self._counters.discard(key)


def settings_from_options(
    identity: BoardIdentity, options: Mapping[str, Any]
) -> BoardSettings:
    """Return the board settings the options ask for; absent options touch nothing."""
    outputs = identity.outputs
    values: dict[str, int] = {}
    if CONF_WATCHDOG_TIMEOUT in options:
        values["watchdog_ms"] = round(options[CONF_WATCHDOG_TIMEOUT] * 1000)
    if outputs:
        if CONF_SAFE_ON in options:
            values["safety_value"] = labels_to_mask(outputs, options[CONF_SAFE_ON])
        if CONF_POWER_ON in options:
            values["power_on_value"] = labels_to_mask(outputs, options[CONF_POWER_ON])
        if identity.has_safety_mask and CONF_SAFE_HOLD in options:
            # The register holds the channels that DO take the safe value.
            everything = (1 << len(outputs)) - 1
            held = labels_to_mask(outputs, options[CONF_SAFE_HOLD])
            values["safety_mask"] = everything & ~held
    if identity.has_input_polarity and CONF_INVERTED_INPUTS in options:
        values["input_polarity"] = labels_to_mask(
            identity.inputs, options[CONF_INVERTED_INPUTS]
        )
    return BoardSettings(**values)
