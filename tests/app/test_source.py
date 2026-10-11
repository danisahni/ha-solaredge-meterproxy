from datetime import datetime, timezone

from semp.source import SourceTracker, parse_state, to_kwh, to_pf, to_watts

NOW = 1_800_000_000.0


def st(state, unit="W", age=1.0):
    ts = datetime.fromtimestamp(NOW - age, timezone.utc).isoformat()
    return {"state": str(state), "attributes": {"unit_of_measurement": unit},
            "last_reported": ts, "last_updated": ts}


def tracker(**kw):
    return SourceTracker({"power": "sensor.p", "energy": "sensor.e"}, phases=1,
                         stale_timeout=kw.pop("stale", 300), **kw)


def test_unit_conversion():
    assert to_watts(parse_state(st(1.2, "kW"))) == 1200
    assert to_kwh(parse_state(st(400338, "Wh"))) == 400.338
    assert to_pf(parse_state(st(98, "%"))) == 0.98
    assert to_pf(parse_state(st(0.97, ""))) == 0.97


def test_normal_update():
    t = tracker()
    v = t.update({"power": parse_state(st(500)), "energy": parse_state(st(400338, "Wh"))}, now=NOW)
    assert v.power_w == 500 and v.energy_kwh == 400.338 and t.fresh
    assert round(v.per_phase[1].current_a, 3) == round(500 / 230, 3)
    assert v.per_phase[1].energy_kwh == 400.338


def test_unavailable_power_goes_to_zero_immediately():
    t = tracker()
    t.update({"power": parse_state(st(500))}, now=NOW)
    v = t.update({"power": parse_state(st("unavailable"))}, now=NOW + 2)
    assert v.power_w == 0 and v.per_phase[1].current_a == 0


def test_frozen_entity_goes_to_zero_after_timeout():
    t = tracker(stale=300)
    t.update({"power": parse_state(st(500))}, now=NOW)
    # entity stays "available" but its last report does not move on
    assert t.update({"power": parse_state(st(500))}, now=NOW + 200).power_w == 500
    v = t.update({"power": parse_state(st(500))}, now=NOW + 302)
    assert v.power_w == 0 and not t.fresh


def test_api_outage_keeps_value_until_stale():
    t = tracker(stale=300)
    t.update({"power": parse_state(st(500))}, now=NOW)
    assert t.update({}, now=NOW + 60).power_w == 500       # HA core restart
    assert t.update({}, now=NOW + 301).power_w == 0        # really gone


def test_energy_is_monotonic_and_survives_unavailable():
    t = tracker(initial_energy_kwh=400.0)
    assert t.update({"energy": parse_state(st(399000, "Wh"))}, now=NOW).energy_kwh == 400.0
    assert t.update({"energy": parse_state(st("unavailable", "Wh"))}, now=NOW).energy_kwh == 400.0
    assert t.update({"energy": parse_state(st(401500, "Wh"))}, now=NOW).energy_kwh == 401.5


# ------------------------------------------------------------------ three-phase
def t3(**ent):
    return SourceTracker(ent, phases=(1, 2, 3), stale_timeout=300)


def test_three_phase_per_phase_entities_sum_to_total():
    t = t3(l1_power="a", l2_power="b", l3_power="c", l1_energy="x", l2_energy="y", l3_energy="z",
           l1_voltage="u1")
    v = t.update({
        "l1_power": parse_state(st(1000)), "l2_power": parse_state(st(1.1, "kW")), "l3_power": parse_state(st(900)),
        "l1_energy": parse_state(st(100, "kWh")), "l2_energy": parse_state(st(110, "kWh")),
        "l3_energy": parse_state(st(90, "kWh")), "l1_voltage": parse_state(st(235, "V")),
    }, now=NOW)
    assert v.power_w == 3000
    assert [v.per_phase[n].power_w for n in (1, 2, 3)] == [1000, 1100, 900]
    assert v.energy_kwh == 300 and v.per_phase[2].energy_kwh == 110
    assert v.per_phase[1].voltage_v == 235 and v.per_phase[2].voltage_v == 230
    assert round(v.per_phase[1].current_a, 3) == round(1000 / 235, 3)


