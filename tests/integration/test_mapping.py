import json
from pathlib import Path

from custom_components.solaredge_meterproxy.mapping import (
    candidates,
    device_rank,
    is_device_candidate,
    phase_of,
    suggest,
)

HMS = json.loads((Path(__file__).parent / "fixtures" / "hoymiles_hms800.json").read_text())
P = "sensor.aussenbereich_hms_800_2wt_"


def test_hoymiles_hms800_single_phase():
    assert suggest(HMS) == {
        "power_entity": P + "ac_leistung",
        "energy_entity": P + "gesamtertrag",
        "voltage_entity": P + "netzspannung",
        "current_entity": P + "ac_strom",
        "frequency_entity": P + "netzfrequenz",
        "power_factor_entity": P + "leistungsfaktor",
        "reactive_power_entity": P + "ac_blindleistung",
    }
    assert is_device_candidate(HMS)


def test_candidates_ranked_ac_first():
    c = [e["entity_id"] for e in candidates(HMS, "energy")]
    assert c[0] == P + "gesamtertrag"
    assert P + "ac_tagesertrag" in c  # still offered for manual choice
    assert P + "wechselrichter_temperatur" not in c


def _e(eid, name, dc, unit, sc="measurement"):
    return {"entity_id": eid, "name": name, "device_class": dc, "unit": unit, "state_class": sc}


def test_english_three_phase_inverter():
    ents = [
        _e("sensor.inv_ac_power", "AC Power", "power", "W"),
        _e("sensor.inv_pv_power", "PV Power", "power", "W"),
        *[_e(f"sensor.inv_l{n}_power", f"L{n} Power", "power", "W") for n in (1, 2, 3)],
        *[_e(f"sensor.inv_l{n}_voltage", f"Phase {n} Voltage", "voltage", "V") for n in (1, 2, 3)],
        _e("sensor.inv_energy_today", "Energy Today", "energy", "kWh", "total_increasing"),
        _e("sensor.inv_total_energy", "Total Energy", "energy", "kWh", "total_increasing"),
    ]
    s = suggest(ents, three_phase=True)
    assert s["power_entity"] == "sensor.inv_ac_power"
    assert s["energy_entity"] == "sensor.inv_total_energy"
    assert [s[f"l{n}_power_entity"] for n in (1, 2, 3)] == [f"sensor.inv_l{n}_power" for n in (1, 2, 3)]
    assert s["l2_voltage_entity"] == "sensor.inv_l2_voltage"
    assert "voltage_entity" not in s  # only phase voltages exist -> no double use


def test_phase_detection():
    assert phase_of({"name": "Phase B Current"}) == 2
    assert phase_of({"name": "AC Power"}) is None


def test_device_ranking_is_generic():
    assert device_rank(HMS) == 2  # power + lifetime energy
    assert device_rank([_e("sensor.plug_power", "Power", "power", "W")]) == 1  # e.g. a smart plug measuring a BKW
    assert device_rank([_e("sensor.t", "Temperature", "temperature", "°C")]) == 0
    assert device_rank([_e("sensor.x_pv1_power", "PV1 Power", "power", "W")]) == 0  # DC side only


def test_other_languages_and_units():
    ents = [
        _e("sensor.onduleur_puissance_pv1", "Puissance PV1", "power", "kW"),
        _e("sensor.onduleur_puissance_reseau", "Puissance réseau", "power", "kW"),
        _e("sensor.omvormer_energie_vandaag", "Energie vandaag", "energy", "kWh", "total_increasing"),
        _e("sensor.omvormer_energie_totaal", "Energie totaal", "energy", "kWh", "total_increasing"),
    ]
    s = suggest(ents)
    assert s["power_entity"] == "sensor.onduleur_puissance_reseau"
    assert s["energy_entity"] == "sensor.omvormer_energie_totaal"
