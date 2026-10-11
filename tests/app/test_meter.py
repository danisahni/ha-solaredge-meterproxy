from datetime import datetime, timezone

import pytest

from semp.config import ConfigError, normalize
from semp.meter import Meter
from semp.source import parse_state

NOW = 1_800_000_000.0


def st(state, unit="W", age=1.0, now=NOW):
    ts = datetime.fromtimestamp(now - age, timezone.utc).isoformat()
    return parse_state({"state": str(state), "attributes": {"unit_of_measurement": unit},
                        "last_reported": ts, "last_updated": ts})


def ent(sid, phase="L1", **entities):
    return {"id": sid, "type": "entities", "phase": phase,
            "entities": entities or {"power": f"sensor.{sid}_p", "energy": f"sensor.{sid}_e"}}


def meter_cfg(*sources, mid="m1", address=3, port="/dev/ttyX"):
    return normalize({"meters": [{"id": mid, "name": mid, "port": port, "address": address,
                                  "sources": list(sources)}]})["meters"][0]


def make(*sources, state=None, source_states=None):
    return Meter(meter_cfg(*sources), stale_timeout=300, state=state, source_states=source_states or {})


# ------------------------------------------------------------------ config
def test_config_defaults_and_validation():
    cfg = normalize({"meters": [{"id": "a", "port": "/dev/ttyUSB0", "address": 3,
                                 "sources": [ent("s1")]}]})
    m = cfg["meters"][0]
    assert (m["baudrate"], m["parity"], m["ct_current"], m["serial_number"]) == (9600, "N", 50, 10_000_003)
    assert cfg["settings"] == {"stale_timeout": 300, "poll_timeout": 300, "update_interval": 2}


@pytest.mark.parametrize("bad, msg", [
    ({"address": 0}, "Modbus address"),
    ({"port": ""}, "serial port"),
    ({"sources": []}, "at least one source"),
    ({"sources": [{"id": "s", "type": "entities", "phase": "L1", "entities": {"energy": "sensor.e"}}]},
     "power sensor is required"),
    ({"sources": [{"id": "s", "type": "entities", "phase": "L1", "entities": {"power": "AC Power"}}]},
     "not an entity id"),
    ({"sources": [{"id": "s", "type": "mqtt", "phase": "L1"}]}, "MQTT topic"),
])
def test_config_errors(bad, msg):
    m = {"id": "a", "port": "/dev/ttyUSB0", "address": 3, "sources": [ent("s1")], **bad}
    with pytest.raises(ConfigError) as exc:
        normalize({"meters": [m]})
    assert msg in str(exc.value)


def test_config_port_rules():
    a = {"id": "a", "port": "/dev/ttyUSB0", "address": 3, "sources": [ent("s1")]}
    with pytest.raises(ConfigError, match="already used by a"):
        normalize({"meters": [a, {**a, "id": "b"}]})
    with pytest.raises(ConfigError, match="same baud rate"):
        normalize({"meters": [a, {**a, "id": "b", "address": 4, "baudrate": 19200}]})
    assert len(normalize({"meters": [a, {**a, "id": "b", "address": 4}]})["meters"]) == 2


def test_three_phase_source_needs_power():
    s = {"id": "s", "type": "entities", "phase": "3P",
         "entities": {f"l{n}_power": f"sensor.p{n}" for n in (1, 2, 3)}}
    normalize({"meters": [{"id": "a", "port": "/dev/x", "address": 3, "sources": [s]}]})
    s["entities"].pop("l3_power")
    with pytest.raises(ConfigError, match="three phase power"):
        normalize({"meters": [{"id": "a", "port": "/dev/x", "address": 3, "sources": [s]}]})


# ------------------------------------------------------------------ meter
def test_single_source_is_identical_to_v1():
    m = make(ent("a"))
    v = m.aggregate({"a": {"power": st(812.5), "energy": st(400338, "Wh")}}, NOW)
    assert v.phases == (1,) and v.power_w == 812.5
    assert v.energy_kwh == pytest.approx(400.338)  # new meter adopts the lifetime counter
    assert v.per_phase[1].current_a == pytest.approx(812.5 / 230)


def test_sources_on_same_and_different_phases_are_summed():
    m = make(ent("a", "L1"), ent("b", "L1"), ent("c", "L2"))
    v = m.aggregate({
        "a": {"power": st(300), "energy": st(10, "kWh")},
        "b": {"power": st(200), "energy": st(5, "kWh")},
        "c": {"power": st(1000), "energy": st(100, "kWh")},
    }, NOW)
    assert v.phases == (1, 2)
    assert (v.per_phase[1].power_w, v.per_phase[2].power_w, v.power_w) == (500, 1000, 1500)
    assert (v.per_phase[1].energy_kwh, v.per_phase[2].energy_kwh, v.energy_kwh) == (15, 100, 115)


