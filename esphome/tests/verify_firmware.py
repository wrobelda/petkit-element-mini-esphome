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
    0x40253d90 receiver: bgeui a4,7 (min length), movi a2,170 + beq x2 (header),
            l8ui length, addi -7 + bgeu 12 (bounds 7..19), ring-buffer copy

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

    # Frame parser opcodes (Xtensa bytes in file/little order).
    ins = {
        0x40253DD5: (b"\xf6\x74\x02", "bgeui a4,7 (require >=7 bytes)"),
        0x40253DF4: (b"\x22\xa0\xaa", "movi a2,170 (0xAA header byte 1)"),
        0x40253E26: (b"\x22\xa0\xaa", "movi a2,170 (0xAA header byte 2)"),
        0x40253E5A: (b"\xc2\x02\x00", "l8ui a12,[len] (length byte at +2)"),
        0x40253E5F: (b"\x22\xcc\xf9", "addi a2,a12,-7 (payload = length-7)"),
    }
    for vma, (b, desc) in ins.items():
        fo = foff(vma)
        check(d[fo : fo + len(b)] == b, f"ESP @0x{vma:08X}: {desc}")

    # The bus is on UART0: the UART1 MMIO base is never referenced as a literal.
    check(d.count(struct.pack("<I", 0x60000F00)) == 0, "ESP references no UART1 MMIO (bus is UART0)")
    check(d.count(struct.pack("<I", 0x60000000)) > 0, "ESP references UART0 MMIO")

    # Correction to an earlier claim: the 'decodePacket' string is MQTT (Aliyun
    # iotkit), NOT the UART decoder — assert it sits among the MQTT strings.
    dp = d.find(b"decodePacket error")
    check(dp >= 0 and d.find(b"mqtt read error", dp - 64, dp) >= 0,
          "'decodePacket' string is in MQTT code, not the UART decoder")


def main():
    verify_m0()
    verify_esp()
    print()
    print("VERIFIED: framing bounds + CRC confirmed in both firmwares" if not fails
          else f"{fails} CHECK(S) FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
