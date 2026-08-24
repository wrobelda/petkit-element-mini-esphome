# AGENTS.md — Petkit feeder local ESPHome

## Goal
Run the **Petkit Fresh Element Mini** feeder fully local via Home Assistant so
it survives Petkit cloud outages, by reflashing the feeder's own **ESP8266**
with ESPHome. No external ESP32, no motor rewiring.

## Architecture (the load-bearing finding)
The feeder has **two chips**:
- **ESP8266** — Wi-Fi + high-level/cloud logic. Stock firmware is Petkit's
  Aliyun-IoT cloud stack (`Feedermini`, `iotkit-embedded`, codename `D2`). This
  is what breaks during outages.
- **Nuvoton ISD91230 (Cortex-M0)** — co-processor that does **all** motor
  driving, door control, and food/door sensing.

The ESP8266 never touches the motor/sensors directly — it sends commands to the
M0 over UART. So the fix is to reflash the ESP8266 with ESPHome and speak that
bus to the M0, which keeps its safety logic. Cleaner than the HA-forum
external-ESP32 approach that bypasses the M0 and re-drives the motor.

## Bus protocol (framing + CRC — fully verified)
- **UART0**, GPIO1 (TX) / GPIO3 (RX), **115200 8N1**.
- Frame: `AA AA <len> <type> <seq> <payload…> <crc_hi> <crc_lo>`
  - `len` = total frame length incl. `AA AA` header and 2 CRC bytes; range
    **[7, 19]** in real traffic (both firmwares bound it; see disassembly).
  - CRC = **CRC-16/CCITT-FALSE** (poly `0x1021`, init `0xFFFF`, no reflect/xor),
    appended **big-endian**; whole-frame recompute == 0 to validate.
- `seq` is echoed in replies; the M0 does not validate it (earlynerd note,
  consistent with captures). We use one global counter.

### Confirmed FOUR independent ways (none rely on the README prose)
1. **Raw logic captures** (`petkit-serial-bus/CSV export/*.csv`): baud ~115200
   from byte timing; `AA AA` + CRC gate → **350/354 valid frames (98.9%)**.
2. **CRC math**: CCITT-FALSE reproduces every captured packet's CRC.
3. **ISD91230 (Cortex-M0) disassembly** — ARM Thumb via capstone:
   - Receiver @ `0x1994`: `cmp r0,#0xAA` ×2 (`0x19B8`/`0x19C0`) = header;
     `ldrb [r1,#2]` = length; `cmp #6`/`cmp #0x13` = bounds **6..19**;
     `subs r1,#3` + loop = read `length-3` body bytes.
   - CRC @ `0x36A8`: table-less CCITT (the swap/`>>4`/`<<12`/`<<5` bit-hack =
     poly 0x1021 only); seed literal **0xFFFF @ 0x36E8**. A byte-for-byte port
     validates the captured packets.
4. **ESP8266 (Xtensa LX106) disassembly** — Espressif `xtensa-lx106-elf-objdump`
   over `irom0.text` mapped at `0x40200000` (found via the `0xAA` opcode
   signature, NOT a string). This confirms **framing only**, not CRC:
   - Frame parser @ `0x40253d90`: `bgeui a4,7` (need ≥7 bytes); `movi a2,170` +
     `beq` ×2 (`0x40253df7`/`0x40253e29`) = `AA AA` header; `addi +2` + `l8ui` =
     length byte; `movi 12` + `bgeu` = bounds **7..19**; then a 500-byte
     ring-buffer split copy. Confirms the `OVERHEAD=7`/`MAX_FRAME=19` used in
     `petkit_protocol.h`.
   - This routine is the **feeder** command parser (not MQTT): it references
     `_DOOR_OPEN` and `motor open:...%d` strings via `l32r`.
   - **CRC scope:** the ESP8266's own CRC routine was **not** located, so CRC is
     proven from the captures and the M0 firmware only — *not* from both
     disassemblies. Do not state "CRC proven from both firmwares."
   - UART0 vs UART1: the firmware references `0x60000000` (UART0) and never
     `0x60000F00` (UART1), consistent with the bus being on UART0 per the
     wiring/captures — but that count alone does not prove this parser reads it.

**CORRECTION to an earlier claim:** the string `decodePacket error,rc = %d` is
in the bundled Aliyun **MQTT** code (it sits between `mqtt read error` and
`mqtt read buffer is too short`), NOT the Petkit UART decoder. An earlier note
attributed it to the serial protocol; that was wrong. The real UART parser is
`0x40253d90` above.

Reproduce: `esphome/tests/verify_firmware.py` asserts these opcodes in both
dumps and re-derives the CRC; `esphome/tests/test_captures.py` is the capture
regression. Toolchain: capstone `CS_ARCH_ARM`+`CS_MODE_THUMB` (M0);
`xtensa-lx106-elf-objdump` from the PlatformIO ESP8266 toolchain (ESP8266).

## Command payloads — grounded vs assumed
Framing/CRC are proven. Command *semantics* are only as good as the evidence:

