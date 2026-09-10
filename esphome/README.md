# Petkit Fresh Element Mini — local ESPHome firmware

This firmware runs the Fresh Element Mini through Home Assistant and local
controls, without Petkit's cloud. ESPHome replaces the firmware on the feeder's
built-in ESP8266 and keeps the existing motor controller and wiring.

For a complete installation procedure, start with the [project README](../README.md#installation).
This page explains the feeder's operation, configuration, and serial protocol.

## Why this works

The feeder divides its work between two processors:

| Processor | Responsibility |
|---|---|
| ESP8266 | Wi-Fi, Home Assistant communication, schedules, and requests to feed or move the outlet door |
| Nuvoton ISD91230 (ARM Cortex-M0) | Motor driving, motion timing, door-safety logic, indicators, beeper, and sensor readings |

The ESP8266 sends commands to the ISD91230 motor controller over UART0. The
ISD91230 motor controller performs each operation and reports its result, so
ESPHome does not need to drive the motors or read their sensors directly.

## What you get in Home Assistant

| Category | Entities |
|---|---|
| Controls | Feed now, Open door, Close door, Beep, Refresh status, Restart motor controller |
| Status | Food detected, Power source, Adapter voltage, Battery voltage |
| Configuration | Wi-Fi indicator; enable, time, and amount for each schedule |
| Diagnostics | Dispenser door sensor level, Dispenser wheel sensor level, Adapter ADC, Battery ADC, Manual feed button, Wi-Fi reset button |

`Feed now` requests one complete motor-controller wheel cycle. Petkit describes
this serving as approximately 5 g, but the feeder does not measure food weight.
Actual weight varies with kibble size, density, and hopper level; weigh repeated
servings with your food before relying on the nominal gram conversion.

`Food detected` follows the optical food sensor. The food-level assembly must
be connected and installed in its normal container geometry for calibration.

The door and wheel sensor levels are numeric diagnostics of electrical inputs.
They can change during motion and should not be interpreted as final
open/closed or moving/stopped states. Adapter and battery voltages, raw ADC
counts, and the mains/battery `Power source` state come from the ISD91230 motor
controller.

## Feeding behavior

### Physical button

Holding the manual-feed button keeps the outlet open and dispenses one serving
at a time, with a 100 ms pause after each completed serving. Releasing the
button prevents another serving from starting; a serving already in motion
finishes before the outlet closes. This works without Wi-Fi or Home Assistant.

The GPIO13 input is active-low and uses the internal pull-up. A press must
remain active for 30 ms, while release stops repetition immediately.

### Home Assistant control

A fixed-amount feed follows this sequence:

1. Open the outlet door.
2. Request the required number of counted wheel cycles.
3. Poll progress once per second and wait for completion matching the motion
   command's sequence number.
4. Close the outlet door.

Progress-query replies cannot complete the transaction; completion must match
the command that started the counted motion.

### Safety and recovery

If counted motion times out, the ESP8266 resets the ISD91230 motor controller,
replays its setup, and sends a close-door command. No safe serial stop command
is established, and sending a zero cycle count could wrap to 255 when the
ISD91230 decrements it.

The firmware also sends a close-door command after every ESP8266 or
motor-controller startup handshake. This is a local safety policy; physical
recovery from an already-open outlet at startup and jammed-door conditions
still needs validation. Outstanding hardware checks are in [TODO.md](../TODO.md).

## Real Time Clock

The PCF8563 real-time clock (RTC) has a small internal backup battery. ESPHome
reads the RTC at boot, so the system can recover the time after a power loss
even when the network is unavailable. Home Assistant corrects the clock and
writes the new time to the RTC after the native API reconnects.

## Feeding schedule

The supplied YAML defines four daily schedule slots, all disabled by default.
Each slot has a persistent enable switch, time, and amount in nominal 5 g
increments. The ESP8266 stores and evaluates the schedule, then asks the
ISD91230 motor controller to perform each feed.

The schedule follows these rules:

- A slot runs at most once per local calendar day, including when daylight-saving
  time repeats an hour.
- Missed times are not replayed after a reboot or a large clock correction;
  spring-forward times that do not occur are skipped.
- Short event-loop delays are handled by ESPHome's datetime trigger.
- A slot that finds the feeder busy is skipped rather than queued for later.

Schedule settings and last-run dates survive a normal reboot. With valid time
from the PCF8563, enabled schedules do not depend on Home Assistant, Wi-Fi, the
router, or Internet access. Feeding history for events completed while Home
Assistant is disconnected is not persisted or replayed.

Four slots is a configuration choice, not a hardware limit. To add slots,
extend the schedule entities, the `schedule_last_run` array, and the slot bound
in `run_scheduled_feed` in [`petkit-feeder.yaml`](petkit-feeder.yaml).

## Indicator behavior

The upper Wi-Fi indicator and lower food indicator have separate outputs on
the ISD91230 motor controller:

| Indicator | UART subcommand | GPIO bit |
|---|---|---|
| Upper / Wi-Fi | 1 | 12 |
| Lower / food | 2 | 11 |

Both outputs accept timing commands. Slow blink uses 1000 ms on and 1000 ms
off; fast blink uses 100 ms on and 100 ms off. The 0 ms on / 1000 ms off command
turns the lower indicator off when the food-level assembly is connected.

Automatic food-indicator control is disabled because the mapping from physical
food level to indicator mode remains unresolved. The upper indicator's
permanent-disable and night-mode behavior also remains under investigation.

## The bus protocol

UART0 uses GPIO1 for transmit and GPIO3 for receive, at **115200 baud, 8N1**.
Each frame is between 7 and 19 bytes long:

```text
AA AA <len> <type> <seq> <payload…> <crc_hi> <crc_lo>
```

| Field | Meaning |
|---|---|
| `AA AA` | Start of frame |
| `len` | Total length, including the header and CRC |
| `type` | Command or reply type |
| `seq` | Sequence number echoed in replies |
| `payload` | Command-specific data |
| `crc_hi`, `crc_lo` | CRC-16/CCITT-FALSE, most significant byte first |

The CRC uses polynomial `0x1021`, initial value `0xFFFF`, no reflection, and no
final XOR. Calculate it over the header and body, then append the CRC bytes.
Recomputing it over the complete received frame, including the CRC, yields
zero for a valid frame.

### Commands and replies

| Type | Use |
|---|---|
| `0x01` → `0x02` | Request and return status |
| `0x07` / `0x09` | Open / close outlet door, with the single-byte payload `0x1E` |
| `0x0B` | Control the dispenser wheel or query progress |
| `0x0C` | Return wheel count in byte 0 and counted-motion completion in byte 1 (`1` means complete) |
| `0x0E` | LED or beeper timing: subcommand, on time (2 bytes), off time (2 bytes), count (2 bytes) |

The status payload contains the door, food, and wheel sensor levels, followed
by ADC counts and centivolt values for the adapter and battery channels.

Wheel-control payloads have three distinct meanings:

| Payload | Operation |
|---|---|
| `N 01 01 50` | Request N counted wheel cycles |
| `00 02 01 50` | Query progress without starting motion |
| `FF 01 01 50` | Start stock free-running first-feed motion |

The local firmware uses counted motion. It does not use the stock free-running
first-feed path because that path's entry condition and recovery policy remain
unresolved.

At startup, the component sends parameter packets `0x13`, `0x03`, `0x05`,
`0x04`, `0x06`, and `0x0D` in that order, waiting for an acknowledgement between
each packet. Whether every packet is required remains an open hardware question.

For firmware analysis, see the [ISD91230 firmware map](../research/ISD91230.md).
The [test documentation](tests/README.md) describes protocol checks against
reference captures and stock firmware.

## ESP8266 GPIO map (this feeder)

| GPIO | Function | Used here |
|---|---|---|
| 2 | UART1 TX; board TX1 pad | Diagnostic logs |
| 1 | UART0 TX → ISD91230 RX; board TX0 pad | Bus TX |
| 3 | UART0 RX ← ISD91230 TX; board RX0 pad | Bus RX |
| 15 | ISD91230 RESETB, active-low; ESP8266 boot strap | Motor-controller reset |
| 0 | Wi-Fi/reset button; ESP8266 boot strap | Binary sensor |
| 13 | Manual-feed button | Local feeding |
| 5 / 14 | I²C SDA/SCL to PCF8563 RTC | Battery-backed local time |
| 16 | Deep-sleep wake | Unused |

## Files

| Path | Purpose |
|---|---|
| [`petkit-feeder.yaml`](petkit-feeder.yaml) | Device configuration, entities, and local schedules; the package Kickstart advertises for Device Builder |
| [`petkit-feeder-local.yaml`](petkit-feeder-local.yaml) | Local build of that package with this checkout's API key, fallback-AP password, and time zone |
| [`components/petkit_feeder/`](components/petkit_feeder/) | UART framing, CRC, status parsing, and feeder actions |
| [`secrets.yaml.example`](secrets.yaml.example) | Template for the private Wi-Fi, API, recovery, and time-zone settings of a local build |
| [`tests/`](tests/) | Host-side protocol and firmware checks |

## Build and install

Use the [installation tutorial](../README.md#installation) to prepare the
Python environment and private secrets. From this directory, with that
environment active, build with:

```sh
pip install 'esphome==2026.9.0b1'
esphome compile petkit-feeder-local.yaml
```

ESPHome 2026.9.0b1 is the hardware-tested **beta** used by this configuration.
The encrypted-API build uses 48.2% flash and 46.9% RAM on the 2 MiB module.

Wireless installation uses Kickstart for the **non-OS V2 to eboot V1** layout
transition; see [KICKSTART.md](KICKSTART.md) for the internal steps. After the
final image boots, subsequent updates use ESPHome OTA.

Serial flashing is also a direct installation and recovery option. Before
connecting an adapter, read the [Fresh Element Mini hardware page][mini-hardware]
and the [backup and recovery guide][nonos-hardware]. GPIO15 must be low during
ESP8266 reset because it is a boot strap as well as the motor-controller reset
line. A pristine stock backup requires a full UART flash read before installing
Kickstart.

[nonos-hardware]: https://github.com/wrobelda/petkit-compat-server/blob/main/devices/esp8266/nonos_v2/HARDWARE.md
[mini-hardware]: https://github.com/wrobelda/petkit-compat-server/blob/main/devices/esp8266/nonos_v2/fresh-element-mini/HARDWARE.md
