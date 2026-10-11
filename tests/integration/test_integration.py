"""Config flow, meter subentries and entities of the integration."""

from types import MappingProxyType

from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solaredge_meterproxy.config_flow import _unique_id
from custom_components.solaredge_meterproxy.const import DOMAIN

from .conftest import TOKEN

PORT = "/dev/serial/by-id/usb-FTDI_RS485-if00"
HMS = "sensor.aussenbereich_hms_800_2wt_"
METER_DATA = {"serial_port": PORT, "address": 3, "baudrate": 9600, "parity": "N", "ct_current": 50,
              "serial_number": 41613858,
              "sources": [{"id": "s1", "name": "HMS-800", "type": "entities", "phase": "L1", "invert_power": False,
                           "entities": {"power": "sensor.p", "energy": "sensor.e"}}]}


def entry_with_meter(**meter):
    return MockConfigEntry(
        domain=DOMAIN, title="SolarEdge MeterProxy", unique_id=_unique_id(TOKEN),
        data={CONF_HOST: "local-solaredge-meterproxy", CONF_PORT: 8099, "token": TOKEN},
        subentries_data=[ConfigSubentryData(data={**METER_DATA, **meter}, subentry_type="meter", title="Balkon",
                                            unique_id=f"{PORT}:3")],
    )


async def setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# ----------------------------------------------------------------- main flow
async def test_hassio_discovery(hass: HomeAssistant, fake_app):
    info = HassioServiceInfo(config={"host": "local-solaredge-meterproxy", "port": 8099, "token": TOKEN},
                             name="SolarEdge MeterProxy", slug="local_solaredge_meterproxy", uuid="1234")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
                                                       data=info)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "local-solaredge-meterproxy", "port": 8099, "token": TOKEN}
    await hass.async_block_till_done()
    assert fake_app.pushes[-1]["meters"] == []

    # app rediscovered with a new host: entry updated, no second flow
    info2 = HassioServiceInfo(config={"host": "abc-solaredge-meterproxy", "port": 8099, "token": TOKEN},
                              name="SolarEdge MeterProxy", slug="abc_solaredge_meterproxy", uuid="5678")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_HASSIO},
                                                       data=info2)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert hass.config_entries.async_entries(DOMAIN)[0].data[CONF_HOST] == "abc-solaredge-meterproxy"


async def test_manual_setup(hass: HomeAssistant, fake_app):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "local-solaredge-meterproxy", CONF_PORT: 8099, "token": "wrong"})
    assert result["errors"] == {"token": "invalid_auth"}
    fake_app.reachable = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "local-solaredge-meterproxy", CONF_PORT: 8099, "token": TOKEN})
    assert result["errors"] == {"base": "cannot_connect"}
    fake_app.reachable = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "local-solaredge-meterproxy", CONF_PORT: 8099, "token": TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == _unique_id(TOKEN)


async def test_options_are_pushed(hass: HomeAssistant, fake_app):
    entry = await setup(hass, entry_with_meter())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"stale_timeout": 120, "poll_timeout": 0, "update_interval": 5})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert fake_app.pushes[-1]["settings"] == {"stale_timeout": 120, "poll_timeout": 0, "update_interval": 5}


# ----------------------------------------------------------------- setup + entities
async def test_setup_pushes_meters_and_creates_entities(hass: HomeAssistant, fake_app):
    entry = await setup(hass, entry_with_meter())
    sub_id = next(iter(entry.subentries))
    meter = fake_app.pushes[-1]["meters"][0]
    assert meter == {"id": sub_id, "name": "Balkon", "port": PORT, "address": 3, "baudrate": 9600, "parity": "N",
                     "ct_current": 50, "serial_number": 41613858, "sources": METER_DATA["sources"]}

    ent_reg = er.async_get(hass)
    power = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{sub_id}_power")
    assert hass.states.get(power).state == "812.5"
    assert ent_reg.async_get(power).config_subentry_id == sub_id
    status = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{sub_id}_status")
    st = hass.states.get(status)
    assert st.state == "ok" and st.attributes["sources"]["HMS-800"]["fresh"] is True
    assert st.attributes["bus_requests_by_device_id"] == {"1": 5, "3": 7}
    energy = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{sub_id}_energy")
    assert float(hass.states.get(energy).state) == 400.338
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, sub_id)})
    assert device.name == "Balkon" and device.serial_number == "41613858" and device.sw_version == "0.1.0"
    # meter + its sources, all inside the meter's subentry - nothing "outside a subentry"
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert sorted(d.name for d in devices) == ["Balkon", "HMS-800"]
    assert all(d.config_entries_subentries == {entry.entry_id: {sub_id}} for d in devices)
    src_dev = next(d for d in devices if d.name == "HMS-800")
    assert src_dev.via_device_id == device.id and src_dev.model == "L1 · Home Assistant"
    src_power = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{sub_id}_s1_source_power")
    assert hass.states.get(src_power).state == "812.5"
    fresh = ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{sub_id}_s1_source_fresh")
    assert hass.states.get(fresh).state == "on"


