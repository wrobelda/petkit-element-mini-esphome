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
- normal ESPHome updates after the initial firmware migration.

One serving is one motor-controller wheel cycle, which Petkit describes as
approximately 5 g. Actual weight varies with the food and hopper level.

## Installation

The wireless installation starts with Petkit's stock firmware, installs a small
ESPHome transition image, then installs the complete feeder firmware. It does
not require opening the feeder. A power failure while the final bootloader
sector is being written can still require serial recovery, which means opening
the feeder. Read the [hardware and serial
connections][mini-hardware] and [recovery procedure][nonos-hardware] before
starting; preserving an exact stock backup requires serial access.

### Guided installation

The guided installer handles the complete migration:

1. Download the supporting projects and prepare the build environment.
2. Ask for Wi-Fi and recovery credentials, then build Kickstart and the final
   feeder firmware.
3. Run the local Petkit API and provision the feeder to download Kickstart.
4. Save a recovery image and install the final firmware.
5. Wait for the final ESPHome API to become reachable.

The installer pauses when you need to put the feeder in setup mode or reconnect
the computer to your normal Wi-Fi network.

```sh
git clone https://github.com/wrobelda/petkit-element-mini-esphome.git
cd petkit-element-mini-esphome
python3 install.py
```

Use the manual procedure below when developing the firmware or diagnosing a
failed stage.

### Manual installation

#### 1. Get the three projects

Keep the checkouts next to each other so the commands below work unchanged:

```sh
git clone https://github.com/wrobelda/petkit-element-mini-esphome.git
git clone https://github.com/wrobelda/petkit-compat-server.git
git clone https://github.com/wrobelda/esphome-kickstart.git
git -C petkit-compat-server checkout --detach 5ac65f80de2a78e659a303442c32a82e23ce49a0
git -C esphome-kickstart checkout --detach 451a67abf2488aef7f0c6d26b9a637d5be328d5e
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

- the target 2.4 GHz Wi-Fi network name and password;
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
computer's address on the target Wi-Fi network. Set the network name once, then
load its saved password without placing the password itself in shell history.

On Linux with NetworkManager:

```bash
export ESPHOME_WIFI_SSID='<target Wi-Fi name>'
export ESPHOME_WIFI_PASSWORD="$(nmcli --show-secrets \
  --get-values 802-11-wireless-security.psk \
  connection show "$ESPHOME_WIFI_SSID")"
```

On macOS:

```sh
export ESPHOME_WIFI_SSID='<target Wi-Fi name>'
export ESPHOME_WIFI_PASSWORD="$(security find-generic-password \
  -D 'AirPort network password' -a "$ESPHOME_WIFI_SSID" -gw)"
```

Hold the feeder's Wi-Fi/reset button for about five seconds, until the long
confirmation beep. Connect this computer to the `PETKIT_FEEDER_...` network,
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

Reconnect the computer to the target Wi-Fi network. The server terminal should
show the feeder's startup requests, OTA download, and successful completion.
The transition image relocates itself to the safe upper slot automatically
when Petkit's stock ESP8266 OTA client initially installs it in the lower slot.

#### 5. Preserve recovery data and install the final image

Find the Petkit Kickstart address in Home Assistant or the router's client list.
Download and keep its 2 MiB recovery image before installing the final firmware.

The recovery image preserves the stock bootloader, device identity, RF data,
system parameters, and the current contents of both application slots. It is
not a pristine stock backup: stock OTA and automatic relocation can overwrite
both stock applications. Keep the image private because it contains device
credentials.

Set the address and the web username from `secrets.yaml`; `curl` prompts for the
web password. Return to the `petkit-element-mini-esphome` checkout first, so the
firmware path below resolves correctly:

```sh
cd ../petkit-element-mini-esphome
export KICKSTART_IP='<Kickstart IP address>'
export KICKSTART_WEB_USERNAME='admin'
curl --digest --user "$KICKSTART_WEB_USERNAME" --fail-with-body \
  --output petkit-post-kickstart.bin \
  "http://$KICKSTART_IP/hub/flash_read"
test "$(wc -c < petkit-post-kickstart.bin | tr -d ' ')" -eq 2097152

curl --digest --user "$KICKSTART_WEB_USERNAME" --fail-with-body \
  --form firmware=@esphome/.esphome/build/petkit-feeder/.pioenvs/petkit-feeder/firmware.factory.bin \
  "http://$KICKSTART_IP/hub/migrate?confirm=replace-vendor-bootloader"
```

Kickstart writes the application, validates the complete factory image and
flash readback, then writes the new bootloader and reboots. Home Assistant should
reuse the Kickstart device entry and rename it to Petkit Feeder. Future updates
use normal ESPHome OTA.

The image format, slot behavior, validation, and recovery controls are
explained in the [Kickstart transition guide](esphome/KICKSTART.md).

## Configuration and development

The ESPHome configuration and component documentation are under
[`esphome/`](esphome/). Run its regression suite with:

```sh
cd esphome/tests
./run_tests.sh
```

Open work and hardware checks are tracked in [TODO.md](TODO.md).

[mini-hardware]: https://github.com/wrobelda/petkit-compat-server/blob/main/devices/esp8266/nonos_v2/fresh-element-mini/HARDWARE.md
[nonos-hardware]: https://github.com/wrobelda/petkit-compat-server/blob/main/devices/esp8266/nonos_v2/HARDWARE.md
