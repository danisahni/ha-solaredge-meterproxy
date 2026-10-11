"""Runs all configured meters: serial ports, source reading, persistence, watchdog.

Exit codes (the Supervisor watchdog restarts the app on a non-zero exit):
  2  no meter on a port was polled for ``poll_timeout`` seconds (max 3 times in a row)
  3  a serial port disappeared
  4  a Modbus server task ended unexpectedly / self-restart failed
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from . import __version__
from .config import EMPTY, ConfigError, normalize, port_signature, revision
from .meter import Meter
from .server import PortServer
from .source import EntityReading, HaClient, parse_state

_LOG = logging.getLogger("semp.engine")

MAX_NO_POLL_RESTARTS = 3
EXIT_NO_POLL, EXIT_SERIAL_GONE, EXIT_SERVER_DIED = 2, 3, 4
STATE_SAVE_INTERVAL = 60.0


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)
    except OSError as exc:
        _LOG.warning("Could not write %s: %s", path, exc)


def list_serial_ports() -> list[dict]:
    """Serial devices, stable /dev/serial/by-id paths first."""
    out, seen = [], set()
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        for p in sorted(by_id.iterdir()):
            target = os.path.realpath(p)
            seen.add(target)
            out.append({"path": str(p), "name": p.name, "device": target})
    for pattern in ("ttyUSB*", "ttyACM*", "ttyAMA*", "ttyXRUSB*"):
        for p in sorted(Path("/dev").glob(pattern)):
            if str(p) not in seen:
                out.append({"path": str(p), "name": p.name, "device": str(p)})
    return out


class Engine:
    def __init__(self, *, data_dir: Path, ha: HaClient, supervisor_api: str):
        self.data_dir = data_dir
        self.config_file = data_dir / "meters.json"
        self.state_file = data_dir / "state.json"
        self.ha = ha
        self.supervisor_api = supervisor_api
        raw = load_json(self.config_file, EMPTY)
        try:
            self.cfg = normalize(raw)
        except ConfigError as exc:
            _LOG.error("Stored configuration invalid, starting without meters: %s", exc)
            self.cfg = normalize(EMPTY)
        self.state = load_json(self.state_file, {})
        self.state.setdefault("meters", {})
        self.state.setdefault("sources", {})
        self.state.setdefault("no_poll_restarts", {})
        self.state["restarts"] = int(self.state.get("restarts", 0)) + 1
        self.started = time.time()
        self.ports: dict[str, PortServer] = {}
        self.port_tasks: dict[str, asyncio.Task] = {}
        self.meters: dict[str, Meter] = {}
        self.stop = asyncio.Event()
        self.exit_code = 0
        self.restart_pending = False
        self._mqtt_broker: dict | None = None
        self._no_poll_logged: set[str] = set()
        self._build()
        self.save_state()

    # ------------------------------------------------------------ properties
    @property
    def settings(self) -> dict:
        return self.cfg["settings"]

    @property
    def revision(self) -> str:
        return revision(self.cfg)

    # ------------------------------------------------------------ building
    def _build(self) -> None:
        for port, (baud, parity) in port_signature(self.cfg).items():
            self.ports[port] = PortServer(port=port, baudrate=baud, parity=parity)
        self._sync_meters()

    def _sync_meters(self) -> None:
        stale = float(self.settings["stale_timeout"])
        wanted = {m["id"]: m for m in self.cfg["meters"]}
        for mid, meter in list(self.meters.items()):
            mc = wanted.get(mid)
            if mc is None or mc["port"] != meter.port or mc["address"] != meter.address:
                self._remember(meter)
                if meter.port in self.ports:
                    self.ports[meter.port].remove(meter.address)
                meter.stop()
                del self.meters[mid]
                if mc is None:
                    self.state["meters"].pop(mid, None)
                    self.state["sources"].pop(mid, None)
                    _LOG.info("Meter %s removed", meter.name)
        for mid, mc in wanted.items():
            if mid in self.meters:
                self.meters[mid].reconfigure(mc, stale_timeout=stale,
                                             source_states=self.state["sources"].get(mid, {}))
                continue
            meter = Meter(mc, stale_timeout=stale, state=self.state["meters"].get(mid),
                          source_states=self.state["sources"].get(mid, {}))
            self.ports[mc["port"]].add(meter.slave)
            self.meters[mid] = meter
            _LOG.info("Meter %s: device ID %d on %s, sources: %s", meter.name, meter.address, meter.port,
                      ", ".join(f"{s.name} ({s.type}, {s.cfg['phase']})" for s in meter.sources))

    def _remember(self, meter: Meter) -> None:
        self.state["meters"][meter.id] = meter.state()
        self.state["sources"][meter.id] = {s.id: s.state() for s in meter.sources}

    def save_state(self) -> None:
        for meter in self.meters.values():
            self._remember(meter)
        write_json(self.state_file, self.state)

    # ------------------------------------------------------------ config API
    def get_config(self) -> dict:
        return {"revision": self.revision, **self.cfg}

    async def apply_config(self, raw: dict) -> dict:
        """Validate, persist and apply. Raises ConfigError."""
        cfg = normalize(raw)
        if cfg == self.cfg:
            return {"revision": self.revision, "changed": False, "restart": False}
        restart = port_signature(cfg) != port_signature(self.cfg)
        self.save_state()
        write_json(self.config_file, cfg)
        self.cfg = cfg
        _LOG.info("New configuration %s: %d meter(s)", self.revision, len(cfg["meters"]))
        if restart:
            # a new port or other serial parameters: reopen everything cleanly
            self.restart_pending = True
            asyncio.get_running_loop().call_later(0.5, lambda: asyncio.ensure_future(self._restart_self()))
        else:
            self._sync_meters()
            await self._start_mqtt_feeds()
            self.save_state()
        return {"revision": self.revision, "changed": True, "restart": restart}

    async def _restart_self(self) -> None:
        _LOG.info("Serial ports changed - restarting the app")
        try:
            await self.ha.post_json(f"{self.supervisor_api}/addons/self/restart", {})
        except Exception as exc:  # noqa: BLE001
            if "disconnected" in str(exc).lower():
                return  # the Supervisor is already stopping us - that is the restart
            _LOG.warning("Self-restart via Supervisor failed (%s) - exiting for a watchdog restart", exc)
            self._exit(EXIT_SERVER_DIED)

    # ------------------------------------------------------------ MQTT
    async def _broker(self) -> dict:
        if self._mqtt_broker is None:
            try:
                data = (await self.ha.get_json(f"{self.supervisor_api}/services/mqtt")).get("data", {})
                self._mqtt_broker = {"host": data.get("host"), "port": data.get("port"),
                                     "username": data.get("username"), "password": data.get("password")}
                _LOG.info("Using MQTT broker from Supervisor: %s:%s", data.get("host"), data.get("port"))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("No MQTT service from Supervisor (%s), using core-mosquitto:1883", exc)
                self._mqtt_broker = {}
        return self._mqtt_broker

    async def _start_mqtt_feeds(self) -> None:
        pending = [(m, s) for m in self.meters.values() for s in m.sources if s.type == "mqtt" and s.feed is None]
        if not pending:
            return
        from .mqtt_source import MqttFeed

        b = await self._broker()
        for meter, src in pending:
            src.feed = MqttFeed(host=b.get("host") or "core-mosquitto", port=int(b.get("port") or 1883),
                                topic=src.cfg["topic"], username=b.get("username"), password=b.get("password"),
                                phases=src.phases, client_id=f"semp-{meter.id}-{src.id}-{os.getpid()}")
            src.feed.start()

    # ------------------------------------------------------------ loops
    async def _read_entities(self) -> dict[str, EntityReading]:
        ids = sorted({e for m in self.meters.values() for s in m.sources if s.type == "entities"
                      for e in s.entities.values()})

        async def one(eid):
            try:
                return eid, parse_state(await self.ha.get_state(eid))
            except Exception as exc:  # noqa: BLE001 - HA restart, network, ...
                _LOG.debug("Reading %s failed: %s", eid, exc)
                return eid, None

        results = await asyncio.gather(*(one(e) for e in ids))
        return {eid: r for eid, r in results if r is not None}

    def readings_for(self, meter: Meter, states: dict[str, EntityReading]) -> dict[str, dict]:
        out = {}
        for src in meter.sources:
            if src.type == "mqtt":
                out[src.id] = src.feed.readings() if src.feed is not None else {}
            else:
                # missing key = HA API unreachable: keep values until stale
                out[src.id] = {k: states[eid] for k, eid in src.entities.items() if eid in states}
        return out

    async def update_once(self, now: float | None = None) -> None:
        states = await self._read_entities()
        now = now or time.time()
        for meter in list(self.meters.values()):
            try:
                meter.aggregate(self.readings_for(meter, states), now)
            except Exception:  # noqa: BLE001 - one broken meter must not stop the others
                _LOG.exception("Updating meter %s failed", meter.name)

    async def update_loop(self) -> None:
        last_saved = time.time()
        while not self.stop.is_set():
            try:
                await self.update_once()
                if time.time() - last_saved > STATE_SAVE_INTERVAL:
                    self.save_state()
                    last_saved = time.time()
            except Exception:  # noqa: BLE001
                _LOG.exception("Update loop error")
            await self._sleep(float(self.settings["update_interval"]))

    def check_ports(self, now: float | None = None) -> int | None:
        """Watchdog decision; returns an exit code or None."""
        now = now or time.time()
        poll_timeout = float(self.settings["poll_timeout"])
        restarts = self.state["no_poll_restarts"]
        for port, server in self.ports.items():
            if not os.path.exists(port):
                _LOG.critical("Serial port %s disappeared - exiting for restart", port)
                return EXIT_SERIAL_GONE
            if not server.slaves:
                continue
            last = server.last_poll
            if last is not None and restarts.get(port):
                restarts[port] = 0
                self._no_poll_logged.discard(port)
                self.save_state()
            if poll_timeout <= 0:
                continue
            since = now - (last or self.started)
            if since <= poll_timeout:
                continue
            n = int(restarts.get(port, 0))
            if n < MAX_NO_POLL_RESTARTS:
                restarts[port] = n + 1
                self.save_state()
                _LOG.critical("No request from the inverter on %s for %.0f s - exiting for restart (%d/%d)",
                              port, since, n + 1, MAX_NO_POLL_RESTARTS)
                return EXIT_NO_POLL
            if port not in self._no_poll_logged:
                self._no_poll_logged.add(port)
                _LOG.error("Still no request on %s after %d restarts - staying up. Check the meter(s) in "
                           "the inverter's commissioning menu (device ID, protocol SolarEdge), "
                           "baud rate/parity and the A/B wiring",
                           port, MAX_NO_POLL_RESTARTS)
        return None

    async def watchdog_loop(self) -> None:
        while not self.stop.is_set():
            await self._sleep(5)
            if self.restart_pending:
                continue
            code = self.check_ports()
            if code is not None:
                return self._exit(code)
            for port, task in self.port_tasks.items():
                if task.done() and not self.stop.is_set():
                    exc = task.exception() if not task.cancelled() else None
                    _LOG.critical("Modbus server on %s stopped: %r - exiting for restart", port, exc)
                    return self._exit(EXIT_SERVER_DIED)

    async def log_loop(self) -> None:
        while not self.stop.is_set():
            await self._sleep(300)
            now = time.time()
            for m in self.meters.values():
                st = m.status(now, float(self.settings["poll_timeout"]))
                _LOG.info("%s: status=%s polls=%d power=%.1f W energy=%.3f kWh", m.name, st["status"],
                          st["polls"], st["power_w"], st["energy_kwh"])

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        now = time.time()
        poll_timeout = float(self.settings["poll_timeout"])
        return {
            "version": __version__,
            "revision": self.revision,
            "uptime_s": round(now - self.started),
            "restarts": self.state.get("restarts"),
            "restart_pending": self.restart_pending,
            "settings": self.settings,
            "ports": {p: {"present": os.path.exists(p), "baudrate": s.baudrate, "parity": s.parity,
                          "running": p in self.port_tasks and not self.port_tasks[p].done(),
                          "frames": {str(k): v for k, v in sorted(s.bus.frames.items())},
                          "last_frame": s.bus.last_frame}
                      for p, s in self.ports.items()},
            "meters": {mid: m.status(now, poll_timeout) for mid, m in self.meters.items()},
        }

    # ------------------------------------------------------------ run
    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _exit(self, code: int) -> None:
        self.exit_code = code
        self.stop.set()

    async def run(self) -> int:
        if not self.meters:
            _LOG.warning("No meters configured yet - add one in Home Assistant: Settings > Devices & services > "
                         "SolarEdge MeterProxy > Add meter")
        await self._start_mqtt_feeds()
        for port, server in self.ports.items():
            self.port_tasks[port] = asyncio.create_task(server.serve(), name=f"modbus {port}")
        tasks = [asyncio.create_task(c) for c in (self.update_loop(), self.watchdog_loop(), self.log_loop())]
        await self.stop.wait()

        self.save_state()
        for t in tasks + list(self.port_tasks.values()):
            t.cancel()
        for server in self.ports.values():
            try:
                await asyncio.wait_for(server.shutdown(), 5)
            except Exception:  # noqa: BLE001
                pass
        for meter in self.meters.values():
            meter.stop()
        _LOG.info("Stopped (exit code %d)", self.exit_code)
        return self.exit_code
