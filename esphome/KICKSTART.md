# How Petkit Kickstart installs ESPHome

Petkit Kickstart is temporary firmware that lets the Fresh Element Mini move
from its stock firmware to ESPHome over Wi-Fi. It runs on the feeder's own
ESP8266; the separate ISD91230 motor controller keeps its existing firmware.

The stock OTA updater and ESPHome use different image formats and flash
layouts. Kickstart bridges the two before the final feeder firmware is installed:

```text
Petkit stock firmware
    │  Stock OTA installs an Espressif non-OS SDK V2 application
    ▼
Petkit Kickstart
    │  Save a backup, then convert Kickstart to the eboot layout
    ▼
Petkit Kickstart under eboot
    │  Ordinary ESPHome OTA installs the final feeder firmware
    ▼
Petkit ESPHome firmware
       Future updates use ESPHome OTA
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
address zero, and uses the eboot V1 layout. The stock updater cannot install
that image directly. Kickstart boots as a V2 application first, then rebuilds
its own application in the eboot format and replaces the bootloader. The final
feeder firmware is installed afterward through ordinary ESPHome OTA.

## What happens during migration

### 1. Stock OTA installs Kickstart into the inactive slot

The compatibility HTTP server offers the bridge through Petkit's OTA-check
response. The stock firmware downloads and validates the V2 user-bin, writes
it into the inactive slot, and asks the stock bootloader to start that slot.

The server must offer an image built for the validated hardware profile. The
stock OTA client chooses the physical destination; the server must not infer
the active slot from a firmware version such as `1.406` or `2207006`.

### 2. Save the recovery image before changing either slot

The Petkit profile leaves automatic relocation and conversion disabled. Once
Kickstart is reachable, the installer downloads its full 2 MiB flash image and
saves the slot status alongside it. At this point the image contains:

- The stock bootloader, Kickstart in one slot, and the other slot's application.
- Device identity and saved configuration, including Wi-Fi and cloud credentials.
- RF calibration data and SDK system parameters.

This is a **post-Kickstart backup**, not a pristine stock dump: stock OTA has
already replaced one application. If neither relocation nor conversion has
run, the other slot can still contain a bootable stock application. Keep the
original backup when retrying installation; a later download records whatever
changes have already happened.

A complete pre-installation stock backup requires reading flash through the
ESP8266 ROM UART loader before installing Kickstart. Keep either kind of backup
private because it can contain Petkit and Aliyun credentials.

### 3. Kickstart moves to the upper slot if necessary

The final installation replaces the lower flash layout. Kickstart therefore
needs to run from `user2` before converting, so it does not overwrite the
application it is executing.

Kickstart asks the ESP8266 SDK which slot is running. The installer, or the
**Relocate to upper slot** button in the web interface, triggers relocation when
needed:

- If Kickstart is already in `user2`, no relocation is needed.
- If Kickstart is in `user1`, it validates its own image, copies the bridge
  to `user2`, validates the copy, selects `user2`, and reboots.

Relocation is a separate operation from converting the bridge: after
relocation, the stock bootloader still starts Kickstart in the stock V2 layout.

**Relocation overwrites the remaining stock application.** Immediately after
stock OTA, the opposite slot still contains stock firmware. Once Kickstart has
copied itself there, both slots contain the bridge.

### 4. Convert the bridge and install the ESPHome firmware

The authenticated `/hub/convert` request records the conversion request and
reboots Kickstart. On the next boot, before Wi-Fi starts, Kickstart rebuilds
its own application as an eboot V1 image and replaces the vendor bootloader
with eboot last. This preserves the vendor bootloader until the application
has been written, but power loss during bootloader replacement can still
require UART recovery. The [generic transition guide][transition-guide]
documents validation, write order, and recovery limits.

Read `GET /hub/convert` after the reboot to confirm that Kickstart recognized
the eboot layout. Installing the feeder firmware afterward is a separate step:
select **Take Control** in ESPHome Device Builder, or build and upload it
from the command line. Conversion alone does not install feeder controls.

Manual installation and the current Device Builder requirements are described
in the [user procedure](../README.md#5-preserve-recovery-data-migrate-kickstart-and-install-the-final-image).
Subsequent updates also use ESPHome OTA. The transition component suppresses
the configured native OTA listener during normal V2 startup; the generic
[transition guide][transition-guide] covers the required setup and safe-mode
constraints.

## What runs while Kickstart is active

Kickstart provides network access, diagnostics, and recovery services. It does
not configure the feeder-control component, GPIO inputs, or GPIO buttons, so
it cannot dispense food or operate the outlet door. The generic Kickstart pin
scanner is also omitted because those pins connect to the motor-controller
bus and physical controls.

| Interface | Purpose |
|---|---|
| Wi-Fi station connection | Access through the configured home network |
| Password-protected fallback AP | Diagnostics and recovery when the station connection is unavailable; the web interface is at `192.168.4.1` |
| Authenticated web interface | Diagnostics, migration, and recovery |
| ESPHome native API | Network logs and Home Assistant connection |
| UART1 / GPIO2 / board TX1 pad | Transmit-only serial logs |

UART0, on GPIO1 and GPIO3, connects to the ISD91230 motor controller and is left
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
| `esp8266_nonos_v2_to_eboot_v1` | Defines the flash layout and mapped-code limits, generates the linker script, and converts the bridge itself to eboot |
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
3. Download and verify the recovery image and save its slot status. For a bridge
   initially installed in `user1`, then confirm that relocation succeeds and
   the bridge boots from `user2`.
4. Convert the bridge, confirm the eboot boot, and install the final ESPHome
   firmware through ordinary OTA.
5. Confirm that a subsequent ESPHome OTA update succeeds.
6. Restore stock over UART and repeat with the other initial destination slot.

## Home Assistant handoff

Home Assistant identifies the feeder by its MAC address. The API encryption
key controls access to that device and depends on how the final firmware is built:

| Installation | API encryption key | Time zone |
|---|---|---|
| Local, using `petkit-feeder-local.yaml` | Reuses `api_key` from this checkout's `secrets.yaml` | Uses `timezone` from the same file |
| Device Builder | Generates a key when you select **Take Control** | Uses Device Builder's time zone |

Home Assistant can retrieve the new key from an available Device Builder
integration during re-authentication. If that lookup is unavailable or fails,
provide the key from the final configuration when prompted.

To override Device Builder's time zone, extend both `hardware_time` and
`homeassistant_time` in the Device Builder configuration. The local configuration provides an
[example](petkit-feeder-local.yaml).

### Firmware-update credentials

The Petkit bridge accepts unauthenticated OTA after conversion so Device Builder
can perform the first installation without sharing the bridge's API key.
Before conversion, its OTA listener is disabled.

The feeder package uses a separate `ota_password` substitution, empty by
default. Set it in the Device Builder configuration to protect later updates.
`fallback_ap_password` independently protects the fallback Wi-Fi network and
also defaults to empty in the package; local builds take that password from
`secrets.yaml`.

The first upload uses password-based OTA rather than OTA encryption: ESPHome
refuses an unencrypted connection when the uploading configuration requires
OTA encryption. Native API encryption is separate and remains enabled in both
installation paths.

## Shared implementation

The bridge follows the staged-installation design of
[ESPHome Kickstart](https://github.com/libretiny-eu/esphome-kickstart), including
its fallback AP and recovery web interface. The Petkit profile loads the generic
migration components directly from
[`wrobelda/esphome-kickstart`](https://github.com/wrobelda/esphome-kickstart)
instead of carrying private copies. For the reusable algorithm and recovery
API details, see the [non-OS V2 transition guide][transition-guide].

Before migration changes the other slot, slot control can validate and boot
the remaining stock application. After eboot replaces the vendor bootloader,
the bridge has no reverse conversion path; restoring a saved vendor-layout
image requires UART access. See [Recovery and rollback](../README.md#recovery-and-rollback)
for the user procedure.

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

[transition-guide]: https://github.com/wrobelda/esphome-kickstart/blob/master/components/esp8266_nonos_v2_to_eboot_v1/README.md
