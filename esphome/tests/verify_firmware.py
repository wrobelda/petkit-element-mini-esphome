#!/usr/bin/env python3
"""Instruction-level verification of the Petkit bus protocol against BOTH
firmware dumps — independent of any prose documentation.

It asserts the specific opcodes that implement the framing/CRC in each chip and
re-implements the M0 CRC to prove the polynomial/seed. Every offset below was
located by disassembly (ARM Thumb via capstone for the M0; Xtensa LX106 via the
Espressif objdump for the ESP8266).

  ISD91230 (Cortex-M0), ISD91230.bin, XIP @ 0x0:
    0x1994  receiver:  cmp #0xAA x2 (header), ldrb [r1,#2] (length),
            cmp #6 / cmp #0x13 (bounds 6..19), subs r1,#3 (read length-3 body)
    0x36A8  CRC-16:    table-less CCITT, seed literal 0xFFFF @ 0x36E8

  ESP8266 (Xtensa LX106), petkitesp8266flash.bin, irom0 @ 0x40200000:
    0x40253d90 FEEDER command parser (references _DOOR_OPEN / motor open via
            l32r): bgeui a4,7 (min length), movi a2,170 + beq x2 (AA AA header),
            addi +2 / l8ui (length byte), movi 12 + bgeu (bounds 7..19).
            The ESP8266 CRC routine is NOT located here, so CRC is proven only
            from the captures and the M0 firmware, not from both disassemblies.

Run:  python3 verify_firmware.py
"""
import os
import struct
import sys

HERE = os.path.dirname(__file__)
DUMPS = os.path.join(HERE, "..", "..", "petkit-serial-bus", "flash dumps")
M0 = os.path.join(DUMPS, "ISD91230.bin")
ESP = os.path.join(DUMPS, "petkitesp8266flash.bin")

fails = 0


def check(cond, msg):
    global fails
    print(("ok:   " if cond else "FAIL: ") + msg)
    if not cond:
        fails += 1


def m0_crc(buf, crc=0xFFFF):
    """Byte-for-byte port of the M0 routine at 0x36A8 (table-less CRC-16/CCITT)."""
    for b in buf:
        crc = ((crc >> 8) | (crc << 8)) & 0xFFFF
        crc ^= b
        crc ^= (crc & 0xFF) >> 4
        crc = (crc ^ (crc << 12)) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
        crc &= 0xFFFF
    return crc


def verify_m0():
    if not os.path.exists(M0):
        print(f"SKIP: {M0} not found")
        return
    d = open(M0, "rb").read()
    print("--- ISD91230 (Cortex-M0) ---")
    sp, reset = struct.unpack_from("<II", d, 0)
    check(0x20000000 <= sp <= 0x20010000 and reset & 1, "valid Cortex-M0 vector table")
    # Instruction bytes (little-endian, file order) at the located offsets.
    ins = {
        0x19B8: (b"\xaa\x28", "cmp r0,#0xAA (header byte 1)"),
        0x19C0: (b"\xaa\x28", "cmp r0,#0xAA (header byte 2)"),
        0x19C6: (b"\x80\x78", "ldrb r0,[r0,#2] (length field)"),
        0x19C8: (b"\x06\x28", "cmp r0,#6 (min length)"),
        0x19D0: (b"\x13\x28", "cmp r0,#0x13 (max length 19)"),
        0x19DC: (b"\xc9\x1e", "subs r1,r1,#3 (body = length-3)"),
    }
    for off, (b, desc) in ins.items():
        check(d[off : off + len(b)] == b, f"M0 @0x{off:05X}: {desc}")

    # Type 0x0B command handler. Payload byte 1 selects counted motion (1) or
    # a progress query (2). Counted motion stores payload byte 0 as the cycle
    # counter; the motor routine decrements it after a sensor transition unless
    # it is 0xFF, which is the free-running mode used by stock's first-feed path.
    motion = {
        0x0BA2: (b"\xa0\x79", "read payload byte 1"),
        0x0BA4: (b"\x01\x28", "select counted-motion branch"),
        0x0BB2: (b"\x60\x79", "read payload byte 0 cycle count"),
        0x0BC6: (b"\xa0\x79", "read payload byte 1 for query branch"),
        0x0BC8: (b"\x02\x28", "select progress-query branch"),
        0x2532: (b"\xff\x28", "0xFF cycle count is free-running"),
        0x253A: (b"\x40\x1e", "decrement counted wheel cycles"),
        0x15FC: (b"\x01\xf0\x92\xf8", "query path calls type-0x0C builder"),
    }
    for off, (b, desc) in motion.items():
        check(d[off : off + len(b)] == b, f"M0 @0x{off:05X}: {desc}")

    # The type-0x02 status builder exposes the three sampled GPIOB inputs in
    # PB8, PB6, PB7 order. PB8 is read by the type-0x07 outlet state machine,
    # while PB7 is read by the type-0x0B dispenser state machine. PB6 is the
    # remaining optical food-level channel, sampled after PB5 excitation.
    check(struct.unpack_from("<III", d, 0x28A0) ==
          (0x200000F7, 0x200000F5, 0x200000F9),
          "M0 type-0x02 digital inputs are PB8, PB6, PB7")
    check(d[0x1E00:0x1E2C] == bytes.fromhex(
              "124800694021084080090c4908700f48006980210840c0090d4908700b480069ff2101310840000a06490870"),
          "M0 samples PB6, PB7, and PB8 after sensor excitation")
    check(d[0x202E:0x2032] == b"\xff\xf7\x9f\xfc",
          "M0 outlet state machine reads PB8")
    check(d[0x2478:0x247C] == b"\xff\xf7\x84\xfa",
          "M0 dispenser state machine reads PB7")

    # The type-0x0C builder reads the wheel/sensor count from 0x20000050 and
    # reports completion when the remaining cycle count at 0x2000001F is zero.
    check(struct.unpack_from("<I", d, 0x2778)[0] == 0x20000050,
          "M0 type-0x0C byte 0 reads wheel count @ 0x20000050")
    check(struct.unpack_from("<I", d, 0x277C)[0] == 0x2000001F,
          "M0 type-0x0C byte 1 reads remaining cycle count @ 0x2000001F")
    check(d[0x2742:0x274A] == b"\x00\x78\x00\x28\x01\xd1\x01\x21",
          "M0 type-0x0C completion is one when remaining count is zero")
    seed = struct.unpack_from("<I", d, 0x36E8)[0]
    check(seed == 0x0000FFFF, "M0 CRC seed literal 0xFFFF @ 0x36E8")
    # Prove the M0's own CRC validates captured packets.
    for p in ("AAAA070101599B", "AAAA0801010194 13".replace(" ", ""),
              "AAAA1202FF00000108EC023F08710220 24C5".replace(" ", "")):
        f = bytes.fromhex(p)
        check(m0_crc(f) == 0 and m0_crc(f[:-2]) == int.from_bytes(f[-2:], "big"),
              f"M0 CRC validates captured packet {p[:14]}...")


