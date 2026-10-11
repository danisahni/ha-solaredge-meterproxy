"""One emulated meter: several sources summed into one WattNode register image."""

from __future__ import annotations

import logging
import time

from .config import fingerprint, parse_phases
from .registers import MeterValues
from .server import MeterSlave
from .source import EntityReading, SourceTracker

_LOG = logging.getLogger("semp.meter")


class Source:
    """One data source (HA entities or an MQTT topic) of a meter."""

    def __init__(self, cfg: dict, *, stale_timeout: float, state: dict | None):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name") or cfg["id"]
        self.type = cfg["type"]
        self.phases = parse_phases(cfg["phase"])
        self.fp = fingerprint(cfg)
        state = state if state and state.get("fp") == self.fp else {}
        self.tracker = SourceTracker(
            dict(cfg.get("entities") or {}) if self.type == "entities" else {},
            phases=self.phases,
            stale_timeout=stale_timeout,
            initial_energy_kwh=float(state.get("energy", 0.0)),
            invert_power=bool(cfg.get("invert_power")),
        )
        for n, e in (state.get("phases") or {}).items():
            if int(n) in self.tracker.values.per_phase:
                self.tracker.values.per_phase[int(n)].energy_kwh = float(e)
        # last per-phase energy that was added to the meter (None = no baseline yet)
        self.counted: dict[int, float] | None = (
            {int(n): float(e) for n, e in state["counted"].items()} if state.get("counted") else None)
        self.epoch = self.tracker.energy_epoch
        self.feed = None  # MqttFeed for type == "mqtt"

    @property
    def entities(self) -> dict[str, str]:
        return self.tracker.entities

    def update(self, readings: dict[str, EntityReading], now: float) -> MeterValues:
        return self.tracker.update(readings, now=now)

    def state(self) -> dict:
        v = self.tracker.values
        return {"fp": self.fp, "energy": v.energy_kwh,
                "phases": {str(n): v.per_phase[n].energy_kwh for n in self.phases},
                "counted": {str(n): e for n, e in self.counted.items()} if self.counted is not None else None}

    def status(self, now: float) -> dict:
        v = self.tracker.values
        age = self.tracker.data_age(now)
        out = {"name": self.name, "type": self.type, "phase": self.cfg["phase"],
               "fresh": self.tracker.fresh and self.tracker.available,
               "power_w": round(v.power_w, 1), "energy_kwh": round(v.energy_kwh, 3),
               "data_age_s": round(age, 1) if age is not None else None}
        if self.feed is not None:
            out.update(mqtt_connected=self.feed.connected, mqtt_messages=self.feed.messages)
        return out


