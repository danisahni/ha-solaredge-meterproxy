"""SolarEdge MeterProxy: third-party inverters as SolarEdge revenue meters.

The integration is the user interface: it holds the meter configuration
(one config subentry per emulated meter), sends it to the MeterProxy app and
shows the status. The app does the actual work on the RS485 bus and keeps
running with its last configuration even when this integration is not loaded.
"""

from __future__ import annotations

import asyncio

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MeterProxyAuthError, MeterProxyClient, MeterProxyConfigError, MeterProxyError
from .const import CONF_TOKEN, DOMAIN
from .coordinator import MeterProxyConfigEntry, MeterProxyCoordinator, MeterProxyData

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: MeterProxyConfigEntry) -> bool:
    client = MeterProxyClient(async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT],
                              entry.data[CONF_TOKEN])
    coordinator = MeterProxyCoordinator(hass, entry, client)
    try:
        info = await client.info()
        result = await coordinator.async_push_config()
        if result.get("restart"):
            await _async_wait_for_app(client)
    except MeterProxyAuthError as err:
        raise ConfigEntryAuthFailed("The MeterProxy app rejected the token") from err
    except MeterProxyConfigError as err:
        raise ConfigEntryError(f"The MeterProxy app rejected the configuration: {err}") from err
    except MeterProxyError as err:
        raise ConfigEntryNotReady(f"MeterProxy app not reachable: {err}") from err
    entry.runtime_data = MeterProxyData(client=client, coordinator=coordinator, info=info)
    _async_remove_stale_devices(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # meters added/changed/removed or options changed -> reload = push to the app
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


def _async_remove_stale_devices(hass: HomeAssistant, entry: MeterProxyConfigEntry) -> None:
    """Remove devices of removed sources (and the app device of pre-release versions)."""
    wanted = set()
    for sub in entry.subentries.values():
        wanted.add(sub.subentry_id)
        wanted.update(f"{sub.subentry_id}_{s['id']}" for s in sub.data.get("sources", []))
    reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(reg, entry.entry_id):
        ids = {i for d, i in device.identifiers if d == DOMAIN}
        if ids and not ids & wanted:
            reg.async_remove_device(device.id)


async def _async_wait_for_app(client: MeterProxyClient) -> None:
    """The app restarts to open new serial ports; give it up to 60 s."""
    await asyncio.sleep(3)
    for _ in range(20):
        try:
            if not (await client.status()).get("restart_pending"):
                return
        except MeterProxyError:
            pass
        await asyncio.sleep(3)


async def _async_reload(hass: HomeAssistant, entry: MeterProxyConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MeterProxyConfigEntry) -> bool:
    # the app keeps running with its stored configuration
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
