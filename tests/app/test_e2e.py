"""End-to-end: fake HA API + pseudo-terminal acting as the RS485 bus."""

import asyncio
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone

import pytest
from aiohttp import web

from semp import engine as E
from semp.api import create_app
from semp.source import HaClient

needs_pty = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX pseudo-terminals (Linux/macOS/WSL)")


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def read_req(slave: int, addr: int, count: int) -> bytes:
    pdu = struct.pack(">BBHH", slave, 3, addr, count)
    return pdu + crc16(pdu)


async def transact(fd: int, frame: bytes, timeout: float = 1.5) -> bytes:
    os.write(fd, frame)
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        await asyncio.sleep(0.05)
        try:
            buf += os.read(fd, 1024)
        except BlockingIOError:
            pass
    return buf


def decode_f32(words, i):
    lo, hi = words[i], words[i + 1]          # word order little
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


@pytest.fixture
async def fake_ha(aiohttp_unused_port=None):
    states = {}
    posted = {}
    now = datetime.now(timezone.utc).isoformat()
    states["sensor.p"] = {"state": "812.5", "attributes": {"unit_of_measurement": "W"},
                          "last_reported": now, "last_updated": now}
    states["sensor.e"] = {"state": "400338.0", "attributes": {"unit_of_measurement": "Wh"},
                          "last_reported": now, "last_updated": now}

    async def get_state(req):
        s = states.get(req.match_info["eid"])
        return web.json_response(s) if s else web.Response(status=404)

    async def mqtt_service(req):
        return web.json_response({"data": states.get("_mqtt", {})})

    async def post_state(req):
        posted[req.match_info["eid"]] = await req.json()
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/api/states/{eid}", get_state)
    app.router.add_post("/api/states/{eid}", post_state)
    app.router.add_get("/services/mqtt", mqtt_service)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/api", states, posted
    await runner.cleanup()


TOKEN = "t" * 32


def pty_port():
    import tty

    master, slave = os.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    os.set_blocking(master, False)
    return master, os.ttyname(slave)


def meters_cfg(port, **meter):
    m = {"id": "m1", "name": "Balkon", "port": port, "address": 3, "serial_number": 41613858,
         "sources": [{"id": "s1", "type": "entities", "phase": "L1",
                      "entities": {"power": "sensor.p", "energy": "sensor.e"}}]}
    m.update(meter)
    return {"settings": {"update_interval": 1, "stale_timeout": 300, "poll_timeout": 300}, "meters": [m]}


def make_engine(tmp_path, api, cfg=None):
    if cfg is not None:
        (tmp_path / "meters.json").write_text(json.dumps(cfg))
    sup = api.rsplit("/api", 1)[0]
    return E.Engine(data_dir=tmp_path, ha=HaClient(api, "x"), supervisor_api=sup)


async def read_basic(fd, slave=3):
    resp = await transact(fd, read_req(slave, 1000, 34))
    assert resp[:3] == bytes([slave, 3, 68]), resp
    assert crc16(resp[:-2]) == resp[-2:]
    return list(struct.unpack(">34H", resp[3:71]))


@needs_pty
async def test_serves_values_and_ignores_other_slaves(tmp_path, fake_ha):
    api, states, _ = fake_ha
    fd, port = pty_port()
    eng = make_engine(tmp_path, api, meters_cfg(port))
    run = asyncio.create_task(eng.run())
    await asyncio.sleep(1.2)

    words = await read_basic(fd)
    assert decode_f32(words, 0) == pytest.approx(400.338, rel=1e-6)   # energy kWh
    assert decode_f32(words, 8) == pytest.approx(812.5)              # total power
    assert decode_f32(words, 10) == pytest.approx(812.5)             # L1 power

    serial = await transact(fd, read_req(3, 1700, 2))
    w = struct.unpack(">2H", serial[3:7])
    assert struct.unpack(">i", struct.pack(">HH", w[1], w[0]))[0] == 41613858

    # requests for the grid meter / battery / other devices must stay unanswered
    assert await transact(fd, read_req(2, 1000, 34), timeout=0.8) == b""
    assert await transact(fd, read_req(0, 1000, 2), timeout=0.8) == b""

    # source goes unavailable -> power 0, energy kept
    states["sensor.p"]["state"] = "unavailable"
    states["sensor.e"]["state"] = "unavailable"
    await asyncio.sleep(1.2)
    words = await read_basic(fd)
    assert decode_f32(words, 8) == 0.0
    assert decode_f32(words, 0) == pytest.approx(400.338, rel=1e-6)

    st = eng.status()
    assert st["meters"]["m1"]["polls"] >= 3 and st["meters"]["m1"]["status"] == "source_unavailable"
    assert st["ports"][port]["frames"] == {"2": 1, "3": 3}  # broadcasts (0) are not counted
    eng.stop.set()
    assert await asyncio.wait_for(run, 10) == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["meters"]["m1"]["energy"]["1"] == pytest.approx(400.338)