class Meter:
    """Sum of its sources, reported under one Modbus device ID.

    Power, current and reactive power are summed per phase, voltage, frequency
    and power factor averaged. The meter's energy counters only grow by the
    *increase* of each source's counter: adding, removing or swapping a source
    never makes the inverter see a jump or a counter going backwards. A brand
    new meter adopts its sources' lifetime counters once.
    """

    def __init__(self, cfg: dict, *, stale_timeout: float, state: dict | None, source_states: dict):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.port = cfg["port"]
        self.address = cfg["address"]
        self.slave = MeterSlave(address=cfg["address"], serial_number=cfg["serial_number"],
                                ct_current=cfg["ct_current"])
        self.sources: list[Source] = []
        # never reported before -> take over the sources' lifetime counters once
        self.adopt = bool((state or {}).get("adopt", True))
        self.energy: dict[int, float] = {int(n): float(e) for n, e in (state or {}).get("energy", {}).items()}
        self.set_sources(cfg["sources"], stale_timeout=stale_timeout, source_states=source_states)
        self.values = self._empty_values()
        self.slave.publish(self.values)

    # ------------------------------------------------------------ config
    def set_sources(self, sources: list[dict], *, stale_timeout: float, source_states: dict) -> None:
        old = {s.id: s for s in self.sources}
        new = []
        for cfg in sources:
            prev = old.pop(cfg["id"], None)
            if prev is not None and prev.fp == fingerprint(cfg):
                prev.cfg, prev.name = cfg, cfg.get("name") or cfg["id"]
                prev.tracker.stale_timeout = stale_timeout
                new.append(prev)
            else:
                if prev is not None:
                    self._stop_feed(prev)
                new.append(Source(cfg, stale_timeout=stale_timeout, state=source_states.get(cfg["id"])))
        for gone in old.values():
            self._stop_feed(gone)
        self.sources = new
        self.phases = tuple(sorted({n for s in new for n in s.phases})) or (1,)
        # phase changed (e.g. L1 -> L2): move the energy, the total must never decrease
        total = sum(self.energy.values())
        energy = {n: self.energy.get(n, 0.0) for n in self.phases}
        missing = total - sum(energy.values())
        if missing > 0:
            for n in energy:
                energy[n] += missing / len(energy)
        self.energy = energy

    def reconfigure(self, cfg: dict, *, stale_timeout: float, source_states: dict) -> None:
        self.cfg, self.name = cfg, cfg["name"]
        self.slave.configure(serial_number=cfg["serial_number"], ct_current=cfg["ct_current"])
        self.set_sources(cfg["sources"], stale_timeout=stale_timeout, source_states=source_states)

    @staticmethod
    def _stop_feed(src: Source) -> None:
        if src.feed is not None:
            try:
                src.feed.stop()
            except Exception:  # noqa: BLE001
                pass
            src.feed = None

    def stop(self) -> None:
        for s in self.sources:
            self._stop_feed(s)

    # ------------------------------------------------------------ values
    def _empty_values(self) -> MeterValues:
        v = MeterValues(phases=self.phases)
        for n in self.phases:
            v.per_phase[n].energy_kwh = self.energy.get(n, 0.0)
        v.energy_kwh = sum(v.per_phase[n].energy_kwh for n in self.phases)
        return v

    def _count_energy(self, src: Source) -> None:
        t = src.tracker
        if not t.energy_valid:
            return
        current = {n: t.values.per_phase[n].energy_kwh for n in src.phases}
        if src.epoch != t.energy_epoch:  # integration replaced by a real counter: new baseline
            src.epoch, src.counted = t.energy_epoch, current
            return
        if src.counted is None:
            if self.adopt:
                for n, e in current.items():
                    self.energy[n] = self.energy.get(n, 0.0) + e
            src.counted = current
            return
        for n, e in current.items():
            delta = e - src.counted.get(n, e)
            if delta > 0:
                self.energy[n] = self.energy.get(n, 0.0) + delta
            src.counted[n] = max(e, src.counted.get(n, e))

    def aggregate(self, readings: dict[str, dict[str, EntityReading]], now: float | None = None) -> MeterValues:
        """Update all sources and build the meter values. ``readings`` per source id."""
        now = now or time.time()
        v = MeterValues(phases=self.phases, frequency_hz=self.values.frequency_hz)
        pf_weight = {n: 0.0 for n in self.phases}
        pf_sum = {n: 0.0 for n in self.phases}
        volt = {n: [] for n in self.phases}
        freqs = []
        for src in self.sources:
            sv = src.update(readings.get(src.id, {}), now)
            self._count_energy(src)
            for n in src.phases:
                p, ph = sv.per_phase[n], v.per_phase[n]
                ph.power_w += p.power_w
                ph.current_a += p.current_a
                ph.reactive_var += p.reactive_var
                volt[n].append(p.voltage_v)
                pf_sum[n] += p.power_factor * max(p.power_w, 0.001)
                pf_weight[n] += max(p.power_w, 0.001)
            if src.tracker.fresh:
                freqs.append(sv.frequency_hz)
        for n in self.phases:
            ph = v.per_phase[n]
            if volt[n]:
                ph.voltage_v = sum(volt[n]) / len(volt[n])
            ph.power_factor = pf_sum[n] / pf_weight[n] if pf_weight[n] else 1.0
            ph.energy_kwh = self.energy.get(n, 0.0)
        v.power_w = sum(v.per_phase[n].power_w for n in self.phases)
        v.reactive_var = sum(v.per_phase[n].reactive_var for n in self.phases)
        total_w = sum(max(v.per_phase[n].power_w, 0.001) for n in self.phases)
        v.power_factor = sum(v.per_phase[n].power_factor * max(v.per_phase[n].power_w, 0.001)
                             for n in self.phases) / total_w
        if freqs:
            v.frequency_hz = sum(freqs) / len(freqs)
        v.energy_kwh = sum(self.energy.get(n, 0.0) for n in self.phases)
        if self.sources and all(s.counted is not None for s in self.sources):
            self.adopt = False
        self.values = v
        self.slave.publish(v)
        return v

    # ------------------------------------------------------------ status
    def compute_status(self, now: float, poll_timeout: float) -> str:
        last = self.slave.stats.last_poll
        if last is None or now - last > (poll_timeout or 300):
            return "no_poll"
        fresh = [s.tracker.fresh and s.tracker.available for s in self.sources]
        if not any(fresh):
            return "source_unavailable"
        if not all(fresh):
            return "partial"
        return "ok"

    def status(self, now: float, poll_timeout: float) -> dict:
        st = self.slave.stats
        v = self.values
        return {
            "name": self.name,
            "status": self.compute_status(now, poll_timeout),
            "port": self.port,
            "address": self.address,
            "polls": st.count,
            "last_poll": st.last_poll,
            "power_w": round(v.power_w, 1),
            "energy_kwh": round(v.energy_kwh, 3),
            "phases": {f"l{n}": {"power_w": round(v.per_phase[n].power_w, 1),
                                 "voltage_v": round(v.per_phase[n].voltage_v, 1),
                                 "current_a": round(v.per_phase[n].current_a, 2),
                                 "energy_kwh": round(v.per_phase[n].energy_kwh, 3)} for n in self.phases},
            "sources": {s.id: s.status(now) for s in self.sources},
        }

    def state(self) -> dict:
        return {"energy": {str(n): e for n, e in self.energy.items()}, "adopt": self.adopt}
