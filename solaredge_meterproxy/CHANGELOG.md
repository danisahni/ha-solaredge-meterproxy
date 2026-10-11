# Changelog

## 0.1.0

First public release. Complete rework of the former MQTT-based local add-on.

**App + integration**

- The app does the RS485/Modbus work; the new integration (HACS) is the user
  interface: discovered automatically, meters are added as "Add meter"
  dialogs, status entities per meter
- Several meters, each with its own Modbus device ID; several meters per
  adapter, several adapters
- Several sources per meter (summed), each L1/L2/L3 or three-phase, from Home
  Assistant sensors or MQTT JSON payloads
- Device picker with automatic sensor matching for any inverter
- Configuration is stored in the app: meters keep working when Home Assistant
  or the integration is down

**Fail-safe**

- 0 W when a source is unavailable or stale; Home Assistant restarts cause no meter error
- Energy counters are monotonic, persisted and only grow by the increase of
  each source (no jumps when sources are added, removed or swapped)
- Energy is integrated from power if a source has no lifetime counter
- Self-restart when the inverter stops polling a port (max. 3× in a row),
  an adapter disappears or a Modbus server dies
- Never answers foreign device IDs or broadcasts (shared RS485 bus);
  diagnostics of the device IDs seen on the bus