def test_single_and_three_phase_sources_mix():
    s3 = {"id": "t", "type": "entities", "phase": "3P", "entities": {"power": "sensor.tp", "energy": "sensor.te"}}
    m = make(ent("a", "L2"), s3)
    v = m.aggregate({"a": {"power": st(600)}, "t": {"power": st(3000), "energy": st(30, "kWh")}}, NOW)
    assert v.phases == (1, 2, 3) and v.three_phase
    assert [v.per_phase[n].power_w for n in (1, 2, 3)] == [1000, 1600, 1000]


def test_energy_counts_only_increases():
    m = make(ent("a"), ent("b"))
    m.aggregate({"a": {"power": st(1), "energy": st(10, "kWh")}, "b": {"power": st(1), "energy": st(20, "kWh")}},
                NOW)
    assert m.values.energy_kwh == 30 and not m.adopt
    v = m.aggregate({"a": {"power": st(1), "energy": st(10.5, "kWh")},
                     "b": {"power": st(1), "energy": st(20.25, "kWh")}}, NOW + 2)
    assert v.energy_kwh == pytest.approx(30.75)


def test_adding_a_source_later_does_not_jump():
    m = make(ent("a"))
    m.aggregate({"a": {"power": st(1), "energy": st(10, "kWh")}}, NOW)
    m.reconfigure(meter_cfg(ent("a"), ent("b")), stale_timeout=300, source_states={})
    v = m.aggregate({"a": {"power": st(1), "energy": st(11, "kWh")},
                     "b": {"power": st(1), "energy": st(500, "kWh")}}, NOW + 2)
    assert v.energy_kwh == pytest.approx(11)   # b's lifetime counter is only the baseline
    v = m.aggregate({"a": {"power": st(1), "energy": st(11, "kWh")},
                     "b": {"power": st(1), "energy": st(502, "kWh")}}, NOW + 4)
    assert v.energy_kwh == pytest.approx(13)


def test_removing_a_source_or_changing_phase_never_decreases():
    m = make(ent("a"), ent("b", "L2"))
    m.aggregate({"a": {"power": st(1), "energy": st(10, "kWh")}, "b": {"power": st(1), "energy": st(20, "kWh")}},
                NOW)
    m.reconfigure(meter_cfg(ent("a", "L3")), stale_timeout=300, source_states={})
    v = m.aggregate({"a": {"power": st(1), "energy": st(10, "kWh")}}, NOW + 2)
    assert v.phases == (3,) and v.energy_kwh == pytest.approx(30)


def test_restart_keeps_counters_and_counts_downtime_production():
    m = make(ent("a"))
    m.aggregate({"a": {"power": st(1), "energy": st(10, "kWh")}}, NOW)
    meter_state = m.state()
    src_states = {s.id: s.state() for s in m.sources}
    m2 = make(ent("a"), state=meter_state, source_states=src_states)
    assert m2.values.energy_kwh == 10         # published before the first reading
    v = m2.aggregate({"a": {"power": st(1), "energy": st(12, "kWh")}}, NOW + 3600)
    assert v.energy_kwh == pytest.approx(12)


def test_swapped_sensor_starts_new_baseline():
    m = make(ent("a"))
    m.aggregate({"a": {"power": st(1), "energy": st(10, "kWh")}}, NOW)
    m.reconfigure(meter_cfg(ent("a", power="sensor.other_p", energy="sensor.other_e")), stale_timeout=300,
                  source_states={s.id: s.state() for s in m.sources})
    v = m.aggregate({"a": {"power": st(1), "energy": st(900, "kWh")}}, NOW + 2)
    assert v.energy_kwh == pytest.approx(10)


def test_unavailable_source_counts_zero_power_other_keeps_running():
    m = make(ent("a"), ent("b"))
    m.aggregate({"a": {"power": st(300)}, "b": {"power": st(200)}}, NOW)
    v = m.aggregate({"a": {"power": st("unavailable")}, "b": {"power": st(250, now=NOW + 2)}}, NOW + 2)
    assert v.power_w == 250
    assert m.compute_status(NOW + 2, 300) == "no_poll"
    m.slave.stats.last_poll = NOW + 2
    assert m.compute_status(NOW + 2, 300) == "partial"   # a is asleep, b produces
    v = m.aggregate({"a": {"power": st("unavailable")}, "b": {"power": st(250, now=NOW + 2)}}, NOW + 310)
    assert v.power_w == 0 and m.compute_status(NOW + 310, 400) == "source_unavailable"


def test_status_contains_sources():
    m = make(ent("a"))
    m.aggregate({"a": {"power": st(300), "energy": st(1, "kWh")}}, NOW)
    s = m.status(NOW, 300)
    assert s["status"] == "no_poll" and s["power_w"] == 300 and s["sources"]["a"]["fresh"] is True
    assert s["phases"]["l1"]["power_w"] == 300
