"""Base entity for the Raspihats integration."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import RaspihatsCoordinator


class RaspihatsEntity(CoordinatorEntity[RaspihatsCoordinator]):
    """One channel of a board."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RaspihatsCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}-{key}"
        self._attr_device_info = coordinator.device_info
