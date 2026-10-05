"""Blocking access to one I2C-HAT through the raspihats library.

Every method here performs I2C transfers and has to run in the executor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from raspihats import i2c_hats
from raspihats.i2c_hats import ResponseException
from raspihats.protocol import BOARDS, board_info

#: What the library reports when the board did not acknowledge at all, as
#: opposed to answering with a frame that fails the check.
_NO_RESPONSE = "no response"

#: Entries the board's capture queue holds; it drops the oldest beyond that.
_CAPTURE_QUEUE_DEPTH = 128


class BoardError(Exception):
    """The board did not answer, or its answer was unusable."""


class BusUnavailable(BoardError):
    """The I2C device node is missing or not accessible."""


class WrongBoard(BoardError):
    """A different board answered at the configured address."""

    def __init__(self, found: str) -> None:
        """Initialize with the name the board reported."""
        super().__init__(f"board reports {found!r}")
        self.found = found


class Edge(StrEnum):
    """Which edge counter to read."""

    RISING = "rising"
    FALLING = "falling"


type CounterKey = tuple[int, Edge]


@dataclass(frozen=True)
class BoardIdentity:
    """What the board is and which optional registers its firmware has."""

    model: str
    firmware: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    has_safety_mask: bool
    has_input_polarity: bool
    #: The capture queue with its arming bit (firmware 3.0.0 and later), which
    #: is what drives the interrupt line.
    has_irq: bool = False


@dataclass(frozen=True)
class BoardState:
    """One poll: channel bitmasks, the counters asked for, the status word.

    ``captures`` holds the input states at each edge the board captured
    since the last read, oldest first; ``inputs`` is the state after them.
    """

    inputs: int | None
    outputs: int | None
    status: int
    counters: Mapping[CounterKey, int] = field(default_factory=dict)
    captures: tuple[int, ...] = ()


@dataclass(frozen=True)
class BoardSettings:
    """The persistent registers this integration manages.

    A field is None when the board has no such register, or, for a desired
    configuration, when it is not to be touched.
    """

    watchdog_ms: int | None = None
    safety_value: int | None = None
    safety_mask: int | None = None
    power_on_value: int | None = None
    input_polarity: int | None = None


class Board:
    """One I2C-HAT on the Raspberry Pi header bus."""

    def __init__(self, address: int, model: str) -> None:
        """Initialize; nothing is transferred until open()."""
        self.address = address
        self.model = model
        self._hat: Any = None
        self.identity: BoardIdentity | None = None

    def open(self) -> BoardIdentity:
        """Identify the board and probe its firmware."""
        reported = _call(lambda: _connect(self.address).name)
        info = board_info(reported)
        if info is None or info.name != self.model:
            raise WrongBoard(reported)

        hat = _call(lambda: getattr(i2c_hats, self.model)(self.address))
        firmware = _call(lambda: hat.fw_version).removeprefix("v")
        inputs = tuple(info.labels.get("di", ()))
        outputs = tuple(info.labels.get("dq", ()))
        self._hat = hat
        self.identity = BoardIdentity(
            model=self.model,
            firmware=firmware,
            inputs=inputs,
            outputs=outputs,
            has_safety_mask=bool(outputs) and _supported(lambda: hat.dq.safety_mask),
            has_input_polarity=bool(inputs) and _supported(lambda: hat.di.polarity),
            has_irq=info.has_irq and _supported(lambda: hat.di.irq_reg.global_enable),
        )
        return self.identity

    def read_state(
        self, counters: Iterable[CounterKey] = (), drain: bool = False
    ) -> BoardState:
        """Read the channel states, the status word and the given counters.

        With ``drain``, the capture queue is emptied first, so the edges come
        before the states they led to. Any valid frame feeds the board's
        communication watchdog, so this poll is also what keeps the outputs
        out of their safe state.
        """
        hat = self._hat
        identity = self._identity
        status = _call(lambda: hat.status.value)
        captures = tuple(self.drain_captures()) if drain else ()
        inputs = outputs = None
        if identity.inputs:
            inputs = _call(lambda: hat.di.value) & _mask(identity.inputs)
        if identity.outputs:
            outputs = _call(lambda: hat.dq.value) & _mask(identity.outputs)
        values = {}
        for index, edge in counters:
            source = hat.di.r_counters if edge is Edge.RISING else hat.di.f_counters
            values[(index, edge)] = _call(source.__getitem__, index)
        return BoardState(inputs, outputs, status, values, captures)

    def arm_irq(self) -> list[str]:
        """Capture every edge of every input, then arm the capture queue.

        The edge masks live in EEPROM, so they are written only when they
        differ. The arming bit is volatile and clears on a board reset or a
        watchdog trip, which is why this runs again after either. Arming comes
        last, so the board never captures with half-set masks.
        """
        registers = self._hat.di.irq_reg
        everything = _mask(self._identity.inputs)
        changed = []
        for name in ("rising_edge_control", "falling_edge_control"):
            have = _call(getattr, registers, name)
            if have != everything:
                _call(setattr, registers, name, everything)
                changed.append(f"{name} {have:#x} -> {everything:#x}")
        _call(setattr, registers, "capture", 0)
        _call(setattr, registers, "global_enable", 1)
        return changed

    def disarm_irq(self) -> None:
        """Disarm: the queue is dropped and the interrupt line released."""
        _call(setattr, self._hat.di.irq_reg, "global_enable", 0)

    def drain_captures(self) -> list[int]:
        """Empty the capture queue; the input states at each edge, oldest first.

        Each entry is ``(states << 16) | edges``, and 0 means empty. Reading
        is bounded by the queue depth, so an input chattering faster than
        the bus can read stops here and continues on the next drain.
        """
        registers = self._hat.di.irq_reg
        everything = _mask(self._identity.inputs)
        states = []
        for _ in range(_CAPTURE_QUEUE_DEPTH):
            capture = _call(getattr, registers, "capture")
            if not capture:
                break
            states.append(capture >> 16 & everything)
        return states

    def write_output(self, index: int, value: bool) -> None:
        """Set one output; single-channel writes leave the others alone."""
        hat = self._hat
        _call(lambda: hat.dq.channels.__setitem__(index, int(value)))

    def read_settings(self) -> BoardSettings:
        """Read the persistent registers the board has."""
        hat = self._hat
        identity = self._identity
        values: dict[str, int] = {}
        if identity.outputs:
            # The library hands the period over in seconds; the wire and the
            # options both count whole milliseconds and seconds.
            values["watchdog_ms"] = _call(lambda: round(hat.cwdt.period * 1000))
            values["safety_value"] = _call(lambda: hat.dq.safety_value)
            values["power_on_value"] = _call(lambda: hat.dq.power_on_value)
            if identity.has_safety_mask:
                values["safety_mask"] = _call(lambda: hat.dq.safety_mask)
        if identity.has_input_polarity:
            values["input_polarity"] = _call(lambda: hat.di.polarity)
        return BoardSettings(**values)

    def apply_settings(self, desired: BoardSettings) -> list[str]:
        """Write the settings that differ from the board's, and only those.

        These registers live in the board's EEPROM, which wears out, so a
        value that is already right is never written again. The safe state is
        written before the watchdog period, so the watchdog never runs with a
        safe state that is about to change.
        """
        hat = self._hat
        current = self.read_settings()
        writers: tuple[tuple[str, Callable[[int], None]], ...] = (
            ("safety_value", lambda v: setattr(hat.dq, "safety_value", v)),
            ("safety_mask", lambda v: setattr(hat.dq, "safety_mask", v)),
            ("power_on_value", lambda v: setattr(hat.dq, "power_on_value", v)),
            ("input_polarity", lambda v: setattr(hat.di, "polarity", v)),
            ("watchdog_ms", lambda v: setattr(hat.cwdt, "period", v / 1000)),
        )
        changed = []
        for name, write in writers:
            want = getattr(desired, name)
            have = getattr(current, name)
            if want is None or have is None or want == have:
                continue
            _call(write, want)
            if name == "watchdog_ms":
                changed.append(f"watchdog {have} ms -> {want} ms")
            else:
                changed.append(f"{name} {have:#x} -> {want:#x}")
        return changed

    @property
    def _identity(self) -> BoardIdentity:
        if self.identity is None:
            raise BoardError("board is not open")
        return self.identity


def _connect(address: int) -> Any:
    """Return a model-agnostic handle, for reading the board name."""
    try:
        return i2c_hats.I2CHat(address)
    except OSError as err:
        # Opening the bus is the only OSError the library lets through; I/O
        # errors on transfers come back as ResponseException.
        raise BusUnavailable(str(err)) from err


def _call[T](function: Callable[..., T], *args: Any) -> T:
    """Run one library call, translating its errors."""
    try:
        return function(*args)
    except BoardError:
        raise
    except OSError as err:
        raise BusUnavailable(str(err)) from err
    except ResponseException as err:
        raise BoardError(str(err)) from err
    except Exception as err:
        # The library raises a bare Exception for a board name mismatch.
        raise BoardError(str(err)) from err


def _supported(read: Callable[[], object]) -> bool:
    """Probe for a register the firmware may not have.

    A board without it still acknowledges, then answers filler that fails the
    frame check; that is the answer "no". Silence is not an answer, so it
    raises instead of being taken as one.
    """
    try:
        read()
    except ResponseException as err:
        if str(err) == _NO_RESPONSE:
            raise BoardError(str(err)) from err
        return False
    return True


def _mask(labels: tuple[str, ...]) -> int:
    return (1 << len(labels)) - 1


def labels_to_mask(labels: tuple[str, ...], selected: Iterable[str]) -> int:
    """Turn a list of channel labels into a bitmask."""
    chosen = set(selected)
    return sum(1 << index for index, label in enumerate(labels) if label in chosen)


def mask_to_labels(labels: tuple[str, ...], mask: int) -> list[str]:
    """Turn a bitmask into the list of channel labels it selects."""
    return [label for index, label in enumerate(labels) if mask >> index & 1]


def address_range(model: str) -> range:
    """Return the addresses a model's jumpers can select."""
    low, high = BOARDS[model].address_range
    return range(low, high + 1)