def foreign_response(slave: int, n_regs: int = 34) -> bytes:
    pdu = bytes([slave, 3, n_regs * 2]) + bytes(range(n_regs * 2))
    return pdu + crc16(pdu)


@needs_pty
async def test_busy_shared_bus_never_drops_our_requests(tmp_path, fake_ha):
    """Regression: the inverter must get an answer to every request for our ID,
    also when it arrives in the same chunk as traffic of other devices
    (grid meter / battery answers). A missed answer = meter fault on the inverter."""
    api, _, _ = fake_ha
    fd, port = pty_port()
    eng = make_engine(tmp_path, api, meters_cfg(port))
    run = asyncio.create_task(eng.run())
    await asyncio.sleep(1.2)
    answered = 0
    for i in range(20):
        noise = foreign_response(2) + read_req(1, 40000 + i, 10) + foreign_response(1, 5 + i)
        resp = await transact(fd, noise + read_req(3, 1000, 34), timeout=0.6)
        if resp[:3] == bytes([3, 3, 68]) and crc16(resp[:71]) == resp[71:73]:
            answered += 1
    assert answered == 20
    assert eng.status()["meters"]["m1"]["polls"] >= 20
    eng.stop.set()
    assert await asyncio.wait_for(run, 10) == 0


@needs_pty
async def test_live_config_adds_second_meter_on_same_port(tmp_path, fake_ha):
    api, states, _ = fake_ha
    now = datetime.now(timezone.utc).isoformat()
    states["sensor.q"] = {"state": "100", "attributes": {"unit_of_measurement": "W"},
                          "last_reported": now, "last_updated": now}
    fd, port = pty_port()
    eng = make_engine(tmp_path, api, meters_cfg(port))
    run = asyncio.create_task(eng.run())
    await asyncio.sleep(0.5)
    cfg = meters_cfg(port)
    cfg["meters"].append({"id": "m2", "name": "Garage", "port": port, "address": 4,
                          "sources": [{"id": "s1", "type": "entities", "phase": "L2",
                                       "entities": {"power": "sensor.q"}}]})
    res = await eng.apply_config(cfg)
    assert res["changed"] and not res["restart"]
    await asyncio.sleep(1.2)
    w3, w4 = await read_basic(fd, 3), await read_basic(fd, 4)
    assert decode_f32(w3, 8) == pytest.approx(812.5)
    assert decode_f32(w4, 8) == 100 and decode_f32(w4, 12) == 100   # on L2
    assert json.loads((tmp_path / "meters.json").read_text())["meters"][1]["id"] == "m2"
    # removing it again: address 4 goes silent
    await eng.apply_config(meters_cfg(port))
    assert await transact(fd, read_req(4, 1000, 34), timeout=0.8) == b""
    eng.stop.set()
    assert await asyncio.wait_for(run, 10) == 0


async def test_new_port_requests_restart(tmp_path, fake_ha, monkeypatch):
    api, _, _ = fake_ha
    eng = make_engine(tmp_path, api)
    calls = []

    async def fake_restart():
        calls.append(1)

    monkeypatch.setattr(eng, "_restart_self", fake_restart)
    res = await eng.apply_config(meters_cfg("/dev/ttyNEW"))
    assert res["restart"] and eng.restart_pending
    await asyncio.sleep(0.7)
    assert calls == [1]
    # persisted for the next start
    eng2 = make_engine(tmp_path, api)
    assert list(eng2.meters) == ["m1"] and "/dev/ttyNEW" in eng2.ports


@needs_pty
async def test_exits_when_inverter_stops_polling(tmp_path, fake_ha):
    api, _, _ = fake_ha
    fd, port = pty_port()
    cfg = meters_cfg(port)
    cfg["settings"]["poll_timeout"] = 3
    eng = make_engine(tmp_path, api, cfg)
    code = await asyncio.wait_for(eng.run(), 20)
    assert code == E.EXIT_NO_POLL
    assert json.loads((tmp_path / "state.json").read_text())["no_poll_restarts"] == {port: 1}


def test_no_poll_restarts_are_limited(tmp_path):
    cfg = meters_cfg("/dev/null")
    cfg["settings"]["poll_timeout"] = 10
    (tmp_path / "meters.json").write_text(json.dumps(cfg))
    eng = E.Engine(data_dir=tmp_path, ha=HaClient("http://x", "x"), supervisor_api="http://x")
    eng.state["no_poll_restarts"] = {"/dev/null": 3}
    assert eng.check_ports(eng.started + 60) is None      # stays up, reports no_poll
    eng.ports["/dev/null"].slaves[3].stats.last_poll = eng.started + 59
    assert eng.check_ports(eng.started + 60) is None
    assert eng.state["no_poll_restarts"]["/dev/null"] == 0  # a poll resets the counter


