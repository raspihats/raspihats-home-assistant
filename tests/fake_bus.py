"""A simulated I2C bus with I2C-HATs on it.

It stands in for smbus2.SMBus underneath the raspihats library and answers
real frames, so the tests run the library's framing, CRC and retries rather
than a mock of them.
"""

from __future__ import annotations

from raspihats.protocol import BOARDS, Command, Frame, StatusWordBits

FILLER = 0xEE


def _u32(value: int) -> list[int]:
    return [value >> shift & 0xFF for shift in (0, 8, 16, 24)]


def _from_u32(data: list[int]) -> int:
    return data[0] | data[1] << 8 | data[2] << 16 | data[3] << 24


class FakeBoard:
    """One I2C-HAT: channel state plus the persistent registers.

    ``safety_mask`` and ``polarity`` select whether the firmware has those
    registers; without them it answers filler, like older firmware does.
    """

    def __init__(
        self,
        model: str,
        firmware: tuple[int, int, int] = (3, 1, 0),
        *,
        safety_mask: bool = True,
        polarity: bool = True,
    ) -> None:
        info = BOARDS[model]
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

    def trip_watchdog(self) -> None:
        """What the firmware does when the watchdog period runs out."""
        mask = self.safety_mask if self.safety_mask is not None else -1
        self.outputs = (self.outputs & ~mask) | (self.safety_value & mask)
        self.status |= StatusWordBits.CWDT_TIMEOUT.value

    def power_cycle(self) -> None:
        """Power loss: outputs to the power-on value, counters to zero."""
        self.outputs = self.power_on_value
        self.rising = [0] * self.di_count
        self.falling = [0] * self.di_count
        self.status |= StatusWordBits.POR_RESET.value

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
        if self.di_count:
            match cmd:
                case Command.DI_GET_ALL_CHANNEL_STATES:
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


class FakeBus:
    """Stands in for smbus2.SMBus, the way the raspihats library uses it."""

    def __init__(self) -> None:
        self.boards: dict[int, FakeBoard] = {}
        self._answers: dict[int, list[int]] = {}

    def add(self, address: int, board: FakeBoard) -> FakeBoard:
        self.boards[address] = board
        return board

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
