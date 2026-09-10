# Petkit Element Mini ESPHome

This project replaces the cloud-dependent firmware on the Petkit Fresh Element
Mini pet feeder with ESPHome. It keeps the feeder's ISD91230 motor controller,
motor wiring, sensors, buttons, indicators, beeper, and battery-backed clock.

Only the Fresh Element Mini P530 hardware described here is currently
supported. The ESP32-based Fresh Element Solo is a different device.

## Features

- local feeding from Home Assistant or the feeder's physical button;
- four schedules by default, with additional slots configurable in YAML;
- battery-backed timekeeping during network and power outages;
- food detection, power-source reporting, and diagnostic feedback;
- over-the-air ESPHome updates after the initial firmware migration.

One serving is one motor-controller wheel cycle, which Petkit describes as
approximately 5 g. Actual weight varies with the food and hopper level.

## Installation

The wireless installation starts with Petkit's stock firmware, installs a small
ESPHome transition image, then installs the complete feeder firmware. It does
not require opening the feeder. Both procedures save a recovery backup before
relocating or converting Kickstart; see [Recovery and rollback](#recovery-and-rollback)
if you need to restore it.

### Guided installation

The guided installer handles the migration up to the final ESPHome firmware:

1. Download the supporting projects and prepare the build environment.
2. Ask for Wi-Fi and recovery credentials, then build the temporary bridge
   firmware.
3. Run the local Petkit API and provision the feeder to download the bridge.
4. Save a recovery image and slot status, then prepare the feeder for the final
   ESPHome firmware.
5. Recommend taking control of the feeder in ESPHome Device Builder, or, if
   you prefer, build and install the final firmware from the installer and
   confirm that it is running on the same physical device.

The installer pauses when you need to put the feeder in setup mode or reconnect
the computer to your regular 2.4 GHz Wi-Fi network. After preparing the feeder,
it recommends taking control of it in ESPHome Device Builder and only builds and
installs the final firmware if you ask it to. If the process is interrupted after the bridge
or the final firmware boots, run the same command again. The installer
identifies the running firmware and continues from that phase without repeating
stock provisioning or a completed migration.

```sh
git clone https://github.com/wrobelda/petkit-element-mini-esphome.git
cd petkit-element-mini-esphome
python3 install.py
```

Run `python3 install.py --debug` to show the commands and diagnostic output
hidden during a normal installation.

Use the manual procedure below when developing the firmware or diagnosing a
failed stage.

### Manual installation

#### 1. Get the three projects

Keep the checkouts next to each other so the commands below work unchanged:

```sh
git clone https://github.com/wrobelda/petkit-element-mini-esphome.git
git clone https://github.com/wrobelda/petkit-compat-server.git
git clone https://github.com/wrobelda/esphome-kickstart.git
git -C petkit-compat-server checkout --detach 91bf4d57e1dceecc48eccde936d1532e3fd5e972
git -C esphome-kickstart checkout --detach 07a195fc1f036bcbd9c9d124a80cb85585172b58
```

#### 2. Configure and build both ESPHome images

Create a virtual environment, install the hardware-tested ESPHome beta, and create
the private secrets file:

```sh
cd petkit-element-mini-esphome
python3 -m venv .venv
.venv/bin/pip install 'esphome==2026.9.0b1'
cp esphome/secrets.yaml.example esphome/secrets.yaml
```

The process uses two ESPHome images. Kickstart is a small transition image that
can boot under the stock flash layout; the final image operates the feeder
under ESPHome's normal layout. Both configurations read the same
`esphome/secrets.yaml`, so Home Assistant retains one device entry through the
migration.

Edit `esphome/secrets.yaml` and set:

- the regular 2.4 GHz Wi-Fi network name and password;
- the feeder's IANA time-zone name;
- a unique fallback-AP password and Kickstart web login;
- an API encryption key generated with `openssl rand -base64 32`.

Build the transition and final images:

```sh
cd esphome
export KICKSTART_COMPONENTS_PATH="$(realpath ../../esphome-kickstart/components)"
../.venv/bin/esphome compile petkit-kickstart.yaml
../.venv/bin/esphome compile petkit-feeder.yaml
```

Package the transition ELF in the V2 format expected by Petkit's stock OTA
code:

```sh
mkdir -p ../local-cache/release
../.venv/bin/python ../../esphome-kickstart/tools/build_esp8266_nonos_v2.py \
  --irom-vma 0x40201010 \
  --entry-symbol app_entry \
  --max-size 0x0fa000 \
  --flash-mode qio \
  --flash-frequency 40m \
  --flash-layout 2MB-c1 \
  .esphome/build/petkit-kickstart/.pioenvs/petkit-kickstart/firmware.elf \
  ../local-cache/release/petkit-element-mini-kickstart-v2.bin
```

#### 3. Prepare the local Petkit API

Start the server while this computer is connected to the Wi-Fi network that
the feeder will use:

```sh
cd ../../petkit-compat-server
python3 serve_petkit_api.py \
  --host 0.0.0.0 \
  --port 8080 \
  --profile devices/esp8266/nonos_v2/fresh-element-mini/profile.json \
  --ota-image ../petkit-element-mini-esphome/local-cache/release/petkit-element-mini-kickstart-v2.bin
```

Leave that terminal running and allow inbound TCP port 8080 through the local
firewall.

#### 4. Provision the stock feeder

Open another terminal in the `petkit-compat-server` checkout. Record the
computer's address on the regular 2.4 GHz Wi-Fi network. Set the network name
once, then load its saved password without placing the password itself in shell
history.

On Linux with NetworkManager:

```bash
export ESPHOME_WIFI_SSID='<regular 2.4 GHz Wi-Fi name>'
export ESPHOME_WIFI_PASSWORD="$(nmcli --show-secrets \
  --get-values 802-11-wireless-security.psk \
  connection show "$ESPHOME_WIFI_SSID")"
```

On macOS:

```sh
export ESPHOME_WIFI_SSID='<regular 2.4 GHz Wi-Fi name>'
export ESPHOME_WIFI_PASSWORD="$(security find-generic-password \
  -D 'AirPort network password' -a "$ESPHOME_WIFI_SSID" -gw)"
```

Hold the feeder's Wi-Fi/reset button for about five seconds, until the long
confirmation beep. Connect this computer to the `PETKIT_FEEDER_xyz` network,
then run:

```sh
python3 provision_petkit_device.py \
  --profile devices/esp8266/nonos_v2/fresh-element-mini/profile.json \
  --ssid "$ESPHOME_WIFI_SSID" \
  --server 'http://YOUR_COMPUTER_IP:8080/6/' \
  --timezone '<UTC offset in hours>' \
  --locale '<IANA time zone>' \
  --send
unset ESPHOME_WIFI_PASSWORD
```

Reconnect the computer to the regular 2.4 GHz Wi-Fi network. The server terminal
should show the feeder's startup requests, OTA download, and successful
completion. The transition image is installed in whichever slot Petkit's stock
ESP8266 OTA client chose; step 5 relocates it to the upper slot if needed.

#### 5. Preserve recovery data, migrate Kickstart, and install the final image

Find the Petkit Kickstart address in Home Assistant or the router's client list.
Download and keep its 2 MiB recovery image and its slot status before migrating.

Set the address and the web username from `secrets.yaml`; `curl` prompts for the
web password. Return to the `petkit-element-mini-esphome` checkout first:

```sh
cd ../petkit-element-mini-esphome
export KICKSTART_IP='<Kickstart IP address>'
export KICKSTART_WEB_USERNAME='admin'
curl --digest --user "$KICKSTART_WEB_USERNAME" --fail-with-body \
  --output petkit-post-kickstart.bin \
  "http://$KICKSTART_IP/hub/flash_read"
test "$(wc -c < petkit-post-kickstart.bin | tr -d ' ')" -eq 2097152
curl --digest --user "$KICKSTART_WEB_USERNAME" --fail-with-body \
  --output petkit-post-kickstart-slot-status.json \
  "http://$KICKSTART_IP/hub/slot_status"
```

If the slot status reports `"current_slot": 1`, Kickstart is in the lower slot;
relocate it to the upper slot first, then wait for it to return:

```sh
curl --digest --user "$KICKSTART_WEB_USERNAME" --fail-with-body \
  --request POST \
  "http://$KICKSTART_IP/hub/copy_lower_to_upper_slot?confirm=copy-lower-to-upper-slot"
```

The copy reboots the bridge. Once it returns, confirm that the status reports
`"current_slot": 2` before converting. Display the new status in the terminal
rather than overwriting the status saved with the backup:

```sh
curl --digest --user "$KICKSTART_WEB_USERNAME" --fail-with-body \
  "http://$KICKSTART_IP/hub/slot_status"
```

Convert the bridge to the eboot V1 layout:

```sh
curl --digest --user "$KICKSTART_WEB_USERNAME" --fail-with-body \
  --request POST \
  "http://$KICKSTART_IP/hub/convert?confirm=convert-v2-to-eboot"
```

The request schedules a reboot. Kickstart then converts its own application,
replaces the bootloader, and reboots again. After it returns, check the result:

```sh
curl --digest --user "$KICKSTART_WEB_USERNAME" --fail-with-body \
  "http://$KICKSTART_IP/hub/convert"
```

Proceed when `result` reports `already_converted`, meaning Kickstart recognized
the eboot layout on this boot. If conversion failed, retain the reported result
and resolve the failure before retrying.

Build the final firmware, then install it from the project directory:

```sh
.venv/bin/esphome compile esphome/petkit-feeder.yaml
.venv/bin/esphome upload esphome/petkit-feeder.yaml --device "$KICKSTART_IP"
```

**Take Control in ESPHome Device Builder:** Kickstart advertises the feeder
configuration, so Device Builder can discover it before or after conversion.
Take Control only creates a configuration; it does not migrate Kickstart or
install firmware.
The current feeder YAML uses a local `components/` directory, so it is not yet
a self-contained remote package. Use the command above for this checkout.
Installing through Device Builder requires an import-ready package or a local
copy of the feeder component, together with the YAML's secrets, including the
same `api_key` used by Kickstart.

Home Assistant should reuse the Kickstart device entry and rename it to Petkit
Feeder. Future updates use ESPHome OTA.

The image format, slot behavior, validation, and recovery controls are
explained in the [Kickstart transition guide](esphome/KICKSTART.md).

## Recovery and rollback

If installation stops partway through, first rerun `python3 install.py`. The
guided installer can continue when Kickstart or the final firmware is already
running, including after a manual installation with the same `secrets.yaml`.

Both procedures download a complete 2 MiB flash backup and save the slot status
before requesting migration. Keep the first backup and its status file somewhere
safe and private because the image contains device and Wi-Fi credentials.
A backup downloaded after an interrupted migration may contain fewer recovery
options than the original:

- **Guided installation:** `local-cache/release/petkit-post-kickstart-<timestamp>.bin`.
- **Manual installation:** `petkit-post-kickstart.bin` in the project directory.

The status file has the same filename stem followed by `-slot-status.json`.
Do not overwrite either file when checking the device after relocation.

If the feeder no longer boots, restoring this backup requires opening the
feeder and connecting a 3.3 V USB-to-serial adapter. A power failure during the
final bootloader write is one situation that can require this procedure.

1. Connect the adapter and put the feeder into serial download mode using the
   [hardware and recovery details][nonos-hardware].
2. In the project directory, set `PORT` to your adapter's serial port (for
   example, `/dev/ttyUSB0` on Linux or `/dev/cu.usbserial-...` on macOS).
   Set `BACKUP` to your saved backup: use the actual timestamped filename for a
   guided installation, or `petkit-post-kickstart.bin` for a manual installation.

   ```sh
   PORT='/dev/ttyUSB0'
   BACKUP='petkit-post-kickstart.bin'
   .venv/bin/esptool --chip esp8266 --port "$PORT" --baud 460800 write-flash \
     0x0 "$BACKUP"
   ```

   If the serial connection is unreliable, retry without `--baud 460800`.
