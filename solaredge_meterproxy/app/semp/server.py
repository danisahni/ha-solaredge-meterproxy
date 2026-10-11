"""Modbus RTU slave that emulates the WattNode meter on the inverter's RS485 bus."""

from __future__ import annotations

import logging
import time

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.server.async_io import ModbusSerialServer
from pymodbus.transaction import ModbusRtuFramer

from . import registers as R

_LOG = logging.getLogger("semp.server")


class PollStats:
    """Counts read requests addressed to our slave id."""

    def __init__(self) -> None:
        self.last_poll: float | None = None   # wall time
        self.count = 0

    def hit(self) -> None:
        self.last_poll = time.time()
        self.count += 1


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class BusStats:
    """Read requests (CRC-checked) seen on the bus, per slave id.

    Tells "no traffic at all" (wiring, baud rate, parity) apart from "traffic,
    but not for our address" (device ID / meter not set up on the inverter).

    Strictly passive: it works on its own copy of the received bytes and never
    touches the framing pymodbus uses to answer the inverter. (An earlier
    version changed how pymodbus looks for frame starts; that made it miss
    requests for our own ID now and then, which the inverter treats as a meter
    communication fault.)
    """

    MAX_BUFFER = 512

    def __init__(self) -> None:
        self.frames: dict[int, int] = {}
        self.last_frame: float | None = None
        self._buf = b""

    def seen(self, uid: int) -> None:
        self.frames[uid] = self.frames.get(uid, 0) + 1
        self.last_frame = time.time()

    def feed(self, data: bytes) -> None:
        """Find 8-byte read requests (function 3/4) with a valid CRC."""
        buf = (self._buf + data)[-self.MAX_BUFFER:]
        i = 0
        while i + 8 <= len(buf):
            frame = buf[i:i + 8]
            if frame[1] in (3, 4) and 0 < frame[0] < 248 and \
                    _crc16(frame[:6]) == frame[6] | (frame[7] << 8):
                self.seen(frame[0])
                i += 8
            else:
                i += 1
        self._buf = buf[i:]


def sniffing_framer(stats: BusStats):
    class SniffingRtuFramer(ModbusRtuFramer):
        def processIncomingPacket(self, data, callback, slave, **kwargs):  # noqa: N802 - pymodbus API
            try:
                stats.feed(bytes(data))
            except Exception:  # noqa: BLE001 - diagnostics must never break the server
                pass
            return super().processIncomingPacket(data, callback, slave, **kwargs)

    return SniffingRtuFramer


class TrackingBlock(ModbusSequentialDataBlock):
    """Holding-register block that records every read from the inverter."""

    def __init__(self, stats: PollStats):
        super().__init__(0, [0] * 65536)
        self._stats = stats

    def getValues(self, address, count=1):  # noqa: N802 - pymodbus API
        self._stats.hit()
        return super().getValues(address, count)


class MeterSlave:
    """Register image of one emulated meter (one Modbus device ID)."""

    def __init__(self, *, address: int, serial_number: int, ct_current: int,
                 ct_inverted: int = 0, phase_offset: int = 0):
        self.address = address
        self.stats = PollStats()
        self.context = ModbusSlaveContext(hr=TrackingBlock(self.stats), zero_mode=False)
        self.configure(serial_number=serial_number, ct_current=ct_current,
                       ct_inverted=ct_inverted, phase_offset=phase_offset)

    def configure(self, *, serial_number: int, ct_current: int, ct_inverted: int = 0,
                  phase_offset: int = 0) -> None:
        # static blocks (writes don't count as polls: setValues bypasses getValues)
        self.context.setValues(3, R.BLOCK_CONFIG, R.config_block(ct_current, ct_inverted, phase_offset))
        self.context.setValues(3, R.BLOCK_COMM, R.comm_block(self.address))
        self.context.setValues(3, R.BLOCK_INFO, R.info_block(serial_number))

    def publish(self, values: R.MeterValues) -> None:
        d = values.as_dict()
        self.context.setValues(3, R.BLOCK_BASIC, R.basic_block(d))
        self.context.setValues(3, R.BLOCK_EXTENDED, R.extended_block(d))


class PortServer:
    """Modbus RTU slave on one serial port, answering for one or more meters."""

    def __init__(self, *, port: str, baudrate: int, parity: str):
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.bus = BusStats()
        self.slaves: dict[int, MeterSlave] = {}
        self.context = ModbusServerContext(slaves={}, single=False)
        self.started = time.time()
        self._server: ModbusSerialServer | None = None

    def add(self, slave: MeterSlave) -> None:
        self.slaves[slave.address] = slave
        self.context[slave.address] = slave.context  # live: the server reads the slave list per frame

    def remove(self, address: int) -> None:
        self.slaves.pop(address, None)
        try:
            del self.context[address]
        except Exception:  # noqa: BLE001 - not present
            pass

    @property
    def last_poll(self) -> float | None:
        return max((s.stats.last_poll for s in self.slaves.values() if s.stats.last_poll), default=None)

    async def serve(self) -> None:
        identity = ModbusDeviceIdentification()
        identity.VendorName = "ha-solaredge-meterproxy"
        identity.ProductName = "WattNode emulator"
        self._server = ModbusSerialServer(
            context=self.context,
            framer=sniffing_framer(self.bus),
            identity=identity,
            port=self.port,
            baudrate=self.baudrate,
            parity=self.parity,
            bytesize=8,
            stopbits=1,
            timeout=0.1,
            # Shared bus (grid meter, battery, backup interface): never answer
            # for slave ids that are not ours and never react to broadcasts.
            ignore_missing_slaves=True,
            broadcast_enable=False,
        )
        _LOG.info("Serving WattNode emulation on %s @ %d 8%s1, device id(s) %s", self.port, self.baudrate,
                  self.parity, ", ".join(str(a) for a in sorted(self.slaves)) or "-")
        await self._server.serve_forever()

    async def shutdown(self) -> None:
        if self._server:
            await self._server.shutdown()
