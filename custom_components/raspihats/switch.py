"""Outputs (relays) of a Raspihats I2C-HAT."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RaspihatsConfigEntry, RaspihatsCoordinator
from .entity import RaspihatsEntity

# The coordinator serializes nothing itself, but the library holds one lock
# for the whole bus, so concurrent writes simply queue there.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RaspihatsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one switch per output."""
    coordinator = entry.runtime_data
    async_add_entities(
        RaspihatsOutput(coordinator, index, label)
        for index, label in enumerate(coordinator.identity.outputs)
    )


class RaspihatsOutput(RaspihatsEntity, SwitchEntity):
    """One output channel."""

    def __init__(
        self, coordinator: RaspihatsCoordinator, index: int, label: str
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, f"dq-{index}")
        self._index = index
        self._attr_name = label

    @property
    def is_on(self) -> bool:
        """Return the state the board reports."""
        return bool((self.coordinator.data.outputs or 0) >> self._index & 1)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the output on."""
        await self.coordinator.async_write_output(self._index, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the output off."""
        await self.coordinator.async_write_output(self._index, False)
