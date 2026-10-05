"""Digital inputs of a Raspihats I2C-HAT."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import RaspihatsConfigEntry, RaspihatsCoordinator
from .entity import RaspihatsEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RaspihatsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one binary sensor per input."""
    coordinator = entry.runtime_data
    async_add_entities(
        RaspihatsInput(coordinator, index, label)
        for index, label in enumerate(coordinator.identity.inputs)
    )


class RaspihatsInput(RaspihatsEntity, BinarySensorEntity):
    """One input channel.

    No device class: what is wired to the input is the user's to say, and
    Home Assistant lets them pick one ("Show as") in the entity settings.
    """

    def __init__(
        self, coordinator: RaspihatsCoordinator, index: int, label: str
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, f"di-{index}")
        self._index = index
        self._attr_name = label

    @property
    def is_on(self) -> bool:
        """Return the input state."""
        return bool((self.coordinator.data.inputs or 0) >> self._index & 1)
