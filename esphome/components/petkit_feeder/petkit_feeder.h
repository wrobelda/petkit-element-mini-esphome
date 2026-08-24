#pragma once

// ESPHome external component that replaces the Petkit "Feedermini" (Fresh
// Element Mini) stock ESP8266 firmware. The ESP8266 does NOT drive the motor
// or read the food/door sensors directly: those belong to a Nuvoton ISD91230
// Cortex-M0 co-processor. The ESP8266 only exchanges short framed packets with
// the M0 over its single hardware UART (GPIO1 TX / GPIO3 RX, 115200 8N1).
//
// Frame layout on the bus:
//   AA AA <len> <type> <seq> <payload...> <crc_hi> <crc_lo>
//   len  = total frame length including the two 0xAA header bytes and the CRC
//   crc  = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect/xor),
//          computed over the whole frame; a recompute over the received frame
//          including its CRC bytes yields 0 when valid.
//
// Protocol facts are from https://github.com/earlynerd/petkit-serial-bus

#include "esphome/core/component.h"
#include "esphome/core/hal.h"
#include "esphome/components/uart/uart.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/binary_sensor/binary_sensor.h"

namespace esphome {
namespace petkit_feeder {

// Command / response packet types on the bus.
enum PetkitPacketType : uint8_t {
  PKT_BOOT = 0x00,
  PKT_GET_STATUS = 0x01,
  PKT_STATUS = 0x02,
  PKT_SET_PARAM3 = 0x03,
  PKT_SET_PARAM4 = 0x04,
  PKT_SET_PARAM5 = 0x05,
  PKT_SET_PARAM6 = 0x06,
  PKT_OPEN_DOOR = 0x07,
  PKT_DOOR_OPENED = 0x08,
  PKT_CLOSE_DOOR = 0x09,
  PKT_DOOR_CLOSED = 0x0A,
  PKT_DISPENSE = 0x0B,
  PKT_DISPENSED = 0x0C,
  PKT_CONFIG13 = 0x0D,
  PKT_BLINK_BEEP = 0x0E,
  PKT_SLEEP = 0x0F,
  PKT_WAKE = 0x10,
  PKT_REQ17 = 0x11,
  PKT_REPLY18 = 0x12,
  PKT_MOTOR_CFG19 = 0x13,
  PKT_REPLY20 = 0x14,
  PKT_DISPENSE_DATA21 = 0x15,
};

class PetkitFeeder : public PollingComponent, public uart::UARTDevice {
 public:
  void setup() override;
  void loop() override;
  void update() override;  // polls status
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  // Wiring.
  void set_reset_pin(GPIOPin *pin) { this->reset_pin_ = pin; }
  void set_send_init(bool v) { this->send_init_ = v; }

  // Diagnostic sensors (all optional; owned elsewhere, we only publish).
  void set_food_ok(binary_sensor::BinarySensor *s) { this->food_ok_ = s; }
  void set_door_fault(binary_sensor::BinarySensor *s) { this->door_fault_ = s; }
  void set_adapter_adc(sensor::Sensor *s) { this->adapter_adc_ = s; }
  void set_adapter_mv(sensor::Sensor *s) { this->adapter_mv_ = s; }
  void set_battery_adc(sensor::Sensor *s) { this->battery_adc_ = s; }
  void set_battery_mv(sensor::Sensor *s) { this->battery_mv_ = s; }

  // High-level actions, callable from YAML lambdas.
  // Dispense `portions` short wheel turns using the stock dispense parameters.
  void feed(uint8_t portions);
  void dispense(uint8_t duration, uint8_t distance, uint8_t direction, uint8_t current);
  void open_door(uint8_t duration, uint8_t strength);
  void close_door(uint8_t duration, uint8_t strength);
  void get_status();
  void beep(uint16_t on_ms, uint16_t off_ms, uint16_t count);
  void blink_upper(uint16_t on_ms, uint16_t off_ms, uint16_t count);
  void blink_lower(uint16_t on_ms, uint16_t off_ms, uint16_t count);
  // Pulse the ISD91230 reset line (if configured) to recover a wedged M0.
  void reset_mcu();

 protected:
  static uint16_t crc16_ccitt(const uint8_t *data, size_t len);
  // Build and transmit a frame. seq auto-increments unless you pass one.
  void send_packet_(uint8_t type, const uint8_t *payload, uint8_t payload_len);
  void handle_frame_(const uint8_t *frame, uint8_t len);
  void blink_beep_(uint8_t subcommand, uint16_t on_ms, uint16_t off_ms, uint16_t count);

  GPIOPin *reset_pin_{nullptr};
  bool send_init_{true};

  binary_sensor::BinarySensor *food_ok_{nullptr};
  binary_sensor::BinarySensor *door_fault_{nullptr};
  sensor::Sensor *adapter_adc_{nullptr};
  sensor::Sensor *adapter_mv_{nullptr};
  sensor::Sensor *battery_adc_{nullptr};
  sensor::Sensor *battery_mv_{nullptr};

  // Receive assembly buffer.
  uint8_t rx_buf_[64];
  uint8_t rx_len_{0};
  uint32_t last_byte_ms_{0};

  // Startup init state machine.
  uint8_t init_step_{0};
  uint32_t init_next_ms_{0};

  uint8_t seq_{0};
};

}  // namespace petkit_feeder
}  // namespace esphome
