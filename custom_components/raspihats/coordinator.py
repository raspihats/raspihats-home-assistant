"""Polling coordinator for one Raspihats I2C-HAT."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
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
    CONF_POLL_INTERVAL,
    CONF_POWER_ON,
    CONF_SAFE_HOLD,
    CONF_SAFE_ON,
    CONF_WATCHDOG_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MANUFACTURER,
    PRODUCT_URL,
)

_LOGGER = logging.getLogger(__name__)

_RESTARTED = (
    StatusWordBits.POR_RESET.value
    | StatusWordBits.SOFT_RESET.value
    | StatusWordBits.IWD_RESET.value
)
_WATCHDOG_TRIPPED = StatusWordBits.CWDT_TIMEOUT.value

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
        self._counters: set[CounterKey] = set()
        self._writes = 0
        self._polled = False
        self._settings_due = False

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
            state = await self.hass.async_add_executor_job(
                self.board.read_state, tuple(self._counters)
            )
            if self._polled and state.status & _RESTARTED:
                self.logger.warning("%s restarted (power loss or reset)", self.name)
                # Possibly a replacement board, with factory settings. The
                # status bit is gone once read, so remember until it is done.
                self._settings_due = True
            if self._settings_due:
                await self._async_apply_settings()
                self._settings_due = False
        except BoardError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="not_responding",
                translation_placeholders={"error": str(err)},
            ) from err

        if state.status & _WATCHDOG_TRIPPED and self.identity.outputs:
            # The bit survives until read, so on the first poll it records a
            # trip from before Home Assistant (re)connected - a restart of
            # Home Assistant itself, typically.
            self.logger.log(
                logging.WARNING if self._polled else logging.INFO,
                "%s: the communication watchdog timed out and the outputs went "
                "to their safe state",
                self.name,
            )
        self._polled = True

        if writes != self._writes and self.data is not None:
            # A write overlapped this read, so its output states may predate
            # the write; keep the ones the write left behind.
            state = replace(state, outputs=self.data.outputs)
        return state

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
    if outputs:
        if CONF_WATCHDOG_TIMEOUT in options:
            values["watchdog_ms"] = int(options[CONF_WATCHDOG_TIMEOUT] * 1000)
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
