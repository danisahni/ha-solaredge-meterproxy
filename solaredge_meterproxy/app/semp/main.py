"""Entry point: API for the integration + the meter engine + Supervisor discovery."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import sys
from pathlib import Path

from aiohttp import web

from . import __version__
from .api import create_app
from .engine import Engine
from .source import HaClient

_LOG = logging.getLogger("semp")

DATA_DIR = Path(os.environ.get("SEMP_DATA", "/data"))
HA_API = os.environ.get("SEMP_HA_API", "http://supervisor/core/api")
SUPERVISOR_API = os.environ.get("SEMP_SUPERVISOR_API", "http://supervisor")
API_PORT = int(os.environ.get("SEMP_API_PORT", "8099"))
DISCOVERY_SERVICE = "solaredge_meterproxy"


def setup_logging(level: str) -> None:
    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("pymodbus").setLevel(logging.DEBUG if level.upper() == "DEBUG" else logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def load_token(path: Path) -> str:
    try:
        token = path.read_text().strip()
        if len(token) >= 20:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        path.write_text(token)
        os.chmod(path, 0o600)
    except OSError as exc:
        _LOG.warning("Could not store API token: %s", exc)
    return token


async def announce(ha: HaClient, token: str, stop: asyncio.Event) -> None:
    """Tell Home Assistant about us (Supervisor discovery) -> "New device found"."""
    delay = 5.0
    while not stop.is_set():
        try:
            self_info = (await ha.get_json(f"{SUPERVISOR_API}/addons/self/info")).get("data", {})
            host = self_info.get("hostname") or self_info.get("ip_address")
            await ha.post_json(f"{SUPERVISOR_API}/discovery", {
                "service": DISCOVERY_SERVICE,
                "config": {"host": host, "port": API_PORT, "token": token},
            })
            _LOG.info("Announced to Home Assistant as %s:%d", host, API_PORT)
            return
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("Discovery failed (%s), retrying in %.0f s", exc, delay)
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, 300)


async def run(opts: dict) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    token = load_token(DATA_DIR / "token")
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not supervisor_token:
        _LOG.warning("SUPERVISOR_TOKEN missing - reading HA entities will fail")
    ha = HaClient(HA_API, supervisor_token)
    engine = Engine(data_dir=DATA_DIR, ha=ha, supervisor_api=SUPERVISOR_API)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, engine.stop.set)

    runner = web.AppRunner(create_app(engine, token), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", API_PORT).start()
    _LOG.info("API on port %d. Token for manual setup of the integration: %s", API_PORT, token)
    announcer = asyncio.create_task(announce(ha, token, engine.stop)) if supervisor_token else None

    code = await engine.run()
    if announcer:
        announcer.cancel()
    await runner.cleanup()
    await ha.close()
    return code


def main() -> int:
    try:
        opts = json.loads(Path(os.environ.get("SEMP_OPTIONS", "/data/options.json")).read_text())
    except (OSError, ValueError):
        opts = {}
    setup_logging(str(opts.get("log_level", "info")))
    _LOG.info("Starting ha-solaredge-meterproxy %s", __version__)
    return asyncio.run(run(opts))


if __name__ == "__main__":
    sys.exit(main())
