// Host unit test for the Petkit bus protocol logic that the firmware actually
// ships (components/petkit_feeder/petkit_protocol.h). No ESPHome/Arduino deps.
//
// Build & run:  c++ -std=c++17 -I../components/petkit_feeder test_protocol.cpp -o /tmp/t && /tmp/t
//
// Vectors are the real captured packets from the logic-analyzer traces, so a
// pass means our framing/CRC/parsing agree with genuine bus traffic.

#include "petkit_protocol.h"

#include <cstdio>
#include <cstdint>
#include <initializer_list>
#include <vector>

using namespace esphome::petkit_feeder::protocol;

static int failures = 0;
#define CHECK(cond, msg)                                                   \
  do {                                                                     \
    if (!(cond)) {                                                         \
      std::printf("FAIL: %s\n", msg);                                      \
      failures++;                                                          \
    } else {                                                              \
      std::printf("ok:   %s\n", msg);                                      \
    }                                                                      \
  } while (0)

static std::vector<uint8_t> bytes(std::initializer_list<int> v) {
  std::vector<uint8_t> out;
  for (int b : v)
    out.push_back(static_cast<uint8_t>(b));
  return out;
}

int main() {
  // --- CRC over captured frames, whole-frame recompute must be 0 ---
  auto get_status = bytes({0xAA, 0xAA, 0x07, 0x01, 0x01, 0x59, 0x9B});
  auto status_ack = bytes({0xAA, 0xAA, 0x08, 0x01, 0x01, 0x01, 0x94, 0x13});
  auto status = bytes({0xAA, 0xAA, 0x12, 0x02, 0xFF, 0x00, 0x00, 0x01, 0x08, 0xEC,
                       0x02, 0x3F, 0x08, 0x71, 0x02, 0x20, 0x24, 0xC5});

  CHECK(crc16_ccitt(get_status.data(), get_status.size()) == 0, "get_status whole-frame CRC == 0");
  CHECK(crc16_ccitt(status_ack.data(), status_ack.size()) == 0, "status_ack whole-frame CRC == 0");
  CHECK(crc16_ccitt(status.data(), status.size()) == 0, "status whole-frame CRC == 0");

  // CRC over body (excluding the 2 CRC bytes) equals the transmitted CRC.
  CHECK(crc16_ccitt(get_status.data(), 5) == 0x599B, "get_status body CRC == 0x599B");
  CHECK(crc16_ccitt(status.data(), 16) == 0x24C5, "status body CRC == 0x24C5");

  CHECK(frame_is_valid(get_status.data(), get_status.size()), "frame_is_valid(get_status)");
  CHECK(frame_is_valid(status.data(), status.size()), "frame_is_valid(status)");

  // Corrupt one byte -> must be rejected.
  auto bad = status;
  bad[9] ^= 0x01;
  CHECK(!frame_is_valid(bad.data(), bad.size()), "corrupted frame rejected");

  // Truncated / bogus length -> rejected, no OOB read.
  CHECK(!frame_is_valid(get_status.data(), 3), "too-short length rejected");

  // --- build_frame reproduces captured command frames byte-for-byte ---
  // This is the strong test: our transmitter must match real bus traffic.
  auto exact = [](std::initializer_list<int> expect, uint8_t type, uint8_t seq,
                  std::initializer_list<int> pl) -> bool {
    std::vector<uint8_t> want = bytes(expect), payload = bytes(pl);
    uint8_t out[MAX_FRAME];
    uint8_t n = build_frame(out, type, seq, payload.data(), payload.size());
    if (n != want.size())
      return false;
    for (uint8_t i = 0; i < n; i++)
      if (out[i] != want[i])
        return false;
    return true;
  };

  CHECK(exact({0xAA, 0xAA, 0x07, 0x01, 0x01, 0x59, 0x9B}, 0x01, 0x01, {}),
        "build_frame(get_status) == captured AA AA 07 01 01 59 9B");
  // Captured stock OPEN_DOOR / CLOSE_DOOR: single payload byte 0x1E.
  CHECK(exact({0xAA, 0xAA, 0x08, 0x07, 0x01, 0x1E, 0xC5, 0x6D}, 0x07, 0x01, {0x1E}),
        "build_frame(open_door 0x1E) == captured door-open frame");
  CHECK(exact({0xAA, 0xAA, 0x08, 0x09, 0x01, 0x1E, 0xDE, 0x6C}, 0x09, 0x01, {0x1E}),
        "build_frame(close_door 0x1E) == captured door-close frame");
  // Captured stock repeated dispense: payload 00 02 01 50.
  CHECK(exact({0xAA, 0xAA, 0x0B, 0x0B, 0x03, 0x00, 0x02, 0x01, 0x50, 0x99, 0xCA}, 0x0B, 0x03,
              {0x00, 0x02, 0x01, 0x50}),
        "build_frame(stock dispense) == captured 00 02 01 50 frame");
  // Captured 0x0D boot config (the largest frame, exactly MAX_FRAME).
  CHECK(exact({0xAA, 0xAA, 0x13, 0x0D, 0x01, 0x00, 0x3C, 0x01, 0x90, 0x0F, 0x01, 0x22, 0x22, 0x01,
               0xF4, 0x0F, 0x01, 0x62, 0xBA},
              0x0D, 0x01, {0x00, 0x3C, 0x01, 0x90, 0x0F, 0x01, 0x22, 0x22, 0x01, 0xF4, 0x0F, 0x01}),
        "build_frame(0x0D config) == captured 19-byte boot frame");

  // --- parse_status field extraction (AC adapter plugged) ---
  Status s = parse_status(status.data(), status.size());
  CHECK(s.valid, "parse_status valid");
  CHECK(s.dispenser_door_sensor == false, "dispenser-door sensor byte == 0");
  CHECK(s.food_detected == false, "food-detected byte == 0");
  CHECK(s.dispenser_wheel_sensor == true, "dispenser-wheel sensor byte == 1");
  CHECK(s.adapter_adc == 0x08EC, "adapter_adc == 0x08EC");
  CHECK(s.adapter_centivolts == 0x023F, "adapter_centivolts == 0x023F");
  CHECK(s.battery_adc == 0x0871, "battery_adc == 0x0871");
  CHECK(s.battery_centivolts == 0x0220, "battery_centivolts == 0x0220");

  // --- parse_status on a battery-only capture: adapter fields read ~0 ---
  // From petkit_press_button.csv: payload 00 00 01 0002 0000 0767 01DD
  auto batt = bytes({0xAA, 0xAA, 0x12, 0x02, 0xFF, 0x00, 0x00, 0x01, 0x00, 0x02,
                     0x00, 0x00, 0x07, 0x67, 0x01, 0xDD, 0x00, 0x00});
  // Fix CRC for this synthetic-but-real payload.
  uint16_t c = crc16_ccitt(batt.data(), batt.size() - 2);
  batt[batt.size() - 2] = c >> 8;
  batt[batt.size() - 1] = c & 0xFF;
  Status b = parse_status(batt.data(), batt.size());
  CHECK(b.valid, "parse_status(battery) valid");
  CHECK(b.adapter_adc == 0x0002 && b.adapter_centivolts == 0x0000, "adapter fields ~0 on battery");
  CHECK(b.battery_adc == 0x0767 && b.battery_centivolts == 0x01DD, "battery fields nonzero");

  // --- type-0x0C stock dispense results ---
  // petkit03: a follow-up result reports 1; the delayed result reports 2.
  auto dispense_one = bytes({0xAA, 0xAA, 0x0C, 0x0C, 0x04, 0x01, 0x00, 0x00,
                             0x00, 0x00, 0x00, 0x00});
  c = crc16_ccitt(dispense_one.data(), dispense_one.size() - 2);
  dispense_one[dispense_one.size() - 2] = c >> 8;
  dispense_one[dispense_one.size() - 1] = c & 0xFF;
  DispenseResult one = parse_dispense_result(dispense_one.data(), dispense_one.size());
  CHECK(one.valid && one.wheel_count == 1 && !one.complete,
        "parse in-progress wheel result");

  auto dispense_two = bytes({0xAA, 0xAA, 0x0C, 0x0C, 0x03, 0x02, 0x01, 0x03,
                             0x01, 0x3C, 0x00, 0x00});
  c = crc16_ccitt(dispense_two.data(), dispense_two.size() - 2);
  dispense_two[dispense_two.size() - 2] = c >> 8;
  dispense_two[dispense_two.size() - 1] = c & 0xFF;
  DispenseResult two = parse_dispense_result(dispense_two.data(), dispense_two.size());
  CHECK(two.valid && two.wheel_count == 2 && two.complete,
        "parse completed wheel result");
  CHECK(two.detail[0] == 0x03 && two.detail[1] == 0x01 && two.detail[2] == 0x3C,
        "preserve completion measurements");

  std::printf("\n%s (%d failure%s)\n", failures ? "TESTS FAILED" : "ALL TESTS PASSED",
              failures, failures == 1 ? "" : "s");
  return failures ? 1 : 0;
}
