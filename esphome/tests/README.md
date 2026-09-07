# Tests

Run everything:

```sh
./run_tests.sh
```

## What each test proves

| Test | Kind | Proves |
|------|------|--------|
| `test_protocol.cpp` | native C++ (g++/clang, `-std=c++17`) | The shipping `petkit_protocol.h` — CRC, frame build/validate, status and wheel-result parsing — is correct, rejects corrupted/truncated frames, and `build_frame` reproduces **captured** get-status, open/close-door (`0x1E`), wheel query (`00 02 01 50`), and the 19-byte `0x0D` boot config byte-for-byte. |
| `test_framer.cpp` | native C++ | The shipping `petkit_framer.h` RX `FrameAssembler` resyncs correctly after stray/triple `0xAA` and bogus lengths, and handles split and back-to-back frames. |
| `test_captures.py` | Python, no deps | The framing + CRC model matches genuine bus traffic: ≥300 whole-frame-CRC-valid frames across the four captures, all core command types present. |
| `verify_firmware.py` | Python (no deps) | Instruction-level, **both** firmwares: the M0 header check / length bounds (6..19) / `length-3` body loop / table-less CRC-16 (0x1021, seed 0xFFFF), AND the ESP8266 parser (`AA AA` header, length bounds 7..19) — asserted as exact opcodes at located offsets, plus the M0 CRC re-derived against captured packets. Also encodes the correction that the `decodePacket` string is MQTT code, not the UART decoder. |
The component (`components/petkit_feeder/petkit_feeder.cpp`) and
`test_protocol.cpp` both include `petkit_protocol.h`, so the unit test exercises
the exact code that runs on the device.

## Fixtures
Tests read the raw captures and firmware dump from the reverse-engineering repo
at `../../petkit-serial-bus/`. If that directory is absent, the capture/firmware
tests SKIP (the C++ test is self-contained and always runs).
