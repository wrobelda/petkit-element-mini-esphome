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
// Framing + CRC independently confirmed from the ISD91230 firmware disassembly
// and the raw logic captures (see ../../tests/). Command PAYLOAD semantics
// below are taken from the captured stock traffic where available, and flagged
// as needing hardware validation where they are not.

#include "esphome/core/component.h"
#include "esphome/core/hal.h"
#include "esphome/components/uart/uart.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/binary_sensor/binary_sensor.h"
#include "petkit_protocol.h"

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
  // Names reflect what is actually evidenced: food/door bytes and two raw
  // 16-bit pairs (adapter pair reads ~0 on battery). Units/scaling unconfirmed.
  void set_food_ok(binary_sensor::BinarySensor *s) { this->food_ok_ = s; }
  void set_door_flag(binary_sensor::BinarySensor *s) { this->door_flag_ = s; }
  void set_adapter_a(sensor::Sensor *s) { this->adapter_a_ = s; }
  void set_adapter_b(sensor::Sensor *s) { this->adapter_b_ = s; }
  void set_battery_a(sensor::Sensor *s) { this->battery_a_ = s; }
  void set_battery_b(sensor::Sensor *s) { this->battery_b_ = s; }

  // High-level actions, callable from YAML lambdas.
  // Queue `portions` dispense turns; they are sent one at a time, each waiting
  // for the M0's 0x0C completion (or a timeout) before the next — matching the
  // ~1 s stock pacing rather than flooding the bus.
  void feed(uint8_t portions);
  // Enqueue one raw dispense command: payload duration,distance,direction,current.
  void dispense(uint8_t duration, uint8_t distance, uint8_t direction, uint8_t current);
  // Door commands take a single payload byte (stock firmware sends 0x1E).
  void open_door(uint8_t param = 0x1E);
  void close_door(uint8_t param = 0x1E);
  void get_status();
  void beep(uint16_t on_ms, uint16_t off_ms, uint16_t count);
  void blink_upper(uint16_t on_ms, uint16_t off_ms, uint16_t count);
  void blink_lower(uint16_t on_ms, uint16_t off_ms, uint16_t count);
  // Pulse the ISD91230 reset line (if configured) to recover a wedged M0.
  void reset_mcu();

 protected:
  // Build and transmit a frame. seq auto-increments.
  void send_packet_(uint8_t type, const uint8_t *payload, uint8_t payload_len);
  void handle_frame_(const uint8_t *frame, uint8_t len);
  void blink_beep_(uint8_t subcommand, uint16_t on_ms, uint16_t off_ms, uint16_t count);
  bool ready_() const { return this->init_step_ >= this->init_seq_len_(); }
  uint8_t init_seq_len_() const;
  void enqueue_dispense_(const uint8_t payload[4]);
  void service_dispense_queue_(uint32_t now);
  void feed_byte_(uint8_t c, uint32_t now);  // RX state machine

  GPIOPin *reset_pin_{nullptr};
  bool send_init_{true};

  binary_sensor::BinarySensor *food_ok_{nullptr};
  binary_sensor::BinarySensor *door_flag_{nullptr};
  sensor::Sensor *adapter_a_{nullptr};
  sensor::Sensor *adapter_b_{nullptr};
  sensor::Sensor *battery_a_{nullptr};
  sensor::Sensor *battery_b_{nullptr};

  // Receive state machine. A frame is AA AA <len> <body...>; we sync on two
  // consecutive 0xAA, then a length byte, then len-3 body bytes.
  enum RxState : uint8_t { RX_SYNC, RX_LEN, RX_BODY };
  RxState rx_state_{RX_SYNC};
  uint8_t rx_aa_{0};       // consecutive 0xAA seen while syncing
  uint8_t rx_buf_[protocol::MAX_FRAME];
  uint8_t rx_len_{0};      // bytes buffered so far
  uint8_t rx_need_{0};     // total frame length once known
  uint32_t last_byte_ms_{0};

  // Startup init state machine. Each config packet is sent, then we wait for
  // the M0's matching ack (same type, len 8, payload 0x01) before the next —
  // mirroring the captured stock boot — with a timeout fallback.
  uint8_t init_step_{0};
  uint32_t init_next_ms_{0};
  bool init_waiting_ack_{false};
  uint32_t init_deadline_{0};

  // Dispense pacing queue.
  uint8_t dispense_pending_[4]{0x00, 0x02, 0x01, 0x50};  // last-enqueued payload
  uint16_t dispense_queue_{0};   // portions still to send
  bool dispense_busy_{false};    // waiting for a 0x0C completion
  uint32_t dispense_deadline_{0};

  uint8_t seq_{0};
};

}  // namespace petkit_feeder
}  // namespace esphome
