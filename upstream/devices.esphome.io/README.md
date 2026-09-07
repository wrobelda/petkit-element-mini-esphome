# ESPHome Devices upstream staging

This directory is shaped like a single `src/docs/devices` entry in the
`esphome/devices.esphome.io` repository. Copy the
`petkit-fresh-element-mini` directory there when this repository has a public
URL.

Before opening an upstream pull request:

1. Publish this repository at its permanent GitHub location.
2. Pin the component source in `config.yaml` to a stable tag or commit.
3. Add product and board photographs that you have permission to publish.

The page and YAML passed `npm run validate-devices` against
`esphome/devices.esphome.io` commit `acc4ef455d5c5405d0d4168576fa03078f4b28c4`.

Keep this as a separate device page rather than a revision of the Fresh Element
Solo page. The products provide similar functions, but the Solo is ESP32-based
and directly controls motor and sensor GPIOs, while the Mini is ESP8266-based
and delegates those functions to a Nuvoton Cortex-M0 over UART.
