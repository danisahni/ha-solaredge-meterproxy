# SolarEdge MeterProxy

Brings third-party PV inverters (micro inverters, string inverters, a plug-in
PV system measured by a smart plug, …) into a **SolarEdge** system. Each
configured meter appears on the inverter's RS485 bus as a WattNode revenue
meter with its own device ID; SolarEdge then counts that production in its
energy balance, monitoring and self-consumption logic.

```
HA sensors / MQTT ──► MeterProxy app ──► USB-RS485 adapter ──► SolarEdge RS485 (Modbus multi-device)
                          ▲
   MeterProxy integration (setup dialogs, status entities)
```

The **app** does the work on the RS485 bus. The **integration** (installed via
HACS) is where you set up meters and see their status. The app keeps running
with its last configuration even if Home Assistant or the integration is down.

> **⚠️ Use at your own risk – read this first.** Once the emulated meter is
> configured on the inverter, the inverter depends on it: if it stops
> answering (host off, app stopped, adapter unplugged), the inverter goes to
> fault within seconds and **stops charging/discharging the battery** until
> the meter answers again. **Backup power is untested:** during a grid outage
> the Home Assistant host may reboot and the meter is missing for 1–1.5
> minutes – backup power may be interrupted. Use a **UPS** for the host and
> **test** a grid outage before relying on it. Details: README, section
> *Important: risks and limitations*.

## 1. Install

1. Install and start this app (Watchdog on).
2. Install the integration: *HACS → ⋮ → Custom repositories* →
   `https://github.com/danisahni/ha-solaredge-meterproxy`, type
   *Integration* → install **SolarEdge MeterProxy** → restart Home Assistant.