3. After the write succeeds, release the Wi-Fi/reset button and turn the
   feeder off and on again without holding the button. The feeder returns to
   Kickstart; run `python3 install.py` to retry installation.

**Returning to stock depends on when the backup was taken.** Before relocation
or conversion, the other slot can still contain a valid stock application.
The authenticated **Switch to other slot** control can boot that application
while the vendor bootloader remains in place. After restoring a backup from
that stage over UART, the same control can select the saved stock application.
Image validity alone does not identify a slot as stock; retain the original
slot status and installation history.

A backup taken after relocation may contain Kickstart in both slots. It can
restore the bridge but cannot recover an overwritten stock application. To
preserve the exact original flash contents, take a full serial backup before
starting either installation method.

### In case you lost your backup

[`earlynerd/petkit-serial-bus`](https://github.com/earlynerd/petkit-serial-bus)
publishes [communication
captures](https://github.com/earlynerd/petkit-serial-bus/tree/main/Logic%20Analyzer%20Captures)
and a [complete ESP8266 flash
dump](https://github.com/earlynerd/petkit-serial-bus/tree/main/flash%20dumps).
The dump can restore stock firmware when your own backup is unavailable, but a
complete restore also copies the donor feeder's serial number.

For wiring, serial download mode, and backup and restore commands, see the
[hardware and recovery details][nonos-hardware].

## Configuration and development

The ESPHome configuration and component documentation are under
[`esphome/`](esphome/). Run its regression suite with:

```sh
cd esphome/tests
./run_tests.sh
```

Open work and hardware checks are tracked in [TODO.md](TODO.md).

[nonos-hardware]: https://github.com/wrobelda/petkit-compat-server/blob/main/devices/esp8266/nonos_v2/HARDWARE.md
