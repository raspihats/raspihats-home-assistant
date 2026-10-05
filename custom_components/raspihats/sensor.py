"""Edge counters of the Raspihats input boards."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .board import CounterKey, Edge
from .coordinator import RaspihatsConfigEntry, RaspihatsCoordinator
from .entity import RaspihatsEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RaspihatsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a rising and a falling edge counter per input."""
    coordinator = entry.runtime_data
    async_add_entities(
        RaspihatsCounter(coordinator, index, label, edge)
        for index, label in enumerate(coordinator.identity.inputs)
        for edge in Edge
    )


class RaspihatsCounter(RaspihatsEntity, SensorEntity):
    """An edge counter, kept by the board itself.

    The board counts every edge, including pulses too short for a poll to
    see. Its counters live in RAM and restart from 0 when the board loses
    power; the total-increasing state class makes Home Assistant's
    statistics treat that as a meter reset rather than a drop.
    """

    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: RaspihatsCoordinator,
        index: int,
        label: str,
        edge: Edge,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, f"di-{index}-{edge}")
        self._key: CounterKey = (index, edge)
        self._attr_translation_key = f"{edge}_edges"
        self._attr_translation_placeholders = {"channel": label}

    async def async_added_to_hass(self) -> None:
        """Start reading this counter in the polls."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_track_counter(self._key))

    @property
    def available(self) -> bool:
        """Unavailable until the first poll that includes this counter."""
        return super().available and self._key in self.coordinator.data.counters

    @property
    def native_value(self) -> int | None:
        """Return the count."""
        return self.coordinator.data.counters.get(self._key)
