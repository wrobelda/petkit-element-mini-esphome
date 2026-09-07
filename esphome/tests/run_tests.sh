#!/usr/bin/env bash
# Run the Petkit protocol tests: native C++ unit test + capture regression test.
set -euo pipefail
cd "$(dirname "$0")"

echo "== C++ protocol unit test =="
c++ -std=c++17 -Wall -Wextra -I../components/petkit_feeder test_protocol.cpp -o /tmp/petkit_test_protocol
/tmp/petkit_test_protocol

echo
echo "== C++ framer unit test =="
c++ -std=c++17 -Wall -Wextra -I../components/petkit_feeder test_framer.cpp -o /tmp/petkit_test_framer
/tmp/petkit_test_framer

echo
echo "== Capture regression test =="
python3 test_captures.py

echo
echo "== Firmware instruction checks =="
python3 verify_firmware.py