def test_three_phase_totals_only_are_split_evenly():
    t = t3(power="p", energy="e", voltage="u")
    v = t.update({"power": parse_state(st(3000)), "energy": parse_state(st(600, "kWh")),
                  "voltage": parse_state(st(231, "V"))}, now=NOW)
    assert [v.per_phase[n].power_w for n in (1, 2, 3)] == [1000, 1000, 1000]
    assert [v.per_phase[n].energy_kwh for n in (1, 2, 3)] == [200, 200, 200]
    assert all(v.per_phase[n].voltage_v == 231 for n in (1, 2, 3))


def test_three_phase_partial_phases_take_the_remainder():
    t = t3(power="p", l1_power="a")
    v = t.update({"power": parse_state(st(3000)), "l1_power": parse_state(st(1200))}, now=NOW)
    assert [v.per_phase[n].power_w for n in (1, 2, 3)] == [1200, 900, 900]


def test_three_phase_unavailable_and_stale():
    t = t3(l1_power="a", l2_power="b", l3_power="c")
    t.update({k: parse_state(st(500)) for k in ("l1_power", "l2_power", "l3_power")}, now=NOW)
    v = t.update({k: parse_state(st("unavailable")) for k in ("l1_power", "l2_power", "l3_power")}, now=NOW + 2)
    assert v.power_w == 0 and all(v.per_phase[n].power_w == 0 for n in (1, 2, 3))
    t.update({k: parse_state(st(500)) for k in ("l1_power", "l2_power", "l3_power")}, now=NOW)
    v = t.update({}, now=NOW + 400)
    assert v.power_w == 0 and all(v.per_phase[n].current_a == 0 for n in (1, 2, 3))


# ------------------------------------------------------- generic source handling
def test_energy_integrated_from_power_when_no_counter():
    t = SourceTracker({"power": "sensor.p"}, phases=1, stale_timeout=300, initial_energy_kwh=10.0)
    for i in range(0, 3601, 10):          # 1 h at a constant 600 W, readings every 10 s
        t.update({"power": parse_state(st(600, age=-i))}, now=NOW + i)
    assert abs(t.values.energy_kwh - 10.6) < 1e-6


def test_integration_skips_gaps_and_stale_data():
    t = SourceTracker({"power": "sensor.p"}, phases=1, stale_timeout=300)
    t.update({"power": parse_state(st(1000, age=0))}, now=NOW)
    t.update({}, now=NOW + 3600)           # HA down for an hour: nothing integrated
    assert t.values.energy_kwh == 0


def test_negative_power_is_clamped_or_inverted():
    t = tracker()
    assert t.update({"power": parse_state(st(-3.5))}, now=NOW).power_w == 0      # standby draw
    ti = SourceTracker({"power": "sensor.p"}, phases=1, stale_timeout=300, invert_power=True)
    assert ti.update({"power": parse_state(st(-800))}, now=NOW).power_w == 800


def test_mqtt_without_energy_integrates_until_counter_appears():
    t = SourceTracker({}, phases=1, stale_timeout=300)
    t.update({"power": parse_state(st(3600, age=0))}, now=NOW)
    t.update({"power": parse_state(st(3600, age=0))}, now=NOW + 10)
    assert abs(t.values.energy_kwh - 0.01) < 1e-9
    t.update({"power": parse_state(st(3600, age=0)), "energy": parse_state(st(5, "kWh", age=0))}, now=NOW + 20)
    t.update({"power": parse_state(st(3600, age=0))}, now=NOW + 30)
    assert t.values.energy_kwh == 5  # real counter wins, integration stops
