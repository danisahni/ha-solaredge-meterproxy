"""Sensors per emulated meter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import STATUSES, SUBENTRY_METER
from .coordinator import MeterProxyConfigEntry
from .entity import MeterEntity, SourceEntity


def _ts(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value else None


@dataclass(frozen=True, kw_only=True)
class MeterSensorDescription(SensorEntityDescription):
    value: Callable[[dict], Any]
    attrs: Callable[[dict], dict] | None = None


def _status_attrs(m: dict) -> dict:
    return {
        "serial_port": m.get("port"),
        "modbus_address": m.get("address"),
        "phases": m.get("phases"),
        "sources": {s["name"]: {k: v for k, v in s.items() if k != "name"} for s in m.get("sources", {}).values()},
    }


SENSORS = (
    MeterSensorDescription(key="status", device_class=SensorDeviceClass.ENUM, options=STATUSES,
                           value=lambda m: m.get("status"), attrs=_status_attrs),
    MeterSensorDescription(key="power", device_class=SensorDeviceClass.POWER, state_class=SensorStateClass.MEASUREMENT,
                           native_unit_of_measurement=UnitOfPower.WATT, value=lambda m: m.get("power_w")),
    MeterSensorDescription(key="energy", device_class=SensorDeviceClass.ENERGY,
                           state_class=SensorStateClass.TOTAL_INCREASING,
                           native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2,
                           value=lambda m: m.get("energy_kwh")),
    MeterSensorDescription(key="last_poll", device_class=SensorDeviceClass.TIMESTAMP,
                           value=lambda m: _ts(m.get("last_poll"))),
    MeterSensorDescription(key="polls", state_class=SensorStateClass.TOTAL_INCREASING,
                           entity_category=EntityCategory.DIAGNOSTIC, value=lambda m: m.get("polls")),
)

SOURCE_SENSORS = (
    MeterSensorDescription(key="source_power", device_class=SensorDeviceClass.POWER,
                           state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfPower.WATT,
                           value=lambda s: s.get("power_w")),
    MeterSensorDescription(key="source_energy", device_class=SensorDeviceClass.ENERGY,
                           state_class=SensorStateClass.TOTAL_INCREASING,
                           native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2,
                           value=lambda s: s.get("energy_kwh")),
    MeterSensorDescription(key="source_data_age", device_class=SensorDeviceClass.DURATION,
                           native_unit_of_measurement="s", entity_category=EntityCategory.DIAGNOSTIC,
                           value=lambda s: s.get("data_age_s")),
)


async def async_setup_entry(hass: HomeAssistant, entry: MeterProxyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    for sub in entry.subentries.values():
        if sub.subentry_type == SUBENTRY_METER:
            entities: list[SensorEntity] = [MeterSensor(coordinator, sub, d) for d in SENSORS]
            for src in sub.data.get("sources", []):
                entities += [SourceSensor(coordinator, sub, src, d) for d in SOURCE_SENSORS]
            async_add_entities(entities, config_subentry_id=sub.subentry_id)


class MeterSensor(MeterEntity, SensorEntity):
    entity_description: MeterSensorDescription

    def __init__(self, coordinator, subentry, description: MeterSensorDescription) -> None:
        super().__init__(coordinator, subentry, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        m = self.meter
        return self.entity_description.value(m) if m else None

    @property
    def extra_state_attributes(self) -> dict | None:
        m = self.meter
        if not m or not self.entity_description.attrs:
            return None
        attrs = self.entity_description.attrs(m)
        if self.entity_description.key == "status":
            # requests seen on this RS485 bus per device ID: no traffic at all = wiring/baud rate,
            # traffic but not for our ID = meter not set up on the inverter
            port = ((self.coordinator.data or {}).get("ports") or {}).get(m.get("port")) or {}
            attrs["bus_requests_by_device_id"] = port.get("frames")
            attrs["app_version"] = (self.coordinator.data or {}).get("version")
        return attrs


class SourceSensor(SourceEntity, SensorEntity):
    entity_description: MeterSensorDescription

    def __init__(self, coordinator, subentry, source: dict, description: MeterSensorDescription) -> None:
        super().__init__(coordinator, subentry, source, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        s = self.source
        return self.entity_description.value(s) if s else None
