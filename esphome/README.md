# Petkit Fresh Element Mini — local ESPHome firmware

Replaces the stock cloud firmware on the feeder's **built-in ESP8266** so the
feeder runs fully local through Home Assistant, with no dependence on Petkit's
servers. This uses the feeder's own hardware — no external ESP32, no rewiring
of the motor.

## Why this works

The feeder has two chips:

- **ESP8266** — Wi-Fi and all high-level logic. Stock firmware is Petkit's
  Aliyun-IoT cloud stack (confirmed by strings in the flash dump: `Feedermini`,
  `iotkit-embedded`, `guider.c`, internal codename `D2`). This is the part that
  breaks when Petkit's cloud has an outage.
- **Nuvoton ISD91230 (ARM Cortex-M0)** — a co-processor that does *all* the
  real work: driving the dispense wheel and door motor, and reading the food
  and door IR sensors. The ESP8266 never touches the motor or sensors directly.

The two chips talk over the ESP8266's hardware UART with a small, well-defined
packet protocol. So the local fix is simply: **reflash the ESP8266 with
ESPHome and have ESPHome speak that same protocol to the M0.** The M0 keeps all
of its motor timing and door-safety logic; we just replace who gives it orders.

This is cleaner than the external-ESP32 approach from the Home Assistant forum
(which bypasses the M0 and re-drives the motor from GPIO4/GPIO5) — that method
throws away the M0's door and dispense-completion logic.

## The bus protocol

- **UART0**, GPIO1 (TX) / GPIO3 (RX), **115200 8N1**.
- Frame: `AA AA <len> <type> <seq> <payload…> <crc_hi> <crc_lo>`
  - `len` = total frame length including the `AA AA` header and the 2 CRC bytes.
  - `crc` = **CRC-16/CCITT-FALSE** (poly `0x1021`, init `0xFFFF`, no reflection,
    no final XOR), computed over the whole frame; appended big-endian. A
    recompute over a received frame *including* its CRC bytes yields `0` when
    valid. (Verified against the captured `get status` / `status` packets.)
- Key packet types the component uses:
  - `0x01` get status → `0x02` status reply
    (payload: food-ok, door-fault, unknown, adapter ADC/mV, battery ADC/mV)
  - `0x07`/`0x09` open/close door, payload `duration, strength`
  - `0x0B` dispense, payload `duration, distance, direction, current`
  - `0x0E` blink LED / beep, payload `subcmd, on_ms(2), off_ms(2), count(2)`
  - `0x13/0x03/0x05/0x04/0x06/0x0D` — parameter-setting packets replayed at boot
    to match the stock initialization handshake.

Full protocol notes: <https://github.com/earlynerd/petkit-serial-bus>.

## ESP8266 GPIO map (this feeder)

| GPIO | Function                          | Used here                         |
|------|-----------------------------------|-----------------------------------|
| 1    | UART0 TX → ISD91230 RX            | bus TX                            |
| 3    | UART0 RX ← ISD91230 TX            | bus RX                            |
| 15   | ISD91230 reset (low = running)   | `reset_pin` (recover a wedged M0) |
| 0    | "Wi-Fi reset" button (+boot pin) | binary_sensor                     |
| 13   | "Manual feed" button             | binary_sensor → local feed        |
| 5/14 | I²C SDA/SCL to PCF8563 RTC        | not used                          |
| 16   | deep-sleep wake                  | not used                          |

## Files

- `petkit-feeder.yaml` — the ESPHome device config.
- `components/petkit_feeder/` — the external component that implements the bus
  master (framing, CRC, status parsing, and the feed/door/beep actions).
- `secrets.yaml` — fill in your Wi-Fi credentials.

## Build & flash

```sh
pip install esphome
# First flash must be over serial (see wiring note below):
esphome run petkit-feeder.yaml
```

Verified: `esphome config` passes and the firmware compiles for `esp8266`
(≈37% flash, ≈40% RAM on the 2 MB module).

### Flashing notes (important)

- GPIO1/GPIO3 are shared between the ESP8266 bootloader and the M0. Use the
  debug header on the control board. While serial-flashing the ESP8266, **hold
  the M0 in reset** (GPIO15 high) or power it down so it doesn't talk on the bus
  and corrupt the upload.
- GPIO0 must be pulled low to enter flash mode (standard ESP8266), and GPIO15
  must be low to boot — both already true on this board.
- After the first serial flash, updates are OTA over Wi-Fi.
- **Back up the stock flash first** (`esptool.py read_flash 0 0x200000
  stock.bin`) so you can restore Petkit firmware if ever needed. The repo's
  `flash dumps/petkitesp8266flash.bin` is one such dump for reference.

## What you get in Home Assistant

- **Buttons:** Feed now (× *Feed portions*), Dispense 1 portion, Open door,
  Close door, Beep, Refresh status, Reset motor controller.
- **Number:** Feed portions (how many wheel turns per feed).
- **Binary sensors:** Food level OK, Door fault, Manual feed button, Wi-Fi
  button.
- **Diagnostics:** adapter/battery ADC + millivolt readings from the status
  packet.

The physical manual-feed button dispenses **locally**, even with Wi-Fi down —
which is the whole point.

## Calibration / caveats

- One "portion" replays the stock short dispense (`duration=3, distance=1,
  direction=0, current=16`). Adjust `feed()` in `petkit_feeder.cpp` if your
  portion size differs, or drive `dispense()` directly from a lambda.
- The status packet's voltage fields are exposed raw (ADC counts and the
  firmware's millivolt field). Their exact scaling wasn't nailed down in the
  reverse engineering, so calibrate against a meter before trusting them as
  real volts.
- `reset_pin` polarity assumes active-high (drive high = reset). It's only
  pulsed by the "Reset motor controller" button; leave it out of the config if
  you'd rather not touch GPIO15.
