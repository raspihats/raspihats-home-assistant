"""The Raspihats integration: I2C-HAT relay and input boards on a Raspberry Pi."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import RaspihatsConfigEntry, RaspihatsCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: RaspihatsConfigEntry) -> bool:
    """Set up one board."""
    coordinator = RaspihatsCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Cancelled when the entry unloads. Polling also feeds the board's
    # watchdog, so it runs whether or not any entity is enabled.
    entry.async_create_background_task(
        hass, coordinator.async_poll(), f"{entry.title} polling"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RaspihatsConfigEntry) -> bool:
    """Unload one board."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