3. *Settings → Devices & services*: **SolarEdge MeterProxy** shows up under
   *Discovered* → *Add*. (Manual setup: host `local-solaredge-meterproxy`,
   port `8099`, token from this app's log.)

## 2. Add meters

On the integration: **Add meter**.

1. **Meter**: name, RS485 adapter, device ID (Modbus address). The advanced
   section has baud rate, parity, CT rating and serial number – the defaults
   match the inverter's defaults.
2. **Sources**: *Add device* lets you pick any device with a power sensor; the
   matching sensors (AC power, lifetime energy, voltage, …) are filled in
   automatically. Alternatively pick sensors manually or use an MQTT topic.
   Choose the phase the source feeds into (`3P` for three-phase inverters).
3. *Save meter*. The app applies it right away (a new adapter makes the app
   restart once).

All sources of one meter are **added up**. Every meter is a **separate meter
on the inverter** with its own device ID. Several meters can share one adapter.

| What you have | Suggested setup |
|---|---|
| One plug-in PV system | 1 meter, 1 source |
| Two micro inverters, only the total matters | 1 meter, 2 sources |
| Two systems you want to see separately in the SolarEdge monitoring | 2 meters (IDs 3 and 4), 1 source each |
| Three-phase string inverter | 1 meter, 1 source with phase `3P` |

## 3. Set up the SolarEdge inverter (SetApp or web page 172.16.0.1)

Do this for **every** meter, with the values from the integration:

1. Wiring: adapter **A → A(+)**, **B → B(−)**, GND → G if available, to an RS485
   port of the inverter (RS485-1 or RS485-2 terminal block). If the port is
   already used (grid meter, battery, backup interface, …) connect in parallel
   – the protocol must then already be *Modbus (Multi-Device)*. Terminate the
   bus only at its two ends.
2. Open the inverter's commissioning menu, either with the **SetApp** mobile
   app or **in a browser without SetApp**: activate the inverter's Wi-Fi
   access point (see the inverter's installation guide – on most models by
   switching ON/OFF/P briefly to *P*), connect a phone or laptop to the
   Wi-Fi **SEDG-xxxxxxxx** (password on the inverter's label) and open
   **http://172.16.0.1**. Both show the same menus.

   **Commissioning → Site Communication → RS485-x**
   - *Protocol* / *Device type*: **Modbus (Multi-Device)**
   - **Add Modbus device → Meter**
3. Meter settings:

   | Setting | Value |
   |---|---|
   | Meter protocol | **SolarEdge** |
   | Device ID | the meter's *device ID* (e.g. 3) – must not be used by any other device on this bus |
   | CT rating | the meter's *CT rating* (default 50 A) |
   | Meter function | **External Production** |
   | Baud rate / parity | as in the integration (default 9600, None) |

4. After a few seconds the meter shows **OK** in the status screen (SetApp / 172.16.0.1), and
   the integration's *Status* entity changes to `ok`.

**Meter function:** use *External Production* (production of another
inverter that is not part of the SolarEdge system). Do not use the emulated
meter for *Export+Import* or *Consumption* – export limitation and grid
functions stay with the real grid meter. This does **not** make a failure
harmless: if the emulated meter stops answering, the inverter goes to fault
and stops the battery (see the warning at the top).

**After adding or removing a meter** the SolarEdge Modbus register map
changes. If you use the *SolarEdge Modbus Multi* integration, reload it once
(it becomes unavailable otherwise).

Background on the meter emulation: [nmakel/solaredge_meterproxy](https://github.com/nmakel/solaredge_meterproxy).

## Fail-safe behaviour

| Situation | Behaviour |
|---|---|
| Source `unavailable` (e.g. micro inverter asleep at night) | Its power counts as **0 W** immediately, energy keeps its value |
| Source "available" but no new value for *source timeout* (default 300 s) | 0 W |
| Home Assistant restarting / API unreachable | Last values kept until the source timeout, then 0 W. The meters keep answering → **no meter error** on the inverter |
| No lifetime energy sensor | Energy is calculated from the power (gaps > 60 s are not counted) |
| Energy counter goes backwards / unavailable | Highest value is kept and persisted |
| Source added, removed, swapped or phase changed | The meter's energy only grows by the increase of each source; no jumps, never backwards |
| Inverter stops polling a port for the *poll timeout* | App restarts (fresh serial port); after 3 restarts in a row it stays up and reports `no_poll` |
| USB adapter disappears / Modbus server crashes | Exit → Watchdog restart |
| **Home Assistant host off, app stopped, adapter unplugged** | **Not covered by software:** the meter stops answering, the inverter goes to fault within seconds and stops the battery until the meter is back (about 1 min after it answers again). Use a UPS, set up notifications |
| Requests for other device IDs / broadcasts | Never answered (shared bus) |
| Integration not loaded / broken after an update | App keeps running with its stored configuration |

Timeouts are set in the integration's *Configure* dialog. Keep **Watchdog**
enabled on this app's info page.

## Entities (per meter)

| Entity | Content |
|---|---|
| Status | `ok`, `partial` (some sources without data), `source_unavailable`, `no_poll`; attributes: port, device ID, per-phase values, per-source power/age, requests seen on the bus per device ID |
| Reported power / energy | What the inverter reads from this meter |
| Last inverter poll, Polls | When / how often the inverter read the meter |
| Inverter polling | On while the inverter reads the meter (good for notifications) |

Below each meter, every source is its own device (linked to the meter) with
*Power*, *Energy*, *Current data* (off = counted as 0 W) and *Data age*.
The app itself has no device. If the app is
not reachable, all meter entities become `unavailable`.

## MQTT sources

Payload: a JSON object, units W, kWh, V, A, Hz, var; power factor 0…1 or %.
The broker of the Mosquitto app is used automatically.

```json
{"power_active": 812.5, "energy_active": 400.338, "voltage_ln": 233.7,
 "frequency": 49.98, "power_factor": 1.0, "timestamp": "2026-10-11T12:00:00+02:00"}
```

| Key | Meaning |
|---|---|
| `power_active` | AC power in W (required) |
| `energy_active` / `import_energy_active` | Lifetime energy in **kWh** |
| `voltage_ln`, `current`, `frequency`, `power_factor`, `power_reactive` | Optional |
| `l1_power_active` … `l3_power_active`, `l1n_voltage` …, `l1_current` …, `l1_energy_active` … | Per-phase values for `3P` sources |
| `timestamp` | Optional, epoch seconds or ISO 8601 – recommended with retained messages |

`null` means unavailable (0 W). Publish at least every source timeout.

## Troubleshooting

- *Status* `no_poll` right after setup: device ID / protocol on the inverter, baud
  rate/parity, A/B swapped. The *Status* entity's attribute
  `bus_requests_by_device_id` lists the device IDs the inverter asks for: no frames at all = wiring or baud
  rate; frames, but not for your ID = meter not set up on the inverter.
- Set the app's `log_level` to `debug` to see every Modbus request.
- Power always 0: check that the source's power sensor changes and is not `unavailable`.
