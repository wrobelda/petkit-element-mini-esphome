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
- Key packet types the component uses (payloads taken from captured stock
  traffic, not the README prose — see AGENTS.md for the grounded/assumed audit):
  - `0x01` get status → `0x02` status reply. Payload: dispenser-door sensor,
    optical food input, dispenser-wheel sensor, then the ADC count and
    centivolt value for the adapter and battery channels.
  - `0x07`/`0x09` open/close door, payload = **single byte `0x1E`** (stock).
  - `0x0B` wheel control: `FF 01 01 50` starts the free-running first-feed
    motion, `N 01 01 50` requests N counted wheel cycles, and `00 02 01 50`
    queries progress without starting motion.
  - `0x0C` wheel result: byte 0 is the wheel/sensor count and byte 1 is one
    when counted motion has completed.
  - `0x0E` blink LED / beep, payload `subcmd, on_ms(2), off_ms(2), count(2)`.
  - `0x13/0x03/0x05/0x04/0x06/0x0D` — parameter packets replayed at boot in the
    captured order; the component waits for the M0's ack between each.

Full protocol notes: <https://github.com/earlynerd/petkit-serial-bus>.

## ESP8266 GPIO map (this feeder)

| GPIO | Function                          | Used here                         |
|------|-----------------------------------|-----------------------------------|
| 2    | ESP8266 UART1 TX                   | diagnostic logs                   |
| 1    | UART0 TX → ISD91230 RX            | bus TX                            |
| 3    | UART0 RX ← ISD91230 TX            | bus RX                            |
| 15   | ISD91230 RESETB (active-low)     | motor-controller reset            |
| 0    | "Wi-Fi reset" button (+boot pin) | binary_sensor                     |
| 13   | "Manual feed" button             | binary_sensor → local feed        |
| 5/14 | I²C SDA/SCL to PCF8563 RTC        | battery-backed local time         |
| 16   | deep-sleep wake                  | not used                          |

## Real Time Clock

The feeder has a PCF8563 RTC backed by a small internal battery. ESPHome reads
it at boot, so the system clock survives a temporary power loss even when the
network remains unavailable. Home Assistant corrects the clock and writes the
new time back to the RTC after the native API reconnects.

## Feeding schedule

Four daily schedule slots run entirely on the feeder. Each slot has a
persistent enable switch, time, and amount in 5 g increments. All slots are
disabled by default. A slot runs at most once per local calendar day, including
when daylight-saving time repeats an hour. A skipped time is not run later
after a reboot or a large clock correction, because delayed feeding can be
unsafe. Spring-forward times that do not occur are also skipped. Short event-
loop delays are handled by ESPHome's datetime trigger.

Schedule configuration and its last-run dates survive a normal reboot. The
PCF8563 keeps the clock while external power is absent, so enabled schedules do
not depend on Home Assistant, Wi-Fi, the router, or Internet access. Feeding
history for events completed while Home Assistant is disconnected is not yet
persisted or replayed.

This matches the stock division of responsibility. The stock ESP8266 downloads,
stores, and evaluates the feed list against its local clock, then asks the
ISD91230 to perform each dispense transaction. There is no evidence that the
ISD91230 stores the schedule itself.

## Files

- `petkit-feeder.yaml` — the ESPHome device config.
- `components/petkit_feeder/` — the external component that implements the bus
  master (framing, CRC, status parsing, and the feed/door/beep actions).
- `secrets.yaml` — fill in your Wi-Fi credentials.

## Relationship to the Fresh Element Solo

The Mini should have a separate ESPHome Devices page, not be presented as a
Solo board revision. Both products are Petkit feeders, but their control
architectures differ: the Solo page documents an ESP32 that directly drives
motor and sensor GPIOs, while this Mini has an ESP8266 that sends commands to a
Nuvoton ISD91230 over UART. Configuration and safety assumptions therefore do
not carry between the two models. The published Solo configuration handles its
manual button with a single `on_press` action; it has no `on_release` action or
held-button repetition, so it is not prior art for the Mini's press-and-hold
behavior.

## Build and install

