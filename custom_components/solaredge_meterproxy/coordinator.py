"""Polls the app's status and keeps its meter configuration in sync."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MeterProxyClient, MeterProxyConfigError, MeterProxyError
from .const import (
    CONF_ADDRESS,
    CONF_BAUDRATE,
    CONF_CT_CURRENT,
    CONF_PARITY,
    CONF_PORT_PATH,
    CONF_SERIAL_NUMBER,
    CONF_SOURCES,
    DEFAULT_SETTINGS,
    DOMAIN,
    SCAN_INTERVAL_S,
    SUBENTRY_METER,
)

_LOGGER = logging.getLogger(__name__)

type MeterProxyConfigEntry = ConfigEntry[MeterProxyData]


@dataclass
class MeterProxyData:
    client: MeterProxyClient
    coordinator: MeterProxyCoordinator
    info: dict = field(default_factory=dict)


def build_app_config(entry: ConfigEntry) -> dict[str, Any]:
    """Config entry (options + meter subentries) -> app configuration."""
    settings = {k: int(entry.options.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}
    meters = []
    for sub in entry.subentries.values():
        if sub.subentry_type != SUBENTRY_METER:
            continue
        d = sub.data
        meters.append({
            "id": sub.subentry_id,
            "name": sub.title,
            "port": d[CONF_PORT_PATH],
            "address": int(d[CONF_ADDRESS]),
            "baudrate": int(d.get(CONF_BAUDRATE, 9600)),
            "parity": d.get(CONF_PARITY, "N"),
            "ct_current": int(d.get(CONF_CT_CURRENT, 50)),
            "serial_number": int(d.get(CONF_SERIAL_NUMBER, 0)),
            "sources": [
                {k: v for k, v in s.items() if k in ("id", "name", "type", "phase", "invert_power",
                                                   "entities", "topic")}
                for s in d.get(CONF_SOURCES, [])
            ],
        })
    return {"settings": settings, "meters": meters}


class MeterProxyCoordinator(DataUpdateCoordinator[dict]):
    config_entry: MeterProxyConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: MeterProxyClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN,
                         update_interval=timedelta(seconds=SCAN_INTERVAL_S))
        self.client = client
        self.revision: str | None = None

    async def async_push_config(self) -> dict:
        """Send the configuration to the app. Raises MeterProxyError."""
        result = await self.client.put_config(build_app_config(self.config_entry))
        self.revision = result.get("revision")
        if result.get("restart"):
            _LOGGER.info("MeterProxy app restarts to open the changed serial port(s)")
        return result

    async def _async_update_data(self) -> dict:
        try:
            status = await self.client.status()
            # app reinstalled / data lost / restored from an older backup: push again
            if self.revision and status.get("revision") != self.revision and not status.get("restart_pending"):
                _LOGGER.warning("MeterProxy app has a different configuration - sending it again")
                await self.async_push_config()
                status = await self.client.status()
        except MeterProxyConfigError as err:
            raise UpdateFailed(f"Configuration rejected by the app: {err}") from err
        except MeterProxyError as err:
            raise UpdateFailed(f"MeterProxy app not reachable: {err}") from err
        return status
