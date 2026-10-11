# SolarEdge MeterProxy for Home Assistant

Bring any third-party PV inverter (micro inverters, string inverters, plug-in
PV measured by a smart plug, …) into your **SolarEdge** energy balance. The
MeterProxy emulates WattNode revenue meters on the SolarEdge RS485 bus and
fills them with values from Home Assistant sensors or MQTT.

```
HA sensors / MQTT ──► MeterProxy app ──► USB-RS485 adapter ──► SolarEdge RS485 (Modbus multi-device)
                          ▲
   MeterProxy integration (setup dialogs, status entities)
```

This repository contains two parts that work together:

| Part | Installed via | Job |
|---|---|---|
| **App** (formerly add-on) `solaredge_meterproxy/` | App store repository | Talks Modbus RTU on the RS485 bus, reads the sources, fail-safe logic, keeps running on its own |
| **Integration** `custom_components/solaredge_meterproxy/` | HACS (custom repository) | Setup dialogs ("Add meter", device picker with automatic sensor matching), status entities |

Features:

- Several meters, each with its own Modbus device ID; several meters can share one adapter
- Several sources per meter (summed), each single-phase (L1/L2/L3) or three-phase
- Works with any inverter Home Assistant knows – only a power sensor is
  required (energy is calculated if there is no lifetime counter)
