"""Config and options flows for the Raspihats integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
import voluptuous as vol

from .board import (
    Board,
    BoardError,
    BoardIdentity,
    BoardSettings,
    BusUnavailable,
    WrongBoard,
    address_range,
    mask_to_labels,
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
    DEFAULT_IRQ_GPIO,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    IRQ_GPIOS,
    IRQ_OFF,
    MAX_POLL_INTERVAL,
    MAX_WATCHDOG_TIMEOUT,
    MIN_POLL_INTERVAL,
    MIN_WATCHDOG_TIMEOUT,
    SUPPORTED_BOARDS,
    WATCHDOG_POLLS,
)
from .coordinator import RaspihatsConfigEntry


class RaspihatsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one board: the model, its address, and for input boards the interrupt line."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._model = SUPPORTED_BOARDS[0]
        self._address = 0

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: RaspihatsConfigEntry,
    ) -> RaspihatsOptionsFlow:
        """Return the options flow."""
        return RaspihatsOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the board model."""
        if user_input is not None:
            self._model = user_input[CONF_BOARD]
            return await self.async_step_address()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BOARD): SelectSelector(
                        SelectSelectorConfig(
                            options=list(SUPPORTED_BOARDS),
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_address(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the address and check the board answers there."""
        addresses = [f"0x{address:02X}" for address in address_range(self._model)]
        errors: dict[str, str] = {}
        placeholders = {"board": self._model, "default": addresses[0], "found": ""}

        if user_input is not None:
            address = int(user_input[CONF_ADDRESS], 16)
            await self.async_set_unique_id(f"0x{address:02x}")
            self._abort_if_unique_id_configured()
            board = Board(address, self._model)
            try:
                identity = await self.hass.async_add_executor_job(board.open)
            except BusUnavailable:
                errors["base"] = "bus_unavailable"
            except WrongBoard as err:
                errors["base"] = "wrong_board"
                placeholders["found"] = err.found
            except BoardError:
                errors["base"] = "no_response"
            else:
                self._address = address
                if identity.has_irq:
                    return await self.async_step_interrupts()
                return self._async_create()

        return self.async_show_form(
            step_id="address",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_ADDRESS): SelectSelector(
                            SelectSelectorConfig(
                                options=addresses,
                                mode=SelectSelectorMode.DROPDOWN,
                            )
                        )
                    }
                ),
                user_input or {CONF_ADDRESS: addresses[0]},
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_interrupts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the GPIO the board signals input changes on."""
        if user_input is not None:
            return self._async_create({CONF_IRQ_GPIO: user_input[CONF_IRQ_GPIO]})
        return self.async_show_form(
            step_id="interrupts",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_IRQ_GPIO): _irq_selector()}),
                {CONF_IRQ_GPIO: DEFAULT_IRQ_GPIO},
            ),
            description_placeholders={"board": self._model},
        )

    @callback
    def _async_create(self, options: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"{self._model} 0x{self._address:02X}",
            data={CONF_BOARD: self._model, CONF_ADDRESS: self._address},
            options=options or {},
        )


class RaspihatsOptionsFlow(OptionsFlowWithReload):
    """Polling speed and the settings stored on the board itself."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the options."""
        entry: RaspihatsConfigEntry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        coordinator = entry.runtime_data
        if not coordinator.last_update_success:
            return self.async_abort(reason="not_loaded")
        identity = coordinator.identity

        errors: dict[str, str] = {}
        if user_input is not None:
            timeout = user_input.get(CONF_WATCHDOG_TIMEOUT, 0)
            needed = max(
                MIN_WATCHDOG_TIMEOUT,
                WATCHDOG_POLLS * user_input[CONF_POLL_INTERVAL] / 1000,
            )
            if timeout and timeout < needed:
                errors[CONF_WATCHDOG_TIMEOUT] = "watchdog_too_short"
            else:
                return self.async_create_entry(data=_normalize(identity, user_input))
            suggested = user_input
        else:
            # Start from what the board holds, so that saving without changes
            # writes nothing to it; the options saved before take precedence.
            try:
                settings = await self.hass.async_add_executor_job(
                    coordinator.board.read_settings
                )
            except BoardError:
                return self.async_abort(reason="not_loaded")
            suggested = {
                **_options_from_settings(identity, settings),
                **entry.options,
            }

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _options_schema(identity), suggested
            ),
            errors=errors,
            description_placeholders={
                "polls": str(WATCHDOG_POLLS),
                "minimum": str(MIN_WATCHDOG_TIMEOUT),
            },
        )


