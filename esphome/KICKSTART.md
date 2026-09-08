# How Petkit Kickstart installs ESPHome

Petkit Kickstart is temporary firmware that lets the Fresh Element Mini move
from its stock firmware to ESPHome over Wi-Fi. It runs on the feeder's own
ESP8266; the separate Nuvoton motor controller keeps its existing firmware.

The migration needs two steps because the stock OTA updater and ESPHome use
different image formats and flash layouts:

```text
Petkit stock firmware
    │  Stock OTA installs an Espressif non-OS SDK V2 application
    ▼
Petkit Kickstart
    │  Kickstart installs an ESPHome factory image and replaces the bootloader
    ▼
Petkit ESPHome firmware
       Future updates use ordinary ESPHome OTA
```

For installation commands, follow the [user procedure](../README.md). This
document explains what happens inside the ESP8266, how the bridge image is
built, and what can be recovered at each stage.

## Why the stock updater needs a bridge

The feeder has a 2 MiB flash chip. Its stock bootloader can start an application
from either of two areas, called `user1` and `user2`. Only one application runs
at a time; the other area is the destination for the next stock OTA update.

| Flash address | Stock use |
|---|---|
| `0x000000` | Bootloader and supporting data |
| `0x001000`–`0x100fff` | Lower application slot: `user1` |
| `0x101000`–`0x1fafff` | Upper application slot: `user2` |
| `0x1fb000` onward | PHY data, calibration, SDK parameters, and saved configuration |

The slot names describe locations, not versions. Either slot can contain the
newer stock firmware. The stock OTA client writes the inactive slot, validates
the downloaded application, selects that slot for the next boot, and reboots.
The previously running application remains in the other slot at this point.

The stock updater accepts an Espressif non-OS SDK **V2 user-bin**: a file
containing one application in the format expected by the stock bootloader.
A user-bin does not contain the bootloader, the other slot, or the complete
set of device configuration and calibration data.

An **ESPHome factory image** establishes a different layout, starting at flash
address zero, and uses the eboot bootloader. The stock updater cannot install
that image directly. Kickstart solves this by booting as a V2 application first,
then providing a separate installer for the factory image.

## What happens during migration

### 1. Stock OTA installs Kickstart into the inactive slot

The compatibility HTTP server offers the bridge through Petkit's OTA-check
response. The stock firmware downloads and validates the V2 user-bin, writes
it into the inactive slot, and asks the stock bootloader to start that slot.

The server must offer an image built for the validated hardware profile. The
stock OTA client chooses the physical destination; the server must not infer
the active slot from a firmware version such as `1.406` or `2207006`.

### 2. Kickstart moves to the upper slot if necessary

The final installation replaces the lower flash layout. Kickstart therefore
needs to run from `user2` before performing that installation, so it does not
overwrite the application it is executing.

Kickstart asks the ESP8266 SDK which slot is running. The
[Petkit configuration](petkit-kickstart.yaml) enables automatic relocation:

- If Kickstart starts in `user2`, no relocation is needed.
- If Kickstart starts in `user1`, it validates its own image, copies the bridge
  to `user2`, validates the copy, selects `user2`, and reboots.

Relocation happens before ESPHome's Wi-Fi component initializes. It is a
separate operation from installing the final factory image: after relocation,
the stock bootloader still starts Kickstart in the stock V2 layout.

**Relocation can overwrite the remaining stock application.** Immediately after
stock OTA, the opposite slot still contains stock firmware. Once Kickstart has
copied itself there, both slots can contain the bridge.

### 3. Save the recovery image

Once Kickstart is reachable, download and verify its full 2 MiB flash image
before replacing the bootloader. The download preserves the flash contents at
that moment, including:

- The stock bootloader and the current contents of both application slots.
- Device identity and saved configuration, including Wi-Fi and cloud credentials.
- RF calibration data and SDK system parameters.

This is a **post-transition recovery image**, not a pristine stock backup.
Stock OTA has already overwritten one stock application, and automatic
relocation may have overwritten the other. Do not assume that the download
contains a bootable stock application.

A complete pre-installation stock backup requires reading flash through the
ESP8266 ROM UART loader before installing Kickstart. Keep either kind of backup
private because it can contain Petkit and Aliyun credentials.

### 4. Install the ESPHome factory image

The authenticated `/hub/migrate` endpoint accepts the complete Petkit ESPHome
factory image. Kickstart validates the image, writes the application first,
and replaces the vendor bootloader with eboot last. The ESP8266 then reboots
into the final feeder firmware.

Writing the bootloader last postpones the change in boot layout until the
application has been written. It does not make the operation immune to power
loss; interruption during bootloader replacement can still require UART
recovery. The generic validation rules, write order, and power-loss limits are
documented in the [ESPHome Kickstart non-OS V2 transition guide][transition-guide].

Ordinary ESPHome OTA is disabled while the bridge uses the V2 layout. After the
factory-image migration, subsequent updates use normal ESPHome OTA.

## What runs while Kickstart is active

Kickstart provides network access, diagnostics, and recovery services. It does
not configure the feeder-control component, GPIO inputs, or GPIO buttons, so
it cannot dispense food or operate the outlet door. The generic Kickstart pin
scanner is also omitted because those pins connect to the motor-controller
bus and physical controls.

