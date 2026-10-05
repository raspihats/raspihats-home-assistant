"""Diagnostics for the Raspihats integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .board import BoardError
from .coordinator import RaspihatsConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RaspihatsConfigEntry
) -> dict[str, Any]:
    """Return what the board is, what it holds and what it last reported."""
    coordinator = entry.runtime_data
    try:
        settings: dict[str, Any] = asdict(
            await hass.async_add_executor_job(coordinator.board.read_settings)
        )
    except BoardError as err:
        settings = {"error": str(err)}
    state = coordinator.data
    return {
        "data": dict(entry.data),
        "options": dict(entry.options),
        "identity": asdict(coordinator.identity),
        "settings": settings,
        "last_update_success": coordinator.last_update_success,
        "interrupts": {
            "gpio": coordinator.irq_gpio,
            "armed": coordinator.uses_irq,
            "line_error": coordinator.irq_line_error,
        },
        "state": {
            "inputs": state.inputs,
            "outputs": state.outputs,
            "status": state.status,
            "counters": {
                f"{index}-{edge}": value
                for (index, edge), value in state.counters.items()
            },
        },
    }
