# Petkit Kickstart migration bridge

`petkit-kickstart.yaml` builds an intermediate bridge between Petkit's stock
firmware and the final ESPHome firmware. The bridge is needed because the OTA
code inside Petkit's stock firmware accepts an Espressif non-OS SDK V2
`user1.bin` or `user2.bin`; it cannot install a normal ESPHome factory image
directly. The bridge boots in the stock flash layout and provides recovery,
full-flash download, and a bootloader-last installer for the final ESPHome
factory image.

This follows the staged-installation design of the canonical
[ESPHome Kickstart](https://github.com/libretiny-eu/esphome-kickstart) project,
including its fallback access point and recovery web interface. The generic
migration components currently live in the tested
[`wrobelda/esphome-kickstart`](https://github.com/wrobelda/esphome-kickstart)
fork while they are prepared for upstream review. The bridge has booted
successfully on the feeder with the current ESP8266 Arduino core 3.1.2.

The bridge deliberately has no `uart`, `petkit_feeder`, `pinscan`, GPIO binary
sensors, or GPIO buttons, so it cannot operate the feeder. UART0 connects to
the motor-controller MCU. UART1 is transmit-only and carries bridge logs.

The final Petkit ESPHome image also sends logs through UART1/TX1 while it uses
UART0 to exchange framed commands and status messages with the motor controller.
UART1 is transmit-only on the ESP8266, which is sufficient for logging. Network
logging through the ESPHome API remains available in both images.

## Stock application slots

The stock feeder firmware uses Espressif's older non-OS SDK OTA layout. It
keeps two independently bootable application areas in the 2 MiB flash chip;
Espressif names them `user1` and `user2`. This differs from a normal ESPHome
factory installation, which replaces the flash layout starting at address zero.

The relevant stock flash layout is:

```text
0x000000                 stock bootloader and supporting data
0x001000 .. 0x100fff     user1 application area
0x101000 .. end of apps  user2 application area
near end of flash        SDK parameters, RF data and saved configuration
```

Only one user-bin runs at a time. Espressif's stock bootloader records which
slot is active and starts that application after reset. Petkit's OTA client,
which is part of the stock feeder firmware, downloads a V2 user-bin built for
the other slot, validates it, tells the Espressif bootloader to select that
slot, and reboots. Petkit's stock OTA client does not normally erase the
currently active application first, so that copy remains available if the new
image cannot be selected or must be replaced.

### What the slot names mean

`user1` and `user2` identify flash addresses; they do not identify firmware
versions. Either area may contain the newer application because each successful
OTA installation changes which area is active. A `user1.bin` and a `user2.bin`
may therefore contain the same source version but differ in addresses encoded
in their image metadata.

### What a user-bin contains

A user-bin file contains only one application. It is not a full-flash backup:
it omits the bootloader, the other application area, RF calibration, SDK system
parameters, and some device-specific configuration.

### Why Kickstart relocates

End-to-end hardware testing covered both possible starting slots. In the
lower-slot test, stock user2 installed the bridge image into user1. Before
initializing ESPHome's Wi-Fi component, the bridge automatically detected the
lower slot, validated its image, copied it to user2, validated the copy,
selected user2, and rebooted. The final layout migration was a separate manual
step: an authenticated `/hub/migrate` upload supplied the ESPHome factory
image. The upper bridge validated that image, replaced the vendor bootloader,
and booted the final feeder firmware. A subsequent standard ESPHome OTA update
also completed successfully under the new eboot layout. An earlier test booted
the transition image when the stock OTA client installed it directly in user2.

The reusable relocation and final-install algorithm is documented in ESPHome
Kickstart's [non-OS V2 transition
guide](https://github.com/wrobelda/esphome-kickstart/blob/master/ESP8266-NONOS-TRANSITION.md).

### What the recovery image contains

The bridge cannot make a pristine backup of the target slot's former contents
because Petkit's stock OTA code overwrites that slot before the bridge runs. A
bridge full-flash download is still a useful recovery image because it contains
the bootloader, the unchanged stock ESP8266 firmware in the other slot, device
identity, RF data, and system parameters. A complete pre-installation stock
backup must be read through the ESP8266 ROM UART loader before installing the
bridge. The recovery image may contain stock cloud credentials, so keep it
private and do not publish it.

### How the bridge identifies the active slot

The implementation does not assume that user1 is always older or that user2 is
always active. Those facts describe only the feeder's state during the first
installation. Every successful stock OTA update reverses the roles: the OTA
code running inside the active Petkit stock user-bin writes the image offered
by our compatibility HTTP server into the inactive slot, then asks the
Espressif bootloader to boot that slot. After the bridge starts, its recovery
component asks the ESP8266 SDK which slot is currently running; the previous
stock ESP8266 firmware is therefore in the opposite slot, regardless of its
version number. Recovery uses that relationship rather than matching `1.406`,
`2207006`, or another release-specific value.

The Petkit HTTP endpoints and metadata fields may be reusable across multiple
Petkit products, but the firmware container and flash transition remain
platform-specific. Other ESP8266 products may use a different flash size or
slot boundary, while an ESP32 uses an ESP-IDF partition table rather than the
ESP8266 `user1`/`user2` format. The compatibility server should select a
validated hardware profile; it must not send one universal binary merely
because two products call the same OTA endpoint.

## Required stages

1. Compile the minimal ESPHome image and retain its ELF file.
2. Generate an Espressif V2 user-bin from that ELF.
3. Verify the ESP image checksum and appended SDK CRC32.
4. Confirm that every mapped segment is valid for ESP8266 RAM, then document
   any difference from the stock image's segment count or ordering.
5. Package the transition application as a profile-compatible V2 user-bin,
   then offer it in the compatibility server's OTA-check response. Petkit's
   stock OTA client selects and writes the inactive physical slot; the image
   does not encode which slot becomes active.
6. Confirm that the device boots the bridge and that its recovery API remains
   available.
7. Download and verify the post-transition full-flash recovery image before
   allowing any bootloader replacement.
8. If automatic relocation is enabled and the bridge runs from user1, validate
   and copy it to user2, then verify that user2 boots before replacing the
   bootloader.
9. Install and boot the complete Petkit ESPHome factory image.
10. Restore stock over UART and repeat stages 5–9 with the transition initially
    installed in user1.

`build_esp8266_nonos_v2.py` performs stages 2 and 3 plus the static range checks
from stage 4. Run it with the same Python environment that provides `esptool`:

```sh
python3 KICKSTART_CHECKOUT/tools/build_esp8266_nonos_v2.py \
  --irom-vma 0x40201010 \
  --entry-symbol app_entry \
  --max-size 0x0fa000 \
  --flash-mode qio \
  --flash-frequency 40m \
  --flash-layout 2MB-c1 \
  path/to/firmware.elf path/to/petkit-kickstart-v2.bin
```

The builder refuses an ELF whose flash-mapped code does not begin at the
configured address or whose entry point differs from the configured value. An
Arduino ESP8266 build must enter through `app_entry`, because that function
sets up the continuation context and UMM heap before it calls the SDK's
`call_user_start`. Matching the numeric address used by stock firmware is not
valid when the two images assign different startup code to that address.

The builder generates the requested V2 header, then verifies that header, the
segment checksum, appended SDK CRC32, absence of trailing bytes, and configured
size limit. The P530 user2 limit is `0x0fa000`, because its slot begins at
`0x101000` and must end before the PHY-data sector at `0x1fb000`. The following
sector at `0x1fc000` contains RC-calibration data. The image file is a build
artifact and must remain outside Git.

## Bridge configuration

The feeder profile uses three components from the ESPHome Kickstart checkout:

- `esp8266_nonos_v2_to_eboot_v1` owns the flash size, both V2 slot ranges, and
  the IROM virtual address and capacity. It generates the linker script required
  by the stock bootloader and provides the V2-to-eboot migration installer. The
  generated V2 image preserves Arduino's `app_entry`, which initializes its
  continuation context and UMM heap before entering the non-OS SDK.
- `hub_api` provides the flash download used to preserve the complete stock
  image.
- `esp8266_nonos_v2_slot_control` references the layout owned by
  `esp8266_nonos_v2_to_eboot_v1`, then provides recovery controls and optional
  automatic lower-to-upper relocation. This component is optional.

None of these components contains P530 addresses or Petkit firmware versions;
the feeder-specific values are in `petkit-kickstart.yaml`. The generic
component schema, authenticated recovery routes, validation rules, relocation
algorithm, write order, and power-loss limits belong in ESPHome Kickstart's
[non-OS V2 transition
guide](https://github.com/wrobelda/esphome-kickstart/blob/master/ESP8266-NONOS-TRANSITION.md).

The normal ESPHome `firmware.bin` and `firmware.factory.bin` files must not be
offered directly to Petkit's stock OTA client. That client expects Espressif's
non-OS SDK V2 user-bin format.

The current bridge disables ordinary ESPHome OTA while the V2 layout is active
and exposes a dedicated migration installer. Replacing that temporary interface
with a transparent Kickstart OTA backend, and adding the reverse eboot V1 to
non-OS V2 restoration path, are tracked in the project [TODO](../TODO.md). The
reverse path has a separate [investigation
brief](../research/ESP8266-EBOOT-V1-TO-NONOS-V2-PROMPT.md).

## Home Assistant handoff

The hardware test verified the Home Assistant handoff. Kickstart and the final
device configuration must use the same native API encryption key. Home
Assistant retained Kickstart's encrypted connection settings and could not
connect when the first final image exposed a plaintext API. After a final image
with the same key was installed, Home Assistant reconnected to the existing
device entry, changed its displayed name from Petkit Kickstart to Petkit
Feeder, and did not require a duplicate device.

Each supported device's Kickstart configuration should therefore use the same
API encryption secret that its final ESPHome configuration will use. The node
and friendly names may change during migration without breaking device-registry
continuity.

## Relationship to upstream ESPHome Kickstart

The upstream ESP8266 Kickstart configuration establishes useful prior art for
the fallback AP, web server, API, OTA, diagnostics, and dashboard import. Its
generic pin scanner is intentionally omitted because Petkit GPIOs operate the
motor-controller bus and physical controls.

The password-protected fallback AP and captive portal provide Wi-Fi
provisioning. The authenticated web server is available at `192.168.4.1` while
the device is in fallback-AP mode.

The proposed ESPHome Kickstart changes extend `hub_api` with ESP8266 flash
reads, so the Petkit profile loads that component directly from the Kickstart
checkout rather than carrying a private copy.

The resulting flash download is not a pristine stock backup. Installing the
bridge has already replaced the previously inactive stock user-bin. The
download still preserves the stock bootloader, the unchanged remaining stock
user-bin, system parameters, RF calibration, Wi-Fi configuration, and
device-specific Petkit and Aliyun identity. A literal pre-transition backup
still requires reading the flash through the ESP8266 ROM UART loader before the
OTA installation.

## Known product variants

Public regulatory records identify the Fresh Element Mini as model P530 under
one original 2018 FCC application. Manuals and seller material also call it D2
and D2-C, but continue to identify the product as P530. No public source found
so far documents a P530 mainboard Revision A/B or a changed Wi-Fi MCU.

The stock SoftAP name contains `PETKIT_FEEDER_HW2`, which is direct evidence of
a hardware-generation identifier but does not establish what changed from
HW1. Before offering the bridge image to another unit, verify at least the
P530 identity, ESP8266 target, 2 MiB flash size, V2 slots at `0x001000` and
`0x101000`, and the stock request's hardware identifier. Do not treat the lack
of a published revision as proof that all units are identical.
