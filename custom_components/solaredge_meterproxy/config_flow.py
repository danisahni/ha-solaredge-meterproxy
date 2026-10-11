"""Config flow: connect to the app, then add meters as subentries."""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector as sel
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from . import mapping
from .api import MeterProxyAuthError, MeterProxyClient, MeterProxyError
from .const import (
    BAUDRATES,
    CONF_ADDRESS,
    CONF_BAUDRATE,
    CONF_CT_CURRENT,
    CONF_PARITY,
    CONF_POLL_TIMEOUT,
    CONF_PORT_PATH,
    CONF_SERIAL_NUMBER,
    CONF_SOURCES,
    CONF_STALE_TIMEOUT,
    CONF_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SETTINGS,
    DEVICE_CLASS,
    DOMAIN,
    PARITIES,
    PHASE_KEYS,
    PHASES,
    SUBENTRY_METER,
    TOTAL_KEYS,
)

_LOGGER = logging.getLogger(__name__)


def _unique_id(token: str) -> str:
    """Stable per app installation, identical for discovery and manual setup."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


async def _validate(hass, host: str, port: int, token: str) -> dict:
    return await MeterProxyClient(async_get_clientsession(hass), host, port, token).info()


# --------------------------------------------------------------------------- main flow
class MeterProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._conn: dict[str, Any] = {}
        self._addon_name = "SolarEdge MeterProxy"

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MeterProxyOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_METER: MeterSubentryFlow}

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        cfg = discovery_info.config
        self._conn = {CONF_HOST: cfg["host"], CONF_PORT: int(cfg.get("port", DEFAULT_PORT)),
                      CONF_TOKEN: cfg["token"]}
        self._addon_name = discovery_info.name or self._addon_name
        await self.async_set_unique_id(_unique_id(cfg["token"]))
        self._abort_if_unique_id_configured(updates=self._conn)
        self.context["title_placeholders"] = {"name": self._addon_name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _validate(self.hass, self._conn[CONF_HOST], self._conn[CONF_PORT], self._conn[CONF_TOKEN])
            except MeterProxyError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="SolarEdge MeterProxy", data=self._conn)
        return self.async_show_form(step_id="hassio_confirm", errors=errors,
                                    description_placeholders={"addon": self._addon_name})

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _validate(self.hass, user_input[CONF_HOST], int(user_input[CONF_PORT]), user_input[CONF_TOKEN])
            except MeterProxyAuthError:
                errors[CONF_TOKEN] = "invalid_auth"
            except MeterProxyError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(_unique_id(user_input[CONF_TOKEN]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="SolarEdge MeterProxy",
                                               data={**user_input, CONF_PORT: int(user_input[CONF_PORT])})
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=DEFAULT_HOST): sel.TextSelector(),
            vol.Required(CONF_PORT, default=DEFAULT_PORT): sel.NumberSelector(
                sel.NumberSelectorConfig(min=1, max=65535, mode=sel.NumberSelectorMode.BOX)),
            vol.Required(CONF_TOKEN): sel.TextSelector(sel.TextSelectorConfig(type=sel.TextSelectorType.PASSWORD)),
        })
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(
            schema, user_input or {}), errors=errors)

    async def async_step_reauth(self, entry_data: dict) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _validate(self.hass, entry.data[CONF_HOST], entry.data[CONF_PORT], user_input[CONF_TOKEN])
            except MeterProxyAuthError:
                errors[CONF_TOKEN] = "invalid_auth"
            except MeterProxyError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input,
                                                          unique_id=_unique_id(user_input[CONF_TOKEN]))
        return self.async_show_form(step_id="reauth_confirm", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_TOKEN): sel.TextSelector(sel.TextSelectorConfig(type=sel.TextSelectorType.PASSWORD)),
        }))


class MeterProxyOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={k: int(v) for k, v in user_input.items()})

        def num(lo, hi):
            return sel.NumberSelector(sel.NumberSelectorConfig(min=lo, max=hi, mode=sel.NumberSelectorMode.BOX,
                                                               unit_of_measurement="s"))

        schema = vol.Schema({
            vol.Required(CONF_STALE_TIMEOUT): num(10, 3600),
            vol.Required(CONF_POLL_TIMEOUT): num(0, 3600),
            vol.Required(CONF_UPDATE_INTERVAL): num(1, 60),
        })
        return self.async_show_form(step_id="init", data_schema=self.add_suggested_values_to_schema(
            schema, {**DEFAULT_SETTINGS, **self.config_entry.options}))


# --------------------------------------------------------------------------- meter subentry
def _entity_sel(key: str) -> sel.EntitySelector:
    role = key.split("_", 1)[1] if key[:1] == "l" and key[1:2].isdigit() else key
    return sel.EntitySelector(sel.EntitySelectorConfig(domain="sensor", device_class=DEVICE_CLASS[role]))


def _phase_sel() -> sel.SelectSelector:
    return sel.SelectSelector(sel.SelectSelectorConfig(options=PHASES, translation_key="phase",
                                                       mode=sel.SelectSelectorMode.LIST))


class MeterSubentryFlow(ConfigSubentryFlow):
    """Add or change one emulated meter and its sources."""

    def __init__(self) -> None:
        self._meter: dict[str, Any] = {}
        self._sources: list[dict[str, Any]] = []
        self._editing: int | None = None     # index into _sources
        self._draft: dict[str, Any] = {}     # source being added/edited
        self._ports: list[dict] = []

    # ------------------------------------------------------------ entry points
    async def async_step_user(self, user_input: dict | None = None) -> SubentryFlowResult:
        if not self._meter:
            used = {(m[CONF_PORT_PATH], int(m[CONF_ADDRESS])) for m in self._other_meters()}
            self._meter = {CONF_NAME: "", CONF_ADDRESS: next(a for a in range(3, 248) if not any(
                u[1] == a for u in used)), CONF_BAUDRATE: "9600", CONF_PARITY: "N", CONF_CT_CURRENT: 50,
                CONF_SERIAL_NUMBER: 10_000_000 + secrets.randbelow(89_999_999)}
        return await self.async_step_meter()

    async def async_step_reconfigure(self, user_input: dict | None = None) -> SubentryFlowResult:
        sub = self._get_reconfigure_subentry()
        d = sub.data
        self._meter = {CONF_NAME: sub.title, CONF_PORT_PATH: d[CONF_PORT_PATH], CONF_ADDRESS: d[CONF_ADDRESS],
                       CONF_BAUDRATE: str(d.get(CONF_BAUDRATE, 9600)), CONF_PARITY: d.get(CONF_PARITY, "N"),
                       CONF_CT_CURRENT: d.get(CONF_CT_CURRENT, 50), CONF_SERIAL_NUMBER: d.get(CONF_SERIAL_NUMBER)}
        self._sources = [dict(s) for s in d.get(CONF_SOURCES, [])]
        return await self.async_step_meter()

    # ------------------------------------------------------------ helpers
    @property
    def _reconfiguring(self) -> bool:
        return self.source == SOURCE_RECONFIGURE

    def _other_meters(self) -> list[dict]:
        own = self._get_reconfigure_subentry().subentry_id if self._reconfiguring else None
        return [{**s.data, CONF_NAME: s.title} for s in self._get_entry().subentries.values()
                if s.subentry_type == SUBENTRY_METER and s.subentry_id != own]

    async def _port_options(self) -> list[sel.SelectOptionDict]:
        options: dict[str, str] = {}
        try:
            entry = self._get_entry()
            info = await entry.runtime_data.client.info()
            for p in info.get("serial_ports", []):
                by_id = p["path"] != p.get("device")
                options[p["path"]] = f"{p['name']} ({p['device']})" if by_id else p["path"]
        except (MeterProxyError, AttributeError) as err:
            _LOGGER.debug("Could not list serial ports: %s", err)
        for m in self._other_meters():
            options.setdefault(m[CONF_PORT_PATH], m[CONF_PORT_PATH])
        if self._meter.get(CONF_PORT_PATH):
            options.setdefault(self._meter[CONF_PORT_PATH], self._meter[CONF_PORT_PATH])
        return [sel.SelectOptionDict(value=k, label=v) for k, v in options.items()]

    def _sources_text(self) -> str:
        if not self._sources:
            return "–"
        lines = []
        for s in self._sources:
            what = s.get("topic") if s["type"] == "mqtt" else (
                s["entities"].get("power") or ", ".join(s["entities"].get(f"l{n}_power", "") for n in (1, 2, 3)))
            lines.append(f"- **{s['name']}** ({s['phase']}): {what}")
        return "\n".join(lines)

    # ------------------------------------------------------------ step: meter
    async def async_step_meter(self, user_input: dict | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"other": ""}
        if user_input is not None:
            adv = user_input.pop("advanced", {})
            meter = {**self._meter, **user_input, **adv}
            meter[CONF_ADDRESS] = int(meter[CONF_ADDRESS])
            meter[CONF_CT_CURRENT] = int(meter[CONF_CT_CURRENT])
            meter[CONF_SERIAL_NUMBER] = int(meter[CONF_SERIAL_NUMBER])
            for other in self._other_meters():
                if other[CONF_PORT_PATH] != meter[CONF_PORT_PATH]:
                    continue
                placeholders["other"] = other[CONF_NAME]
                if int(other[CONF_ADDRESS]) == meter[CONF_ADDRESS]:
                    errors[CONF_ADDRESS] = "address_in_use"
                elif (str(other.get(CONF_BAUDRATE, 9600)), other.get(CONF_PARITY, "N")) != (
                        str(meter[CONF_BAUDRATE]), meter[CONF_PARITY]):
                    errors["base"] = "port_params"
            if not errors:
                self._meter = meter
                return await self.async_step_sources()

        m = self._meter
        schema = vol.Schema({
            vol.Required(CONF_NAME): sel.TextSelector(),
            vol.Required(CONF_PORT_PATH): sel.SelectSelector(sel.SelectSelectorConfig(
                options=await self._port_options(), custom_value=True, mode=sel.SelectSelectorMode.DROPDOWN)),
            vol.Required(CONF_ADDRESS): sel.NumberSelector(sel.NumberSelectorConfig(
                min=1, max=247, mode=sel.NumberSelectorMode.BOX)),
            vol.Required("advanced"): section(vol.Schema({
                vol.Required(CONF_BAUDRATE): sel.SelectSelector(sel.SelectSelectorConfig(options=BAUDRATES)),
                vol.Required(CONF_PARITY): sel.SelectSelector(sel.SelectSelectorConfig(
                    options=PARITIES, translation_key="parity")),
                vol.Required(CONF_CT_CURRENT): sel.NumberSelector(sel.NumberSelectorConfig(
                    min=1, max=6000, mode=sel.NumberSelectorMode.BOX, unit_of_measurement="A")),
                vol.Required(CONF_SERIAL_NUMBER): sel.NumberSelector(sel.NumberSelectorConfig(
                    min=0, max=2**31 - 1, mode=sel.NumberSelectorMode.BOX)),
            }), {"collapsed": True}),
        })
        values = {k: v for k, v in m.items() if k in (CONF_NAME, CONF_PORT_PATH, CONF_ADDRESS)}
        values["advanced"] = {k: m.get(k) for k in (CONF_BAUDRATE, CONF_PARITY, CONF_CT_CURRENT, CONF_SERIAL_NUMBER)}
        if user_input is not None:
            values.update(user_input)
        return self.async_show_form(step_id="meter", data_schema=self.add_suggested_values_to_schema(schema, values),
                                    errors=errors, description_placeholders=placeholders)

    # ------------------------------------------------------------ step: sources menu
    async def async_step_sources(self, user_input: dict | None = None) -> SubentryFlowResult:
        options = ["add_device", "add_entities", "add_mqtt"]
        if self._sources:
            options += ["edit_source", "remove_source", "finish"]
        return self.async_show_menu(step_id="sources", menu_options=options,
                                    description_placeholders={"name": self._meter[CONF_NAME],
                                                              "sources": self._sources_text()})

    async def async_step_add_device(self, user_input: dict | None = None) -> SubentryFlowResult:
        if user_input is not None:
            device = dr.async_get(self.hass).async_get(user_input["device"])
            ents = self._device_sensors(user_input["device"])
            three = all(any(mapping.phase_of(c) == n for c in mapping.candidates(ents, "power")
                            if c["score"] > -6) for n in (1, 2, 3))
            suggested = mapping.suggest(ents, three_phase=three)
            self._editing = None
            self._draft = {
                "id": uuid.uuid4().hex[:8], "type": "entities", "phase": "3P" if three else "L1",
                "name": (device.name_by_user or device.name) if device else "Source",
                "device_id": user_input["device"], "invert_power": False,
                "entities": {k.removesuffix("_entity"): v for k, v in suggested.items()},
            }
            return await self.async_step_source()
        return self.async_show_form(step_id="add_device", data_schema=vol.Schema({
            vol.Required("device"): sel.DeviceSelector(sel.DeviceSelectorConfig(
                entity=[sel.EntityFilterSelectorConfig(domain="sensor", device_class="power")])),
        }))

    def _device_sensors(self, device_id: str) -> list[dict]:
        out = []
        for e in er.async_entries_for_device(er.async_get(self.hass), device_id):
            if e.domain != "sensor" or e.disabled_by:
                continue
            state = self.hass.states.get(e.entity_id)
            attrs = state.attributes if state else {}
            out.append({
                "entity_id": e.entity_id,
                "name": e.name or e.original_name or attrs.get("friendly_name") or "",
                "device_class": e.device_class or e.original_device_class or attrs.get("device_class"),
                "state_class": (e.capabilities or {}).get("state_class") or attrs.get("state_class"),
                "unit": e.unit_of_measurement or attrs.get("unit_of_measurement") or "",
            })
        return out

    async def async_step_add_entities(self, user_input: dict | None = None) -> SubentryFlowResult:
        self._editing = None
        self._draft = {"id": uuid.uuid4().hex[:8], "type": "entities", "phase": "L1",
                       "name": f"Source {len(self._sources) + 1}", "invert_power": False, "entities": {}}
        return await self.async_step_source()

    async def async_step_edit_source(self, user_input: dict | None = None) -> SubentryFlowResult:
        if user_input is not None:
            self._editing = int(user_input["source"])
            self._draft = dict(self._sources[self._editing])
            if self._draft["type"] == "mqtt":
                return await self.async_step_mqtt()
            return await self.async_step_source()
        return self.async_show_form(step_id="edit_source", data_schema=self._source_choice())

    async def async_step_remove_source(self, user_input: dict | None = None) -> SubentryFlowResult:
        if user_input is not None:
            del self._sources[int(user_input["source"])]
            return await self.async_step_sources()
        return self.async_show_form(step_id="remove_source", data_schema=self._source_choice())

    def _source_choice(self) -> vol.Schema:
        return vol.Schema({vol.Required("source"): sel.SelectSelector(sel.SelectSelectorConfig(
            options=[sel.SelectOptionDict(value=str(i), label=f"{s['name']} ({s['phase']})")
                     for i, s in enumerate(self._sources)], mode=sel.SelectSelectorMode.LIST))})

    def _store_draft(self) -> None:
        if self._editing is None:
            self._sources.append(self._draft)
        else:
            self._sources[self._editing] = self._draft
        self._draft, self._editing = {}, None

    # ------------------------------------------------------------ step: entities of a source
    async def async_step_source(self, user_input: dict | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            phases = user_input.pop("three_phase", {})
            phase = user_input["phase"]
            ents = {k: user_input[k] for k in TOTAL_KEYS if user_input.get(k)}
            if phase == "3P":
                ents.update({k: phases[k] for k in PHASE_KEYS if phases.get(k)})
            if not ents.get("power") and not (phase == "3P" and all(ents.get(f"l{n}_power") for n in (1, 2, 3))):
                errors["base"] = "power_required_3p" if phase == "3P" else "power_required"
            else:
                self._draft.update(name=user_input[CONF_NAME].strip() or self._draft["name"], phase=phase,
                                   invert_power=bool(user_input.get("invert_power")), entities=ents)
                self._store_draft()
                return await self.async_step_sources()
            self._draft.update(phase=phase, entities=ents)

        d = self._draft
        schema = {
            vol.Required(CONF_NAME): sel.TextSelector(),
            vol.Required("phase"): _phase_sel(),
            **{vol.Optional(k): _entity_sel(k) for k in TOTAL_KEYS},
            vol.Optional("invert_power"): sel.BooleanSelector(),
            vol.Required("three_phase"): section(vol.Schema({vol.Optional(k): _entity_sel(k) for k in PHASE_KEYS}),
                                                 {"collapsed": d.get("phase") != "3P"}),
        }
        ents = d.get("entities", {})
        values = {CONF_NAME: d.get("name"), "phase": d.get("phase", "L1"), "invert_power": d.get("invert_power"),
                  **{k: ents[k] for k in TOTAL_KEYS if ents.get(k)},
                  "three_phase": {k: ents[k] for k in PHASE_KEYS if ents.get(k)}}
        return self.async_show_form(step_id="source", errors=errors,
                                    data_schema=self.add_suggested_values_to_schema(vol.Schema(schema), values),
                                    description_placeholders={"name": d.get("name", "")})

    # ------------------------------------------------------------ step: MQTT source
    async def async_step_add_mqtt(self, user_input: dict | None = None) -> SubentryFlowResult:
        self._editing = None
        self._draft = {"id": uuid.uuid4().hex[:8], "type": "mqtt", "phase": "L1",
                       "name": f"MQTT {len(self._sources) + 1}", "invert_power": False, "topic": ""}
        return await self.async_step_mqtt()

    async def async_step_mqtt(self, user_input: dict | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            topic = user_input["topic"].strip()
            if not topic or "#" in topic or "+" in topic:
                errors["topic"] = "invalid_topic"
            else:
                self._draft.update(name=user_input[CONF_NAME].strip() or self._draft["name"], topic=topic,
                                   phase=user_input["phase"], invert_power=bool(user_input.get("invert_power")))
                self._store_draft()
                return await self.async_step_sources()
        schema = vol.Schema({
            vol.Required(CONF_NAME): sel.TextSelector(),
            vol.Required("topic"): sel.TextSelector(),
            vol.Required("phase"): _phase_sel(),
            vol.Optional("invert_power"): sel.BooleanSelector(),
        })
        d = self._draft
        return self.async_show_form(step_id="mqtt", errors=errors, data_schema=self.add_suggested_values_to_schema(
            schema, {CONF_NAME: d["name"], "topic": d.get("topic"), "phase": d["phase"],
                     "invert_power": d.get("invert_power")}))

    # ------------------------------------------------------------ finish
    async def async_step_finish(self, user_input: dict | None = None) -> SubentryFlowResult:
        m = self._meter
        data = {CONF_PORT_PATH: m[CONF_PORT_PATH], CONF_ADDRESS: m[CONF_ADDRESS], CONF_BAUDRATE: int(m[CONF_BAUDRATE]),
                CONF_PARITY: m[CONF_PARITY], CONF_CT_CURRENT: m[CONF_CT_CURRENT],
                CONF_SERIAL_NUMBER: m[CONF_SERIAL_NUMBER], CONF_SOURCES: self._sources}
        unique_id = f"{m[CONF_PORT_PATH]}:{m[CONF_ADDRESS]}"
        if self._reconfiguring:
            return self.async_update_and_abort(self._get_entry(), self._get_reconfigure_subentry(),
                                               title=m[CONF_NAME], data=data, unique_id=unique_id)
        return self.async_create_entry(title=m[CONF_NAME], data=data, unique_id=unique_id)

