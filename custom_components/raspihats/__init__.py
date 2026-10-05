"""The Raspihats integration: I2C-HAT relay and input boards on a Raspberry Pi."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import RaspihatsConfigEntry, RaspihatsCoordinator
from .irq import LineError, async_attach

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: RaspihatsConfigEntry) -> bool:
    """Set up one board."""
    coordinator = RaspihatsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if coordinator.uses_irq:
        await _async_attach_irq(hass, entry, coordinator)
    # Cancelled when the entry unloads. Polling also feeds the board's
    # watchdog, so it runs whether or not any entity is enabled.
    entry.async_create_background_task(
        hass, coordinator.async_poll(), f"{entry.title} polling"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RaspihatsConfigEntry) -> bool:
    """Unload one board."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_disarm()
    return True


async def _async_attach_irq(
    hass: HomeAssistant, entry: RaspihatsConfigEntry, coordinator: RaspihatsCoordinator
) -> None:
    gpio = coordinator.irq_gpio
    try:
        entry.async_on_unload(await async_attach(hass, gpio, coordinator.async_drain))
    except LineError as err:
        # The board stays armed and every poll reads its captured edges, so
        # short pulses still arrive, at the polling interval.
        coordinator.irq_line_error = str(err)
        _LOGGER.warning(
            "%s: the interrupt line GPIO%d is not available (%s). Input "
            "changes are picked up by polling instead, short pulses included",
            entry.title,
            gpio,
            err,
        )