def _options_schema(identity: BoardIdentity) -> vol.Schema:
    def channels(labels: tuple[str, ...]) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(
                options=list(labels), multiple=True, mode=SelectSelectorMode.LIST
            )
        )

    schema: dict[vol.Marker, Any] = {
        vol.Required(CONF_POLL_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_POLL_INTERVAL,
                max=MAX_POLL_INTERVAL,
                step=10,
                unit_of_measurement="ms",
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
    schema[vol.Required(CONF_WATCHDOG_TIMEOUT)] = NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=MAX_WATCHDOG_TIMEOUT,
            step=0.1,
            unit_of_measurement="s",
            mode=NumberSelectorMode.BOX,
        )
    )
    if identity.outputs:
        schema[vol.Optional(CONF_SAFE_ON)] = channels(identity.outputs)
        if identity.has_safety_mask:
            schema[vol.Optional(CONF_SAFE_HOLD)] = channels(identity.outputs)
        schema[vol.Optional(CONF_POWER_ON)] = channels(identity.outputs)
    if identity.has_input_polarity:
        schema[vol.Optional(CONF_INVERTED_INPUTS)] = channels(identity.inputs)
    if identity.has_irq:
        schema[vol.Required(CONF_IRQ_GPIO)] = _irq_selector()
    return vol.Schema(schema)


def _irq_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[*IRQ_GPIOS, IRQ_OFF],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key=CONF_IRQ_GPIO,
        )
    )


def _options_from_settings(
    identity: BoardIdentity, settings: BoardSettings
) -> dict[str, Any]:
    # The watchdog exactly as the board holds it, so that a period the options
    # would not allow (one left by a test rig, say) is shown, not rounded away.
    options: dict[str, Any] = {
        CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
        CONF_WATCHDOG_TIMEOUT: (settings.watchdog_ms or 0) / 1000,
    }
    outputs = identity.outputs
    if outputs:
        options[CONF_SAFE_ON] = mask_to_labels(outputs, settings.safety_value or 0)
        options[CONF_POWER_ON] = mask_to_labels(outputs, settings.power_on_value or 0)
        if settings.safety_mask is not None:
            everything = (1 << len(outputs)) - 1
            options[CONF_SAFE_HOLD] = mask_to_labels(
                outputs, everything & ~settings.safety_mask
            )
    if settings.input_polarity is not None:
        options[CONF_INVERTED_INPUTS] = mask_to_labels(
            identity.inputs, settings.input_polarity
        )
    if identity.has_irq:
        options[CONF_IRQ_GPIO] = IRQ_OFF
    return options


def _normalize(identity: BoardIdentity, user_input: dict[str, Any]) -> dict[str, Any]:
    """Store the polling interval as an int, and every channel list even when empty.

    An absent list means "leave the board alone" and an empty one means "no
    channels"; the form leaves a list out when nothing is ticked, which has
    to read as the second.
    """
    options = dict(user_input)
    options[CONF_POLL_INTERVAL] = int(options[CONF_POLL_INTERVAL])
    if CONF_WATCHDOG_TIMEOUT in options:
        options[CONF_WATCHDOG_TIMEOUT] = round(options[CONF_WATCHDOG_TIMEOUT], 3)
    for key in _options_schema(identity).schema:
        if isinstance(key, vol.Optional):
            options.setdefault(str(key), [])
    return options