async def test_app_down_then_config_resent_after_reinstall(hass: HomeAssistant, fake_app, freezer):
    entry = await setup(hass, entry_with_meter())
    coordinator = entry.runtime_data.coordinator
    fake_app.reachable = False
    await coordinator.async_refresh()
    power = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{next(iter(entry.subentries))}_power")
    assert hass.states.get(power).state == "unavailable"
    # app comes back empty (reinstalled): integration pushes the configuration again
    fake_app.reachable = True
    fake_app.config, fake_app.revision = {"settings": {}, "meters": []}, "empty"
    n = len(fake_app.pushes)
    await coordinator.async_refresh()
    assert len(fake_app.pushes) == n + 1 and fake_app.config["meters"][0]["name"] == "Balkon"
    assert hass.states.get(power).state == "812.5"


async def test_old_app_device_is_removed(hass: HomeAssistant, fake_app):
    entry = entry_with_meter()
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)},
                                           name="SolarEdge MeterProxy")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)}) is None


async def test_setup_retries_when_app_unreachable(hass: HomeAssistant, fake_app):
    fake_app.reachable = False
    entry = entry_with_meter()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY


# ----------------------------------------------------------------- meter subentry flow
def add_hoymiles_device(hass, entry):
    """Device with the sensors of a Hoymiles HMS-800 (OpenDTU), as in the field."""
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("opendtu", "hms")},
                                                 name="HMS-800-2T")
    ent_reg = er.async_get(hass)
    for key, name, dc, unit, sc in (
        ("ac_leistung", "AC-Leistung", "power", "W", "measurement"),
        ("port_1_dc_leistung", "Port 1 DC-Leistung", "power", "W", "measurement"),
        ("gesamtertrag", "Gesamtertrag", "energy", "Wh", "total_increasing"),
        ("ac_tagesertrag", "AC-Tagesertrag", "energy", "Wh", "total_increasing"),
        ("netzspannung", "Netzspannung", "voltage", "V", "measurement"),
    ):
        ent_reg.async_get_or_create("sensor", "opendtu", key, device_id=dev.id,
                                    suggested_object_id=f"aussenbereich_hms_800_2wt_{key}",
                                    original_name=name, original_device_class=dc, unit_of_measurement=unit,
                                    capabilities={"state_class": sc})
        hass.states.async_set(f"{HMS}{key}", "1", {"unit_of_measurement": unit, "device_class": dc})
    return dev


