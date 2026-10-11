"""Devices: one per meter (= config subentry) and, below it, one per source.

The app itself has no device. Source devices are linked to their meter
(``via_device``) and belong to the meter's subentry, so the integration page
lists every meter with its sources underneath.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADDRESS, CONF_PORT_PATH, DOMAIN
from .coordinator import MeterProxyCoordinator


class MeterEntity(CoordinatorEntity[MeterProxyCoordinator]):
    """Entity of one emulated meter (= one config subentry)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MeterProxyCoordinator, subentry: ConfigSubentry, key: str) -> None:
        super().__init__(coordinator)
        self.meter_id = subentry.subentry_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{subentry.subentry_id}_{key}"
        port = str(subentry.data.get(CONF_PORT_PATH, ""))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="MeterProxy",
            model="WattNode WNC-3Y-400-MB (emulated)",
            serial_number=str(subentry.data.get("serial_number", "")),
            sw_version=(coordinator.data or {}).get("version"),
            hw_version=f"{port.rsplit('/', 1)[-1]} · ID {subentry.data.get(CONF_ADDRESS)}",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        # show the app version on the meter device
        version = (self.coordinator.data or {}).get("version")
        if version and self.device_entry and self.device_entry.sw_version != version:
            dr.async_get(self.hass).async_update_device(self.device_entry.id, sw_version=version)
        super()._handle_coordinator_update()

    @property
    def meter(self) -> dict | None:
        return ((self.coordinator.data or {}).get("meters") or {}).get(self.meter_id)

    @property
    def available(self) -> bool:
        return super().available and self.meter is not None


SOURCE_TYPES = {"entities": "Home Assistant", "mqtt": "MQTT"}


def source_identifier(subentry_id: str, source_id: str) -> tuple[str, str]:
    return (DOMAIN, f"{subentry_id}_{source_id}")


class SourceEntity(CoordinatorEntity[MeterProxyCoordinator]):
    """Entity of one source of a meter."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MeterProxyCoordinator, subentry: ConfigSubentry, source: dict, key: str) -> None:
        super().__init__(coordinator)
        self.meter_id = subentry.subentry_id
        self.source_id = source["id"]
        self._attr_translation_key = key
        self._attr_unique_id = f"{subentry.subentry_id}_{source['id']}_{key}"
        what = source.get("topic") if source.get("type") == "mqtt" else (source.get("entities") or {}).get("power")
        self._attr_device_info = DeviceInfo(
            identifiers={source_identifier(subentry.subentry_id, source["id"])},
            name=source.get("name") or source["id"],
            manufacturer="MeterProxy",
            model=f"{source.get('phase', 'L1')} · {SOURCE_TYPES.get(source.get('type'), source.get('type'))}",
            hw_version=what,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # link to the meter device ("connected via"); done here instead of DeviceInfo.via_device,
        # which newer Home Assistant versions deprecate
        reg = dr.async_get(self.hass)
        # look the meter up among our own devices (works on old and new HA versions)
        meter = next((d for d in dr.async_entries_for_config_entry(reg, self.coordinator.config_entry.entry_id)
                      if (DOMAIN, self.meter_id) in d.identifiers), None)
        if meter and self.device_entry and self.device_entry.via_device_id != meter.id:
            reg.async_update_device(self.device_entry.id, via_device_id=meter.id)

    @property
    def source(self) -> dict | None:
        meter = ((self.coordinator.data or {}).get("meters") or {}).get(self.meter_id) or {}
        return (meter.get("sources") or {}).get(self.source_id)

    @property
    def available(self) -> bool:
        return super().available and self.source is not None
