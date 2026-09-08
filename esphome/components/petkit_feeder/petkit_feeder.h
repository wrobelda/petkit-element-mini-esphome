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
#include "esphome/components/text_sensor/text_sensor.h"
#include "petkit_protocol.h"
#include "petkit_framer.h"

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
  explicit PetkitFeeder(GPIOPin *reset_pin) : reset_pin_(reset_pin) {}

  void setup() override;
  void loop() override;
  void update() override;  // polls status
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  // Wiring.
  void set_send_init(bool v) { this->send_init_ = v; }

  // M0 digital inputs and power measurements.
  void set_dispenser_door_feedback(sensor::Sensor *s) { this->dispenser_door_feedback_ = s; }
  void set_food_detected(binary_sensor::BinarySensor *s) { this->food_detected_ = s; }
  void set_dispenser_wheel_feedback(sensor::Sensor *s) { this->dispenser_wheel_feedback_ = s; }
  void set_adapter_adc(sensor::Sensor *s) { this->adapter_adc_ = s; }
  void set_adapter_voltage(sensor::Sensor *s) { this->adapter_voltage_ = s; }
  void set_battery_adc(sensor::Sensor *s) { this->battery_adc_ = s; }
  void set_battery_voltage(sensor::Sensor *s) { this->battery_voltage_ = s; }
  void set_power_source(text_sensor::TextSensor *s) { this->power_source_ = s; }

  // Run a normal stock feed transaction. One serving selects one counted M0 wheel
  // cycle and is approximately 5 g, with the usual variation from kibble size,
  // density, and hopper level.
  void feed(uint8_t servings);
  // Physical-button mode: keep the outlet open and dispense one serving at a
  // time while the button remains held. Releasing the button prevents another
  // serving from starting; an in-progress serving completes before the outlet
  // closes.
  void manual_feed_start();
  void manual_feed_stop();
  // Door commands take a single payload byte (stock firmware sends 0x1E).
  void open_door(uint8_t param = 0x1E);
  void close_door(uint8_t param = 0x1E);
  void get_status();
  void beep(uint16_t on_ms, uint16_t off_ms, uint16_t count);
  void blink_upper(uint16_t on_ms, uint16_t off_ms, uint16_t count);
  void blink_lower(uint16_t on_ms, uint16_t off_ms, uint16_t count);
  // Pulse the ISD91230 reset line to recover a wedged M0.
  void reset_mcu();

 protected:
  // Build and transmit a frame; returns the sequence number used.
  uint8_t send_packet_(uint8_t type, const uint8_t *payload, uint8_t payload_len);
  void handle_frame_(const uint8_t *frame, uint8_t len);
  void blink_beep_(uint8_t subcommand, uint16_t on_ms, uint16_t off_ms, uint16_t count);
  bool ready_() const { return this->init_step_ >= this->init_seq_len_(); }
  uint8_t init_seq_len_() const;
  void service_feed_(uint32_t now);
  void start_counted_motion_();
  void start_closing_(uint32_t now);
  void finish_feed_();
  GPIOPin *reset_pin_{nullptr};
  bool send_init_{true};

  sensor::Sensor *dispenser_door_feedback_{nullptr};
  binary_sensor::BinarySensor *food_detected_{nullptr};
  sensor::Sensor *dispenser_wheel_feedback_{nullptr};
  sensor::Sensor *adapter_adc_{nullptr};
  sensor::Sensor *adapter_voltage_{nullptr};
  sensor::Sensor *battery_adc_{nullptr};
  sensor::Sensor *battery_voltage_{nullptr};
  text_sensor::TextSensor *power_source_{nullptr};

  // Receive frame assembler (see petkit_framer.h).
  protocol::FrameAssembler assembler_;
  uint32_t last_byte_ms_{0};
  uint8_t status_inputs_[3]{0, 0, 0};
  bool have_status_inputs_{false};

  // Startup init state machine. Each config packet is sent, then we wait for
  // the M0's matching ack (same type, len 8, payload 0x01) before the next —
  // mirroring the captured stock boot — with a timeout fallback.
  uint8_t init_step_{0};
  uint32_t init_next_ms_{0};
  bool init_waiting_ack_{false};
  uint32_t init_deadline_{0};
  // Restore the feeder's safe mechanical state after ESP or M0 startup.
  bool close_on_ready_{true};

  enum FeedState : uint8_t {
    FEED_IDLE,
    FEED_WAIT_OPEN,
    FEED_OPEN_DELAY,
    FEED_DISPENSING,
    FEED_MANUAL_REPEAT,
    FEED_CLOSE_DELAY,
    FEED_WAIT_CLOSE,
  };
  FeedState feed_state_{FEED_IDLE};
  uint8_t feed_servings_{1};
  bool manual_feed_{false};
  bool manual_button_held_{false};
  uint8_t feed_door_seq_{0};
  uint8_t feed_motion_seq_{0};
  uint8_t feed_query_seq_{0};
  bool feed_query_pending_{false};
  uint32_t feed_next_action_ms_{0};
  uint32_t feed_deadline_ms_{0};
  uint8_t seq_{0};
};

}  // namespace petkit_feeder
}  // namespace esphome
