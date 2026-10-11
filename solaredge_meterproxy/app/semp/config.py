"""Meter configuration as pushed by the Home Assistant integration.

Persisted in ``/data/meters.json`` so the meters keep running when Home
Assistant (or the integration) is down or broken after an update::

    {"settings": {"stale_timeout": 300, "poll_timeout": 300, "update_interval": 2},
     "meters": [{"id": "m1", "name": "Balcony", "port": "/dev/serial/by-id/...",
                 "baudrate": 9600, "parity": "N", "address": 3, "ct_current": 50,
                 "serial_number": 41613858,
                 "sources": [{"id": "s1", "name": "HMS-800", "type": "entities",
                              "phase": "L1", "invert_power": false,
                              "entities": {"power": "sensor.x", "energy": "sensor.y"}},
                             {"id": "s2", "type": "mqtt", "phase": "3P", "topic": "a/b"}]}]}

Every meter is its own Modbus device (own device ID) towards the inverter;
the sources of one meter are summed.
"""

from __future__ import annotations

import hashlib
import json
import re

BAUDRATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
PARITIES = ("N", "E", "O")
PHASES = ("L1", "L2", "L3", "3P")
TOTAL_KEYS = ("power", "energy", "voltage", "current", "frequency", "power_factor", "reactive_power")
PER_PHASE_KEYS = ("power", "voltage", "current", "energy")
ENTITY_KEYS = TOTAL_KEYS + tuple(f"l{n}_{k}" for n in (1, 2, 3) for k in PER_PHASE_KEYS)
ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

DEFAULT_SETTINGS = {"stale_timeout": 300, "poll_timeout": 300, "update_interval": 2}
EMPTY = {"settings": dict(DEFAULT_SETTINGS), "meters": []}


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_phases(value) -> tuple[int, ...]:
    v = str(value).upper()
    if v in ("3P", "L1L2L3", "3"):
        return (1, 2, 3)
    if v in ("L1", "L2", "L3"):
        return (int(v[-1]),)
    raise ValueError(f"invalid phase {value!r}")


def _int(value, name: str, lo: int, hi: int, errors: list[str]) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        errors.append(f"{name}: {value!r} is not a number")
        return None
    if not lo <= v <= hi:
        errors.append(f"{name}: {v} not in {lo}..{hi}")
        return None
    return v


def normalize(raw: dict) -> dict:
    """Validate and fill defaults. Raises ConfigError with all problems found."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError(["configuration must be a JSON object"])
    settings = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}
    settings = {
        "stale_timeout": _int(settings["stale_timeout"], "stale_timeout", 10, 3600, errors),
        "poll_timeout": _int(settings["poll_timeout"], "poll_timeout", 0, 3600, errors),
        "update_interval": _int(settings["update_interval"], "update_interval", 1, 60, errors),
    }
    meters = []
    seen_ids: set[str] = set()
    seen_addr: dict[tuple[str, int], str] = {}
    port_params: dict[str, tuple[int, str, str]] = {}
    for i, m in enumerate(raw.get("meters") or []):
        where = f"meter {m.get('name') or m.get('id') or i + 1}"
        mid = str(m.get("id", ""))
        if not ID.match(mid):
            errors.append(f"{where}: invalid id {mid!r}")
        elif mid in seen_ids:
            errors.append(f"{where}: duplicate id {mid!r}")
        seen_ids.add(mid)
        port = str(m.get("port") or "")
        if not port.startswith("/dev/"):
            errors.append(f"{where}: serial port missing")
        baud = _int(m.get("baudrate", 9600), f"{where}: baudrate", 1200, 115200, errors)
        if baud is not None and baud not in BAUDRATES:
            errors.append(f"{where}: unsupported baudrate {baud}")
        parity = str(m.get("parity", "N")).upper()
        if parity not in PARITIES:
            errors.append(f"{where}: parity must be N, E or O")
        address = _int(m.get("address"), f"{where}: Modbus address", 1, 247, errors)
        ct = _int(m.get("ct_current", 50), f"{where}: CT current", 1, 6000, errors)
        serial = _int(m.get("serial_number", 10_000_000 + (address or 0)), f"{where}: serial number",
                      0, 2**31 - 1, errors)
        if port and address is not None:
            other = seen_addr.get((port, address))
            if other:
                errors.append(f"{where}: Modbus address {address} on {port} is already used by {other}")
            seen_addr[(port, address)] = m.get("name") or mid
        if port and baud is not None:
            prev = port_params.get(port)
            if prev and prev[:2] != (baud, parity):
                errors.append(f"{where}: {port} is already used with {prev[0]} 8{prev[1]}1 by {prev[2]} "
                              f"- all meters on one RS485 port need the same baud rate and parity")
            port_params.setdefault(port, (baud, parity, m.get("name") or mid))

        sources = []
        src_ids: set[str] = set()
        for j, s in enumerate(m.get("sources") or []):
            swhere = f"{where}, source {s.get('name') or s.get('id') or j + 1}"
            sid = str(s.get("id", ""))
            if not ID.match(sid) or sid in src_ids:
                errors.append(f"{swhere}: invalid or duplicate id {sid!r}")
            src_ids.add(sid)
            phase = str(s.get("phase", "L1")).upper()
            if phase not in PHASES:
                errors.append(f"{swhere}: phase must be one of {', '.join(PHASES)}")
                phase = "L1"
            kind = s.get("type", "entities")
            src = {"id": sid, "name": str(s.get("name") or sid), "type": kind, "phase": phase,
                   "invert_power": bool(s.get("invert_power", False))}
            if kind == "entities":
                ents = {k: v for k, v in (s.get("entities") or {}).items() if v}
                for k, v in ents.items():
                    if k not in ENTITY_KEYS:
                        errors.append(f"{swhere}: unknown value {k!r}")
                    elif not ENTITY_ID.match(str(v)):
                        errors.append(f"{swhere}: {v!r} is not an entity id")
                if phase != "3P":
                    ents = {k: v for k, v in ents.items() if not re.match(r"^l\d_", k)}
                has_power = "power" in ents or (phase == "3P" and all(f"l{n}_power" in ents for n in (1, 2, 3)))
                if not has_power:
                    errors.append(f"{swhere}: a power sensor is required"
                                  + (" (or all three phase power sensors)" if phase == "3P" else ""))
                src["entities"] = ents
            elif kind == "mqtt":
                topic = str(s.get("topic") or "").strip()
                if not topic:
                    errors.append(f"{swhere}: MQTT topic missing")
                src["topic"] = topic
            else:
                errors.append(f"{swhere}: unknown source type {kind!r}")
            sources.append(src)
        if not sources:
            errors.append(f"{where}: at least one source is required")
        meters.append({"id": mid, "name": str(m.get("name") or mid), "port": port, "baudrate": baud,
                       "parity": parity, "address": address, "ct_current": ct, "serial_number": serial,
                       "sources": sources})
    if errors:
        raise ConfigError(errors)
    return {"settings": settings, "meters": meters}


def revision(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


def fingerprint(source: dict) -> str:
    """Identity of a source's data: a changed sensor/topic restarts its energy baseline."""
    key = {k: source.get(k) for k in ("type", "phase", "entities", "topic", "invert_power")}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def port_signature(cfg: dict) -> dict[str, tuple[int, str]]:
    """Serial parameters per port; a change needs the serial servers to be reopened."""
    return {m["port"]: (m["baudrate"], m["parity"]) for m in cfg["meters"]}