| Item | Status | Basis |
|------|--------|-------|
| Door cmd 0x07/0x09 payload = single byte `0x1E` | **grounded** | every captured stock door cmd is `AA AA 08 07/09 <seq> 1E <crc>` |
| Dispense 0x0B repeated command `00 02 01 50` exists | **grounded** | captured stock dispense stream |
| Dispense pacing: wait for the seq-matched `0x0C` completion | **grounded** | captured timing; each command's completion echoes its seq |
| Boot config packets 0x13,0x03,0x05,0x04,0x06,0x0D + payloads/order | **grounded** | captured factory boot; M0 acks each (same-type, len 8, `01`) |
| Not sending 0x11 at boot | **grounded** | 0x11 appears only in the earlynerd *test script*, not stock boot |
| 0x0E blink/beep payload = subcmd,on,off,count | **grounded** | boot-log 0x0E frames match this layout |
| Omitting the five boot 0x0E LED/beep packets | assumed-safe | cosmetic startup animation; omitted to avoid beeping |
| Whether every init packet is *required* for feed/door to work | **UNVERIFIED** | needs hardware |
| status byte0 "food ok" polarity | **UNVERIFIED** | all captures show `0x00`; polarity from README only |
| status byte1 meaning ("door fault") | **contradicted** | idle AC-powered boots show `0x01` constantly — not a fault; exposed as neutral flag |
| status 4×16-bit fields: adapter-vs-battery grouping | weak evidence | adapter pair reads ~0 on battery; ADC/mV/units/scale UNVERIFIED (exposed raw, no units) |
| reset_pin GPIO15 active-high (low = run) | reasoned, unverified | ESP boot needs GPIO15 low AND M0 runs from power-on ⇒ low=run; left commented out in YAML |
| "1 dispense step = a food portion" | **contradicted/unproven** | stock feed is a longer transaction (`FF 01 01 50` / `01 01 01 50` lead-in, then repeated `00 02 01 50` steps with *immediate zero-filled* completions, vs a later *non-zero* completion for the lead-in) — so `00 02 01 50` is likely an incremental motor step/probe, not a portion. UI now labels it "Dispense steps (raw)". |

**Nothing here has run on the physical feeder.** The command layer needs
hardware validation before trusting it — especially anything that moves the
door or dispenses.

## Review fixes applied (from Codex Sol reviews)
Round 1:
- Door payload corrected to single byte `0x1E` (was a wrong 2-byte `dur,str`).
- Dispense payload corrected to `00 02 01 50` (was `03 01 00 10`, which matched
  no capture — it came from the test script).
