"""Modbus TCP, the reading half: a server for the virtual devices, a client
for the gateway. Standard library only.

Function codes: 02 Read Discrete Inputs, 03 Read Holding Registers,
04 Read Input Registers. Writes are not implemented on purpose: the twin and
the gateway only ever read from a machine.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import threading
from typing import Callable

log = logging.getLogger("paotwin.modbus")

READ_DISCRETE_INPUTS = 0x02
READ_HOLDING_REGISTERS = 0x03
READ_INPUT_REGISTERS = 0x04

ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03
DEVICE_FAILURE = 0x04


class ModbusError(Exception):
    """A request failed: timeout, closed link, or an exception response."""


def pack_bits(bits: list[bool]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def unpack_bits(data: bytes, count: int) -> list[bool]:
    return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]


def answer(pdu: bytes, image: "DeviceImage") -> bytes:
    """The response PDU for one request PDU, against one device image."""
    if not pdu:
        return bytes([0x80, ILLEGAL_FUNCTION])
    fc = pdu[0]
    if fc not in (READ_DISCRETE_INPUTS, READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS):
        return bytes([fc | 0x80, ILLEGAL_FUNCTION])
    if len(pdu) != 5:
        return bytes([fc | 0x80, ILLEGAL_DATA_VALUE])
    start, count = struct.unpack(">HH", pdu[1:5])
    if fc == READ_DISCRETE_INPUTS:
        if not 1 <= count <= 2000:
            return bytes([fc | 0x80, ILLEGAL_DATA_VALUE])
        table = image.discrete_inputs
        if start + count > len(table):
            return bytes([fc | 0x80, ILLEGAL_DATA_ADDRESS])
        data = pack_bits(table[start:start + count])
        return bytes([fc, len(data)]) + data
    if not 1 <= count <= 125:
        return bytes([fc | 0x80, ILLEGAL_DATA_VALUE])
    table = image.input_registers if fc == READ_INPUT_REGISTERS else image.holding_registers
    if start + count > len(table):
        return bytes([fc | 0x80, ILLEGAL_DATA_ADDRESS])
    values = table[start:start + count]
    return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *(v & 0xFFFF for v in values))


class DeviceImage:
    """What one Modbus unit exposes. Lists are replaced whole, under a lock."""

    def __init__(self, discrete_inputs: int = 0, input_registers: int = 0,
                 holding_registers: int = 0) -> None:
        self.discrete_inputs = [False] * discrete_inputs
        self.input_registers = [0] * input_registers
        self.holding_registers = [0] * holding_registers


class Server:
    """An asyncio Modbus TCP server for one or more unit ids.

    ``responding`` set to False makes it behave like a pulled cable: open
    connections are closed and new ones are refused at once.
    """

    def __init__(self, units: dict[int, DeviceImage], host: str = "127.0.0.1",
                 port: int = 5021, before_read: Callable[[], None] | None = None) -> None:
        self.units, self.host, self.port = units, host, port
        self.before_read = before_read
        self.responding = True
        self._writers: set[asyncio.StreamWriter] = set()
        self._server = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("modbus listening on %s:%s", self.host, self.port)

    def set_responding(self, value: bool) -> None:
        self.responding = value
        if not value:
            for writer in list(self._writers):
                writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for writer in list(self._writers):
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.responding:
            writer.close()
            return
        self._writers.add(writer)
        try:
            while self.responding:
                header = await reader.readexactly(7)
                tid, proto, length, unit = struct.unpack(">HHHB", header)
                if proto != 0 or not 2 <= length <= 254:
                    break
                pdu = await reader.readexactly(length - 1)
                if not self.responding:
                    break
                image = self.units.get(unit)
                if image is None:
                    # A gateway with no such unit behind it (Modbus 4.4.3):
                    # exception 0x0B, target device failed to respond.
                    reply = bytes([(pdu[0] if pdu else 0) | 0x80, 0x0B])
                else:
                    if self.before_read:
                        self.before_read()
                    reply = answer(pdu, image)
                writer.write(struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()


class Client:
    """A blocking Modbus TCP client that reconnects when a request fails."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5021, timeout: float = 1.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._tid = 0
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _recv(self, n: int) -> bytes:
        data = bytearray()
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ModbusError("thiết bị đóng kết nối")
            data += chunk
        return bytes(data)

    def request(self, unit: int, pdu: bytes) -> bytes:
        with self._lock:
            try:
                if self._sock is None:
                    self._sock = socket.create_connection((self.host, self.port), self.timeout)
                    self._sock.settimeout(self.timeout)
                self._tid = (self._tid + 1) & 0xFFFF
                self._sock.sendall(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, unit) + pdu)
                tid, proto, length, r_unit = struct.unpack(">HHHB", self._recv(7))
                reply = self._recv(length - 1)
            except socket.timeout as exc:
                self._close()
                raise ModbusError("hết thời gian chờ thiết bị trả lời") from exc
            except ModbusError:
                self._close()
                raise
            except OSError as exc:
                self._close()
                raise ModbusError(f"không kết nối được thiết bị ({exc.__class__.__name__})") from exc
            if tid != self._tid or proto != 0 or r_unit != unit:
                self._close()
                raise ModbusError("câu trả lời không khớp với câu hỏi")
        if reply and reply[0] & 0x80:
            code = reply[1] if len(reply) > 1 else 0
            raise ModbusError(f"thiết bị báo lỗi Modbus mã {code:#04x}")
        if not reply or reply[0] != pdu[0]:
            raise ModbusError("câu trả lời sai mã chức năng")
        return reply

    def read_registers(self, unit: int, start: int, count: int, *, holding: bool = False) -> list[int]:
        fc = READ_HOLDING_REGISTERS if holding else READ_INPUT_REGISTERS
        reply = self.request(unit, struct.pack(">BHH", fc, start, count))
        if len(reply) != 2 + 2 * count or reply[1] != 2 * count:
            raise ModbusError("độ dài dữ liệu không đúng")
        return list(struct.unpack(f">{count}H", reply[2:]))

    def read_discrete_inputs(self, unit: int, start: int, count: int) -> list[bool]:
        reply = self.request(unit, struct.pack(">BHH", READ_DISCRETE_INPUTS, start, count))
        size = (count + 7) // 8
        if len(reply) != 2 + size or reply[1] != size:
            raise ModbusError("độ dài dữ liệu không đúng")
        return unpack_bits(reply[2:], count)
