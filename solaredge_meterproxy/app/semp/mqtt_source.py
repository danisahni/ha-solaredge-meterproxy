"""MQTT as alternative data source.

The payload is a JSON object with the generic key set of the original
solaredge_meterproxy project (W, kWh, V, A, Hz, var; PF as 0..1 or %), e.g.::

    {"power_active": 812.5, "energy_active": 400.338, "voltage_ln": 233.7,
     "l1_current": 3.5, "frequency": 49.98, "power_factor": 1.0}

Three-phase sources may send ``l1_power_active`` … ``l3_power_active``,
``l1n_voltage`` …, ``l1_current`` …, ``l1_energy_active`` … in addition to or
instead of the totals. A ``null`` value means "unavailable".
"""

from __future__ import annotations

import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

from .source import EntityReading, _parse_ts

_LOG = logging.getLogger("semp.mqtt")

# reading key -> JSON keys in order of preference, unit for conversions
_TOTALS = {
    "power": (("power_active",), "W"),
    "energy": (("energy_active", "import_energy_active"), "kWh"),
    "voltage": (("voltage_ln",), "V"),
    "current": (("current",), "A"),
    "frequency": (("frequency",), "Hz"),
    "power_factor": (("power_factor",), ""),
    "reactive_power": (("power_reactive",), "var"),
}


def _per_phase(n: int) -> dict:
    return {
        f"l{n}_power": ((f"l{n}_power_active",), "W"),
        f"l{n}_voltage": ((f"l{n}n_voltage",), "V"),
        f"l{n}_current": ((f"l{n}_current",), "A"),
        f"l{n}_energy": ((f"l{n}_energy_active", f"l{n}_import_energy_active"), "kWh"),
    }


def payload_to_readings(payload: dict, received_at: float, phases: tuple[int, ...]) -> dict[str, EntityReading]:
    """Map a JSON payload onto the reading keys used by SourceTracker."""
    spec = dict(_TOTALS)
    if len(phases) == 1:
        n = phases[0]
        # single phase: per-phase keys of that phase act as fallback for the totals
        spec["power"] = (("power_active", f"l{n}_power_active"), "W")
        spec["voltage"] = (("voltage_ln", f"l{n}n_voltage"), "V")
        spec["current"] = (("current", f"l{n}_current"), "A")
        spec["energy"] = (("energy_active", "import_energy_active", f"l{n}_energy_active"), "kWh")
    else:
        for n in phases:
            spec.update(_per_phase(n))

    out: dict[str, EntityReading] = {}
    for key, (json_keys, unit) in spec.items():
        for jk in json_keys:
            if jk not in payload:
                continue
            raw = payload[jk]
            try:
                value = None if raw is None else float(raw)
            except (TypeError, ValueError):
                value = None
            out[key] = EntityReading(value, unit, received_at)
            break
    return out


class MqttFeed:
    """Keeps the latest payload of one topic; reconnects on its own."""

    def __init__(self, *, host: str, port: int, topic: str, username: str | None = None,
                 password: str | None = None, phases: tuple[int, ...] = (1,), client_id: str | None = None):
        self.host, self.port, self.topic, self.phases = host, port, topic, phases
        self.connected = False
        self.last_message: float | None = None
        self.messages = 0
        self._payload: dict | None = None
        self._lock = threading.Lock()
        self._warned_bad = 0.0
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or "",
            clean_session=True,
        )
        if username:
            self._client.username_pw_set(username, password or None)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # paho callbacks run in the network thread: never let an exception escape,
    # paho 2.x would otherwise stop the loop and the values would freeze.
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        try:
            if reason_code.is_failure:
                _LOG.error("MQTT connect to %s:%s refused: %s", self.host, self.port, reason_code)
                return
            self.connected = True
            client.subscribe(self.topic, qos=0)
            _LOG.info("MQTT connected to %s:%s, subscribed to %s", self.host, self.port, self.topic)
        except Exception:  # noqa: BLE001
            _LOG.exception("MQTT on_connect failed")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        _LOG.warning("MQTT disconnected (%s) - reconnecting", reason_code)

    def _on_message(self, client, userdata, message):
        try:
            data = json.loads(message.payload.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("payload is not a JSON object")
        except Exception as exc:  # noqa: BLE001
            now = time.time()
            if now - self._warned_bad > 300:
                self._warned_bad = now
                _LOG.warning("Ignoring invalid MQTT payload on %s: %s", message.topic, exc)
            return
        received = time.time()
        # optional source timestamp (epoch seconds or ISO 8601): lets a retained
        # message from a dead publisher be recognised as stale after a restart
        ts = data.get("timestamp")
        if isinstance(ts, (int, float)):
            received = min(float(ts), received)
        elif isinstance(ts, str):
            parsed = _parse_ts(ts)
            if parsed is not None:
                received = min(parsed, received)
        with self._lock:
            self._payload = data
            self.last_message = received
            self.messages += 1

    def handle_payload(self, payload: bytes, topic: str = "test") -> None:
        """Feed a raw payload (used by tests)."""
        msg = mqtt.MQTTMessage(topic=topic.encode())
        msg.payload = payload
        self._on_message(self._client, None, msg)

    def start(self) -> None:
        # connect_async + loop_start: retries until the broker is reachable,
        # also when it is down while the add-on starts
        self._client.connect_async(self.host, self.port, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def readings(self) -> dict[str, EntityReading]:
        with self._lock:
            if self._payload is None:
                return {}
            payload, ts = dict(self._payload), self.last_message
        return payload_to_readings(payload, ts, self.phases)
