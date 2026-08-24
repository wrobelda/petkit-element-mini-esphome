#pragma once

// Pure, dependency-free implementation of the Petkit <-> ISD91230 serial bus
// framing. No ESPHome/Arduino includes so it can be unit-tested on the host
// (see ../../tests/). petkit_feeder.cpp uses these exact functions, so the
// tests exercise the real shipping logic.
//
// Frame: AA AA <len> <type> <seq> <payload...> <crc_hi> <crc_lo>
//   len = total frame length including the AA AA header and the 2 CRC bytes.
//   crc = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect / no xorout)
//         over the whole frame; big-endian. A recompute over a received frame
//         including its CRC bytes is 0 when valid.

#include <cstddef>
#include <cstdint>

namespace esphome {
namespace petkit_feeder {
namespace protocol {

static const uint8_t HEADER_BYTE = 0xAA;
static const uint8_t OVERHEAD = 7;  // 2 header + len + type + seq + 2 crc
static const uint8_t MAX_FRAME = 32;

inline uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (uint8_t b = 0; b < 8; b++)
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                           : static_cast<uint16_t>(crc << 1);
  }
  return crc;
}

// A received frame is valid when the CRC recomputed over the whole frame
// (including its trailing CRC bytes) is zero, and the length field is sane.
inline bool frame_is_valid(const uint8_t *frame, uint8_t len) {
  if (len < OVERHEAD || len > MAX_FRAME)
    return false;
  if (frame[0] != HEADER_BYTE || frame[1] != HEADER_BYTE || frame[2] != len)
    return false;
  return crc16_ccitt(frame, len) == 0;
}

// Build a frame into out[] (must hold payload_len + OVERHEAD bytes).
// Returns the total frame length written.
inline uint8_t build_frame(uint8_t *out, uint8_t type, uint8_t seq,
                           const uint8_t *payload, uint8_t payload_len) {
  uint8_t len = static_cast<uint8_t>(payload_len + OVERHEAD);
  out[0] = HEADER_BYTE;
  out[1] = HEADER_BYTE;
  out[2] = len;
  out[3] = type;
  out[4] = seq;
  for (uint8_t i = 0; i < payload_len; i++)
    out[5 + i] = payload[i];
  uint16_t crc = crc16_ccitt(out, len - 2);
  out[len - 2] = static_cast<uint8_t>(crc >> 8);
  out[len - 1] = static_cast<uint8_t>(crc & 0xFF);
  return len;
}

// Decoded status (type 0x02) payload.
struct Status {
  bool valid;
  bool food_ok;      // payload[0] != 0
  bool door_fault;   // payload[1] != 0
  uint16_t adapter_adc;
  uint16_t adapter_mv;
  uint16_t battery_adc;
  uint16_t battery_mv;
};

// Parse a validated status frame. Requires >= 11 payload bytes for voltages.
inline Status parse_status(const uint8_t *frame, uint8_t len) {
  Status s{};
  if (!frame_is_valid(frame, len) || frame[3] != 0x02)
    return s;
  const uint8_t *p = &frame[5];
  const uint8_t plen = len - OVERHEAD;
  if (plen < 3)
    return s;
  s.food_ok = p[0] != 0x00;
  s.door_fault = p[1] != 0x00;
  if (plen >= 11) {
    s.adapter_adc = (p[3] << 8) | p[4];
    s.adapter_mv = (p[5] << 8) | p[6];
    s.battery_adc = (p[7] << 8) | p[8];
    s.battery_mv = (p[9] << 8) | p[10];
  }
  s.valid = true;
  return s;
}

}  // namespace protocol
}  // namespace petkit_feeder
}  // namespace esphome
