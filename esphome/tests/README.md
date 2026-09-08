# Tests

Run the protocol and framer checks from this directory:

```sh
./run_tests.sh
```

The runner requires Python 3 and a C++17 compiler available as `c++`. The C++
tests include the same headers as the device component, so they exercise the
shipping framing and parsing code.

## Self-contained checks

| Test | Coverage |
|---|---|
| `test_protocol.cpp` | CRC, frame construction and validation, status parsing, and wheel-result parsing |
| `test_framer.cpp` | Receive assembly, split and consecutive frames, stray headers, invalid lengths, and resynchronization |

The protocol test rejects corrupted and truncated frames and compares generated
packets with captured status, door, wheel-query, and boot-configuration vectors.
These tests do not require private files or feeder hardware.

## Optional capture and firmware checks

Set `PETKIT_SERIAL_BUS_DIR` to a private local checkout of
[`earlynerd/petkit-serial-bus`](https://github.com/earlynerd/petkit-serial-bus)
to enable the reference-data checks:

| Test | Coverage |
|---|---|
| `test_captures.py` | Framing and CRC against four bus captures, with at least 300 valid frames and all core command types present |
| `verify_firmware.py` | Exact parser opcodes in both stock processors' firmware and the ISD91230 CRC implementation |

The firmware checks distinguish what each image establishes:

- The ISD91230 image contains the header checks, length bounds of 6–19, the
  `length-3` body loop, and CRC-16 with polynomial `0x1021` and seed `0xFFFF`.
- The ESP8266 image contains the `AA AA` header checks and length bounds of
  7–19. These checks establish framing, not the ESP8266 CRC implementation.

The script also compares the ISD91230 CRC calculation with captured packets.
Without `PETKIT_SERIAL_BUS_DIR`, the capture and firmware checks skip while
the self-contained C++ tests still run. Reference firmware dumps are not
distributed by this project.