async def test_add_meter_with_device_auto_mapping(hass: HomeAssistant, fake_app):
    entry = await setup(hass, entry_with_meter())
    dev = add_hoymiles_device(hass, entry)
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "meter"),
                                                             context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "meter"
    # address 3 is taken on this port
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "Garage", "serial_port": PORT, "address": 3,
        "advanced": {"baudrate": "9600", "parity": "N", "ct_current": 50, "serial_number": 123}})
    assert result["errors"] == {"address": "address_in_use"}
    # same port, other baud rate
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "Garage", "serial_port": PORT, "address": 4,
        "advanced": {"baudrate": "19200", "parity": "N", "ct_current": 50, "serial_number": 123}})
    assert result["errors"] == {"base": "port_params"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "Garage", "serial_port": PORT, "address": 4,
        "advanced": {"baudrate": "9600", "parity": "N", "ct_current": 50, "serial_number": 123}})
    assert result["type"] is FlowResultType.MENU and "finish" not in result["menu_options"]

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": "add_device"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"device": dev.id})
    assert result["step_id"] == "source"
    suggested = {k.schema: (k.description or {}).get("suggested_value") for k in result["data_schema"].schema}
    assert suggested["power"] == f"{HMS}ac_leistung"
    assert suggested["energy"] == f"{HMS}gesamtertrag"
    assert suggested["voltage"] == f"{HMS}netzspannung"
    assert suggested["phase"] == "L1"

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "HMS-800", "phase": "L2", "power": f"{HMS}ac_leistung", "energy": f"{HMS}gesamtertrag",
        "invert_power": False, "three_phase": {}})
    assert result["type"] is FlowResultType.MENU and "finish" in result["menu_options"]
    # a second source on the same meter, via MQTT
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": "add_mqtt"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "ESP", "topic": "esp/#", "phase": "L2"})
    assert result["errors"] == {"topic": "invalid_topic"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "ESP", "topic": "esp/pv", "phase": "L2"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    garage = next(m for m in fake_app.config["meters"] if m["name"] == "Garage")
    assert garage["address"] == 4 and garage["serial_number"] == 123
    assert [(s["name"], s["type"], s["phase"]) for s in garage["sources"]] == [
        ("HMS-800", "entities", "L2"), ("ESP", "mqtt", "L2")]
    assert garage["sources"][0]["entities"] == {"power": f"{HMS}ac_leistung", "energy": f"{HMS}gesamtertrag"}
    assert garage["sources"][1]["topic"] == "esp/pv"
    # entities of the new meter exist after the automatic reload
    sub_id = next(s for s, sub in entry.subentries.items() if sub.title == "Garage")
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{sub_id}_power")


async def test_power_sensor_required(hass: HomeAssistant, fake_app):
    entry = await setup(hass, entry_with_meter())
    result = await hass.config_entries.subentries.async_init((entry.entry_id, "meter"),
                                                             context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "X", "serial_port": "/dev/ttyUSB1", "address": 3,
        "advanced": {"baudrate": "9600", "parity": "N", "ct_current": 50, "serial_number": 1}})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": "add_entities"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "Wechselrichter", "phase": "3P", "energy": "sensor.e",
        "three_phase": {"l1_power": "sensor.p1", "l2_power": "sensor.p2"}})
    assert result["errors"] == {"base": "power_required_3p"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "Wechselrichter", "phase": "3P", "energy": "sensor.e",
        "three_phase": {"l1_power": "sensor.p1", "l2_power": "sensor.p2", "l3_power": "sensor.p3"}})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": "finish"})
    await hass.async_block_till_done()
    x = next(m for m in fake_app.config["meters"] if m["name"] == "X")
    assert x["sources"][0]["entities"] == {"energy": "sensor.e", "l1_power": "sensor.p1", "l2_power": "sensor.p2",
                                           "l3_power": "sensor.p3"}


async def test_reconfigure_meter(hass: HomeAssistant, fake_app):
    entry = await setup(hass, entry_with_meter())
    sub_id = next(iter(entry.subentries))
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{sub_id}_s1")})
    result = await entry.start_subentry_reconfigure_flow(hass, sub_id)
    assert result["step_id"] == "meter"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "Balkon Süd", "serial_port": PORT, "address": 5,
        "advanced": {"baudrate": "9600", "parity": "N", "ct_current": 50, "serial_number": 41613858}})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": "edit_source"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"source": "0"})
    assert result["step_id"] == "source"
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "HMS-800", "phase": "L3", "power": "sensor.p", "invert_power": True, "three_phase": {}})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    m = fake_app.config["meters"][0]
    assert (m["id"], m["name"], m["address"]) == (sub_id, "Balkon Süd", 5)
    s = m["sources"][0]
    assert (s["id"], s["phase"], s["invert_power"], s["entities"]) == ("s1", "L3", True, {"power": "sensor.p"})
    assert entry.subentries[sub_id].unique_id == f"{PORT}:5"


async def test_removing_meter_pushes_config(hass: HomeAssistant, fake_app):
    entry = await setup(hass, entry_with_meter())
    sub_id = next(iter(entry.subentries))
    assert hass.config_entries.async_remove_subentry(entry, sub_id)
    await hass.async_block_till_done()
    assert fake_app.config["meters"] == []
    assert isinstance(entry.subentries, MappingProxyType)


async def test_source_devices_follow_the_sources(hass: HomeAssistant, fake_app):
    entry = await setup(hass, entry_with_meter())
    sub_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, sub_id)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "Balkon", "serial_port": PORT, "address": 3,
        "advanced": {"baudrate": "9600", "parity": "N", "ct_current": 50, "serial_number": 41613858}})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": "add_mqtt"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {
        "name": "ESP", "topic": "esp/pv", "phase": "L1"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"],
                                                                  {"next_step_id": "remove_source"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"source": "0"})
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": "finish"})
    await hass.async_block_till_done()
    names = sorted(d.name for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id))
    assert names == ["Balkon", "ESP"]
    assert dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{sub_id}_s1")}) is None