```sh
pip install 'esphome==2026.9.0b1'
esphome compile petkit-feeder.yaml
```

The stock firmware can install the minimal Kickstart migration bridge through
its own OTA client. Kickstart then provides an authenticated installer for the
complete ESPHome factory image, which replaces the paired non-OS SDK slot
layout with the normal Arduino/eboot layout. This full stock→lower-slot
Kickstart→upper-slot Kickstart→final ESPHome sequence is hardware-verified;
see [KICKSTART.md](KICKSTART.md).

Serial flashing remains the recovery path and a direct installation option.
After the final image boots, subsequent updates use ordinary ESPHome OTA.

Verified with ESPHome 2026.9.0b1: `esphome config` passes and the encrypted-API
firmware compiles for `esp8266` using 48.2% flash and 46.9% RAM on the 2 MB
module.

### Flashing notes (important)

- GPIO1/GPIO3 are shared with the M0, but the physical feeder accepted an
  esptool stub and slot write through TX0/RX0/GND without M0 isolation. Do not
  hold GPIO15 high during ESP8266 reset because it is also a required low boot
  strap. See the compatibility server's
  [non-OS V2 hardware and recovery guide][nonos-hardware] and
  [Fresh Element Mini hardware page][mini-hardware].
- GPIO0 must be pulled low to enter flash mode (standard ESP8266), and GPIO15
  must be low to boot — both already true on this board.
- After the final ESPHome image is installed, updates are OTA over Wi-Fi.
- The board TX0 pad connects only to GPIO1/UART0 TX, RX0 connects only to
  GPIO3/UART0 RX, and TX1 connects only to GPIO2/UART1 TX. There is no RX1
  pad. These connections were verified with an unpowered continuity test.
- **Back up the stock flash first** (`esptool.py read_flash 0 0x200000
  stock.bin`) so you can restore Petkit firmware if ever needed. The optional
  firmware-verification tests can use a privately supplied reference dump; no
  stock dump is distributed by this project. See the [test
  documentation](tests/README.md).

[nonos-hardware]: https://github.com/wrobelda/petkit-compat-server/blob/main/devices/esp8266/nonos_v2/HARDWARE.md
[mini-hardware]: https://github.com/wrobelda/petkit-compat-server/blob/main/devices/esp8266/nonos_v2/fresh-element-mini/HARDWARE.md

## What you get in Home Assistant

- **Buttons:** Feed now, Open door, Close door, Beep, Refresh status. Feed now
  requests one complete M0 wheel cycle, which corresponds to the stock nominal
  amount of approximately 5 g.
- **Binary sensors:** Food detected, Dispenser door sensor, Dispenser wheel sensor,
  Manual feed button, and Wi-Fi button.
- **Switch:** Wi-Fi indicator; its setting survives reboots. Disabling it sends
  the stock mode-0 timing.
- **Power sensors:** Adapter voltage and Battery voltage, plus their diagnostic
  ADC counts.

The feeder has two separate LEDs. The upper/Wi-Fi LED uses subcommand 1 and M0
GPIO bit 12, while the lower/food LED uses subcommand 2 and M0 GPIO bit 11.
Both LEDs accept the same timing modes, but they do not share an output. The
stock mode encoded as 0 ms on / 1000 ms off turns the lower LED off when the
food-level assembly is connected. Stock uses a 1000 ms on /
1000 ms off cycle for its slow blink and a 100 ms on / 100 ms off cycle for its
fast blink. Automatic food-indicator control remains disabled until the lower
indicator's state policy is verified
against the physical food level.

The physical manual-feed button works **locally**, even with Wi-Fi down. Holding
it keeps the outlet open and dispenses one approximately-5 g serving at a time,
with a 100 ms pause between completed servings. Releasing it prevents another
serving from starting; a serving already in motion finishes before the outlet
closes.
GPIO13 is active-low and uses the internal pull-up, matching the stock pin
configuration. It measures about 3.2 V released and 0 V pressed. Both input
edges are handled asymmetrically: a press must remain active for 30 ms, while
release stops manual repetition immediately.

