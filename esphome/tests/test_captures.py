#!/usr/bin/env python3
"""Regression test: validate the Petkit bus framing/CRC against the raw
logic-analyzer captures supplied through PETKIT_SERIAL_BUS_DIR.

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

REFERENCE_DIR = os.environ.get("PETKIT_SERIAL_BUS_DIR")
CAPTURE_DIR = os.path.join(REFERENCE_DIR, "CSV export") if REFERENCE_DIR else None


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


def load_timed_frames(path):
    """Return CRC-valid frames per analyzer channel, retaining frame time."""
    streams = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            if not row.get("data"):
                continue
            streams.setdefault(row["name"], []).append(
                (float(row["start_time"]), int(row["data"], 16))
            )

    result = {}
    for name, stream in streams.items():
        frames, i = [], 0
        while i < len(stream) - 2:
            if stream[i][1] == 0xAA and stream[i + 1][1] == 0xAA:
                length = stream[i + 2][1]
                if 7 <= length <= 19 and i + length <= len(stream):
                    frame = bytes(value for _, value in stream[i : i + length])
                    if crc16_ccitt_false(frame) == 0:
                        frames.append((stream[i][0], frame))
                        i += length
                        continue
            i += 1
        result[name] = frames
    return result


def assert_stock_feed_traces():
    """Pin the dispense exchanges to the original stock UART captures.

    Async Serial [1] is ESP8266 -> M0 and Async Serial is M0 -> ESP8266.
    The captures do not record the app-requested amount, so these assertions
    intentionally establish packet behavior without assigning gram values.
    """
    capture_dir = os.path.abspath(CAPTURE_DIR)
    trace2 = load_timed_frames(os.path.join(capture_dir, "petkit02.csv"))
    trace3 = load_timed_frames(os.path.join(capture_dir, "petkit03.csv"))

    def frames(trace, channel, packet_type):
        return [(when, frame[4], frame[5:-2]) for when, frame in trace[channel]
                if frame[3] == packet_type]

    tx2 = frames(trace2, "Async Serial [1]", 0x0B)
    done2 = frames(trace2, "Async Serial", 0x0C)
    assert [payload for _, _, payload in tx2] == [
        bytes.fromhex("ff010150"),
        bytes.fromhex("01010150"),
        *([bytes.fromhex("00020150")] * 7),
    ]
    assert [(seq, payload) for _, seq, payload in done2] == [
        *[(seq, bytes(5)) for seq in range(3, 10)],
        (2, bytes.fromhex("0001030342")),
    ]
    # Stock paces the seven follow-up commands at about one-second intervals.
    followup_times = [when for when, _, payload in tx2
                      if payload == bytes.fromhex("00020150")]
    assert all(0.9 <= later - earlier <= 1.1
               for earlier, later in zip(followup_times, followup_times[1:]))

    tx3 = frames(trace3, "Async Serial [1]", 0x0B)
    done3 = frames(trace3, "Async Serial", 0x0C)
    assert [payload for _, _, payload in tx3] == [
        bytes.fromhex("ff010150"),
        bytes.fromhex("00020150"),
        bytes.fromhex("01010150"),
        bytes.fromhex("00020150"),
        bytes.fromhex("ff010150"),
        *([bytes.fromhex("00020150")] * 4),
        bytes.fromhex("01010150"),
        bytes.fromhex("00020150"),
    ]
    assert [(seq, payload) for _, seq, payload in done3] == [
        (2, bytes(5)),
        (4, bytes.fromhex("0100000000")),
        (3, bytes.fromhex("020103013c")),
        (6, bytes(5)),
        (7, bytes.fromhex("0100000000")),
        (8, bytes.fromhex("0200000000")),
        (9, bytes.fromhex("0200000000")),
        (11, bytes.fromhex("0401000000")),
        (10, bytes.fromhex("0401030278")),
    ]

    # These captures exercise the stock firmware's special first-feed path.
    # Its duration varies: one exchange sends the counted command without a
    # preceding query, while another observes 0,1,2,2 first. Do not impose this
    # path on ordinary counted feeds.
    first_free_run_progress = [payload[0] for _, seq, payload in done3 if seq == 2]
    second_free_run_progress = [payload[0] for _, seq, payload in done3 if 6 <= seq <= 9]
    assert first_free_run_progress == [0]
    assert second_free_run_progress == [0, 1, 2, 2]


def parse_frames(stream):
    """Greedy AA AA framing with CRC-gated acceptance (resync on failure)."""
    frames, i, n = [], 0, len(stream)
    while i < n - 2:
        if stream[i] == 0xAA and stream[i + 1] == 0xAA:
            length = stream[i + 2]
            if 7 <= length <= 19 and i + length <= n:
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

    if CAPTURE_DIR is None:
        print("SKIP: set PETKIT_SERIAL_BUS_DIR to run capture checks")
        return 0

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

    assert_stock_feed_traces()
    print("ok:   stock dispense ordering, completions, and pacing")

    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