- Dispense paced (one at a time), RX rewritten as a state machine, boot
  handshake made reactive (advance on the M0's ack), status decoding
  de-overclaimed, `reboot_timeout: 0s`, length bounds `[7,19]`.

Round 2:
- **"Portion" overclaim removed:** `feed()` counts raw *dispense steps*, not
  food portions; the number entity is "Dispense steps (raw, uncalibrated)".
- **Dispense queue is a real ring buffer** (`PayloadQueue`, `petkit_framer.h`):
  distinct raw commands enqueued back-to-back are now sent in order, each with
  its own payload (was a single shared slot — every entry used the newest).
- **Completions matched by sequence number:** only the `0x0C` whose seq equals
  the in-flight dispense's seq releases the queue, so a late completion from a
  timed-out command can't release the next one early. Timeout raised to 4 s.
- **RX + queue extracted to `petkit_framer.h`** (dependency-free) and unit
  tested (`test_framer.cpp`): resync, split frames, FIFO order, capacity.
- **`reset_mcu()` clears `init_waiting_ack_`/`init_deadline_`** (stale handshake
  state) plus the queue and assembler.
- **Verifier/README overclaims tightened:** the ESP8266 disassembly proves
  *framing* only (CRC routine not located → CRC from captures + M0 only); the
  extra parser opcodes (both `beq`, `addi +2`, `movi 12`, `bgeu`) are now
  asserted, and the parser's feeder-string `l32r`s (`_DOOR_OPEN`, `motor open`)
  are checked to tie it to feeder command handling.

Still open (needs hardware): the step→food-quantity mapping, whether the boot
config packets are required, status field polarities/units, `reset_pin` polarity.

## UART-free installation research

The stock ESP8266 firmware already contains the pieces needed for a possible
wireless takeover, but no working Petkit-specific installer exists yet:

- Provisioning supports ESP-Touch, AirKiss, and a SoftAP TCP/JSON server. The
  local bind schema includes a `server` field, so the first path to investigate
  is whether SoftAP provisioning can redirect the feeder to a controlled API.
- The feeder performs device-driven HTTP(S) OTA. An OTA-check response contains
  `firmwareId`, `version`, `details`, `file`, and `digest`; the downloader uses
  HTTP range requests, writes the inactive SDK user-bin slot, checks an image
  CRC/digest, then calls `system_upgrade_reboot()`.
- Static strings show image integrity checks (`upgrade crc check failed`,
  `[img_crc]... [flash_crc]`) but no Petkit firmware-signature field or key has
  been identified. TLS support is present, so server authentication and image
  authentication must not be conflated; disassemble the OTA-check and final
  verification call paths before assuming arbitrary images are accepted.
- In normal provisioned station mode, the feeder at `192.0.2.64` is reachable
  but refuses connections on the 1,000 common TCP ports. This is consistent
  with the firmware stopping its SoftAP bind server after provisioning.
- A network API may reveal the vendor OTA URL and permit downloading an official
  stock image, but there is no current evidence that it can read arbitrary flash
  back. A literal backup still likely requires the ROM UART loader or a temporary
  OTA flash-loader application.

Relevant prior work and what carries over:

- **Tuya-Convert** creates a fake provisioning/update environment for ESP8266
  devices, installs a small flash loader, backs up stock firmware, then writes a
  full alternative image. Its protocol exploit is Tuya-specific, but its staged
  loader and recovery design are directly reusable:
  <https://github.com/ct-Open-Source/tuya-convert>.
- **SonOTA / Espressif2Arduino** intercepted the factory Sonoff OTA flow and
  used staged images to cross from a vendor non-OS SDK image to an Arduino image.
  This is the closest architectural analogue for Petkit:
  <https://github.com/mirko/SonOTA>.
- Espressif's native non-OS FOTA uses paired `user1.bin`/`user2.bin` images; the
  device selects the inactive slot, downloads it, and changes boot selection.
  A normal ESPHome serial-flash binary is not automatically a valid Petkit
  user-bin, so a stock-compatible transition image may be required:
  <https://www.espressif.com/sites/default/files/99c-esp8266_ota_upgrade_en_v1.6.pdf>.
- A published Xiaomi/FurryTail ESP8266 feeder incident combined weak APIs and
  firmware-update weaknesses, showing that this attack class has reached pet
  feeders, although it is not the same Petkit firmware:
  <https://www.parksassociates.com/bento/uploads/file/connsummit/2020/materials/firedome/XiaomiPetFeedersFleetHack.pdf>.
- Existing Petkit API reverse engineering reports that much app↔Petkit API
  traffic used plain HTTP and documents Feeder Mini/cloud calls. It is useful
  for authentication and endpoint conventions, but does not yet document this
  device's OTA route: <https://github.com/morganpartee/pyPetKit>.

Expected cloud traffic topology from firmware strings:

- The feeder maintains an **outbound** Aliyun MQTT connection for asynchronous
  commands/state (`iot-as-mqtt.*.aliyuncs.com`, `/%s/%s/update`, `/get`, and
  `/shadow`). The app talks to Petkit's cloud; the cloud publishes the command
  over that already-established connection, so no inbound port is needed.
- It also makes outbound Petkit HTTP(S) requests for signup, server information,
  heartbeat, feed lists/reports, device information, and OTA check/start/status.
- A router-side capture should therefore record the full experiment, including
  idle traffic and one app-triggered feed. Use a capture file, not terminal-only
  output, so DNS, TCP timing, TLS SNI/certificates, HTTP, and MQTT can be examined:

  ```sh
  tcpdump -i <lan-bridge> -nn -s0 -w /tmp/petkit.pcap host 192.0.2.64
  ```

  Start before rebooting the feeder, wait for it to reconnect, trigger exactly
  one distinctive app action, wait another minute, then stop the capture. If the
  router offloads or bridges traffic outside the CPU, capture on both the LAN
  bridge and WAN interface or temporarily place the feeder behind a controlled
  access point. Do not publish the PCAP: provisioning and HTTP traffic may
  contain Wi-Fi credentials, device secrets, account tokens, or location data.

## ESP8266 GPIO map
| GPIO | Function | Used |
|---|---|---|
| 1 | UART0 TX → M0 RX | bus TX |
| 3 | UART0 RX ← M0 TX | bus RX |
| 15 | ISD91230 reset (low = running, reasoned) | `reset_pin` (opt-in, commented) |
| 0 | Wi-Fi-reset button (+boot pin) | binary_sensor |
| 13 | Manual-feed button | binary_sensor → local feed |
| 5/14 | I²C SDA/SCL → PCF8563 RTC | unused |
| 16 | deep-sleep wake | unused |

## Deliverable & tests
- `esphome/components/petkit_feeder/` — `petkit_protocol.h` (pure framing/CRC),
  `petkit_framer.h` (pure RX assembler + dispense ring buffer), both shared by
  firmware and tests; `petkit_feeder.{h,cpp}` (bus master); `__init__.py`.
- `esphome/petkit-feeder.yaml`, `esphome/README.md`.
- `esphome/tests/` (`./run_tests.sh`): `test_protocol.cpp` (native C++ on the
  shipping code, vectors = captured frames), `test_captures.py` (capture
  regression), `verify_firmware.py` (dual-chip instruction-level proof).
- Verified: `esphome config` passes; compiles for esp8266 (~37% flash, ~40% RAM).

## Flashing gotchas
- GPIO1/GPIO3 are shared with the M0 — flash via the debug header and hold the
  M0 in reset (GPIO15 high) or power it down during serial upload.
- First flash is serial; then OTA. Back up stock flash first
  (`esptool.py read_flash 0 0x200000 stock.bin`); reference dump is in
  `petkit-serial-bus/flash dumps/petkitesp8266flash.bin`.
