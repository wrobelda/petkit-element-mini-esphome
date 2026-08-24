#include "petkit_feeder.h"
#include "esphome/core/log.h"

namespace esphome {
namespace petkit_feeder {

static const char *const TAG = "petkit_feeder";

// The parameter packets the stock firmware sends to the M0 at boot, in the
// exact type order and with the exact payloads seen in a captured factory boot
// (README "Boot up Sequence"): 0x13, 0x03, 0x05, 0x04, 0x06, 0x0D. The M0 acks
// each with a same-type, len-8, payload-0x01 frame; we advance on that ack (or
// a timeout). Two divergences from stock, both deliberate and unverified on
// hardware: (1) stock re-sends 0x0D a second time — we send it once; (2) stock
// then sends five 0x0E LED/beep packets (startup light/beep animation) which we
// omit to avoid beeping on every boot. Whether every packet here is REQUIRED
// for the M0 to accept feed/door commands is not established without hardware.
struct InitPacket {
  uint8_t type;
  uint8_t len;
  uint8_t payload[12];
};
static const InitPacket INIT_SEQ[] = {
    {PKT_MOTOR_CFG19, 2, {0x05, 0x7E}},
    {PKT_SET_PARAM3, 4, {0x00, 0x05, 0x00, 0x05}},
    {PKT_SET_PARAM5, 2, {0x00, 0x05}},
    {PKT_SET_PARAM4, 4, {0x00, 0xFF, 0x00, 0xFF}},
    {PKT_SET_PARAM6, 2, {0xFF, 0xFF}},
    {PKT_CONFIG13, 12, {0x00, 0x3C, 0x01, 0x90, 0x0F, 0x01, 0x22, 0x22, 0x01, 0xF4, 0x0F, 0x01}},
};
static const uint8_t INIT_SEQ_COUNT = sizeof(INIT_SEQ) / sizeof(INIT_SEQ[0]);
static const uint32_t INIT_ACK_TIMEOUT_MS = 300;  // wait this long for an ack
static const uint32_t INIT_GAP_MS = 20;           // gap after an ack before next

void PetkitFeeder::setup() {
  if (this->reset_pin_ != nullptr) {
    this->reset_pin_->setup();
    // Release the M0 from reset (drive the non-reset level). The board boots
    // the M0 normally with this line low, so low == running.
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
  const uint32_t now = millis();

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

  // Pace queued dispense commands (only once init has completed).
  if (this->ready_())
    this->service_dispense_queue_(now);
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
      // Payload layout (offsets confirmed against captures):
      //   [0] food byte, [1] door byte, [2] unknown (always 0x01 seen),
      //   [3:4],[5:6] = "adapter" pair (both ~0 when running on battery),
      //   [7:8],[9:10] = "battery/system" pair (always non-zero).
      // Only the adapter-vs-battery GROUPING is evidence-based; the ADC/mV
      // distinction, units and absolute scaling are NOT confirmed.
      protocol::Status st = protocol::parse_status(frame, len);
      if (this->food_ok_ != nullptr)
        this->food_ok_->publish_state(st.food_ok);          // polarity unverified (all captures 0x00)
      if (this->door_flag_ != nullptr)
        this->door_flag_->publish_state(st.door_fault);     // meaning of byte[1] unconfirmed
      if (payload_len >= 11) {
        if (this->adapter_a_ != nullptr)
          this->adapter_a_->publish_state(st.adapter_adc);
        if (this->adapter_b_ != nullptr)
          this->adapter_b_->publish_state(st.adapter_mv);
        if (this->battery_a_ != nullptr)
          this->battery_a_->publish_state(st.battery_adc);
        if (this->battery_b_ != nullptr)
          this->battery_b_->publish_state(st.battery_mv);
      }
      break;
    }
    case PKT_DISPENSED:
      ESP_LOGI(TAG, "Dispense complete (seq=%u)", seq);
      // Only the completion for the in-flight command releases the queue; a
      // late completion from a timed-out command (different seq) is ignored.
      if (this->dispense_busy_ && seq == this->dispense_sent_seq_)
        this->dispense_busy_ = false;
      break;
    case PKT_DOOR_OPENED:
      ESP_LOGI(TAG, "Door open complete (seq=%u)", seq);
      break;
    case PKT_DOOR_CLOSED:
      ESP_LOGI(TAG, "Door close complete (seq=%u)", seq);
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

// The repeated dispense command seen in captured stock traffic:
// 00 02 01 50 (duration=0, distance=2, direction=1, current=0x50). NOTE: this
// is one raw motor command, NOT a proven food "portion" — see AGENTS.md. The
// stock feed transaction is more complex (FF 01 01 50 / 01 01 01 50 lead-in,
// then repeated 00 02 01 50 with immediate zero-filled completions).
static const uint8_t STOCK_DISPENSE_STEP[4] = {0x00, 0x02, 0x01, 0x50};
// Timeout waiting for a matching 0x0C completion before giving up on it. Stock
// completions can arrive seconds after the command, so keep this generous;
// sequence matching (not this timeout) is what prevents premature release.
static const uint32_t DISPENSE_TIMEOUT_MS = 4000;

void PetkitFeeder::enqueue_dispense_(const uint8_t payload[4]) {
  if (!this->dispense_q_.push(payload))
    ESP_LOGW(TAG, "Dispense queue full; command dropped");
}

void PetkitFeeder::service_dispense_queue_(uint32_t now) {
  if (this->dispense_busy_) {
    if (static_cast<int32_t>(now - this->dispense_deadline_) < 0)
      return;  // still waiting for the matching completion
    ESP_LOGW(TAG, "Dispense completion (seq=%u) timed out; continuing queue",
             this->dispense_sent_seq_);
    this->dispense_busy_ = false;
  }
  uint8_t p[4];
  if (!this->dispense_q_.pop(p))
    return;
  this->dispense_sent_seq_ = this->send_packet_(PKT_DISPENSE, p, 4);
  this->dispense_busy_ = true;
  this->dispense_deadline_ = now + DISPENSE_TIMEOUT_MS;
  ESP_LOGI(TAG, "Dispense step sent seq=%u (%u queued)", this->dispense_sent_seq_,
           this->dispense_q_.size());
}

void PetkitFeeder::dispense(uint8_t duration, uint8_t distance, uint8_t direction, uint8_t current) {
  const uint8_t p[4] = {duration, distance, direction, current};
  this->enqueue_dispense_(p);
}

void PetkitFeeder::feed(uint8_t steps) {
  // Each "step" is one raw STOCK_DISPENSE_STEP command. The mapping from steps
  // to actual food quantity is NOT established (see AGENTS.md) — calibrate on
  // hardware before relying on it.
  if (steps == 0)
    steps = 1;
  for (uint8_t i = 0; i < steps; i++)
    this->enqueue_dispense_(STOCK_DISPENSE_STEP);
  ESP_LOGI(TAG, "Feed queued: %u dispense step(s)", steps);
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
  // Drop any queued motion so nothing dispenses across a reset.
  this->dispense_q_.clear();
  this->dispense_busy_ = false;
  this->assembler_.reset();
  // Restart the handshake cleanly, clearing any pending ack-wait state.
  this->init_step_ = this->send_init_ ? 0 : INIT_SEQ_COUNT;
  this->init_waiting_ack_ = false;
  this->init_deadline_ = 0;
  this->init_next_ms_ = millis() + 500;
}

void PetkitFeeder::dump_config() {
  ESP_LOGCONFIG(TAG, "Petkit Feeder (ISD91230 bus master):");
  ESP_LOGCONFIG(TAG, "  Init handshake: %s", this->send_init_ ? "yes" : "no");
  LOG_PIN("  Reset pin: ", this->reset_pin_);
  this->check_uart_settings(115200);
}

}  // namespace petkit_feeder
}  // namespace esphome
