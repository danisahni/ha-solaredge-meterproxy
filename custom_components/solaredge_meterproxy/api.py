"""Client for the MeterProxy app's HTTP API."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp


class MeterProxyError(Exception):
    """App not reachable or unexpected answer."""


class MeterProxyAuthError(MeterProxyError):
    """Token rejected."""


class MeterProxyConfigError(MeterProxyError):
    """Configuration rejected by the app."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class MeterProxyClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: str) -> None:
        self._session = session
        self._base = f"http://{host}:{port}/api"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        try:
            async with asyncio.timeout(10):
                async with self._session.request(method, f"{self._base}{path}", json=json,
                                                 headers=self._headers) as resp:
                    if resp.status == 401:
                        raise MeterProxyAuthError("invalid token")
                    data = await resp.json(content_type=None)
                    if resp.status == 400 and isinstance(data, dict) and "errors" in data:
                        raise MeterProxyConfigError(list(data["errors"]))
                    if resp.status >= 400:
                        raise MeterProxyError(f"HTTP {resp.status}")
                    return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise MeterProxyError(str(err) or type(err).__name__) from err

    async def info(self) -> dict:
        return await self._request("GET", "/info")

    async def status(self) -> dict:
        return await self._request("GET", "/status")

    async def get_config(self) -> dict:
        return await self._request("GET", "/config")

    async def put_config(self, config: dict) -> dict:
        return await self._request("PUT", "/config", json=config)
