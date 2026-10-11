"""Binary sensors: meter polled by the inverter, source delivers current data."""

from __future__ import annotations

import time

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_POLL_TIMEOUT, DEFAULT_SETTINGS, SUBENTRY_METER
from .coordinator import MeterProxyConfigEntry
from .entity import MeterEntity, SourceEntity


async def async_setup_entry(hass: HomeAssistant, entry: MeterProxyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    for sub in entry.subentries.values():
        if sub.subentry_type == SUBENTRY_METER:
            async_add_entities([InverterPolling(coordinator, sub, "polled"),
                                *(SourceFresh(coordinator, sub, src, "source_fresh")
                                  for src in sub.data.get("sources", []))],
                               config_subentry_id=sub.subentry_id)


class InverterPolling(MeterEntity, BinarySensorEntity):
    """On while the SolarEdge inverter reads this meter."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool | None:
        m = self.meter
        if not m:
            return None
        timeout = float(self.coordinator.config_entry.options.get(CONF_POLL_TIMEOUT,
                                                                  DEFAULT_SETTINGS[CONF_POLL_TIMEOUT])) or 300
        last = m.get("last_poll")
        return bool(last) and time.time() - last <= timeout


class SourceFresh(SourceEntity, BinarySensorEntity):
    """On while the source delivers current values (off = counted as 0 W)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool | None:
        s = self.source
        return bool(s.get("fresh")) if s else None

    @property
    def extra_state_attributes(self) -> dict | None:
        s = self.source
        if not s or "mqtt_connected" not in s:
            return None
        return {"mqtt_connected": s.get("mqtt_connected"), "mqtt_messages": s.get("mqtt_messages")}
