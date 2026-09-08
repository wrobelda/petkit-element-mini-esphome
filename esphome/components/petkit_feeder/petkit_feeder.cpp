#include "petkit_feeder.h"
#include "esphome/core/application.h"
#include "esphome/core/log.h"

namespace esphome {
namespace petkit_feeder {

static const char *const TAG = "petkit_feeder";
static const uint32_t MANUAL_REPEAT_GAP_MS = 100;

// The parameter packets the stock firmware sends to the M0 at boot. Stock
// answers the M0 boot announcement with a status request, then sends packet
// types 0x13, 0x03, 0x05, 0x04, 0x06, and 0x0D. The local firmware starts the
// same sequence after every ESP8266 boot without waiting for an announcement,
// so it can also synchronize with an M0 that remained powered. The M0
// acknowledges these command types with a len-8, payload-0x01 frame, although
// some acknowledgements are interleaved with later commands in the capture;
// the timeout keeps that interleaving from blocking progress. The second 0x0D
// is part of that setup.
// Stock uses 0x0E separately for its current indicator state, so this list sets
// the normal one-second connecting blink and plays the startup beep. Wi-Fi
// state changes the indicator to solid from the device YAML. Whether every
// parameter packet is required for motion remains unverified on hardware.
struct InitPacket {
  uint8_t type;
  uint8_t len;
  uint8_t payload[12];
};
static const InitPacket INIT_SEQ[] = {
    {PKT_GET_STATUS, 0, {}},
    {PKT_MOTOR_CFG19, 2, {0x05, 0x7E}},
    {PKT_SET_PARAM3, 4, {0x00, 0x05, 0x00, 0x05}},
    {PKT_SET_PARAM5, 2, {0x00, 0x05}},
    {PKT_SET_PARAM4, 4, {0x00, 0xFF, 0x00, 0xFF}},
    {PKT_SET_PARAM6, 2, {0xFF, 0xFF}},
    {PKT_CONFIG13, 12, {0x00, 0x3C, 0x01, 0x90, 0x0F, 0x01, 0x22, 0x22, 0x01, 0xF4, 0x0F, 0x01}},
    {PKT_CONFIG13, 12, {0x00, 0x3C, 0x01, 0x90, 0x0F, 0x01, 0x22, 0x22, 0x01, 0xF4, 0x0F, 0x01}},
    {PKT_BLINK_BEEP, 7, {0x01, 0x03, 0xE8, 0x03, 0xE8, 0xFF, 0xFF}},
    {PKT_BLINK_BEEP, 7, {0x02, 0x00, 0x00, 0x03, 0xE8, 0xFF, 0xFF}},
    {PKT_BLINK_BEEP, 7, {0x03, 0x00, 0xC8, 0x00, 0xC8, 0x00, 0x02}},
};
static const uint8_t INIT_SEQ_COUNT = sizeof(INIT_SEQ) / sizeof(INIT_SEQ[0]);
static const uint32_t INIT_ACK_TIMEOUT_MS = 300;  // wait this long for an ack
static const uint32_t INIT_GAP_MS = 20;           // gap after an ack before next

void PetkitFeeder::setup() {
  if (this->reset_pin_ != nullptr) {
    this->reset_pin_->setup();
    // Release the M0 from reset. Pin inversion belongs in the YAML so these
    // logical levels stay independent of the board's electrical polarity.
    this->reset_pin_->digital_write(false);
  }
  // Kick the init sequence shortly after boot so the M0 UART is up first.
  this->init_step_ = this->send_init_ ? 0 : INIT_SEQ_COUNT;
  this->init_next_ms_ = millis() + 500;
}

uint8_t PetkitFeeder::send_packet_(uint8_t type, const uint8_t *payload, uint8_t payload_len) {
  uint8_t frame[protocol::MAX_FRAME];
  uint8_t seq = this->seq_++;
  uint8_t len = protocol::build_frame(frame, type, seq, payload, payload_len);
  this->write_array(frame, len);
  ESP_LOGV(TAG, "TX type=0x%02X seq=%u len=%u", type, seq, len);
  return seq;
}

uint8_t PetkitFeeder::init_seq_len_() const { return INIT_SEQ_COUNT; }

void PetkitFeeder::loop() {
  const uint32_t now = App.get_loop_component_start_time();

  // Advance the boot handshake: send a packet, then wait for the M0's ack
  // (handled in handle_frame_) before the next, with a timeout fallback.
  if (this->init_step_ < INIT_SEQ_COUNT) {
    if (!this->init_waiting_ack_ && now >= this->init_next_ms_) {
      const InitPacket &p = INIT_SEQ[this->init_step_];
      this->send_packet_(p.type, p.payload, p.len);
      this->init_waiting_ack_ = true;
      this->init_deadline_ = now + INIT_ACK_TIMEOUT_MS;
    } else if (this->init_waiting_ack_ && now >= this->init_deadline_) {
      ESP_LOGW(TAG, "No ack for init step %u (type 0x%02X); continuing", this->init_step_,
               INIT_SEQ[this->init_step_].type);
      this->init_step_++;
      this->init_waiting_ack_ = false;
      this->init_next_ms_ = now + INIT_GAP_MS;
    }
  }

  while (this->available()) {
    uint8_t c;
    this->read_byte(&c);
    this->last_byte_ms_ = now;
    if (this->assembler_.feed(c))
      this->handle_frame_(this->assembler_.buf(), this->assembler_.len());
  }

  // Drop a partial frame if the bus goes quiet mid-frame.
  if (this->assembler_.in_progress() && (now - this->last_byte_ms_) > 50)
    this->assembler_.reset();

  // Advance the active stock-style feed transaction. A motion timeout resets
  // the M0 first because no safe serial stop command has been established.
  if (this->ready_() && this->close_on_ready_) {
    this->close_on_ready_ = false;
    this->close_door();
  }
  if (this->ready_())
    this->service_feed_(now);
}

void PetkitFeeder::handle_frame_(const uint8_t *frame, uint8_t len) {
  if (!protocol::frame_is_valid(frame, len)) {
    ESP_LOGW(TAG, "Bad CRC on frame type=0x%02X len=%u", len >= 4 ? frame[3] : 0, len);
    return;
  }
  const uint8_t type = frame[3];
  const uint8_t seq = frame[4];
  const uint8_t *payload = &frame[5];
  const uint8_t payload_len = len - protocol::OVERHEAD;

  // Boot handshake: advance when the M0 acks the packet we are waiting on.
  if (this->init_waiting_ack_ && this->init_step_ < INIT_SEQ_COUNT &&
      type == INIT_SEQ[this->init_step_].type && len == 8 && payload[0] == 0x01) {
    this->init_step_++;
    this->init_waiting_ack_ = false;
    this->init_next_ms_ = millis() + INIT_GAP_MS;
    return;
  }

  switch (type) {
    case PKT_STATUS: {
      // Payload layout (digital inputs confirmed from the M0 disassembly):
      //   [0] PB8 dispenser-door feedback, [1] PB6 optical food input,
      //   [2] PB7 dispenser-wheel sensor,
      //   [3:4] adapter ADC, [5:6] adapter centivolts,
      //   [7:8] battery ADC, [9:10] battery centivolts.
      protocol::Status st = protocol::parse_status(frame, len);
      const uint8_t *status_payload = &frame[5];
      if (!this->have_status_inputs_ || status_payload[0] != this->status_inputs_[0] ||
          status_payload[1] != this->status_inputs_[1] || status_payload[2] != this->status_inputs_[2]) {
        ESP_LOGI(TAG, "M0 digital inputs: %u %u %u", status_payload[0], status_payload[1], status_payload[2]);
        this->status_inputs_[0] = status_payload[0];
        this->status_inputs_[1] = status_payload[1];
        this->status_inputs_[2] = status_payload[2];
        this->have_status_inputs_ = true;
      }
      if (this->dispenser_door_feedback_ != nullptr)
        this->dispenser_door_feedback_->publish_state(st.dispenser_door_feedback);
      if (this->food_detected_ != nullptr)
        this->food_detected_->publish_state(st.food_detected);
      if (this->dispenser_wheel_feedback_ != nullptr)
        this->dispenser_wheel_feedback_->publish_state(st.dispenser_wheel_feedback);
      if (payload_len >= 11) {
        if (this->adapter_adc_ != nullptr)
          this->adapter_adc_->publish_state(st.adapter_adc);
        if (this->adapter_voltage_ != nullptr)
          this->adapter_voltage_->publish_state(st.adapter_centivolts * 0.01f);
        if (this->battery_adc_ != nullptr)
          this->battery_adc_->publish_state(st.battery_adc);
        if (this->battery_voltage_ != nullptr)
          this->battery_voltage_->publish_state(st.battery_centivolts * 0.01f);
        if (this->power_source_ != nullptr)
          this->power_source_->publish_state(st.adapter_centivolts == 0 ? "Battery" : "Mains");
      }
      break;
    }
    case PKT_DISPENSED: {
      const protocol::DispenseResult result = protocol::parse_dispense_result(frame, len);
      if (result.valid) {
        ESP_LOGI(TAG, "Wheel result seq=%u count=%u complete=%s detail=%02X%02X%02X", seq,
                 result.wheel_count, YESNO(result.complete), result.detail[0], result.detail[1],
                 result.detail[2]);
      } else {
        ESP_LOGW(TAG, "Malformed dispense result (seq=%u len=%u)", seq, len);
        break;
      }

      if (this->feed_query_pending_ && seq == this->feed_query_seq_)
        this->feed_query_pending_ = false;

      if (this->feed_state_ == FEED_DISPENSING && seq == this->feed_motion_seq_ && result.complete) {
        ESP_LOGI(TAG, "Feed motion complete: %u serving(s)", this->feed_servings_);
        if (this->manual_feed_ && this->manual_button_held_) {
          // A type-0x0C completion marks the boundary between the discrete
          // servings during a held manual feed. Stock waits 10 FreeRTOS ticks
          // (100 ms) before it queues the next serving.
          this->feed_state_ = FEED_MANUAL_REPEAT;
          this->feed_next_action_ms_ = millis() + MANUAL_REPEAT_GAP_MS;
        } else {
          this->feed_state_ = FEED_CLOSE_DELAY;
          this->feed_next_action_ms_ = millis() + 1000;
        }
      }
      break;
    }
    case PKT_DOOR_OPENED:
      ESP_LOGI(TAG, "Door open complete (seq=%u)", seq);
      if (this->feed_state_ == FEED_WAIT_OPEN && seq == this->feed_door_seq_) {
        this->feed_state_ = FEED_OPEN_DELAY;
        this->feed_next_action_ms_ = millis() + 400;
      }
      break;
    case PKT_DOOR_CLOSED:
      ESP_LOGI(TAG, "Door close complete (seq=%u)", seq);
      if (this->feed_state_ == FEED_WAIT_CLOSE && seq == this->feed_door_seq_)
        this->finish_feed_();
      break;
    default:
      ESP_LOGV(TAG, "RX type=0x%02X seq=%u len=%u", type, seq, len);
      break;
  }
}

void PetkitFeeder::update() {
  if (!this->ready_())
    return;  // still handshaking
  this->get_status();
}

// ---- high level actions ----

void PetkitFeeder::get_status() { this->send_packet_(PKT_GET_STATUS, nullptr, 0); }

static const uint8_t WHEEL_QUERY[4] = {0x00, 0x02, 0x01, 0x50};
static const uint32_t DOOR_TIMEOUT_MS = 4000;

void PetkitFeeder::feed(uint8_t servings) {
  if (!this->ready_()) {
    ESP_LOGW(TAG, "Feed ignored: still initializing");
    return;
  }
  if (this->feed_state_ != FEED_IDLE) {
    ESP_LOGW(TAG, "Feed ignored: another feed is active");
    return;
  }
  if (!protocol::serving_count_is_valid(servings)) {
    ESP_LOGW(TAG, "Feed ignored: serving count %u is outside 1..%u", servings,
             protocol::MAX_SERVINGS);
    return;
  }
  this->manual_feed_ = false;
  this->manual_button_held_ = false;
  this->feed_servings_ = servings;
  const uint8_t door_param = 0x1E;
  this->feed_door_seq_ = this->send_packet_(PKT_OPEN_DOOR, &door_param, 1);
  this->feed_state_ = FEED_WAIT_OPEN;
  this->feed_deadline_ms_ = millis() + DOOR_TIMEOUT_MS;
  ESP_LOGI(TAG, "Feed started: %u serving(s)", servings);
}

void PetkitFeeder::manual_feed_start() {
  if (!this->ready_()) {
    ESP_LOGW(TAG, "Manual feed ignored: still initializing");
    return;
  }
  if (this->manual_feed_ && this->feed_state_ != FEED_IDLE) {
    if (this->feed_state_ == FEED_CLOSE_DELAY || this->feed_state_ == FEED_WAIT_CLOSE)
      ESP_LOGW(TAG, "Manual feed ignored: outlet is closing");
    else
      this->manual_button_held_ = true;
    return;
  }
  if (this->feed_state_ != FEED_IDLE) {
    ESP_LOGW(TAG, "Manual feed ignored: another feed is active");
    return;
  }

  this->manual_feed_ = true;
  this->manual_button_held_ = true;
  this->feed_servings_ = 1;
  const uint8_t door_param = 0x1E;
  this->feed_door_seq_ = this->send_packet_(PKT_OPEN_DOOR, &door_param, 1);
  this->feed_state_ = FEED_WAIT_OPEN;
  this->feed_deadline_ms_ = millis() + DOOR_TIMEOUT_MS;
  ESP_LOGI(TAG, "Manual feed started");
}

void PetkitFeeder::manual_feed_stop() {
  this->manual_button_held_ = false;
  if (!this->manual_feed_ || this->feed_state_ == FEED_IDLE)
    return;

  ESP_LOGI(TAG, "Manual feed released");
  // The first serving is latched by the press and always completes. A release
  // only closes an outlet that is waiting between completed manual servings.
  if (this->feed_state_ == FEED_MANUAL_REPEAT) {
    this->feed_state_ = FEED_CLOSE_DELAY;
    this->feed_next_action_ms_ = millis();
  }
}

void PetkitFeeder::start_counted_motion_() {
  const uint8_t payload[4] = {this->feed_servings_, 0x01, 0x01, 0x50};
  this->feed_motion_seq_ = this->send_packet_(PKT_DISPENSE, payload, 4);
  this->feed_state_ = FEED_DISPENSING;
  this->feed_query_pending_ = false;
  this->feed_next_action_ms_ = millis() + 1000;
  this->feed_deadline_ms_ = millis() + protocol::motion_timeout_ms(this->feed_servings_);
}

void PetkitFeeder::start_closing_(uint32_t now) {
  const uint8_t param = 0x1E;
  this->feed_query_pending_ = false;
  this->feed_door_seq_ = this->send_packet_(PKT_CLOSE_DOOR, &param, 1);
  this->feed_state_ = FEED_WAIT_CLOSE;
  this->feed_deadline_ms_ = now + DOOR_TIMEOUT_MS;
}

void PetkitFeeder::finish_feed_() {
  this->feed_state_ = FEED_IDLE;
  this->manual_feed_ = false;
  this->manual_button_held_ = false;
  this->feed_query_pending_ = false;
  this->feed_deadline_ms_ = 0;
  ESP_LOGI(TAG, "Feed transaction finished");
}

void PetkitFeeder::service_feed_(uint32_t now) {
  if (this->feed_state_ == FEED_IDLE)
    return;

  if ((this->feed_state_ == FEED_WAIT_OPEN || this->feed_state_ == FEED_DISPENSING ||
       this->feed_state_ == FEED_WAIT_CLOSE) &&
      static_cast<int32_t>(now - this->feed_deadline_ms_) >= 0) {
    if (this->feed_state_ == FEED_WAIT_CLOSE) {
      ESP_LOGW(TAG, "Door-close completion timed out");
      this->finish_feed_();
    } else if (this->feed_state_ == FEED_DISPENSING && this->reset_pin_ != nullptr) {
      // A zero-count command is not a stop: the M0 decrements it and can wrap
      // to 255. Reset is therefore the only verified way to stop timed-out
      // motion before asking the freshly initialized M0 to close the door.
      ESP_LOGE(TAG, "Feed motion timed out; resetting motor controller");
      this->reset_mcu();
    } else {
      ESP_LOGW(TAG, "Feed stage timed out; closing door");
      this->start_closing_(now);
    }
    return;
  }

  if (static_cast<int32_t>(now - this->feed_next_action_ms_) < 0)
    return;

  switch (this->feed_state_) {
    case FEED_OPEN_DELAY:
      this->start_counted_motion_();
      break;
    case FEED_MANUAL_REPEAT:
      if (this->manual_button_held_)
        this->start_counted_motion_();
      else
        this->start_closing_(now);
      break;
    case FEED_DISPENSING:
      // Query replies are immediate. If one is lost, the next one-second tick
      // supersedes it rather than stalling the transaction.
      this->feed_query_seq_ = this->send_packet_(PKT_DISPENSE, WHEEL_QUERY, 4);
      this->feed_query_pending_ = true;
      this->feed_next_action_ms_ = now + 1000;
      break;
    case FEED_CLOSE_DELAY:
      this->start_closing_(now);
      break;
    default:
      break;
  }
}

void PetkitFeeder::open_door(uint8_t param) {
  if (!this->ready_()) {
    ESP_LOGW(TAG, "open_door ignored: still initializing");
    return;
  }
  this->send_packet_(PKT_OPEN_DOOR, &param, 1);
}

void PetkitFeeder::close_door(uint8_t param) {
  if (!this->ready_()) {
    ESP_LOGW(TAG, "close_door ignored: still initializing");
    return;
  }
  this->send_packet_(PKT_CLOSE_DOOR, &param, 1);
}

void PetkitFeeder::blink_beep_(uint8_t subcommand, uint16_t on_ms, uint16_t off_ms, uint16_t count) {
  uint8_t p[7] = {subcommand,
                  static_cast<uint8_t>(on_ms >> 8), static_cast<uint8_t>(on_ms & 0xFF),
                  static_cast<uint8_t>(off_ms >> 8), static_cast<uint8_t>(off_ms & 0xFF),
                  static_cast<uint8_t>(count >> 8), static_cast<uint8_t>(count & 0xFF)};
  this->send_packet_(PKT_BLINK_BEEP, p, 7);
}

void PetkitFeeder::blink_upper(uint16_t on_ms, uint16_t off_ms, uint16_t count) {
  this->blink_beep_(1, on_ms, off_ms, count);
}
void PetkitFeeder::blink_lower(uint16_t on_ms, uint16_t off_ms, uint16_t count) {
  this->blink_beep_(2, on_ms, off_ms, count);
}
void PetkitFeeder::beep(uint16_t on_ms, uint16_t off_ms, uint16_t count) {
  this->blink_beep_(3, on_ms, off_ms, count);
}

void PetkitFeeder::reset_mcu() {
  if (this->reset_pin_ == nullptr) {
    ESP_LOGW(TAG, "reset_pin not configured");
    return;
  }
  ESP_LOGW(TAG, "Pulsing ISD91230 reset");
  this->reset_pin_->digital_write(true);
  delay(20);
  this->reset_pin_->digital_write(false);
  // Drop any active motion state so nothing resumes across an M0 reset.
  if (this->feed_state_ != FEED_IDLE)
    this->finish_feed_();
  this->assembler_.reset();
  // Restart the handshake cleanly, clearing any pending ack-wait state.
  this->init_step_ = this->send_init_ ? 0 : INIT_SEQ_COUNT;
  this->init_waiting_ack_ = false;
  this->init_deadline_ = 0;
  this->init_next_ms_ = millis() + 500;
  this->close_on_ready_ = true;
}

void PetkitFeeder::dump_config() {
  ESP_LOGCONFIG(TAG, "Petkit Feeder (ISD91230 bus master):");
  ESP_LOGCONFIG(TAG, "  Init handshake: %s", this->send_init_ ? "yes" : "no");
  LOG_PIN("  Reset pin: ", this->reset_pin_);
}

}  // namespace petkit_feeder
}  // namespace esphome