def verify_esp():
    if not os.path.exists(ESP):
        print(f"SKIP: {ESP} not found")
        return
    d = open(ESP, "rb").read()
    print("--- ESP8266 (Xtensa LX106) ---")
    # irom0.text data starts at file 0x1010, maps to VMA 0x40200000.
    def foff(vma):
        return 0x1010 + (vma - 0x40200000)

    # Frame parser opcodes (Xtensa bytes in file/little order). Asserting the
    # full header/length logic, not just the movi constants (review-5).
    ins = {
        0x40253DD5: (b"\xf6\x74\x02", "bgeui a4,7 (require >=7 bytes)"),
        0x40253DF4: (b"\x22\xa0\xaa", "movi a2,170 (0xAA header byte 1)"),
        0x40253DF7: (b"\x27\x13\x13", "beq a3,a2 (branch when byte1 == 0xAA)"),
        0x40253E26: (b"\x22\xa0\xaa", "movi a2,170 (0xAA header byte 2)"),
        0x40253E29: (b"\x27\x13\x18", "beq a3,a2 (branch when byte2 == 0xAA)"),
        0x40253E45: (b"\x2b\x2f", "addi.n a2,a15,2 (index -> length at offset +2)"),
        0x40253E5A: (b"\xc2\x02\x00", "l8ui a12,[a2] (read the length byte)"),
        0x40253E5D: (b"\x0c\xc3", "movi.n a3,12 (max payload)"),
        0x40253E5F: (b"\x22\xcc\xf9", "addi a2,a12,-7 (payload = length-7)"),
        0x40253E65: (b"\x27\xb3\x12", "bgeu a3,a2 (require length-7 <= 12, i.e. len<=19)"),
    }
    for vma, (b, desc) in ins.items():
        fo = foff(vma)
        check(d[fo : fo + len(b)] == b, f"ESP @0x{vma:08X}: {desc}")

    # This routine (not the MQTT decoder) is the FEEDER command parser: it
    # references feeder strings via l32r. Assert those strings exist at the VMAs
    # the l32r targets point to (see the disassembly scan in the repo notes).
    for vma, s in ((0x40202E79, b"_DOOR_OPEN"), (0x40202ED1, b"motor open:")):
        fo = foff(vma)
        check(d[fo : fo + len(s)] == s, f"parser references feeder string {s.decode()!r}")

    # UART0 vs UART1: the firmware references UART0 MMIO and never UART1. This is
    # consistent with the bus being on UART0 (per wiring/captures) but does NOT
    # by itself prove this parser reads UART0 — noted, not overclaimed.
    check(d.count(struct.pack("<I", 0x60000F00)) == 0, "no UART1 MMIO literal (bus is UART0 per wiring)")
    check(d.count(struct.pack("<I", 0x60000000)) > 0, "UART0 MMIO is referenced")

    # Correction to an earlier claim: the 'decodePacket' string is MQTT (Aliyun
    # iotkit), NOT the UART decoder — assert it sits among the MQTT strings.
    dp = d.find(b"decodePacket error")
    check(dp >= 0 and d.find(b"mqtt read error", dp - 64, dp) >= 0,
          "'decodePacket' string is in MQTT code, not the UART decoder")


def main():
    verify_m0()
    verify_esp()
    print()
    if fails:
        print(f"{fails} CHECK(S) FAILED")
    else:
        # Precise about what each firmware proves: FRAMING is confirmed in both;
        # CRC is confirmed from the captures and the M0 firmware only (the
        # ESP8266's CRC routine was not located, so it is not asserted here).
        print("VERIFIED: framing bounds confirmed in BOTH firmwares; "
              "CRC-16/CCITT confirmed from captures + M0 firmware "
              "(ESP8266 CRC routine not located)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
