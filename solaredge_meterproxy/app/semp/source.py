"""Read the source inverter's values directly from Home Assistant entities."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import aiohttp

from .registers import MeterValues

_LOG = logging.getLogger("semp.source")

INVALID = {"unknown", "unavailable", "none", ""}

_POWER_FACTORS = {"w": 1.0, "kw": 1000.0, "mw": 1_000_000.0}
_ENERGY_TO_KWH = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class EntityReading:
    value: float | None  # None = unavailable / not numeric
    unit: str
    reported_at: float | None  # unix time of last report by the source integration


def parse_state(state: dict | None) -> EntityReading:
    """Turn an HA REST state object into a numeric reading."""
    if not state:
        return EntityReading(None, "", None)
    raw = str(state.get("state", "")).strip()
    unit = str(state.get("attributes", {}).get("unit_of_measurement") or "")
    ts = max(
        filter(None, (_parse_ts(state.get(k)) for k in ("last_reported", "last_updated", "last_changed"))),
        default=None,
    )
    if raw.lower() in INVALID:
        return EntityReading(None, unit, ts)
    try:
        return EntityReading(float(raw), unit, ts)
    except ValueError:
        return EntityReading(None, unit, ts)


def to_watts(r: EntityReading) -> float | None:
    if r.value is None:
        return None
    return r.value * _POWER_FACTORS.get(r.unit.lower(), 1.0)


def to_kwh(r: EntityReading) -> float | None:
    if r.value is None:
        return None
    factor = _ENERGY_TO_KWH.get(r.unit.lower())
    if factor is None:
        _LOG.warning("Unknown energy unit %r, assuming kWh", r.unit)
        factor = 1.0
    return r.value * factor


def to_pf(r: EntityReading) -> float | None:
    if r.value is None:
        return None
    v = r.value
    if r.unit == "%" or abs(v) > 1.0:
        v = v / 100.0
    return max(-1.0, min(1.0, v))


class HaClient:
    """Minimal Home Assistant REST client via the Supervisor proxy."""

    def __init__(self, base_url: str, token: str, timeout: float = 5.0):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        return self._session

    async def get_state(self, entity_id: str) -> dict | None:
        s = await self._s()
        async with s.get(f"{self._base}/states/{entity_id}") as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            return await resp.json()

    async def set_state(self, entity_id: str, state, attributes: dict) -> None:
        s = await self._s()
        async with s.post(
            f"{self._base}/states/{entity_id}",
            json={"state": state, "attributes": attributes},
        ) as resp:
            resp.raise_for_status()

    async def post_json(self, url: str, payload: dict) -> dict:
        s = await self._s()
        async with s.post(url, json=payload) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {text[:300]}")
            try:
                return await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                return {}

    async def get_json(self, url: str) -> dict:
        s = await self._s()
        async with s.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class SourceTracker:
    """Combine entity readings into MeterValues with fail-safe rules.

    Reading keys: ``power``, ``energy``, ``voltage``, ``current``, ``frequency``,
    ``power_factor``, ``reactive_power`` (totals / single phase) and
    ``l1_power`` … ``l3_energy`` for per-phase values of a three-phase source.

    * Power/current/reactive power drop to 0 when the source is unavailable
      or has not reported for ``stale_timeout`` seconds.
    * Energy counters never decrease; the total survives restarts (persisted).
    * Voltage/frequency/PF keep their last valid value (nominal defaults).
    * Three-phase: missing per-phase values are derived from the totals
      (split evenly) and missing totals from the per-phase values (sum).
    """

    MAX_INTEGRATION_GAP = 60.0  # s; longer gaps (outage, restart) are not integrated

    def __init__(self, entities: dict[str, str | None], phases: tuple[int, ...] | int,
                 stale_timeout: float, initial_energy_kwh: float = 0.0, invert_power: bool = False,
                 integrate_energy: bool | None = None):
        if isinstance(phases, int):
            phases = (phases,)
        self.entities = {k: v for k, v in entities.items() if v}
        self.stale_timeout = stale_timeout
        self.values = MeterValues(phases=tuple(phases), energy_kwh=initial_energy_kwh)
        self.last_fresh: float | None = None  # source report time of last valid power
        self.fresh = False
        self.available = True  # False while the source reports "unavailable" (e.g. asleep at night)
        self._warned_decrease = 0.0
        self.invert_power = invert_power
        # no energy counter configured -> integrate power over time. None = decide on
        # the fly (MQTT): integrate until the payload delivers an energy value once.
        has_energy = any(k == "energy" or k.endswith("_energy") for k in self.entities)
        self.integrate_energy = (not has_energy) if integrate_energy is None and self.entities else integrate_energy
        self._energy_seen = False
        self._last_integration: float | None = None
        # bumped when an MQTT source switches from integrating to a real counter:
        # consumers must take a new baseline instead of counting the jump
        self.energy_epoch = 0

    @property
    def energy_valid(self) -> bool:
        """True once the energy value means something (counter read or integration running)."""
        if self._energy_seen:
            return True
        integrate = self.integrate_energy if self.integrate_energy is not None else True
        return bool(integrate) and self.last_fresh is not None

    def data_age(self, now: float | None = None) -> float | None:
        if self.last_fresh is None:
            return None
        return (now or time.time()) - self.last_fresh

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _split(total: float | None, given: dict[int, float | None], phases) -> dict[int, float]:
        """Fill missing per-phase values: remainder of the total, split evenly."""
        known = {n: v for n, v in given.items() if v is not None}
        missing = [n for n in phases if n not in known]
        out = dict(known)
        if missing:
            rest = (total - sum(known.values())) if total is not None else 0.0
            for n in missing:
                out[n] = rest / len(missing)
        return out

    def _energy(self, now: float, total_kwh: float | None, name: str, last: float) -> float:
        if total_kwh is None:
            return last
        if total_kwh >= last:
            return total_kwh
        if now - self._warned_decrease > 3600:
            self._warned_decrease = now
            _LOG.warning("Energy %s went backwards (%.3f < %.3f kWh), keeping last value",
                         name, total_kwh, last)
        return last

    # ------------------------------------------------------------------ update
    def update(self, readings: dict[str, EntityReading], now: float | None = None) -> MeterValues:
        now = now or time.time()
        v = self.values
        phases = v.phases
        single = len(phases) == 1
        r = readings.get

        def val(key, conv=lambda x: x.value):
            x = r(key)
            return conv(x) if x is not None and x.value is not None else None

        # ---- freshness, based on every power reading that is present
        power_keys = ["power"] + [f"l{n}_power" for n in phases]
        valid_power = [r(k) for k in power_keys if r(k) is not None and r(k).value is not None]
        explicit_unavailable = any(r(k) is not None and r(k).value is None for k in power_keys)
        for x in valid_power:
            reported = min(x.reported_at or now, now)
            self.last_fresh = max(self.last_fresh or 0.0, reported)
        stale = self.last_fresh is None or now - self.last_fresh > self.stale_timeout
        self.fresh = not stale
        if valid_power:
            self.available = True
        elif explicit_unavailable:
            self.available = False

        # ---- power (production meter: optional sign flip, negative = standby -> 0 W)
        def prod(x):
            w = to_watts(x)
            if w is None:
                return None
            return max(0.0, -w if self.invert_power else w)

        total = val("power", prod)
        per = {n: val(f"l{n}_power", prod) for n in phases}
        if single:
            per[phases[0]] = total if total is not None else per[phases[0]]
        if stale or (not valid_power and explicit_unavailable):
            # explicitly unavailable (e.g. micro inverter asleep at night) or stale
            for ph in v.per_phase.values():
                ph.power_w = ph.current_a = ph.reactive_var = 0.0
            v.power_w = v.reactive_var = 0.0
        elif valid_power:
            if total is None:
                total = sum(p for p in per.values() if p is not None)
            for n, p in self._split(total, per, phases).items():
                v.per_phase[n].power_w = p
            v.power_w = total

        # ---- voltage / frequency / power factor: keep last valid value
        u_all = val("voltage")
        pf_all = val("power_factor", to_pf)
        for n in phases:
            ph = v.per_phase[n]
            u = val(f"l{n}_voltage")
            u = u if u is not None else u_all
            if u is not None and u > 0:
                ph.voltage_v = u
            if pf_all is not None and pf_all != 0:
                ph.power_factor = pf_all
        if pf_all is not None and pf_all != 0:
            v.power_factor = pf_all
        f = val("frequency")
        if f is not None and f > 0:
            v.frequency_hz = f

        # ---- current / reactive power (only while data is fresh)
        if not stale and valid_power:
            i_all = val("current")
            q_all = val("reactive_power")
            q_per = self._split(q_all, {n: None for n in phases}, phases) if q_all is not None else {}
            for n in phases:
                ph = v.per_phase[n]
                i = val(f"l{n}_current")
                if i is None and single:
                    i = i_all
                ph.current_a = abs(i) if i is not None else (
                    abs(ph.power_w) / ph.voltage_v if ph.voltage_v > 0 else 0.0)
                if q_per:
                    ph.reactive_var = q_per[n]
            if q_all is not None:
                v.reactive_var = q_all

        # ---- energy: monotonic total and per phase
        e_total = val("energy", to_kwh)
        e_per = {n: val(f"l{n}_energy", to_kwh) for n in phases}
        if e_total is None and not single and all(x is not None for x in e_per.values()):
            e_total = sum(e_per.values())
        if e_total is not None or any(x is not None for x in e_per.values()):
            if not self._energy_seen and self.integrate_energy is None and self._last_integration is not None:
                self.energy_epoch += 1
            self._energy_seen = True
        integrate = self.integrate_energy if self.integrate_energy is not None else not self._energy_seen
        if integrate and e_total is None:
            if self._last_integration is not None and not stale:
                dt = now - self._last_integration
                if 0 < dt <= self.MAX_INTEGRATION_GAP:
                    e_total = v.energy_kwh + v.power_w * dt / 3_600_000
            self._last_integration = now
        v.energy_kwh = self._energy(now, e_total, "total", v.energy_kwh)
        if single:
            v.per_phase[phases[0]].energy_kwh = v.energy_kwh
        else:
            known = {n: x for n, x in e_per.items() if x is not None}
            for n in phases:
                ph = v.per_phase[n]
                target = known.get(n)
                if target is None:  # no per-phase counter: even share of the rest
                    rest = v.energy_kwh - sum(known.values())
                    target = max(rest, 0.0) / (len(phases) - len(known))
                ph.energy_kwh = self._energy(now, target, f"L{n}", ph.energy_kwh)
        return v

    async def poll(self, client: HaClient) -> MeterValues:
        readings: dict[str, EntityReading] = {}
        for key, entity_id in self.entities.items():
            try:
                readings[key] = parse_state(await client.get_state(entity_id))
            except Exception as exc:  # noqa: BLE001 - HA restart, network, ...
                _LOG.debug("Reading %s failed: %s", entity_id, exc)
        if not readings and self.entities:
            # HA API unreachable (e.g. core restart): only the stale rule applies
            return self.update({})
        return self.update(readings)