| Interface | Purpose |
|---|---|
| Wi-Fi station connection | Access through the configured home network |
| Password-protected fallback AP and captive portal | Configure Wi-Fi when the station connection is unavailable |
| Authenticated web interface | Diagnostics and recovery; available at `192.168.4.1` in fallback-AP mode |
| ESPHome native API | Network logs and Home Assistant connection |
| UART1 / GPIO2 / board TX1 pad | Transmit-only serial logs |

UART0, on GPIO1 and GPIO3, connects to the Nuvoton motor controller and is left
unused by Kickstart. The final feeder firmware uses UART0 for framed commands
and status replies, while continuing to send serial logs through UART1.

## Building the stock-compatible bridge image

The bridge starts as a minimal ESPHome build from
[`petkit-kickstart.yaml`](petkit-kickstart.yaml). Its ELF file contains the
compiled code and address information needed to generate a V2 user-bin.
The normal ESPHome `firmware.bin` and `firmware.factory.bin` outputs must not
be offered to Petkit's stock OTA client.

### Components and hardware settings

The Petkit profile loads three components from an ESPHome Kickstart checkout,
selected through `KICKSTART_COMPONENTS_PATH`:

| Component | Responsibility |
|---|---|
| `esp8266_nonos_v2_to_eboot_v1` | Defines the flash layout and mapped-code limits, generates the linker script, and installs the final eboot factory image |
| `hub_api` | Provides the full-flash recovery download |
| `esp8266_nonos_v2_slot_control` | Provides slot recovery controls and optional automatic relocation to the upper slot |

The components are generic; P530 flash addresses and capacities belong in
`petkit-kickstart.yaml`, not in the component implementations. Slot control
uses the layout defined by the migration component and is optional in the
generic design, although the Petkit profile enables it.

### Convert the ELF to a V2 user-bin

After compiling the bridge, retain its ELF file and run the Kickstart builder
with the Python environment that provides `esptool`. Replace
`KICKSTART_CHECKOUT` and the artifact paths with the local checkout and build
locations:

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

These settings enforce three constraints:

- **Code mapping:** flash-mapped code must begin at the configured IROM virtual
  address, `0x40201010`. The builder also checks the mapped segment ranges.
- **Startup:** the entry point must be Arduino's `app_entry`, which initializes
  the continuation context and UMM heap before calling the SDK's
  `call_user_start`. Copying the stock firmware's numeric entry address would
  be wrong because that address can contain different code in the new build.
- **Capacity:** the image must fit within `0x0fa000` bytes. The P530 upper slot
  starts at `0x101000` and ends before PHY data at `0x1fb000`; RC-calibration
  data occupies the following sector at `0x1fc000`.

The builder creates the V2 header and verifies the header, segment checksum,
appended SDK CRC32, size limit, and absence of trailing bytes. Keep generated
images outside Git.

### Validate a changed bridge build

Static image checks establish that the file has the expected structure;
hardware tests establish that the stock updater and bootloader accept it.
When changing the transition code or hardware profile, validate the complete
sequence:

1. Build the ELF and V2 user-bin, check segment ranges, and document any segment
   count or ordering differences from the stock image.
2. Install through stock OTA and confirm that Kickstart boots and its recovery
   API is reachable.
3. For a bridge initially installed in `user1`, confirm that relocation succeeds
   and the bridge boots from `user2`.
4. Download and verify the recovery image, then install and boot the final
   ESPHome factory image.
5. Confirm that a subsequent ordinary ESPHome OTA update succeeds.
6. Restore stock over UART and repeat with the other initial destination slot.

## Home Assistant handoff

Use the **same native API encryption key** in Kickstart and the final feeder
configuration. Home Assistant retains the encrypted connection settings when
the firmware changes.

If the final image exposes a plaintext API, Home Assistant cannot connect using
the retained encrypted settings. Keeping the same key allows Home Assistant to
reuse the existing device entry. The node and friendly names may change during
migration.

## Shared implementation

The bridge follows the staged-installation design of
[ESPHome Kickstart](https://github.com/libretiny-eu/esphome-kickstart), including
its fallback AP and recovery web interface. The Petkit profile loads the generic
migration components directly from
[`wrobelda/esphome-kickstart`](https://github.com/wrobelda/esphome-kickstart)
instead of carrying private copies. For the reusable algorithm and recovery
API details, see the [non-OS V2 transition guide][transition-guide].

The bridge supports migration from stock V2 firmware to eboot. It does not
provide a reverse eboot-to-V2 installer; restoring stock requires UART access.

## Applying the bridge to another feeder

The profile targets the Fresh Element Mini P530 with an ESP8266 and 2 MiB
flash. Product names such as D2 or D2-C and a SoftAP name containing
`PETKIT_FEEDER_HW2` are not sufficient to establish firmware compatibility.
Before offering the bridge to another unit, verify:

- P530 product identity and the stock OTA request's hardware identifier.
- ESP8266 target and 2 MiB flash capacity.
- V2 application slots at `0x001000` and `0x101000`.

Shared Petkit HTTP endpoints do not imply compatible firmware. Another ESP8266
product may have different flash boundaries, while an ESP32 uses an ESP-IDF
partition table instead of this V2 slot layout. The compatibility server must
select a validated hardware profile rather than offer one binary to every
product using the same OTA endpoint.

[transition-guide]: https://github.com/wrobelda/esphome-kickstart/blob/master/ESP8266-NONOS-TRANSITION.md
