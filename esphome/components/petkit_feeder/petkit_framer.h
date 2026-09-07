#pragma once

// Pure, dependency-free receive helpers for the Petkit bus, split out so
// they can be unit-tested on the host without ESPHome (see ../../tests/). The
// component uses these exact types, so the tests exercise the shipping logic.

#include <cstdint>

#include "petkit_protocol.h"

namespace esphome {
namespace petkit_feeder {
namespace protocol {

// Byte-at-a-time frame assembler. Sync on two consecutive 0xAA, read the length
// byte (bounds-checked to [OVERHEAD, MAX_FRAME]), then read length-3 body bytes.
// Resync reconsiders the current byte as a possible header start, so a stray
// 0xAA before a real frame cannot swallow it. CRC is NOT checked here — the
// caller runs frame_is_valid() on the returned frame.
class FrameAssembler {
 public:
  // Returns true when a complete frame is buffered; read it via buf()/len().
  bool feed(uint8_t c) {
    switch (state_) {
      case SYNC:
        if (c == HEADER_BYTE) {
          if (aa_ < 2)
            aa_++;
          if (aa_ >= 2)
            state_ = LEN;
        } else {
          aa_ = 0;
        }
        return false;
      case LEN:
        if (c == HEADER_BYTE)
          return false;  // still in the header run; keep waiting for a length
        if (c >= OVERHEAD && c <= MAX_FRAME) {
          buf_[0] = HEADER_BYTE;
          buf_[1] = HEADER_BYTE;
          buf_[2] = c;
          len_ = 3;
          need_ = c;
          state_ = BODY;
        } else {
          state_ = SYNC;
          aa_ = 0;
        }
        return false;
      case BODY:
        buf_[len_++] = c;
        if (len_ >= need_) {
          state_ = SYNC;
          aa_ = 0;
          return true;
        }
        return false;
    }
    return false;
  }

  void reset() {
    state_ = SYNC;
    aa_ = 0;
    len_ = 0;
  }

  bool in_progress() const { return state_ != SYNC; }
  const uint8_t *buf() const { return buf_; }
  uint8_t len() const { return need_; }

 private:
  enum St : uint8_t { SYNC, LEN, BODY };
  St state_{SYNC};
  uint8_t aa_{0};
  uint8_t buf_[MAX_FRAME];
  uint8_t len_{0};
  uint8_t need_{0};
};

}  // namespace protocol
}  // namespace petkit_feeder
}  // namespace esphome
