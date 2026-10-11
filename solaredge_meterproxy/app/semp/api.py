"""Small HTTP API for the Home Assistant integration (port 8099, internal network only).

All endpoints except ``/api/health`` need ``Authorization: Bearer <token>``;
the token is created on first start and handed to Home Assistant through
Supervisor discovery (it is also printed in the app log for manual setup).

  GET  /api/health   liveness (Supervisor watchdog)
  GET  /api/info     version, configuration revision, available serial ports
  GET  /api/config   current meter configuration
  PUT  /api/config   replace the meter configuration (validated, persisted)
  GET  /api/status   live status of every meter, source and port
"""

from __future__ import annotations

import hmac
import logging

from aiohttp import web

from . import __version__
from .config import ConfigError
from .engine import Engine, list_serial_ports

_LOG = logging.getLogger("semp.api")
ENGINE = web.AppKey("engine", Engine)
TOKEN = web.AppKey("token", str)


@web.middleware
async def auth(request: web.Request, handler):
    if request.path != "/api/health":
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {request.app[TOKEN]}"
        if not hmac.compare_digest(header.encode(), expected.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": __version__})


async def info(request: web.Request) -> web.Response:
    eng = request.app[ENGINE]
    return web.json_response({"version": __version__, "revision": eng.revision,
                              "serial_ports": list_serial_ports(),
                              "used_ports": sorted(eng.ports)})


async def get_config(request: web.Request) -> web.Response:
    return web.json_response(request.app[ENGINE].get_config())


async def put_config(request: web.Request) -> web.Response:
    try:
        raw = await request.json()
    except ValueError:
        return web.json_response({"errors": ["body is not JSON"]}, status=400)
    try:
        result = await request.app[ENGINE].apply_config(raw)
    except ConfigError as exc:
        return web.json_response({"errors": exc.errors}, status=400)
    return web.json_response(result)


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[ENGINE].status())


def create_app(engine: Engine, token: str) -> web.Application:
    app = web.Application(middlewares=[auth], client_max_size=256 * 1024)
    app[ENGINE] = engine
    app[TOKEN] = token
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/info", info)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_get("/api/status", status)
    return app
