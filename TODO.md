# Remaining work

## Wireless installation and upstreaming

### Normal ESPHome OTA handoff

Replace the temporary `/hub/migrate` upload with a Kickstart OTA backend that
accepts a normal authenticated request from ESPHome Device Builder and handles
the layout transition internally. Users should not need to select a migration
route or upload through a separate web interface.

The backend must distinguish these update paths:

- eboot V1 to eboot V1;
- non-OS V2 to non-OS V2;
- non-OS V2 to eboot V1.

Define authenticated layout metadata, image validation, safe write order, and
power-loss behavior for each path. Implement and demonstrate the backend in
Kickstart before discussing whether ESPHome should absorb that backend or the
wider Kickstart project.

Make Home Assistant and Device Builder recognize the transition image and
select the final device configuration. Verify that the automated handoff keeps
one device-registry entry and updates its displayed name.

### Publication

- Submit the generic non-OS V2 to eboot V1 components to ESPHome Kickstart.
  Keep hardware layout values in per-device YAML and public examples free of
  private checkout paths. Remove compatibility aliases for unreleased names.
- Publish the reviewed compatibility-server and Kickstart revisions used by
  the installer, then publish and tag this repository.
- Pin the Fresh Element Mini catalog page's component source to the stable
  revision and submit the page from the `devices.esphome.io` checkout.

### Additional integrations and devices

- Add recognition of ESPHome Petkit devices to the visual editor in
  `homeassistant-extras/petkit-device-cards`, using ESPHome project metadata.
  An explicitly selected Home Assistant device ID works, but the editor lists
  only devices from the cloud `petkit` integration. Keep the local device's
  actual integration identity.
- Validate each additional hardware profile before offering firmware. Confirm
  the MCU, flash size, slot boundaries, protected tail sectors, and stock
  hardware identifier; product labels such as P530, D2, D2-C, and `HW2` do not
  establish compatibility.

## Offline feeding history

Design persistent feeding-history replay jointly with ESPHome and Home
Assistant. Events completed while Home Assistant is disconnected need:

- durable device records with original timestamps and stable event IDs;
- queries for events since a saved cursor;
- receiver-side deduplication and acknowledgement.

ESPHome event responses do not provide those replay fields. The design must
preserve the distinction between discrete feed events and numeric statistics.
Decide separately whether the Petkit cards need a bounded snapshot of the
current day's schedule and status; that snapshot would be a Petkit convention,
not a general ESPHome replay mechanism.

## Feeder operation and recovery

### Counted feeding and manual controls

- Weigh repeated 1-, 2-, and 3-serving samples with representative food to
  calibrate the nominal 5 g conversion against kibble size, density, and hopper
  level.
- Validate held-button feeding on hardware: one serving at a time, a 100 ms gap
  between completions and new requests, and an open outlet between servings.
  Release the button throughout the gap to confirm that no further serving
  starts. The press is debounced, while release must stop repetition immediately.
- Verify the active level and debounce behavior of the Wi-Fi/reset button.

### Startup, faults, and timeouts

- Validate jammed-door and motion-timeout recovery on hardware.
- Open the outlet, reboot only the ESP8266, and verify that the post-handshake
  close command is acknowledged and physically closes the outlet.
- Confirm that an ISD91230 motor controller already running at ESP8266 startup
  accepts the complete boot handshake and final close command without a fresh
  motor-controller power-on announcement.
- Determine which boot configuration packets are required.
- Reconstruct the stock `FEED_FIRST` entry condition and recovery policy before
  implementing its `FF 01 01 50` free-running motion. Ordinary fixed-amount feeds
  use the direct `N 01 01 50` counted path.

Extract the feed transaction state machine from ESPHome I/O and add native
coverage for:

- matched and stale completion frames;
- open, close, motion, and initialization timeouts;
- motor-controller reset and post-reset close;
- held manual feeding and button release.

## Sensors and indicators

- Establish the active polarities and physical meaning of the door and wheel
  sensor levels.
- Calibrate food detection with the optical assembly connected and installed
  in the container.
- Resolve upper/Wi-Fi indicator mode 0, permanent disable, and night mode.
- Determine the lower/food indicator's state policy and reproduce stock startup
  timing.
