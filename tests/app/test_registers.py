"""The register image must be bit-identical to the original semp-rtu.py output."""

import pytest
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadBuilder

from semp import registers as R


def _builder():
    return BinaryPayloadBuilder(byteorder=Endian.Big, wordorder=Endian.Little)


@pytest.mark.parametrize("value", [0.0, 1.5, -812.25, 400.338, 230.1, 1e6])
def test_f32_matches_pymodbus(value):
    b = _builder()
    b.add_32bit_float(value)
    assert R.f32(value) == b.to_registers()


@pytest.mark.parametrize("value", [0, 3, 41613858, -1, 0x0D010556])
def test_i32_matches_pymodbus(value):
    b = _builder()
    b.add_32bit_int(value)
    assert R.i32(value) == b.to_registers()


@pytest.mark.parametrize("value", [0, 50, -1000, 20000, 1500])
def test_i16_matches_pymodbus(value):
    b = _builder()
    b.add_16bit_int(value)
    assert R.i16(value) == b.to_registers()


def test_config_block_matches_original():
    b = _builder()
    b.add_32bit_int(0)
    for _ in range(4):
        b.add_16bit_int(50)
    for v in (0, 1, 0, 15, 1, 10000, 10000, 10000, -1000, -1000, -1000, 1500, 0, 0, 0, 20000, 0):
        b.add_16bit_int(v)
    assert R.config_block(50, 0, 0) == b.to_registers()


def test_info_block_matches_original():
    b = _builder()
    b.add_32bit_int(41613858)
    b.add_32bit_int(0)
    b.add_32bit_int(0)
    for v in [202, 31] + [0] * 17:
        b.add_16bit_int(v)
    assert R.info_block(41613858) == b.to_registers()


def test_block_sizes():
    assert len(R.basic_block({})) == 17 * 2
    assert len(R.extended_block({})) == 41 * 2


def test_single_phase_mapping_l1():
    v = R.MeterValues(phases=(1,), energy_kwh=400.338, power_w=812.0)
    v.per_phase[1].power_w = 812.0
    v.per_phase[1].energy_kwh = 400.338
    d = v.as_dict()
    assert d["power_active"] == d["l1_power_active"] == 812.0
    assert d["energy_active"] == d["l1_import_energy_active"] == 400.338
    assert "l2_power_active" not in d and "voltage_ll" not in d


def test_single_phase_keys_unchanged_from_v1():
    """Single-phase register content must stay exactly as in the field-proven version."""
    d = R.MeterValues(phases=(2,)).as_dict()
    assert set(d) == {
        "energy_active", "import_energy_active", "power_active", "power_reactive", "voltage_ln",
        "frequency", "power_factor", "l2_energy_active", "l2_import_energy_active", "l2_power_active",
        "l2_power_reactive", "l2n_voltage", "l2_current", "l2_power_factor",
    }


def test_three_phase_mapping():
    v = R.MeterValues(phases=(1, 2, 3))
    for n, (p, u) in enumerate([(1000, 230), (1100, 232), (900, 228)], start=1):
        v.per_phase[n].power_w, v.per_phase[n].voltage_v = p, u
    d = v.as_dict()
    assert d["l2_power_active"] == 1100 and d["l3n_voltage"] == 228
    assert d["voltage_ln"] == 230
    assert round(d["l12_voltage"], 1) == round(231 * 3 ** 0.5, 1)


def test_addon_config_is_supervisor_compatible():
    """Catch config.yaml mistakes the Supervisor would reject silently."""
    import json
    from pathlib import Path

    import yaml

    from semp import __version__

    root = Path(__file__).parents[2]
    cfg = yaml.safe_load((root / "solaredge_meterproxy" / "config.yaml").read_text())
    for key in ("name", "version", "slug", "description", "arch"):
        assert key in cfg
    assert isinstance(cfg.get("watchdog", ""), str)  # URL, not a bool
    assert set(cfg["options"]) <= set(cfg["schema"])
    assert "ingress" not in cfg
    # discovery service name == integration domain, versions in lockstep
    manifest = json.loads((root / "custom_components" / "solaredge_meterproxy" / "manifest.json").read_text())
    assert cfg["discovery"] == [manifest["domain"]]
    assert cfg["version"] == manifest["version"] == __version__