async def test_exits_when_serial_port_disappears(tmp_path, fake_ha):
    api, _, _ = fake_ha
    eng = make_engine(tmp_path, api, meters_cfg("/dev/ttyDOESNOTEXIST"))
    code = await asyncio.wait_for(eng.run(), 20)
    assert code == E.EXIT_SERIAL_GONE


@needs_pty
async def test_three_phase_end_to_end(tmp_path, fake_ha):
    api, states, _ = fake_ha
    now = datetime.now(timezone.utc).isoformat()
    for n, w in ((1, 1000), (2, 1100), (3, 900)):
        states[f"sensor.p{n}"] = {"state": str(w), "attributes": {"unit_of_measurement": "W"},
                                  "last_reported": now, "last_updated": now}
    fd, port = pty_port()
    src = {"id": "s1", "type": "entities", "phase": "3P",
           "entities": {"energy": "sensor.e", **{f"l{n}_power": f"sensor.p{n}" for n in (1, 2, 3)}}}
    eng = make_engine(tmp_path, api, meters_cfg(port, sources=[src]))
    run = asyncio.create_task(eng.run())
    await asyncio.sleep(1.2)
    words = await read_basic(fd)
    assert [decode_f32(words, i) for i in (8, 10, 12, 14)] == [3000, 1000, 1100, 900]
    assert decode_f32(words, 0) == pytest.approx(400.338, rel=1e-6)
    eng.stop.set()
    assert await asyncio.wait_for(run, 10) == 0


async def test_api_auth_config_and_status(tmp_path, fake_ha):
    from aiohttp.test_utils import TestClient, TestServer

    api, _, _ = fake_ha
    eng = make_engine(tmp_path, api)
    client = TestClient(TestServer(create_app(eng, TOKEN)))
    await client.start_server()
    assert (await client.get("/api/health")).status == 200
    assert (await client.get("/api/status")).status == 401
    assert (await client.get("/api/status", headers={"Authorization": "Bearer wrong"})).status == 401
    h = {"Authorization": f"Bearer {TOKEN}"}
    info = await (await client.get("/api/info", headers=h)).json()
    assert info["version"] and isinstance(info["serial_ports"], list)
    bad = await client.put("/api/config", headers=h, json={"meters": [{"id": "x", "port": "", "address": 0}]})
    assert bad.status == 400 and len((await bad.json())["errors"]) >= 2
    cfg = meters_cfg("/dev/ttyAPI")
    eng._restart_self = lambda: asyncio.sleep(0)
    ok = await (await client.put("/api/config", headers=h, json=cfg)).json()
    assert ok["restart"] is True
    got = await (await client.get("/api/config", headers=h)).json()
    assert got["revision"] == ok["revision"] and got["meters"][0]["serial_number"] == 41613858
    # same config again: nothing to do
    again = await (await client.put("/api/config", headers=h, json=cfg)).json()
    assert again == {"revision": ok["revision"], "changed": False, "restart": False}
    st = await (await client.get("/api/status", headers=h)).json()
    assert st["revision"] == ok["revision"]
    await client.close()


@needs_pty
async def test_mqtt_source_end_to_end(tmp_path, fake_ha):
    amqtt = pytest.importorskip("amqtt.broker")
    import socket

    import paho.mqtt.client as mqtt

    api, states, _ = fake_ha
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    mport = sock.getsockname()[1]
    sock.close()
    broker = amqtt.Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{mport}"}},
                           "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await broker.start()
    states["_mqtt"] = {"host": "127.0.0.1", "port": mport}       # broker taken from "Supervisor"
    fd, port = pty_port()
    eng = make_engine(tmp_path, api, meters_cfg(port, sources=[
        {"id": "s1", "type": "entities", "phase": "L1", "entities": {"power": "sensor.p"}},
        {"id": "mq", "type": "mqtt", "phase": "L1", "topic": "hoymiles/hms"}]))
    run = asyncio.create_task(eng.run())
    try:
        src = eng.meters["m1"].sources[1]
        for _ in range(50):
            if src.feed is not None and src.feed.connected:
                break
            await asyncio.sleep(0.1)
        pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        pub.connect("127.0.0.1", mport)
        pub.loop_start()
        pub.publish("hoymiles/hms", json.dumps({"power_active": 650.0, "energy_active": 401.5}),
                    qos=1, retain=True).wait_for_publish(5)
        await asyncio.sleep(1.5)
        words = await read_basic(fd)
        assert decode_f32(words, 8) == pytest.approx(812.5 + 650.0)
        assert eng.status()["meters"]["m1"]["sources"]["mq"]["mqtt_connected"] is True
        pub.loop_stop()
        pub.disconnect()
    finally:
        eng.stop.set()
        assert await asyncio.wait_for(run, 10) == 0
        await broker.shutdown()