The Home Assistant action is a fixed-amount transaction. It opens the outlet,
requests the selected number of counted cycles, polls once per second, waits
for the sequence-matched completion, then closes the outlet. Live 1, 2, and 3-serving
tests advanced the M0's settled wheel count by exactly 1, 2, and 3 and
completed the door sequence. Food weight was not measured, so the nominal gram
conversion still needs calibration.

The dedicated `petkit_manual_feed.sal` recording from
`earlynerd/petkit-serial-bus` contains status and indicator traffic but no door
or dispenser packets, so it does not establish the stock serial sequence for a
held button. Stock firmware disassembly establishes the repeat timing through a
separate path: the GPIO13 handler creates a manual feed with the sentinel amount
255, while the matching completion branch waits 10 FreeRTOS ticks before it
queues the next serving. SDK 2.1.1 runs at 100 ticks per second, so the local
implementation waits the same 100 ms after each sequence-matched M0 completion.

If counted motion times out, the ESP resets the M0, replays its setup, and then
closes the door. No safe serial stop command is known. A zero cycle count is
not used as a stop because the M0 can decrement it to 255.
The firmware sends a close command after every ESP or M0 startup handshake.
This is a local fail-safe; normal stock cold-boot traces do not send a close
command when the mechanism is healthy. A deliberate open-outlet reboot test is
still required to confirm the local command is accepted in that condition.

Stock has a separate fault-recovery path. Its state-report processing tests two
door-error bits in the system mask and enqueues distinct repair events for open
and close failures. The same firmware contains illegal-open detection,
open/close current and count thresholds, and a multi-step door-repair state
machine. The disassembly does not yet show whether simply finding the outlet
open after an ESP-only reboot raises one of those fault bits.

## Calibration / caveats — READ THIS

Framing is proven from both firmwares; the CRC is proven from bus traffic and
the M0 firmware (the ESP8266's CRC routine was not located). The physical
feeder now boots ESPHome, completes M0 setup, receives status, and operates its
indicators and beeper. Its complete 1, 2, and 3-serving motion transactions
have also run successfully on the feeder.
The full grounded-vs-assumed audit is in `../AGENTS.md`. Highlights:

- `00 02 01 50` is a progress query, not a motor step. M0 disassembly shows
  that `FF 01 01 50` is free-running motion and `N 01 01 50` requests N
  sensor-counted wheel cycles. Stock uses the free-running command in a
  distinct first-feed path; ordinary fixed-amount feeds must not enter that
  path unconditionally.
- The product manual defines one serving as approximately 5 g, while its
  actual weight varies with kibble size, density, and hopper level. The control
  requests the same M0 cycle unit as stock; it does not present the nominal
  conversion as measured grams.
- The M0 reports an averaged ADC count and a centivolt value for both the mains
  adapter and battery pack. Its conversion is
  `ADC × 3.3 / 4095 / 0.3197278911564626 × 100`. ESPHome exposes the
  converted values in volts and keeps the ADC counts as diagnostic sensors.
  Hardware testing measured the adapter as 6.09 V on mains and 0.00 V on
  batteries. The installed batteries measured 5.77 V without load and 5.14 V
  while powering the feeder. The diagnostic `Power source` entity reports
  `Mains` when the M0 reports a nonzero adapter voltage, otherwise it reports
  `Battery`.
- The M0 status frame carries three digital inputs. Byte 0 is PB8 and belongs
  to the outlet-door motion state machine. Byte 1 is PB6, the optical food-level
  input sampled after a 5 ms emitter pulse. Byte 2 is PB7 and belongs to the
  dispenser-wheel state machine. Home Assistant exposes their raw electrical
  states. The assembled empty feeder reports the food input as zero, while
  covering its optical path changes it to one after the M0's normal reporting
  delay. The dispenser-door polarity still needs calibration. Live feeding
  confirms that the wheel input changes during a serving and returns to its
  idle state.
- GPIO15 controls the ISD91230's active-low reset. It measures 3.3 V while the
  M0 is running; an ESPHome low pulse produced the M0 startup sound and was
  followed by a fresh M0 status frame. The supplied YAML enables this reset
  control and exposes **Restart motor controller** as a diagnostic button.
