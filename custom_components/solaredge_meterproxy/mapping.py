"""Suggest which entities of a device fill which meter role.

Works on plain dicts from the HA registries/states so it can be unit-tested:
``{"entity_id", "name", "device_class", "state_class", "unit"}``.
Names are matched in English and German (the two languages of the users so far);
anything unknown simply scores neutral.
"""

from __future__ import annotations

import re

# role -> (device_class, accepted units)
ROLES: dict[str, tuple[str, set[str]]] = {
    "power": ("power", {"w", "kw", "mw"}),
    "energy": ("energy", {"wh", "kwh", "mwh"}),
    "voltage": ("voltage", {"v"}),
    "current": ("current", {"a", "ma"}),
    "frequency": ("frequency", {"hz"}),
    "power_factor": ("power_factor", {"%", ""}),
    "reactive_power": ("reactive_power", {"var", "kvar"}),
}
SINGLE_ROLES = list(ROLES)
PER_PHASE_ROLES = ("power", "voltage", "current", "energy")

# words that point to the DC/input side or to partial values -> not the AC output
_NEGATIVE = (r"\b(dc|pv|port|string|panel|modul|module|mppt|input|eingang|channel|kanal|ch\d|pv\d"
             r"|battery|batterie|akku)\b")
_AC = (r"\b(ac|grid|netz|output|ausgang|einspeisung|inverter|wechselrichter|onduleur|omvormer|"
       r"inversor|inverter|réseau|reseau|rete|red|net)\b")
_TOTAL = (r"\b(total|gesamt|lifetime|gesamtertrag|summe|totaal|totale|cumul\w*|overall|all time|"
          r"since installation)\b")
_PERIOD = (r"\b(today|heute|daily|day|tag|tages\w*|week|woche|month|monat|year|jahr|yesterday|gestern|"
           r"aujourd'hui|jour|vandaag|dag|hoy|oggi|giorno)\b")
_IMPORT = r"\b(import|bezug|consumption|verbrauch|charge|lade\w*)\b"
_PHASE = {n: rf"\b(l{n}|phase[ _]?{n}|phase[ _]?{'abc'[n - 1]}|p{n})\b" for n in (1, 2, 3)}


def _norm(text: str) -> str:
    """Lower-case and split compound words like 'ac-leistung' / 'tagesertrag'."""
    t = text.lower().replace("_", " ").replace("-", " ").replace(".", " ")
    t = re.sub(r"(tages|gesamt)(ertrag|energie|leistung)", r"\1 \2", t)
    return t


def score(entity: dict, role: str) -> float | None:
    """None = not usable for the role, otherwise higher is better."""
    device_class, units = ROLES[role]
    unit = str(entity.get("unit") or "").lower()
    if entity.get("device_class") != device_class or (units and unit not in units):
        return None
    text = _norm(f"{entity.get('name', '')} {entity.get('entity_id', '')}")
    s = 0.0
    if re.search(_NEGATIVE, text):
        s -= 10
    if re.search(_AC, text):
        s += 3
    if role == "energy":
        if entity.get("state_class") in ("total_increasing", "total"):
            s += 2
        if re.search(_TOTAL, text):
            s += 5
        if re.search(_PERIOD, text):
            s -= 8
        if re.search(_IMPORT, text):
            s -= 4
    if role == "power" and re.search(_IMPORT, text):
        s -= 4
    # prefer entities without a phase marker for the totals
    if any(re.search(p, text) for p in _PHASE.values()):
        s -= 1
    return s


def phase_of(entity: dict) -> int | None:
    text = _norm(f"{entity.get('name', '')} {entity.get('entity_id', '')}")
    hits = [n for n, p in _PHASE.items() if re.search(p, text)]
    return hits[0] if len(hits) == 1 else None


def candidates(entities: list[dict], role: str) -> list[dict]:
    """All entities usable for a role, best first (score attached)."""
    out = []
    for e in entities:
        s = score(e, role)
        if s is not None:
            out.append({**e, "score": s})
    return sorted(out, key=lambda e: (-e["score"], e.get("entity_id", "")))


def suggest(entities: list[dict], three_phase: bool = False) -> dict[str, str]:
    """Best entity per option name, e.g. {'power_entity': 'sensor.x', ...}."""
    result: dict[str, str] = {}
    for role in SINGLE_ROLES:
        best = [c for c in candidates(entities, role) if c["score"] > -5]
        if best:
            result[f"{role}_entity"] = best[0]["entity_id"]
    if three_phase:
        for role in PER_PHASE_ROLES:
            for n in (1, 2, 3):
                for c in candidates(entities, role):
                    if c["score"] > -5 - 1 and phase_of(c) == n:
                        result[f"l{n}_{role}_entity"] = c["entity_id"]
                        break
        # a total that is really a phase value would double count
        for role in PER_PHASE_ROLES:
            total = result.get(f"{role}_entity")
            if total and total in {result.get(f"l{n}_{role}_entity") for n in (1, 2, 3)}:
                del result[f"{role}_entity"]
    return result


def is_device_candidate(entities: list[dict]) -> bool:
    """Offered as source when it has a usable AC power sensor (energy is optional)."""
    return any(c["score"] > -5 for c in candidates(entities, "power"))


def device_rank(entities: list[dict]) -> int:
    """2 = power + lifetime energy, 1 = power only, 0 = unsuitable."""
    if not is_device_candidate(entities):
        return 0
    return 2 if any(c["score"] > -5 for c in candidates(entities, "energy")) else 1
