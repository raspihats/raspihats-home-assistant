"""Constants for the Raspihats integration."""

from typing import Final

DOMAIN: Final = "raspihats"
MANUFACTURER: Final = "Raspihats"

CONF_BOARD: Final = "board"

# Options. The output and input lists hold channel labels ("Q0", "I3"), so the
# stored options read the same as the silkscreen.
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_WATCHDOG_TIMEOUT: Final = "watchdog_timeout"
CONF_SAFE_ON: Final = "safe_on"
CONF_SAFE_HOLD: Final = "safe_hold"
CONF_POWER_ON: Final = "power_on"
CONF_INVERTED_INPUTS: Final = "inverted_inputs"
CONF_IRQ_GPIO: Final = "irq_gpio"

#: Interrupt line choices. The DI boards route their interrupt output to
#: GPIO21 through a bridged jumper pad; GPIO20, 22 and 23 are the alternative
#: pads, for stacks where GPIO21 is taken.
IRQ_OFF: Final = "off"
IRQ_GPIOS: Final = {"gpio21": 21, "gpio20": 20, "gpio22": 22, "gpio23": 23}
DEFAULT_IRQ_GPIO: Final = "gpio21"

#: Milliseconds. Fast enough that a push button registers; the bus cost is
#: two or three short frames per board per poll.
DEFAULT_POLL_INTERVAL: Final = 250
MIN_POLL_INTERVAL: Final = 50
MAX_POLL_INTERVAL: Final = 10_000

#: Seconds; 0 turns the watchdog off. The timeout has to cover a few missed
#: polls, or a busy moment in Home Assistant would trip it.
MIN_WATCHDOG_TIMEOUT: Final = 2
MAX_WATCHDOG_TIMEOUT: Final = 3_600
WATCHDOG_POLLS: Final = 4

#: The boards in production. The raspihats library knows older models too,
#: but those are discontinued and deliberately left out.
SUPPORTED_BOARDS: Final = ("DI16ac", "DI6acDQ6rly", "DQ10rly", "DQ5rly")

PRODUCT_URL: Final = "https://raspihats.com/shop/{slug}-i2c-hat/"
