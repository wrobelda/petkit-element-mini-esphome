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

The stock ESP8266 also owns scheduled feeding. Its image contains the
`/feedermini/dev_feed_get` client, persists feed-list JSON fields including
`items`, `repeats`, `nextTick`, `timestamp`, and `amount`, and queues scheduled
feeds against its RTC-derived time. The observed M0 bus has immediate dispense
commands but no schedule-table transfer. The local firmware should preserve
that division: the ESP8266 stores and evaluates schedules, while the M0 handles
the resulting motor transaction and safety checks.

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
1. **Raw logic captures** from
   [`earlynerd/petkit-serial-bus`](https://github.com/earlynerd/petkit-serial-bus):
   baud ~115200
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
     Physical testing separately confirms that stock diagnostic output is on
     the board's TX1 pad. The missing UART1 MMIO literal therefore cannot show
     that stock firmware does not use TX1; SDK code or an indirect register
     address may implement that logging path.

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
| Dispense `FF 01 01 50` = free-running first-feed motion | **grounded** | M0 type-0x0B handler stores payload byte0 as its cycle counter and does not decrement `0xFF`; stock strings distinguish `FEED_FIRST`/`first feed, free num` from the normal `start feed, ask num` path |
| Dispense `N 01 01 50` = N counted wheel cycles | **grounded** | M0 stores byte0 as its cycle counter and decrements it once per sensor transition |
| Dispense `00 02 01 50` = progress query | **grounded** | M0 has a separate payload-byte1 == 2 branch which sends an immediate type-0x0C reply without commanding motion |
| Type-0x0C byte0 = wheel count; byte1 = motion complete | **grounded** | M0 builder reads byte0 from `0x20000050` and emits byte1=1 when the remaining cycle count is zero |
| Stock first-feed path timing | **trace-derived** | one trace sends the counted command 0.66 s after the free-running command with no intervening query; another queries counts 0,1,2,2 before sending it, so this is not a fixed ordinary-feed preamble |
| Boot config packets 0x13,0x03,0x05,0x04,0x06,0x0D + payloads/order | **grounded** | captured factory boot; M0 acks each (same-type, len 8, `01`) |
| Not sending 0x11 at boot | **grounded** | 0x11 appears only in the earlynerd *test script*, not stock boot |
| 0x0E blink/beep payload = subcmd,on,off,count | **grounded** | boot-log 0x0E frames match this layout |
| Stock 0x0E connecting indication and startup beep | **hardware-verified** | the M0 acknowledged them; the feeder beeped and blinked |
| LED mode encoder | **grounded** | the stock ESP8266 function at user2 VMA `0x4026b628` accepts mode 0=off timing (`0000/03E8`), 1=solid (`03E8/0000`), 3=fast blink (`0064/0064`), or 4=slow blink (`03E8/03E8`), then rejects every other value; it uses the same encoder for upper/Wi-Fi LED subcommand 1 and lower/food LED subcommand 2 |
| Upper/Wi-Fi LED policy | **partly grounded** | stock packets and call sites use subcommand 1 with off timing, solid, slow blink, and fast blink; permanent disable and night-mode policy remain unresolved |
| Lower/food LED policy | **partly grounded** | stock packets use subcommand 2 with off timing, solid, and slow blink, while the disassembly also contains a fast-blink call; mode 0 turns this LED off with the food-level assembly connected, while the exact status-to-mode branches remain unresolved |
| Whether every init packet is *required* for feed/door to work | **UNVERIFIED** | needs hardware |
| Status byte0 | **grounded** | M0 status builder reads PB8; the type-0x07 outlet-door state machine uses PB8. Exact active polarity is unresolved |
| Status byte1 | **hardware-verified** | M0 status builder reads PB6 after PB5 excites the optical assembly for 5 ms; neither motor state machine uses PB6. The assembled empty feeder reports 0, while covering the optical path changes it to 1 after the normal reporting delay |
| Status byte2 | **grounded** | M0 status builder reads PB7; the type-0x0B dispenser state machine counts PB7 transitions. Live feeding changes it from its idle state and back |
| Status power fields | **hardware-verified** | M0 averages ten ADC samples for adapter channel 6 and battery channel 7, then reports each ADC count followed by centivolts calculated as `ADC × 3.3 / 4095 / 0.3197278911564626 × 100`; adapter reads `609` centivolts on mains and `0` on batteries, while the battery reads `577` centivolts unloaded and `514` under battery-only load |
| GPIO15 → ISD91230 reset | **hardware-verified** | GPIO15 measures 3.3 V while the M0 runs; an ESPHome active-low pulse produces the M0 startup sound and a fresh status frame |
| One Petkit serving = one counted M0 wheel cycle | **hardware-verified** | live requests for 1, 2, and 3 servings advanced the settled M0 wheel count by exactly 1, 2, and 3; the manual defines a serving as approximately 5 g, but no food was weighed |
| Physical manual-feed behavior | **hardware-verified** | GPIO13 is active-low with the stock internal pull-up: it measures about 3.2 V released and 0 V pressed; while held, stock dispenses discrete approximately-5 g servings with short pauses and keeps the outlet open; release stops repetition and closes the outlet |
| Stock manual-feed bus sequence | **UNVERIFIED** | earlynerd's `petkit_manual_feed.sal` contains no door or dispenser packets; it only contains status polls and indicator changes around the apparent interaction |
| Stock manual-feed repeat delay | **grounded** | GPIO event 7 creates a feed-list entry with amount 255 at `0x4025B5C0`; the amount-255 completion branch at `0x4025EEAD` calls `vTaskDelay(10)` before queuing the next feed event. SDK 2.1.1 uses 100 ticks/s, so the delay is 100 ms |

The Fresh Element Mini manual defines one serving as approximately 5 g and
warns that its actual weight varies with kibble size, density, and hopper
level. The local implementation reproduces stock's normal counted motion,
completion polling, and close sequence. It does not claim
to measure grams. Validate requests for 1, 2, and 3 servings on the feeder
before relying on the nominal conversion.

The local manual-button path requests one counted serving at a time and waits
for its sequence-matched completion, then waits the stock firmware's 100 ms
before requesting the next. It leaves the outlet open between servings and
stops requesting more as soon as GPIO13 is released. The exact stock serial
sequence is not available in the third-party trace.

Two of the other project's cold-boot traces show that the initial visual effect
is a 50% duty-cycle blink, not a gradual fade: the upper/Wi-Fi LED receives
mode 4 (`1000 ms` on, `1000 ms` off) before it becomes solid after connection.
The lower/food LED independently changes among off, solid, and mode 4. The
optical input cannot be mapped directly to an indicator mode without
reconstructing the stock ESP8266 policy. The M0 drives the LEDs through
distinct GPIO bits: bit 12
for upper/Wi-Fi and bit 11 for lower/food. Lower mode 0 turns the indicator off
when the food-level assembly is connected. Tests performed with that assembly
unplugged are invalid for indicator and food-sensor behavior. The upper/Wi-Fi
mode-0 behavior and the stock night-mode policy remain unresolved.

The physical feeder now boots ESPHome, completes the M0 setup, exchanges valid
status requests and replies, and operates its indicators and beeper. Live 1,
2, and 3-serving transactions opened the door, advanced the wheel by the
requested count, waited for the sequence-matched motion completion, and closed
the door. Food-sensor calibration requires the optical food-level assembly to
be connected and installed in its normal container geometry.

## Implementation corrections

- Door commands use the stock one-byte payload `0x1E`.
- The RX assembler enforces the M0's frame bounds and resynchronizes after bad
  input.
- Feed completion is matched to the counted-motion sequence number; query
  replies cannot finish a transaction.
- If counted motion times out, the ESP resets the M0 before closing the door.
  No safe serial stop command is established, and sending a zero cycle count
  could wrap to 255 when the M0 decrements it.
- An M0 reset discards the active feed state and restarts the boot handshake.
- Completing an ESP or M0 startup handshake sends a close-door command. This is
  a local safety policy, not a reproduced stock boot action: healthy stock
  cold-boot captures contain no type-0x09 command. Its behavior when the outlet
  is already open still needs a deliberate reboot test.
- The ESP8266 disassembly proves framing only. CRC is proven by the captures
  and M0 firmware, because the ESP8266 CRC routine has not been located.

The stock user2 state-report processing path checks the system-error bitmask
with the mask getter at `0x402511A4`. At `0x4026768C` it tests `0x400`, logs a
door self-check failure, and enqueues repair event 7. At `0x402676B5` it tests
`0x200`, logs the other door self-check failure, and enqueues repair event 8.
Nearby strings and branches cover illegal opening, open/close error states,
current/count thresholds, and a multi-step door-repair sequence. This proves
that stock reacts to door faults outside a requested feed transaction. It does
not yet prove that a physically open outlet after an ESP reboot raises either
mask bit; test that case before replacing the local startup-close policy with a
state-dependent one.

Still open (needs hardware): weigh repeated serving samples, determine whether
all boot config packets are required, and resolve the remaining digital-input
polarities.

## Wireless installation

The Petkit-specific wireless installer is hardware-verified end to end. The
complete user procedure is in [`README.md`](README.md), while image construction,
slot relocation, validation, and layout migration are documented in
[`esphome/KICKSTART.md`](esphome/KICKSTART.md). The stock ESP8266 firmware
provides the required entry points:

- Provisioning supports ESP-Touch, AirKiss, and a SoftAP TCP/JSON server. The
  local bind schema includes a `server` field, so the first path to investigate
  is whether SoftAP provisioning can redirect the feeder to a controlled API.
- The feeder performs device-driven HTTP(S) OTA. An OTA-check response contains
  `firmwareId`, `version`, `details`, `file`, and `digest`; the downloader uses
  HTTP range requests, writes the inactive SDK user-bin slot, checks an image
  CRC/digest, then calls `system_upgrade_reboot()`.
- Petkit's stock ESP8266 OTA client checks the V2 image checksum and appended
  SDK CRC32. It accepted and booted the generated Kickstart image; no
  additional Petkit firmware signature was required in the tested path.
- In normal provisioned station mode, the tested feeder is reachable
  but refuses connections on the 1,000 common TCP ports. This is consistent
  with the firmware stopping its SoftAP bind server after provisioning.
- The transition image can read the complete flash through its authenticated
  recovery interface. A pristine pre-transition backup still requires the ROM
  UART loader because stock OTA has already replaced one application slot by
  the time Kickstart runs.

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
- [`ottoherdy/petkit-fresh-element-mini-esphome`](https://github.com/ottoherdy/petkit-fresh-element-mini-esphome)
  is an independent pure-YAML ESPHome implementation for the same feeder. Its
  door and food status mapping agrees with the M0 disassembly here. Its `0x11`
  boot requirement and `03 01 00 10` dispense payload come from the earlynerd
  test script or local experiments rather than the stock traces, so do not copy
  those claims over the trace- and disassembly-backed behavior in this project.

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
  FEEDER_ADDRESS=192.0.2.64
  tcpdump -i <lan-bridge> -nn -s0 -w /tmp/petkit.pcap host "$FEEDER_ADDRESS"
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
| 1 | UART0 TX → M0 RX; board TX0 pad | bus TX |
| 3 | UART0 RX ← M0 TX; board RX0 pad | bus RX |
| 2 | UART1 TX; board TX1 pad | logger TX |
| 15 | ISD91230 RESETB; active-low, 3.3 V while running | `reset_pin` |
| 0 | Wi-Fi-reset button (+boot pin) | binary_sensor |
| 13 | Manual-feed button | binary_sensor → local feed |
| 5/14 | I²C SDA/SCL → PCF8563 RTC | battery-backed local time |
| 16 | deep-sleep wake | unused |

## Deliverable & tests
- `esphome/components/petkit_feeder/` — `petkit_protocol.h` (pure framing/CRC),
  `petkit_framer.h` (pure RX assembler), both shared by
  firmware and tests; `petkit_feeder.{h,cpp}` (bus master); `__init__.py`.
- `esphome/petkit-feeder.yaml`, `esphome/README.md`.
- `esphome/tests/` (`./run_tests.sh`): `test_protocol.cpp` (native C++ on the
  shipping code, vectors = captured frames), `test_captures.py` (capture
  regression), `verify_firmware.py` (dual-chip instruction-level proof).
- Verified: `esphome config` passes and the ESP8266 build fits the 2 MiB module;
  keep current resource measurements in `esphome/README.md` rather than
  duplicating them here.

## Flashing gotchas

The wireless transition is hardware-verified end to end. Petkit stock user2
installed Kickstart into inactive user1 through the stock OTA client;
Kickstart validated and copied itself to user2, booted that copy, accepted the
complete ESPHome factory image, wrote the application before eboot, and booted
the final feeder firmware. A subsequent standard ESPHome OTA update also
succeeded. Home Assistant reused the existing Kickstart device entry and
renamed it to Petkit Feeder when both configurations used the same native API
encryption key. A plaintext final API did not work with the encrypted
connection settings retained from Kickstart.

ESPAsyncWebServer handlers run in the ESP8266 SDK/lwIP context. Calling
Arduino `yield()` while validating a V2 image in that callback caused a
`REASON_SOFT_RESTART` before the destination slot was touched. The working
lower-to-upper route returns first, then performs validation and copying from
ESPHome's normal component context. V2 validation uses direct watchdog feeds
instead of `yield()`, so the same validator remains safe in the synchronous
`boot_other` handler.

- GPIO1/GPIO3 are shared with the M0, but the physical feeder accepted an
  esptool stub and verified user1 write through TX0/RX0/GND without isolating
  the M0. Do not hold GPIO15 high during ESP8266 reset because GPIO15 is also a
  required low boot strap. If another revision has UART contention, validate a
  separate M0 isolation method before using it.
- Hold the Wi-Fi/GPIO0 button while applying normal feeder power, release it
  after one or two seconds, and leave the serial adapter's VCC disconnected.
- An unpowered continuity test established the board-pad routing: TX0 connects
  only to GPIO1/UART0 TX, RX0 only to GPIO3/UART0 RX, and TX1 only to
  GPIO2/UART1 TX. There is no RX1 pad. Stock diagnostics were observed on TX1.
- GPIO15 measures 3.3 V while the M0 runs. Configuring it as an inverted output
  and issuing the ESPHome reset control pulsed it low, produced the M0 startup
  sound, and yielded a fresh M0 status frame about 560 ms later. This confirms
  an active-low M0 reset at the ESP pin.
- The direct-flashed user2 transition image was readable on the board TX1 pad
  with the adapter configured for 74880 baud. It reached
  `!A !C !H !S !B !b !R !r !T !t !U !u`, then reset with hardware-watchdog
  cause 4. This proves that Arduino entry setup, PHY pre-start, and all three
  SDK flash reads completed. The failure is later: `userbin_check`, cache
  enable, or the first mapped-IROM call. The first mapped-IROM call in SDK
  `app_main.o` clears `.bss`, so the next candidate relocates that object's
  early `.irom0.text` to IRAM and retains a separate IROM marker for validation.
- With that SDK startup section relocated to IRAM, the next user2 run reached
  `!A !C !H !S !B !b !L !l !R !r !T !t !U !u !K !k !M !m`, then printed
  the PHY diagnostic `a_mis=1610612736, p_mis=125`. This proves that cache
  enable, the relocated `.bss` clear, print callback reinstall, and the first
  SDK startup data copy completed. The last observed execution is inside
  Espressif PHY calibration, before Arduino `user_init`.
- First flash is serial; then OTA. Back up stock flash first
  (`esptool.py read_flash 0 0x200000 stock.bin`). Set
  `PETKIT_SERIAL_BUS_DIR` to a private checkout containing the reference dumps
  when running the optional firmware checks.

## Stock user1 → user2 reverse OTA: why dev_ota_start never fires

Static disassembly of the ignored stock user-bin images in the sibling
`petkit-compat-server` checkout. All offsets below are reproducible with
`/tmp/disasm.py` (objcopy flat-binary → ELF32-xtensa, then objdump -d). Toolchain:
`.../toolchain-xtensa/bin/xtensa-lx106-elf-obj{copy,dump}` (binutils 2.32; `-D`
on a flat binary segfaults, so the ELF-conversion wrapper is required).

### Address mapping (the load-bearing correction)
For an extracted V2 user-bin, irom0 data starts at file offset `0x10` and maps
to VMA `0x40200000`:
  `VMA = 0x40200000 + (file_off - 0x10)`
  `file_off = (VMA - 0x40200000) + 0x10`
The earlier OTA-RESEARCH.md addresses (`0x402623C0` etc.) were stated as VMAs
but the file offsets quoted alongside them were off by the 0x10 image header,
so several "handler" claims pointed at the wrong function. Re-derivation below.

### Proven: the real HTTP-path OTA handler and its caller (user1)
- **HTTP callback / OTA-start trigger** at VMA `0x40250841` (file `0x50851`):
  - `0x40250847` `l32i a2,[a2]` then `0x4025084a` `l32i a2,a2,0x1c8` — reads
    **config offset 0x1c8** (the persisted OTA failure counter).
  - `0x4025084d` `bgei a2,3,0x4025085e` — **skips the handler if counter ≥ 3**.
  - `0x40250853` `movi.n a3,1` (mode=1 → select `result` wrapper), `0x40250855`
    `mov.n a2,a12` (response obj), `0x40250857` `mov.n a4,a3` (allow=1).
  - `0x40250859` `call0 402623c0` — **calls the OTA handler**.
  - `0x4025085c` `mov.n a13,a2`; `0x40250866` `bnei a13,1,0x4025086c` — if the
    handler returns 1, `0x40250869` `j 0x40250bb2` (proceed to send
    `dev_ota_start`); else it does not.
- **OTA handler** at VMA `0x402623c0` (file `0x623d0`), 80-byte frame. Parses
  the response (`call0 0x4024c638` json-get with mode/allow selecting `result`
  vs `payload`), then builds the `dev_ota_start` body string
  `hardware=%d&firmware=%s&firmwareDetails=[{"module":"userbin","version":%d}]&otaFirmwareId=%d`
  (VMA `0x40206522`). It does **not** reference the "deal otacheck start /
  ota info:no url" diagnostic strings — those live in a *separate* function
  (see below). The handler has **two early-return gates** before building the
  start body; both set `a12=0` and jump to the epilogue (`0x4026242d`
  `movi.n a12,0`), so the handler returns 0 and the caller does not send
  `dev_ota_start`:
  1. **Numeric-string check** at `0x40262445` `call0 0x4027199c` (→
     `0x4027187c`). The wrapper passes the handler argument with a null end
     pointer and base zero, so this is an `atoi`/`strtol`-style conversion, not
     a battery sensor read. If it returns 0:
     `0x4026244b` `bnez a2,…` falls through, prints `sys use bat power!!!`
     (`0x40206e74`), bails. The debug label is misleading; the exact meaning of
     the numeric string produced by the HTTP layer remains unproven.
  2. **Upgrade-flag check** at `0x40262459` `call0 0x4020d5f4` (the getter);
     `0x4026245c` `bnei a2,1,0x4026246d` — if the byte == 1, prints
     `KEY_WIFI_PIN:%d` (`0x40206f8c`) and bails; else continues to build the
     start body at `0x4026246d` → `0x4026254c`.
- **Neither handler compares `firmwareId`, top-level `version`, or module
  `version` before the upgrade decision.** Verified: the only comparisons
  between the json-get and the build-body path are the two gates above. There
  is no `blt/bge/bne` on any version field. (The earlier "upgrade branch at
  `0x402626D0`" note referenced the wrong function.)

### Proven: the transient byte (getter/setter)
- **Getter** VMA `0x4020d5f4` (file `0xd604`): `l32r a2,0x4020d5dc` →
  `0x3ffe9453`; `l8ui a2,a2,0`; `ret`. Reads one byte at RAM `0x3ffe9453`.
- **Setter** VMA `0x4020d5e0` (file `0xd5f0`): `extui a3,a2,0,8`;
  `movi.n a2,0`; `bgeui a3,3,ret` (clamps to <3); `l32r a2,0x4020d5dc` →
  `0x3ffe9453`; `s8i a3,a2,0`; `movi.n a2,1`; `ret`. Writes the byte (0/1/2)
  to RAM `0x3ffe9453`.
- The flag is an ESP8266-style upgrade flag: **0=IDLE, 1=START, 2=FINISH**
  (setter clamps at <3; getter bail is specifically on ==1).
- **Only one reference to `0x3ffe9453` exists in the whole user1 image**: the
  shared literal at `0x4020d5dc`. So no code in the Petkit user-bin loads the
  flag from flash/config at boot. The flag is a pure RAM byte.
- Setter call sites (all in the OTA download/HTTP-client region
  `0x40251xxx–0x40252xxx`):
  - `0x40251a3e` setter(**1** = START) — guarded by `[0x3ffe8810+4]==0`.
  - `0x40251f05` setter(**0** = reset) + immediately getter-read-back.
  - `0x4025233a` setter(**0**) — on the failure path.
  - `0x40251e1e`, `0x402520f2` — setters in the download state machine.
- **The flag (`0x3ffe9453`) is distinct from the failure counter
  (config `0x1c8`).** The counter is incremented at `0x40252319` (clamped via
  `bgei a5,3` at `0x40252316`, written back at `0x4025231b`). The app
  `ota_reset` / inbound `{"ota":0}` command writes config `0x1c8` at
  `0x40260c7b` (per OTA-RESEARCH.md) — it does **not** touch `0x3ffe9453`.

### Proven: the state-report `ota` field is the failure counter, not the flag
The state-report `ota` value tracks config `0x1c8` (the failure counter), as
the `ota_reset` experiment already showed (report → `ota:0`). The getter
(`0x4020d5f4`) is **not** called by the state-report builder; it is only
called from the OTA download state machine and the OTA handler bail at
`0x40262459`. Therefore `ota:0` in a state report does **not** imply the flag
is 0.

### The other "otacheck" function (do not confuse with the HTTP handler)
A second function at VMA `0x4025cc5b` (file `0x5cc4b`, 0x340-byte frame, ends
~`0x4025dca8`) references the diagnostic strings `deal otacheck start`,
`system ota starting`, `ota info:no firewareID/version/url`, `ota check end`,
`digest`, `deviceSecret`, and `write week error,not get ack`. It does **not**
call the getter/setter and does **not** build the `otaFirmwareId` body. It is a
separate (likely MQTT/cloud-command or device-info) OTA path and is not the
HTTP `dev_ota_check` handler that gates `dev_ota_start`.

### Remaining pre-handler and early-handler branches
`0x4026245c` `bnei a2,1,0x4026246d` is the **upgrade-flag bail** (getter
returns 1). It is one remaining candidate, but it is not established as the
branch observed on the feeder. The grounded constraints are:
- The user already cleared the failure counter (`ota:0` in the state report),
  which is evidence that the `0x4025084d` counter gate should pass if that
  state-field mapping is correct.
- The HTTP layer returns a heap string at `0x4024d600`; the handler applies the
  numeric conversion to that string. A live successful downgrade proves this
  gate can pass, but static analysis has not yet named the string's field.
- The flag rejects only value 1. Values 0 and 2 continue. The successful
  download state machine sets 2 before finishing, while cleanup paths set 0.
- JSON member parsing, a non-null details array, and a non-null URL remain later
  rejection points even after both early gates pass.

### Open item requiring runtime evidence
The flag is a RAM byte, and the setter performs only a byte store. The complete
2 MiB dump contains no second literal reference to user1's `0x3ffe9453`; user2
uses the corresponding RAM byte at `0x3ffe99e3`. No evidence was found that
either value is loaded from system-parameter flash, so the earlier persistent
SDK-cache explanation is unsupported. A hard power cycle should initialize
this BSS/global state, although the exact SDK startup initialization has not
been reconstructed. A receive-only UART log remains the decisive discriminator:
the `KEY_WIFI_PIN:%d` line identifies the flag branch, while the neighboring
diagnostics distinguish the numeric-string and later parser failures.

### Smallest safe state transition to unblock (proposed, not yet validated)
The flag is only writable through the firmware setter (`0x4020d5e0`); a local
HTTP server cannot write DRAM directly. The candidate triggers to test, in
order of likelihood, are: (a) an inbound MQTT/cloud command that calls
setter(0) — the existing `{"ota":0}` only writes config `0x1c8`, so a
different key or a second setter(0) path must be found; (b) completing a
full OTA cycle so the success path (`0x40251f05` setter(0)) runs; (c) if the
flag really is the SDK `system_upgrade_flag`, the SDK's own
`system_upgrade_flag_set(UPGRADE_FLAG_IDLE)` invoked through whatever path
user2 uses at boot. None of these is yet confirmed by a code path; this is the
next thing to pin down before any further server-side experiment.

### user1 vs user2 diff
An earlier analysis identified file `0x68644`, VMA `0x40268634`, as the user2
HTTP OTA handler. That address is motor/door telemetry code; the matching
function prologue was not enough to establish semantics. The exact user2 HTTP
OTA handler remains unlocated, so do not publish a replacement address without
re-deriving its complete caller and data flow.

The user2 getter is VMA `0x4020d900`, and its setter is VMA `0x4020d8ec`; they
use RAM byte `0x3ffe99e3`. The setter call sites are structural matches for the
user1 download state machine: `0x402568de` sets 1 at OTA start,
`0x40256cb6` sets 2 on completion, and cleanup paths at `0x40256d9d`,
`0x40256f8d`, and `0x402571da` set 0. This does not identify the HTTP handler.

Both images contain the **same** bail/OTA diagnostic strings (`sys use bat
power`, `KEY_WIFI_PIN:%d`, `ota_flag:%d, boot bin:%d`, `ota fail:no %d`). The
string `ota_flag:%d, boot bin:%d` is present in both images but is **not
referenced as a 4-byte literal anywhere** in either — so it is not a
reachable boot print via a normal `l32r` (dead string, or referenced via a
pointer table not yet found). This does not establish a persistent SDK flag
cache. The remaining decisive step is runtime branch identification over
receive-only UART, followed by a backward trace from the diagnostic that
actually fires.
