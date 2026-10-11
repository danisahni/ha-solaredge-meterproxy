import asyncio
import json
import socket
import time

import pytest

from semp.mqtt_source import MqttFeed, payload_to_readings
from semp.source import SourceTracker

NOW = 1_800_000_000.0


def test_single_phase_payload_mapping():
    r = payload_to_readings({"power_active": 812.5, "energy_active": 400.338, "l1n_voltage": 233.7,
                             "l1_current": 3.5, "power_factor": 100, "frequency": 49.98}, NOW, (1,))
    assert r["power"].value == 812.5 and r["power"].reported_at == NOW
    assert r["energy"].value == 400.338 and r["energy"].unit == "kWh"
    assert r["voltage"].value == 233.7 and r["current"].value == 3.5


def test_payload_from_old_automation_drives_tracker():
    old = {"power_active": 27.1, "l1_power_active": 27.1, "power_reactive": 0, "l1_power_reactive": 0,
           "l1_current": 0.12, "voltage_ln": 233.7, "l1n_voltage": 233.7, "frequency": 49.98,
           "power_factor": 1.0, "l1_power_factor": 1.0, "energy_active": 400.338,
           "import_energy_active": 400.338, "l1_energy_active": 400.338, "l1_import_energy_active": 400.338}
    t = SourceTracker({}, phases=1, stale_timeout=300)
    v = t.update(payload_to_readings(old, NOW, (1,)), now=NOW)
    assert v.power_w == 27.1 and v.energy_kwh == 400.338 and v.per_phase[1].current_a == 0.12
    assert t.update(payload_to_readings(old, NOW, (1,)), now=NOW + 301).power_w == 0  # no new message


def test_null_means_unavailable():
    t = SourceTracker({}, phases=1, stale_timeout=300)
    t.update(payload_to_readings({"power_active": 500}, NOW, (1,)), now=NOW)
    v = t.update(payload_to_readings({"power_active": None, "energy_active": 401}, NOW + 2, (1,)), now=NOW + 2)
    assert v.power_w == 0 and v.energy_kwh == 401


def test_three_phase_payload():
    r = payload_to_readings({"l1_power_active": 1000, "l2_power_active": 1100, "l3_power_active": 900,
                             "l2n_voltage": 231, "energy_active": 600}, NOW, (1, 2, 3))
    t = SourceTracker({}, phases=(1, 2, 3), stale_timeout=300)
    v = t.update(r, now=NOW)
    assert v.power_w == 3000 and v.per_phase[2].voltage_v == 231 and v.per_phase[3].energy_kwh == 200


def test_feed_ignores_bad_payload_and_uses_timestamp():
    f = MqttFeed(host="localhost", port=1, topic="t")
    f.handle_payload(b"not json")
    f.handle_payload(b"[1,2]")
    assert f.readings() == {}
    past = time.time() - 600
    f.handle_payload(json.dumps({"power_active": 10, "timestamp": past}).encode())
    assert f.readings()["power"].reported_at == past
    f.handle_payload(json.dumps({"power_active": 10, "timestamp": "2020-01-01T00:00:00+00:00"}).encode())
    assert f.readings()["power"].reported_at == 1577836800
    f.handle_payload(json.dumps({"power_active": 11}).encode())
    assert abs(f.readings()["power"].reported_at - time.time()) < 2


# ------------------------------------------------------------ real broker test
amqtt = pytest.importorskip("amqtt.broker")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def test_feed_against_real_broker_with_reconnect():
    import paho.mqtt.client as mqtt

    port = _free_port()
    cfg = {"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
           "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}}
    feed = MqttFeed(host="127.0.0.1", port=port, topic="hoymiles/test")
    feed.start()                                   # broker not up yet: must keep retrying
    await asyncio.sleep(1.5)
    assert not feed.connected
    broker = amqtt.Broker(cfg)
    await broker.start()
    try:
        for _ in range(100):
            if feed.connected:
                break
            await asyncio.sleep(0.2)
        assert feed.connected
        pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        pub.connect("127.0.0.1", port)
        pub.loop_start()
        payload = json.dumps({"power_active": 812.5, "energy_active": 400.3})
        pub.publish("hoymiles/test", payload, qos=1).wait_for_publish(5)
        for _ in range(50):
            if feed.readings():
                break
            await asyncio.sleep(0.1)
        r = feed.readings()
        assert r["power"].value == 812.5 and r["energy"].value == 400.3
        pub.loop_stop()
        pub.disconnect()
    finally:
        feed.stop()
        await broker.shutdown()
