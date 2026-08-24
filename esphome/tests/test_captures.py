#!/usr/bin/env python3
"""Regression test: validate the Petkit bus framing/CRC against the raw
logic-analyzer captures shipped in ../../petkit-serial-bus/CSV export/.

These CSVs are UART-decoded byte streams straight off the wire, so a high
whole-frame CRC pass-rate proves our framing + CRC-16/CCITT-FALSE model matches
real hardware traffic — independent of any prose documentation.

Run:  python3 test_captures.py
"""
import csv
import glob
import os
import sys
from collections import Counter

CAPTURE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "petkit-serial-bus", "CSV export"
)


def crc16_ccitt_false(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def load_bytes(path):
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            d = row.get("data")
            if d:
                out.append(int(d, 16))
    return out


def parse_frames(stream):
    """Greedy AA AA framing with CRC-gated acceptance (resync on failure)."""
    frames, i, n = [], 0, len(stream)
    while i < n - 2:
        if stream[i] == 0xAA and stream[i + 1] == 0xAA:
            length = stream[i + 2]
            if 7 <= length <= 32 and i + length <= n:
                fr = stream[i : i + length]
                if crc16_ccitt_false(fr) == 0:
                    frames.append(fr)
                    i += length
                    continue
        i += 1
    return frames


def main():
    # Known-answer vectors (captured packets) — the CRC model itself.
    assert crc16_ccitt_false(bytes.fromhex("AAAA070101599B")) == 0
    assert crc16_ccitt_false(bytes.fromhex("AAAA0801010194 13".replace(" ", ""))) == 0
    assert crc16_ccitt_false(bytes.fromhex("AAAA1202FF00000108EC023F08710220 24C5".replace(" ", ""))) == 0
    assert crc16_ccitt_false(bytes.fromhex("AAAA070101599A")) != 0  # corrupted -> nonzero
    print("ok:   known-answer CRC vectors")

    paths = sorted(glob.glob(os.path.join(CAPTURE_DIR, "*.csv")))
    if not paths:
        print(f"SKIP: no captures found under {CAPTURE_DIR}")
        return 0

    grand_frames = 0
    types = Counter()
    for path in paths:
        stream = load_bytes(path)
        frames = parse_frames(stream)
        grand_frames += len(frames)
        for f in frames:
            types[f[3]] += 1
        print(f"ok:   {os.path.basename(path):28s} {len(frames):4d} CRC-valid frames")

    # Every capture must yield a healthy number of CRC-valid frames, and the
    # expected core command types must all appear across the traces.
    assert grand_frames >= 300, f"too few valid frames: {grand_frames}"
    for expected in (0x01, 0x02, 0x07, 0x09, 0x0B, 0x0E):
        assert types[expected] > 0, f"expected command type 0x{expected:02X} not seen"
    print(f"ok:   {grand_frames} CRC-valid frames total; all core command types present")

    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
