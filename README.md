# PIMA Force — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for the **PIMA Force** alarm system using the JSON-over-TCP protocol (Force Interface JSON Format Specification).

## Features

- **Alarm control panel** — arm away, arm home, disarm
- **Binary sensors** — one per zone, with open/closed state and full attribute set (bypassed, alarmed, tamper, low battery, etc.)
- **Hebrew zone names** — fetched directly from the panel on connection
- **Local push** — the panel connects to HA and pushes all state changes in real time; no polling
- **Auto-reconnect** — zones become unavailable on disconnect and recover automatically when the panel reconnects

## Prerequisites

- PIMA Force alarm panel running a firmware version **with JSON Interface support** — this is not the default firmware. Contact [PIMA Support](https://www.pima-alarms.com) and request a firmware version *"With JSON Support"*
- Home Assistant must have a **static IP address** (the panel initiates the TCP connection to HA)
- The panel must be configured to connect to your HA IP on port **10006** (or your chosen port)
- You must define the account number in the "Moked"

## Installation

### Via HACS (recommended)

1. In HACS, go to **Integrations** → three-dot menu → **Custom repositories**
2. Add `https://github.com/aarbelle/pima-force-ha-integration` as type **Integration**
3. Search for **PIMA Force** and install it
4. Restart Home Assistant

### Manual

Copy the `custom_components/pima` folder into your `<config>/custom_components/` directory, then restart Home Assistant.

## Configuration

Add the following to your `configuration.yaml`:

```yaml
pima:
  account: 123456    # Your panel account number
  password: "1234"   # Your panel password / user code
  port: 10006        # TCP port HA listens on (must match panel config)
```

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `account` | Yes | — | Panel account number |
| `password` | Yes | — | Panel user/installer code |
| `port` | No | `10006` | TCP port for the panel to connect to |

Restart Home Assistant after adding the configuration.

## How it works

The PIMA Force panel **initiates** the TCP connection to Home Assistant (not the other way around). Once connected, HA requests the current partition status, installed zone count, zone names, and zone states. After that, the panel pushes all state changes as CID events in real time.

## Supported entities

| Entity | Description |
|--------|-------------|
| `alarm_control_panel.pima_alarm` | Arm away / arm home / disarm |
| `binary_sensor.pima_zone_N` | One per installed zone |

Zone binary sensors include these extra attributes: `alarmed`, `manual_bypassed`, `auto_bypassed`, `armed`, `supervision_loss`, `low_battery`, `short`, `cut_tamper`, `soak`, `chime`, `anti_mask`, `duress`, `fire`, `medical`, `panic`, `last_event`.

## Credits

- Protocol documentation: PIMA Electronic Systems Ltd — *Force Interface JSON Format Specification*
- Original Hubitat integration: [amithalp/Hubitat-PIMA-Force-Integration](https://github.com/amithalp/Hubitat-PIMA-Force-Integration)
