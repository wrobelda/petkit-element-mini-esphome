# Remaining work

## Wireless installation and upstreaming

- Replace the temporary `/hub/migrate` installer with an ESPHome OTA design
  that understands V1/eboot and non-OS SDK V2 images. It must handle V1→V1,
  V2→V2, V2→V1, and V1→V2 explicitly, with authenticated layout metadata,
  safe write order, validation, and power-loss behavior. While Kickstart is
  running, its OTA backend must detect the required transition and perform it
  internally when Device Builder sends a normal OTA request; the user must not
  call a separate route or select the migration manually. Implement and
  demonstrate that behavior in ESPHome Kickstart first, then ask the ESPHome
  and Kickstart maintainers whether ESPHome should absorb the backend or the
  wider Kickstart project.
- Implement and hardware-test the reverse V1/eboot→paired non-OS V2 migration
  in ESPHome Kickstart. It should let an installed ESPHome device restore its
  vendor bootloader and stock application without UART access, while preserving
  RF calibration, system parameters, and per-device identity. Reuse the
  existing layout schema, upload authentication, image validators, staged
  writes, readback checks, and bootloader-last commit where they apply; do not
  assume that reversing the write order is sufficient. Establish how the
  restored non-OS bootloader selects a known-good upper-slot V2 image before
  replacing the lower application, then use the stock OTA path to restore the
  other slot if needed. The complete investigation brief is
  [reverse-migration investigation
  brief](research/ESP8266-EBOOT-V1-TO-NONOS-V2-PROMPT.md).
- Make the Home Assistant/ESPHome Device Builder flow recognize a transition
  image and install the final device configuration without a separate manual
  web upload. Retest that Home Assistant retains one device-registry entry and
  changes its displayed name after migration.
- Prepare the generic component changes for the ESPHome Kickstart repository.
  Keep device layout values in per-device YAML, remove local checkout paths
  from published examples, and do not retain compatibility aliases for names
  that have never been released.
- Choose and add a repository license before publication. Publish this
  repository, pin the installer's compatibility-server and Kickstart
  dependencies to reviewed tags or commits, pin the staged device page's
  component source to a stable tag or commit, add publishable product/board
  photographs, and submit
  `upstream/devices.esphome.io/petkit-fresh-element-mini`. The page and YAML
  already pass the current devices.esphome.io validators.
- Ask `homeassistant-extras/petkit-device-cards` to recognize ESPHome Petkit
  devices by their ESPHome project metadata. Its card works with an explicitly
  selected Home Assistant device ID, but its visual editor currently lists only
  devices from the cloud `petkit` integration. Do not give the local device
  false cloud-integration identifiers.
- Verify the transition profile on any additional Petkit hardware before
  offering it. Confirm the MCU, real flash size, slot boundaries, protected
  tail sectors, and stock hardware identifier rather than assuming all P530,
  D2, D2-C, or `HW2` units are identical.

## Feeder functionality

- Design persistent offline feeding history jointly with ESPHome and Home
  Assistant. ESPHome event responses carry no source timestamp, stable event
  ID, replay cursor, or acknowledgement. Home Assistant can store a backdated
  event through `async_fire(..., time_fired=...)`, while Feedreader demonstrates
  a persisted deduplication cursor; external statistics are timestamped and
  idempotent but apply to numeric statistics rather than discrete feed events.
  A complete design needs durable device records, original timestamps, stable
  IDs, query-since semantics, receiver-side deduplication, and acknowledgement.
  Decide separately whether the Petkit cards justify a bounded current-day
  schedule/status snapshot; that would be a Petkit convention, not a general
  ESPHome event-replay mechanism.
- Reconstruct the stock `FEED_FIRST` state transition and recovery policy. The
  stock traces exercise its `FF 01 01 50` free-running path, while ordinary
  fixed-amount feeds use the direct `N 01 01 50` counted path. Do not add the
  first-feed path until its entry condition and safety behavior are known.
- Weigh repeated 1, 2, and 3-serving samples with representative food. Live
  tests already confirm that `N 01 01 50` advances the settled M0 wheel count
  by N and completes the full door sequence; only the nominal weight remains
  to be calibrated against kibble size, density, and hopper level.
- Validate jammed-door and motion-timeout recovery on hardware. Normal door
  open, close, and sequence-matched completion behavior passed in the 1, 2,
  and 3-serving tests.
- Open the outlet, reboot only the ESP8266, and verify that the post-handshake
  close command is acknowledged and physically closes it. Stock checks two
  door-error bits and dispatches repair events from its state-report path, but
  normal cold-boot traces do not contain an unconditional close command and it
  is not yet known whether an open outlet at boot raises either fault bit.
- Hardware-verify the 100 ms manual-feed repeat delay now recovered from the
  stock state machine: a held press should request one serving at a time, keep
  the outlet open during the 100 ms gap, and close it when release prevents the
  next request. Test release throughout that gap now that the press edge is
  debounced but the release edge stops repetition immediately. The
  `petkit_manual_feed.sal` recording from
  `earlynerd/petkit-serial-bus` has no door or dispenser packets, so it cannot
  independently verify that timing.
- Extract the feed transaction state machine from ESPHome I/O and add native
  tests for matched and stale completion frames, open and close timeouts,
  motion-timeout reset, post-reset close, initialization timeouts, and held
  manual-feed release.
- Determine which boot configuration packets are required instead of replaying
  the complete observed sequence by assumption.
- Hardware-test an ESP8266-only reboot while the M0 remains running. The local
  firmware starts the complete handshake after every ESP8266 boot without
  waiting for an M0 power-on announcement; confirm that the running M0 accepts
  every step and the final close command.
- Establish the polarity and meaning of both status bytes, then identify units
  and scaling for all four raw 16-bit status values. Calibrate the food sensor
  with its optical assembly connected and installed in the container.
- Finish the two-LED policy: resolve the upper/Wi-Fi mode-0 behavior, reproduce
  permanent Wi-Fi-indicator disable and night mode, determine the lower
  food-indicator state policy, and match the stock startup timing.
- Verify the active level and debounce behavior of the Wi-Fi button. GPIO13's
  manual-feed input is verified active-low with the stock internal pull-up: it
  measures about 3.2 V released and 0 V pressed. Re-test held-button repetition
  after restoring its motor callbacks.
