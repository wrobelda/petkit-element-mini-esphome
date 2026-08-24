#include "petkit_feeder.h"
#include "esphome/core/log.h"

namespace esphome {
namespace petkit_feeder {

static const char *const TAG = "petkit_feeder";

// Stock boot handshake the original firmware sends to the M0 before it starts
// polling status. Values are lifted verbatim from a logic-analyzer capture of
// the factory firmware boot (see the petkit-serial-bus README). They configure
// motor timing/thresholds; replaying them keeps the M0 in its expected state.
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
static const uint32_t INIT_STEP_MS = 150;

uint16_t PetkitFeeder::crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (uint8_t b = 0; b < 8; b++)
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021) : static_cast<uint16_t>(crc << 1);
  }
  return crc;
}

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
  uint8_t frame[64];
  uint8_t len = static_cast<uint8_t>(payload_len + 7);  // 2 hdr + len + type + seq + 2 crc
  frame[0] = 0xAA;
  frame[1] = 0xAA;
  frame[2] = len;
  frame[3] = type;
  frame[4] = this->seq_++;
  for (uint8_t i = 0; i < payload_len; i++)
    frame[5 + i] = payload[i];
  uint16_t crc = crc16_ccitt(frame, len - 2);
  frame[len - 2] = static_cast<uint8_t>(crc >> 8);
  frame[len - 1] = static_cast<uint8_t>(crc & 0xFF);
  this->write_array(frame, len);
  ESP_LOGV(TAG, "TX type=0x%02X seq=%u len=%u", type, frame[4], len);
}

void PetkitFeeder::loop() {
  const uint32_t now = millis();

  // Advance the boot handshake.
  if (this->init_step_ < INIT_SEQ_COUNT && now >= this->init_next_ms_) {
    const InitPacket &p = INIT_SEQ[this->init_step_];
    this->send_packet_(p.type, p.payload, p.len);
    this->init_step_++;
    this->init_next_ms_ = now + INIT_STEP_MS;
  }

  // Byte-wise frame assembler. Frames start with AA AA; rx_buf_[2] is length.
  while (this->available()) {
    uint8_t c;
    this->read_byte(&c);
    this->last_byte_ms_ = now;

    if (this->rx_len_ == 0) {
      if (c == 0xAA)
        this->rx_buf_[this->rx_len_++] = c;  // first header byte
    } else if (this->rx_len_ == 1) {
      if (c == 0xAA)
        this->rx_buf_[this->rx_len_++] = c;  // second header byte -> len next
      // else: not a real header, stay at rx_len_==1 waiting for the pair
    } else {
      this->rx_buf_[this->rx_len_++] = c;
      const uint8_t expect = this->rx_buf_[2];
      if (expect < 7 || expect > sizeof(this->rx_buf_)) {  // bogus length -> resync
        this->rx_len_ = 0;
        continue;
      }
      if (this->rx_len_ == expect) {
        this->handle_frame_(this->rx_buf_, expect);
        this->rx_len_ = 0;
      }
    }
  }

  // Drop a partial frame if the bus goes quiet mid-frame.
  if (this->rx_len_ > 0 && (now - this->last_byte_ms_) > 50)
    this->rx_len_ = 0;
}

void PetkitFeeder::handle_frame_(const uint8_t *frame, uint8_t len) {
  if (crc16_ccitt(frame, len) != 0) {
    ESP_LOGW(TAG, "Bad CRC on frame type=0x%02X len=%u", len >= 4 ? frame[3] : 0, len);
    return;
  }
  const uint8_t type = frame[3];
  const uint8_t seq = frame[4];
  const uint8_t *data = &frame[5];
  const uint8_t data_len = len - 7;

  switch (type) {
    case PKT_STATUS: {
      // Payload: food(1) door(1) unk(1) adapterAdc(2) adapterMv(2) battAdc(2) battMv(2)
      if (data_len >= 3) {
        if (this->food_ok_ != nullptr)
          this->food_ok_->publish_state(data[0] != 0x00);      // 0x01 = ok, 0x00 = low
        if (this->door_fault_ != nullptr)
          this->door_fault_->publish_state(data[1] != 0x00);   // 0x01 = not fully open/closed
      }
      if (data_len >= 11) {
        const uint16_t a_adc = (data[3] << 8) | data[4];
        const uint16_t a_mv = (data[5] << 8) | data[6];
        const uint16_t b_adc = (data[7] << 8) | data[8];
        const uint16_t b_mv = (data[9] << 8) | data[10];
        if (this->adapter_adc_ != nullptr)
          this->adapter_adc_->publish_state(a_adc);
        if (this->adapter_mv_ != nullptr)
          this->adapter_mv_->publish_state(a_mv);
        if (this->battery_adc_ != nullptr)
          this->battery_adc_->publish_state(b_adc);
        if (this->battery_mv_ != nullptr)
          this->battery_mv_->publish_state(b_mv);
      }
      break;
    }
    case PKT_DISPENSED:
      ESP_LOGI(TAG, "Dispense complete (seq=%u)", seq);
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
  if (this->init_step_ < INIT_SEQ_COUNT)
    return;  // still handshaking
  this->get_status();
}

// ---- high level actions ----

void PetkitFeeder::get_status() { this->send_packet_(PKT_GET_STATUS, nullptr, 0); }

void PetkitFeeder::dispense(uint8_t duration, uint8_t distance, uint8_t direction, uint8_t current) {
  uint8_t p[4] = {duration, distance, direction, current};
  this->send_packet_(PKT_DISPENSE, p, 4);
  ESP_LOGI(TAG, "Dispense dur=%u dist=%u dir=%u cur=%u", duration, distance, direction, current);
}

void PetkitFeeder::feed(uint8_t portions) {
  // One "portion" mirrors the short dispense the stock firmware issues:
  // duration=3, distance=1, direction=0, current=16 (0x10).
  if (portions == 0)
    portions = 1;
  for (uint8_t i = 0; i < portions; i++)
    this->dispense(3, 1, 0, 16);
}

void PetkitFeeder::open_door(uint8_t duration, uint8_t strength) {
  uint8_t p[2] = {duration, strength};
  this->send_packet_(PKT_OPEN_DOOR, p, 2);
}

void PetkitFeeder::close_door(uint8_t duration, uint8_t strength) {
  uint8_t p[2] = {duration, strength};
  this->send_packet_(PKT_CLOSE_DOOR, p, 2);
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