- Sources: Home Assistant sensors directly, or MQTT JSON payloads
- Fail-safe: 0 W on stale/unavailable data, monotonic persisted energy
  counters without jumps, self-restart if the inverter stops polling or the
  adapter disappears, never answers foreign device IDs on a shared bus,
  keeps running when Home Assistant restarts – but see
  [risks and limitations](#important-risks-and-limitations): the inverter itself depends on the meter
- WattNode WNC-3Y-400-MB register layout from
  [nmakel/solaredge_meterproxy](https://github.com/nmakel/solaredge_meterproxy)

## Important: risks and limitations

**Use at your own risk.** This is an unofficial hobby project, not affiliated
with or endorsed by SolarEdge or WattNode, without any warranty (see
[LICENSE](LICENSE)). You connect a self-made device to the communication bus
of your inverter. This may affect your warranty, your grid-connection
conditions or your monitoring data. If in doubt, ask your installer first.

**Once a meter is configured on the inverter, the inverter depends on it.**
Observed on a SolarEdge SE10K with Home Battery, grid meter and Backup
Interface on the same RS485 bus:

- If the emulated meter stops answering (Home Assistant host off, app
  stopped, adapter unplugged), the inverter goes to **fault within seconds and
  stops charging/discharging the battery**. Self-consumption from the battery
  stops and the house is supplied from the grid. It recovers about a minute
  after the meter answers again.
- Even occasional missed answers matter: a bug in a pre-release version that
  missed about 1 in 7 requests made the inverter toggle into fault every
  10–30 s, and the battery only worked half of the time.

**Backup power (Backup Interface / whole-house backup) is untested.** It is
unknown whether the inverter behaves the same during a grid outage. When the
grid fails, a Home Assistant host without a UPS reboots during the switchover
(~1 s) and the emulated meter is missing for about 1–1.5 minutes while it
starts. If the inverter goes to fault in backup mode as well, **backup power
may be interrupted or may not start at all**. If backup power matters to you:

- power the Home Assistant host (and with it the USB-RS485 adapter) from a
  **UPS** that bridges the switchover,
- **test it** – switch off the grid at the main breaker and check that backup
  starts and the inverter shows no fault,
- until then, consider not configuring the emulated meter on the inverter.

**Other things to keep in mind**

- A defective adapter can disturb the whole RS485 bus (grid meter, battery,
  Backup Interface). Use a good, preferably isolated adapter.
- If you remove the meter from the inverter later, other Modbus integrations
  (e.g. SolarEdge Modbus Multi) may need a reload because the register map
  changes.
- Set up a notification for the meter's *Inverter polling* entity and for the
  inverter status, so a failure is noticed in minutes, not days.

## Requirements

- Home Assistant OS or Supervised (the app needs the Supervisor), 2025.6 or newer
- [HACS](https://hacs.xyz) for the integration
- A USB-RS485 adapter (preferably isolated) connected to the Home Assistant host
- A SolarEdge inverter with a free or Modbus multi-device RS485 port and access to its commissioning menu (SetApp or http://172.16.0.1 via the inverter's Wi-Fi)

## Installation

1. **App:** [![Add repository][repo-badge]][repo-link] or *Settings → Apps →
   App store → ⋮ → Repositories* → add
   `https://github.com/danisahni/ha-solaredge-meterproxy`. Install
   **SolarEdge MeterProxy**, enable *Watchdog*, start it.
2. **Integration:** *HACS → ⋮ → Custom repositories* → the same URL, type
   *Integration* → install **SolarEdge MeterProxy** → restart Home Assistant.
3. *Settings → Devices & services*: the MeterProxy appears under
   *Discovered* → *Add*.
   (Manual setup: host `local-solaredge-meterproxy`, port `8099`, token from the app log.)

## Adding a meter

On the integration: **Add meter**

1. **Meter** – name, RS485 adapter, device ID (Modbus address). Advanced:
   baud rate, parity, CT rating, serial number (defaults match the inverter defaults).
2. **Sources** – *Add device*: pick your inverter (any device with a power
   sensor); AC power, lifetime energy, voltage, current, frequency, power
   factor and reactive power are matched automatically, DC/string values and
   daily counters are skipped. Or pick sensors manually, or an MQTT topic.
   Set the phase (`3P` for three-phase inverters, with optional per-phase sensors).
3. **Save meter.** Change it later with the meter's ⋮ → *Reconfigure*.

The sources of one meter are summed. Each meter is a separate meter on the inverter:

| What you have | Suggested setup |
|---|---|
| One plug-in PV system | 1 meter, 1 source |
| Two micro inverters, only the total matters | 1 meter, 2 sources |
| Two systems you want to see separately in SolarEdge monitoring | 2 meters (IDs 3 and 4), 1 source each |
| Three-phase string inverter | 1 meter, 1 source with phase `3P` |

## Setting up the SolarEdge inverter (SetApp or web page 172.16.0.1)

Do this for **every** meter, with the values from the integration.

1. **Wiring:** adapter **A → A(+)**, **B → B(−)**, GND → G if available, to
   the RS485-1 or RS485-2 terminal block of the inverter. If that port is
   already used (grid meter, battery, backup interface, …) connect in parallel;
   its protocol is then already *Modbus (Multi-Device)*. Termination only at
   the two ends of the bus.
2. Open the inverter's commissioning menu, either with the **SetApp** mobile
   app or **in a browser without SetApp**: activate the inverter's Wi-Fi
   access point (see the inverter's installation guide – on most models by
   switching ON/OFF/P briefly to *P*), connect a phone or laptop to the
   Wi-Fi **SEDG-xxxxxxxx** (password on the inverter's label) and open
   **http://172.16.0.1**. Both show the same menus.

   **Commissioning → Site Communication → RS485-x**
   - Protocol / device type: **Modbus (Multi-Device)**
   - **Add Modbus device → Meter**
3. **Meter settings:**

   | Setting | Value |
   |---|---|
   | Meter protocol | **SolarEdge** |
   | Device ID | the meter's device ID (e.g. 3) – unique on this bus |
   | CT rating | the meter's CT rating (default 50 A) |
   | Meter function | **External Production** |
   | Baud rate / parity | as in the integration (default 9600, None) |

4. Within seconds the meter shows **OK** in the status screen (SetApp / 172.16.0.1) and the
   meter's *Status* entity in Home Assistant becomes `ok`.

Use *External Production* only – keep export limitation and grid functions
on the real grid meter. Note that this does **not** make a failure harmless:
if the emulated meter stops answering, the inverter goes to fault and stops
the battery (see [risks and limitations](#important-risks-and-limitations)).

If you use the *SolarEdge Modbus Multi* integration: adding or removing a
meter shifts the inverter's Modbus register map – reload that integration once.

nmakel's project describes the meter emulation itself in more detail; the
step-by-step guide above is specific to this project.

More details (fail-safe behaviour, entities, MQTT payload, troubleshooting):
[DOCS.md](solaredge_meterproxy/DOCS.md).

## Development

```bash
# app
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # tests/app, incl. end-to-end over a pseudo-terminal
ruff check .

# integration (separate venv, Home Assistant test harness)
python -m venv .venv-ha && . .venv-ha/bin/activate
pip install -r requirements-test-ha.txt
pytest tests/integration
```

The app exposes a small token-protected HTTP API on port 8099 (internal
network only): `GET /api/health`, `/api/info`, `/api/config`, `/api/status`,
`PUT /api/config`.

Release: bump the version in `solaredge_meterproxy/config.yaml`,
`solaredge_meterproxy/app/semp/__init__.py` and
`custom_components/solaredge_meterproxy/manifest.json` (a test checks they
match), add a `CHANGELOG.md` entry, commit, tag `vX.Y.Z`, create a GitHub release.

## Disclaimer

Not affiliated with, endorsed by or supported by SolarEdge or WattNode. All
product names are trademarks of their owners. The software is provided "as
is", without warranty of any kind (MIT license). You are connecting a
self-made device to your inverter's communication bus – use entirely at your
own risk. See [Important: risks and limitations](#important-risks-and-limitations).

## License

MIT – see [LICENSE](LICENSE).

[repo-badge]: https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg
[repo-link]: https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdanisahni%2Fha-solaredge-meterproxy
