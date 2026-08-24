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

void PetkitFeeder::send_packet_(uint8_t type, const uint8_t *payload, uint8_t payload_len) {
  uint8_t frame[protocol::MAX_FRAME];
  uint8_t len = protocol::build_frame(frame, type, this->seq_++, payload, payload_len);
  this->write_array(frame, len);
  ESP_LOGV(TAG, "TX type=0x%02X seq=%u len=%u", type, frame[4], len);
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
    this->feed_byte_(c, now);
  }

  // Drop a partial frame if the bus goes quiet mid-frame.
  if (this->rx_state_ != RX_SYNC && (now - this->last_byte_ms_) > 50) {
    this->rx_state_ = RX_SYNC;
    this->rx_aa_ = 0;
  }

  // Pace queued dispense commands (only once init has completed).
  if (this->ready_())
    this->service_dispense_queue_(now);
}

// RX state machine: sync on two consecutive 0xAA, then a length byte, then
// len-3 body bytes. Resync reconsiders the current byte as a possible header
// start, so a stray 0xAA before a real frame cannot swallow it.
void PetkitFeeder::feed_byte_(uint8_t c, uint32_t now) {
  this->last_byte_ms_ = now;
  switch (this->rx_state_) {
    case RX_SYNC:
      if (c == 0xAA) {
        if (this->rx_aa_ < 2)
          this->rx_aa_++;
        if (this->rx_aa_ >= 2)
          this->rx_state_ = RX_LEN;
      } else {
        this->rx_aa_ = 0;
      }
      break;
    case RX_LEN:
      if (c == 0xAA) {
        // Another 0xAA: still a valid header pair, keep waiting for a length.
        break;
      }
      if (c >= protocol::OVERHEAD && c <= protocol::MAX_FRAME) {
        this->rx_buf_[0] = 0xAA;
        this->rx_buf_[1] = 0xAA;
        this->rx_buf_[2] = c;
        this->rx_len_ = 3;
        this->rx_need_ = c;
        this->rx_state_ = RX_BODY;
      } else {
        this->rx_state_ = RX_SYNC;  // bogus length; drop and resync
        this->rx_aa_ = 0;
      }
      break;
    case RX_BODY:
      this->rx_buf_[this->rx_len_++] = c;
      if (this->rx_len_ >= this->rx_need_) {
        this->handle_frame_(this->rx_buf_, this->rx_need_);
        this->rx_state_ = RX_SYNC;
        this->rx_aa_ = 0;
      }
      break;
  }
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
      this->dispense_busy_ = false;  // release the queue for the next portion
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

// Per-portion dispense payload as seen in captured stock traffic: the repeated
// command is 00 02 01 50 (duration=0, distance=2, direction=1, current=0x50).
static const uint8_t STOCK_DISPENSE[4] = {0x00, 0x02, 0x01, 0x50};
// Timeout waiting for a 0x0C completion before we give up and send the next.
static const uint32_t DISPENSE_TIMEOUT_MS = 1500;

void PetkitFeeder::enqueue_dispense_(const uint8_t payload[4]) {
  for (uint8_t i = 0; i < 4; i++)
    this->dispense_pending_[i] = payload[i];
  if (this->dispense_queue_ < 0xFFFF)
    this->dispense_queue_++;
}

void PetkitFeeder::service_dispense_queue_(uint32_t now) {
  if (this->dispense_busy_) {
    if (static_cast<int32_t>(now - this->dispense_deadline_) < 0)
      return;  // still waiting for completion
    ESP_LOGW(TAG, "Dispense completion timed out; continuing queue");
    this->dispense_busy_ = false;
  }
  if (this->dispense_queue_ == 0)
    return;
  this->dispense_queue_--;
  this->send_packet_(PKT_DISPENSE, this->dispense_pending_, 4);
  this->dispense_busy_ = true;
  this->dispense_deadline_ = now + DISPENSE_TIMEOUT_MS;
  ESP_LOGI(TAG, "Dispense sent (%u queued)", this->dispense_queue_);
}

void PetkitFeeder::dispense(uint8_t duration, uint8_t distance, uint8_t direction, uint8_t current) {
  const uint8_t p[4] = {duration, distance, direction, current};
  this->enqueue_dispense_(p);
}

void PetkitFeeder::feed(uint8_t portions) {
  if (portions == 0)
    portions = 1;
  for (uint8_t i = 0; i < portions; i++)
    this->enqueue_dispense_(STOCK_DISPENSE);
  ESP_LOGI(TAG, "Feed queued: %u portion(s)", portions);
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
  this->dispense_queue_ = 0;
  this->dispense_busy_ = false;
  this->rx_state_ = RX_SYNC;
  this->rx_aa_ = 0;
  this->init_step_ = this->send_init_ ? 0 : INIT_SEQ_COUNT;
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
