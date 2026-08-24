// Host unit test for the dependency-free RX frame assembler and dispense queue
// (components/petkit_feeder/petkit_framer.h) — the same types the component
// uses. Covers the behaviours that a pure-frame-construction test cannot:
// RX resync, split frames, the multi-payload queue, and FIFO ordering.
//
// Build & run: c++ -std=c++17 -I../components/petkit_feeder test_framer.cpp -o /tmp/t && /tmp/t

#include "petkit_framer.h"

#include <cstdint>
#include <cstdio>
#include <initializer_list>
#include <vector>

using namespace esphome::petkit_feeder::protocol;

static int failures = 0;
#define CHECK(cond, msg)                                                        \
  do {                                                                          \
    if (!(cond)) { std::printf("FAIL: %s\n", msg); failures++; }               \
    else { std::printf("ok:   %s\n", msg); }                                    \
  } while (0)

// Feed a byte stream; return every complete frame the assembler emits.
static std::vector<std::vector<uint8_t>> run(FrameAssembler &fa, std::initializer_list<int> bytes) {
  std::vector<std::vector<uint8_t>> out;
  for (int b : bytes) {
    if (fa.feed(static_cast<uint8_t>(b))) {
      out.emplace_back(fa.buf(), fa.buf() + fa.len());
    }
  }
  return out;
}

int main() {
  // A valid captured get-status frame is assembled and CRC-checks out.
  {
    FrameAssembler fa;
    auto f = run(fa, {0xAA, 0xAA, 0x07, 0x01, 0x01, 0x59, 0x9B});
    CHECK(f.size() == 1 && f[0].size() == 7, "assembles one 7-byte frame");
    CHECK(!f.empty() && frame_is_valid(f[0].data(), f[0].size()), "assembled frame passes CRC");
  }

  // Stray 0xAA before a real frame must NOT swallow it (the review-4 case).
  {
    FrameAssembler fa;
    auto f = run(fa, {0xAA, 0x01,                         // false start
                      0xAA, 0xAA, 0x07, 0x01, 0x01, 0x59, 0x9B});
    CHECK(f.size() == 1 && frame_is_valid(f[0].data(), f[0].size()),
          "recovers the real frame after a stray 0xAA");
  }

  // Triple 0xAA then a length still frames correctly.
  {
    FrameAssembler fa;
    auto f = run(fa, {0xAA, 0xAA, 0xAA, 0x07, 0x01, 0x01, 0x59, 0x9B});
    CHECK(f.size() == 1 && f[0][2] == 0x07, "triple-0xAA resyncs to the length byte");
  }

  // Bogus length is rejected; the following good frame still parses.
  {
    FrameAssembler fa;
    auto f = run(fa, {0xAA, 0xAA, 0xFF,                    // 0xFF > MAX_FRAME -> drop
                      0xAA, 0xAA, 0x07, 0x01, 0x01, 0x59, 0x9B});
    CHECK(f.size() == 1, "bogus length dropped, next frame recovered");
  }

  // Two back-to-back frames both emit.
  {
    FrameAssembler fa;
    auto f = run(fa, {0xAA, 0xAA, 0x07, 0x01, 0x01, 0x59, 0x9B,
                      0xAA, 0xAA, 0x08, 0x07, 0x01, 0x1E, 0xC5, 0x6D});
    CHECK(f.size() == 2 && f[1][3] == 0x07, "two consecutive frames both assemble");
  }

  // ---- PayloadQueue: distinct payloads preserved in FIFO order ----
  {
    PayloadQueue<4> q;
    uint8_t a[4] = {0xFF, 0x01, 0x01, 0x50};
    uint8_t b[4] = {0x01, 0x01, 0x01, 0x50};
    uint8_t c[4] = {0x00, 0x02, 0x01, 0x50};
    CHECK(q.push(a) && q.push(b) && q.push(c) && q.size() == 3, "queue accepts 3 distinct payloads");
    uint8_t out[4];
    bool order_ok = q.pop(out) && out[0] == 0xFF && out[1] == 0x01;
    order_ok &= q.pop(out) && out[0] == 0x01 && out[1] == 0x01;
    order_ok &= q.pop(out) && out[0] == 0x00 && out[1] == 0x02;
    CHECK(order_ok, "distinct payloads pop in FIFO order (not all the newest)");
    CHECK(q.empty() && !q.pop(out), "queue empties and pop() fails when empty");
  }

  // Queue respects capacity (push fails when full, no overwrite).
  {
    PayloadQueue<2> q;
    uint8_t p[4] = {1, 2, 3, 4};
    CHECK(q.push(p) && q.push(p) && !q.push(p), "push fails at capacity");
    CHECK(q.full(), "full() reports capacity reached");
  }

  std::printf("\n%s (%d failure%s)\n", failures ? "TESTS FAILED" : "ALL TESTS PASSED",
              failures, failures == 1 ? "" : "s");
  return failures ? 1 : 0;
}
