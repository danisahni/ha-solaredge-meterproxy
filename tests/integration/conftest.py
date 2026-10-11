"""Fixtures for the Home Assistant integration tests (run with the HA test venv)."""

import copy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from custom_components.solaredge_meterproxy.api import (  # noqa: E402
    MeterProxyAuthError,
    MeterProxyConfigError,
    MeterProxyError,
)

TOKEN = "s3cr3t-token-for-tests-0123456789"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeApp:
    """In-memory stand-in for the app's HTTP API."""

    def __init__(self):
        self.reachable = True
        self.token = TOKEN
        self.config = {"settings": {}, "meters": []}
        self.revision = "r0"
        self.pushes = []
        self.reject = None
        self.ports = [{"path": "/dev/serial/by-id/usb-FTDI_RS485-if00", "name": "usb-FTDI_RS485-if00",
                       "device": "/dev/ttyUSB0"}]

    def client(self, session, host, port, token):
        app = self

        class Client:
            def _check(self):
                if not app.reachable:
                    raise MeterProxyError("down")
                if token != app.token:
                    raise MeterProxyAuthError("invalid token")

            async def info(self):
                self._check()
                return {"version": "0.1.0", "revision": app.revision, "serial_ports": app.ports}

            async def put_config(self, cfg):
                self._check()
                if app.reject:
                    raise MeterProxyConfigError(app.reject)
                restart = cfg != app.config
                app.config = copy.deepcopy(cfg)
                app.revision = f"r{len(app.pushes) + 1}"
                app.pushes.append(copy.deepcopy(cfg))
                return {"revision": app.revision, "changed": restart, "restart": False}

            async def status(self):
                self._check()
                meters = {m["id"]: {"name": m["name"], "status": "ok", "port": m["port"], "address": m["address"],
                                    "polls": 42, "last_poll": 1_800_000_000.0, "power_w": 812.5,
                                    "energy_kwh": 400.338, "phases": {"l1": {"power_w": 812.5}},
                                    "sources": {s["id"]: {"name": s["name"], "fresh": True, "power_w": 812.5}
                                                for s in m["sources"]}}
                          for m in app.config["meters"]}
                return {"version": "0.1.0", "revision": app.revision, "uptime_s": 10, "restarts": 1,
                        "restart_pending": False, "meters": meters,
                        "ports": {m["port"]: {"frames": {"1": 5, "3": 7}} for m in app.config["meters"]}}

        return Client()


@pytest.fixture
def fake_app():
    app = FakeApp()
    with patch("custom_components.solaredge_meterproxy.MeterProxyClient", side_effect=app.client), \
         patch("custom_components.solaredge_meterproxy.config_flow.MeterProxyClient", side_effect=app.client):
        yield app
