"""Constants for the SolarEdge MeterProxy integration."""

from __future__ import annotations

DOMAIN = "solaredge_meterproxy"
SUBENTRY_METER = "meter"

DEFAULT_HOST = "local-solaredge-meterproxy"
DEFAULT_PORT = 8099

CONF_TOKEN = "token"
CONF_STALE_TIMEOUT = "stale_timeout"
CONF_POLL_TIMEOUT = "poll_timeout"
CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_SETTINGS = {CONF_STALE_TIMEOUT: 300, CONF_POLL_TIMEOUT: 300, CONF_UPDATE_INTERVAL: 2}

# meter subentry data
CONF_PORT_PATH = "serial_port"
CONF_ADDRESS = "address"
CONF_BAUDRATE = "baudrate"
CONF_PARITY = "parity"
CONF_CT_CURRENT = "ct_current"
CONF_SERIAL_NUMBER = "serial_number"
CONF_SOURCES = "sources"

BAUDRATES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]
PARITIES = ["N", "E", "O"]
PHASES = ["L1", "L2", "L3", "3P"]

TOTAL_KEYS = ["power", "energy", "voltage", "current", "frequency", "power_factor", "reactive_power"]
PER_PHASE_KEYS = ["power", "voltage", "current", "energy"]
PHASE_KEYS = [f"l{n}_{k}" for n in (1, 2, 3) for k in PER_PHASE_KEYS]
DEVICE_CLASS = {
    "power": "power", "energy": "energy", "voltage": "voltage", "current": "current",
    "frequency": "frequency", "power_factor": "power_factor", "reactive_power": "reactive_power",
}

STATUSES = ["ok", "partial", "source_unavailable", "no_poll"]
SCAN_INTERVAL_S = 10
