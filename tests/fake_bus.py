"""A simulated I2C bus with I2C-HATs on it.

It stands in for smbus2.SMBus underneath the raspihats library and answers
real frames, so the tests run the library's framing, CRC and retries rather
than a mock of them.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import os

from raspihats.protocol import BOARDS, Command, Frame, IrqRegister, StatusWordBits

FILLER = 0xEE
QUEUE_DEPTH = 128


def _u32(value: int) -> list[int]:
    return [value >> shift & 0xFF for shift in (0, 8, 16, 24)]


def _from_u32(data: list[int]) -> int:
    return data[0] | data[1] << 8 | data[2] << 16 | data[3] << 24


class FakeBoard:
    """One I2C-HAT: channel state plus the persistent registers.

    ``safety_mask``, ``polarity``, ``irq`` and ``irq_enable`` select whether
    the firmware has those registers; without them it answers filler, like
    older firmware does. Without ``irq_enable`` (before 3.0.0) the capture
    queue follows the old rules: the edge masks alone arm it and are
    volatile, and reading the inputs releases the interrupt line.
    """

    def __init__(
        self,
        model: str,
        firmware: tuple[int, int, int] = (3, 1, 0),
        *,
        safety_mask: bool = True,
        polarity: bool = True,
        irq: bool = True,
        irq_enable: bool = True,
    ) -> None:
        info = BOARDS[model]
        self.has_irq = info.has_irq and irq
        self.irq_enable = irq_enable
        self.rising_mask = 0
        self.falling_mask = 0
        self._enabled = False
        self.line_released = False
        self.queue: list[int] = []
        #: Called when this board starts pulling the interrupt line low.
        self.on_assert: Callable[[], None] | None = None
        self.board_name = info.board_name
        self.firmware = firmware
        self.di_count = info.channel_count("di")
        self.dq_count = info.channel_count("dq")
        self.inputs = 0
        self.outputs = 0
        self.rising = [0] * self.di_count
        self.falling = [0] * self.di_count
        self.cwdt_ms = 0
        self.safety_value = 0
        self.power_on_value = 0
        everything = (1 << self.dq_count) - 1
        self.safety_mask: int | None = everything if safety_mask else None
        self.di_polarity: int | None = 0 if polarity and self.di_count else None
        self.status = StatusWordBits.POR_RESET.value
        self.status_reads = 0
        self.responding = True
        #: Persistent writes, as (register, value), to check write-on-diff.
        self.writes: list[tuple[str, int]] = []

    @property
    def armed(self) -> bool:
        """Whether edges are captured."""
        if self.irq_enable:
            return self._enabled
        return bool(self.rising_mask | self.falling_mask)

    @armed.setter
    def armed(self, value: bool) -> None:
        self._enabled = value

    @property
    def line_asserted(self) -> bool:
        """The interrupt line is low while armed with captures pending.

        Before 3.0.0 an input read releases it regardless, until the next
        capture.
        """
        return self.armed and bool(self.queue) and not self.line_released

    def set_inputs(self, value: int) -> None:
        """Drive the inputs: counts the edges and captures them if armed."""
        changed = value ^ self.inputs
        self.inputs = value
        for index in range(self.di_count):
            if changed >> index & 1:
                counts = self.rising if value >> index & 1 else self.falling
                counts[index] += 1
        edges = (changed & value & self.rising_mask) | (
            changed & ~value & self.falling_mask
        )
        if self.armed and edges:
            was_asserted = self.line_asserted
            if len(self.queue) == QUEUE_DEPTH:
                self.queue.pop(0)
                self.status |= StatusWordBits.DI_IRQ_CAPTURE_QUEUE_FULL.value
            self.queue.append(value << 16 | edges)
            self.line_released = False
            if not was_asserted and self.on_assert is not None:
                self.on_assert()

    def trip_watchdog(self) -> None:
        """What the firmware does when the watchdog period runs out."""
        mask = self.safety_mask if self.safety_mask is not None else -1
        self.outputs = (self.outputs & ~mask) | (self.safety_value & mask)
        self.status |= StatusWordBits.CWDT_TIMEOUT.value
        if self.irq_enable:
            self._enabled = False
            self.queue.clear()

    def power_cycle(self) -> None:
        """Power loss: outputs to the power-on value, counters to zero."""
        self.outputs = self.power_on_value
        self.rising = [0] * self.di_count
        self.falling = [0] * self.di_count
        self.status |= StatusWordBits.POR_RESET.value
        self._enabled = False
        self.queue.clear()
        if not self.irq_enable:
            self.rising_mask = self.falling_mask = 0

    def _irq(self, cmd: Command, data: list[int]) -> list[int] | None:
        register = IrqRegister(data[0])
        if register is IrqRegister.DI_GLOBAL_ENABLE and not self.irq_enable:
            return None
        if cmd is Command.IRQ_SET_REG:
            value = _from_u32(data[1:])
            match register:
                case IrqRegister.DI_RISING_EDGE_CONTROL:
                    self.rising_mask = self._mask_write("rising_mask", data[1:])
                case IrqRegister.DI_FALLING_EDGE_CONTROL:
                    self.falling_mask = self._mask_write("falling_mask", data[1:])
                case IrqRegister.DI_CAPTURE:
                    self.queue.clear()
                case IrqRegister.DI_GLOBAL_ENABLE:
                    self._enabled = bool(value)
                    if not value:
                        self.queue.clear()
            return data
        match register:
            case IrqRegister.DI_RISING_EDGE_CONTROL:
                value = self.rising_mask
            case IrqRegister.DI_FALLING_EDGE_CONTROL:
                value = self.falling_mask
            case IrqRegister.DI_CAPTURE:
                value = self.queue.pop(0) if self.queue else 0
            case IrqRegister.DI_GLOBAL_ENABLE:
                value = int(self.armed)
        return [register.value, *_u32(value)]

    def handle(self, cmd: Command, data: list[int]) -> list[int] | None:
        """Answer one request; None means the firmware lacks the command."""
        match cmd:
            case Command.GET_BOARD_NAME:
                name = list(self.board_name.encode())
                return name + [0] * (25 - len(name))
            case Command.GET_FIRMWARE_VERSION:
                return list(self.firmware)
            case Command.GET_STATUS_WORD:
                self.status_reads += 1
                status, self.status = self.status, 0
                return _u32(status)
            case Command.CWDT_GET_PERIOD:
                return _u32(self.cwdt_ms)
            case Command.CWDT_SET_PERIOD:
                self.cwdt_ms = self._persist("cwdt_ms", data)
                return data
        if self.has_irq and cmd in (Command.IRQ_GET_REG, Command.IRQ_SET_REG):
            return self._irq(cmd, data)
        if self.di_count:
            match cmd:
                case Command.DI_GET_ALL_CHANNEL_STATES:
                    if not self.irq_enable:
                        self.line_released = True
                    return _u32(self.inputs)
                case Command.DI_GET_COUNTER:
                    index, kind = data
                    counts = self.rising if kind == 1 else self.falling
                    return [index, kind, *_u32(counts[index])]
                case Command.DI_GET_POLARITY if self.di_polarity is not None:
                    return _u32(self.di_polarity)
                case Command.DI_SET_POLARITY if self.di_polarity is not None:
                    self.di_polarity = self._persist("di_polarity", data)
                    return data
        if self.dq_count:
            match cmd:
                case Command.DQ_GET_ALL_CHANNEL_STATES:
                    return _u32(self.outputs)
                case Command.DQ_SET_CHANNEL_STATE:
                    index, value = data
                    if value:
                        self.outputs |= 1 << index
                    else:
                        self.outputs &= ~(1 << index)
                    return data
                case Command.DQ_GET_SAFETY_VALUE:
                    return _u32(self.safety_value)
                case Command.DQ_SET_SAFETY_VALUE:
                    self.safety_value = self._persist("safety_value", data)
                    return data
                case Command.DQ_GET_POWER_ON_VALUE:
                    return _u32(self.power_on_value)
                case Command.DQ_SET_POWER_ON_VALUE:
                    self.power_on_value = self._persist("power_on_value", data)
                    return data
                case Command.DQ_GET_SAFETY_MASK if self.safety_mask is not None:
                    return _u32(self.safety_mask)
                case Command.DQ_SET_SAFETY_MASK if self.safety_mask is not None:
                    self.safety_mask = self._persist("safety_mask", data)
                    return data
        return None

    def _persist(self, register: str, data: list[int]) -> int:
        value = _from_u32(data)
        self.writes.append((register, value))
        return value

    def _mask_write(self, register: str, data: list[int]) -> int:
        # Edge masks are EEPROM-backed from 3.0.0 only.
        return self._persist(register, data) if self.irq_enable else _from_u32(data)


class FakeBus:
    """Stands in for smbus2.SMBus, the way the raspihats library uses it."""

    def __init__(self) -> None:
        self.boards: dict[int, FakeBoard] = {}
        self.lines: list[FakeLine] = []
        self._answers: dict[int, list[int]] = {}

    def add(self, address: int, board: FakeBoard) -> FakeBoard:
        self.boards[address] = board
        board.on_assert = self._asserted
        return board

    @property
    def line_asserted(self) -> bool:
        """The shared interrupt line: wired-OR of every board on it."""
        return any(board.line_asserted for board in self.boards.values())

    def _asserted(self) -> None:
        for line in self.lines:
            line.fire()

    def _board(self, address: int) -> FakeBoard:
        board = self.boards.get(address)
        if board is None or not board.responding:
            raise OSError(121, "Remote I/O error")
        return board

    def write_i2c_block_data(
        self, address: int, register: int, data: list[int]
    ) -> None:
        board = self._board(address)
        raw = [register, *data]
        request = Frame(raw[0], raw[1])
        request.decode(raw)
        answer = board.handle(request.cmd, request.data)
        self._answers[address] = (
            Frame(request.id, request.cmd, answer).encode()
            if answer is not None
            else []
        )

    def read_i2c_block_data(
        self, address: int, register: int, length: int
    ) -> list[int]:
        self._board(address)
        answer = self._answers.pop(address, [])
        return (answer + [FILLER] * length)[:length]

    def close(self) -> None:
        """Nothing to release."""


class FakeLine:
    """Stands in for the GPIO the boards pull low.

    Its file descriptor is a real pipe, so Home Assistant's event loop
    watches it exactly as it watches the GPIO's.
    """

    def __init__(self, bus: FakeBus, offset: int) -> None:
        self.bus = bus
        self.offset = offset
        self.released = False
        self._read, self._write = os.pipe()
        os.set_blocking(self._read, False)
        bus.lines.append(self)

    @property
    def fd(self) -> int:
        return self._read

    def asserted(self) -> bool:
        return self.bus.line_asserted

    def fire(self) -> None:
        """An edge on the line."""
        os.write(self._write, b"\0")

    def clear_events(self) -> None:
        with contextlib.suppress(BlockingIOError):
            os.read(self._read, 4096)

    def release(self) -> None:
        self.released = True
        self.bus.lines.remove(self)
        os.close(self._read)
        os.close(self._write)
