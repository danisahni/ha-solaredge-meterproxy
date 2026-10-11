"""WattNode WNC-3Y-400-MB register image, as read by SolarEdge inverters.

Register layout and encoding follow nmakel/solaredge_meterproxy (MIT):
32-bit values are big-endian per word with the low word first
(pymodbus ``BinaryPayloadBuilder(byteorder=Big, wordorder=Little)``).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Start addresses as passed to ``ModbusSlaveContext.setValues(3, addr, ...)``.
BLOCK_BASIC = 1000      # energy, power, voltage, frequency
BLOCK_EXTENDED = 1100   # per-phase energy, PF, reactive/apparent, current, demand
BLOCK_CONFIG = 1600
BLOCK_COMM = 1650
BLOCK_INFO = 1700


def _words(raw: bytes) -> list[int]:
    hi_lo = list(struct.unpack(">%dH" % (len(raw) // 2), raw))
    out: list[int] = []
    for i in range(0, len(hi_lo), 2):
        out += [hi_lo[i + 1], hi_lo[i]]  # word order: little
    return out


def f32(value: float) -> list[int]:
    return _words(struct.pack(">f", float(value)))


def i32(value: int) -> list[int]:
    return _words(struct.pack(">i", int(value)))


def i16(value: int) -> list[int]:
    return [struct.unpack(">H", struct.pack(">h", int(value)))[0]]


@dataclass
class PhaseValues:
    power_w: float = 0.0
    voltage_v: float = 230.0
    current_a: float = 0.0
    reactive_var: float = 0.0
    power_factor: float = 1.0
    energy_kwh: float = 0.0


@dataclass
class MeterValues:
    """Values of one simulated meter. Energy in kWh, power in W.

    ``phases`` lists the phases the source feeds (1..3): one entry for a
    single-phase source, ``(1, 2, 3)`` for a three-phase source.
    """

    phases: tuple[int, ...] = (1,)
    energy_kwh: float = 0.0
    power_w: float = 0.0
    reactive_var: float = 0.0
    frequency_hz: float = 50.0
    power_factor: float = 1.0
    per_phase: dict[int, PhaseValues] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for n in self.phases:
            self.per_phase.setdefault(n, PhaseValues())

    @property
    def three_phase(self) -> bool:
        return len(self.phases) == 3

    @property
    def voltage_v(self) -> float:
        vs = [self.per_phase[n].voltage_v for n in self.phases]
        return sum(vs) / len(vs)

    def as_dict(self) -> dict[str, float]:
        """Flatten to the generic key set used by the original project."""
        d = {
            "energy_active": self.energy_kwh,
            "import_energy_active": self.energy_kwh,
            "power_active": self.power_w,
            "power_reactive": self.reactive_var,
            "voltage_ln": self.voltage_v,
            "frequency": self.frequency_hz,
            "power_factor": self.power_factor,
        }
        for n in self.phases:
            ph, p = self.per_phase[n], f"l{n}"
            d[f"{p}_energy_active"] = ph.energy_kwh
            d[f"{p}_import_energy_active"] = ph.energy_kwh
            d[f"{p}_power_active"] = ph.power_w
            d[f"{p}_power_reactive"] = ph.reactive_var
            d[f"{p}n_voltage"] = ph.voltage_v
            d[f"{p}_current"] = ph.current_a
            d[f"{p}_power_factor"] = ph.power_factor
        if self.three_phase:
            u = {n: self.per_phase[n].voltage_v for n in (1, 2, 3)}
            r3 = 3 ** 0.5
            d["l12_voltage"] = (u[1] + u[2]) / 2 * r3
            d["l23_voltage"] = (u[2] + u[3]) / 2 * r3
            d["l31_voltage"] = (u[3] + u[1]) / 2 * r3
            d["voltage_ll"] = self.voltage_v * r3
        return d


def basic_block(v: dict) -> list[int]:
    g = lambda k: v.get(k, 0)  # noqa: E731
    regs: list[int] = []
    for key in (
        "energy_active", "import_energy_active", "energy_active", "import_energy_active",
        "power_active", "l1_power_active", "l2_power_active", "l3_power_active",
        "voltage_ln", "l1n_voltage", "l2n_voltage", "l3n_voltage",
        "voltage_ll", "l12_voltage", "l23_voltage", "l31_voltage",
        "frequency",
    ):
        regs += f32(g(key))
    return regs


def extended_block(v: dict) -> list[int]:
    g = lambda k: v.get(k, 0)  # noqa: E731
    regs: list[int] = []
    for key in (
        "l1_energy_active", "l2_energy_active", "l3_energy_active",
        "l1_import_energy_active", "l2_import_energy_active", "l3_import_energy_active",
        "export_energy_active", "export_energy_active",
        "l1_export_energy_active", "l2_export_energy_active", "l3_export_energy_active",
        "energy_reactive", "l1_energy_reactive", "l2_energy_reactive", "l3_energy_reactive",
        "energy_apparent", "l1_energy_apparent", "l2_energy_apparent", "l3_energy_apparent",
        "power_factor", "l1_power_factor", "l2_power_factor", "l3_power_factor",
        "power_reactive", "l1_power_reactive", "l2_power_reactive", "l3_power_reactive",
        "power_apparent", "l1_power_apparent", "l2_power_apparent", "l3_power_apparent",
        "l1_current", "l2_current", "l3_current",
        "demand_power_active", "minimum_demand_power_active", "maximum_demand_power_active",
        "demand_power_apparent",
        "l1_demand_power_active", "l2_demand_power_active", "l3_demand_power_active",
    ):
        regs += f32(g(key))
    return regs


def config_block(ct_current: int, ct_inverted: int, phase_offset: int) -> list[int]:
    regs = i32(0)  # config passcode
    regs += i16(ct_current) * 4  # ct rated current (total, l1, l2, l3)
    regs += i16(ct_inverted)
    regs += i16(1)       # measurement averaging
    regs += i16(0)       # power scale
    regs += i16(15)      # demand period
    regs += i16(1)       # demand subintervals
    regs += i16(10000) * 3   # power/energy adjustment l1..l3
    regs += i16(-1000) * 3   # ct phase angle adjustment l1..l3
    regs += i16(1500)    # minimum power reading
    regs += i16(phase_offset)
    regs += i16(0)       # reset energy
    regs += i16(0)       # reset demand
    regs += i16(20000)   # current scale
    regs += i16(0)       # io pin mode
    return regs


def comm_block(address: int) -> list[int]:
    return i16(0) + i16(address) + i16(4) + i16(0) + i16(0) + i16(5)


def info_block(serial_number: int) -> list[int]:
    regs = i32(serial_number) + i32(0) + i32(0)
    regs += i16(202)  # WattNode model
    regs += i16(31)   # firmware version
    regs += i16(0) * 17  # options, error status/counters, error status 1..8
    return regs
