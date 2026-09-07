#!/usr/bin/env python3
"""Build a reproducible map of the stripped ISD91230 Cortex-M0 firmware.

The binary is intentionally not stored in this repository.  The default input
path points at the ignored earlynerd/petkit-serial-bus checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from capstone import CS_ARCH_ARM, CS_MODE_LITTLE_ENDIAN, CS_MODE_THUMB, Cs
from capstone.arm import ARM_INS_BL, ARM_INS_LDR, ARM_OP_IMM, ARM_OP_MEM, ARM_REG_PC


DEFAULT_IMAGE = Path("petkit-serial-bus/flash dumps/ISD91230.bin")
EXPECTED_SHA256 = "e8557cd0b4a601977cb53f2b42199c486e83ed7fa5187cc1f7d4f2b8ea20c201"

# Names are deliberately conservative.  A name describes control flow that is
# visible in the binary; physical meanings that still need a board trace remain
# in the notes instead of being encoded as facts in a symbol name.
FUNCTIONS: dict[int, tuple[str, str]] = {
    0x00C0: ("runtime_entry", "C runtime startup"),
    0x0104: ("copy_words", "startup copy helper"),
    0x0120: ("zero_words", "startup zero helper"),
    0x0144: ("runtime_start", "initialize data/BSS, call main"),
    0x0164: ("memcpy", "byte and word copy"),
    0x019C: ("reset_handler", "clock protection setup, then runtime entry"),
    0x0200: ("udivmod_u32", "unsigned division helper"),
    0x0214: ("divmod_s32", "signed division helper"),
    0x0AF8: ("process_uart_frame", "receive, CRC-check, and dispatch one frame"),
    0x0D84: ("validate_uart_frame", "validate received frame CRC"),
    0x14EC: ("delay_ms", "delay using SysTick-derived counter"),
    0x1510: ("pb5_low", "clear GPIO PB5 output"),
    0x1524: ("pb0_low", "clear GPIO PB0 output"),
    0x1538: ("foreground_task", "20-tick UART, action, sensor, and watchdog task"),
    0x1584: ("dispatch_pending_actions", "execute command flags set by UART parser"),
    0x1688: ("pb3_low_pb4_high", "GPIO PB3/PB4 drive state"),
    0x16AC: ("pb3_high_pb4_low", "GPIO PB3/PB4 drive state"),
    0x16D0: ("pb3_high_pb4_high", "GPIO PB3/PB4 idle/brake state"),
    0x1798: ("pb5_high", "set GPIO PB5 output"),
    0x17AC: ("pb0_high", "set GPIO PB0 output"),
    0x17C0: ("pb1_high_pb2_low", "GPIO PB1/PB2 drive state"),
    0x17E4: ("pb1_low_pb2_high", "GPIO PB1/PB2 drive state"),
    0x1808: ("pb1_high_pb2_high", "GPIO PB1/PB2 idle/brake state"),
    0x18D0: ("gpio_irq", "clear PA13 GPIO interrupt"),
    0x1908: ("gpio_init", "configure GPIO and GPIO interrupt"),
    0x1940: ("gpio_set_mode", "configure GPIO pin mode"),
    0x1970: ("read_pb8", "read GPIO PB8"),
    0x1984: ("read_pb7", "read GPIO PB7"),
    0x1994: ("receive_uart_frame", "assemble AA AA framed UART message"),
    0x1A20: ("detect_sensor_change", "periodically sample sensors and detect a stable-state change"),
    0x1B4C: ("uart_ring_pop", "remove one byte from UART RX ring"),
    0x1B84: ("enter_low_power", "stop normal timers, drive PB0 low, and enter WFI"),
    0x1C10: ("periodic_sensor_report", "sample changes and send status type 0x02"),
    0x1D30: ("sensor_gpio_init", "configure PB5 output and PB6/PB7/PB8 inputs"),
    0x1DE4: ("sample_digital_sensors", "drive PB5, delay, sample PB6/PB7/PB8"),
    0x1E58: ("pa11_high", "set GPIO PA11"),
    0x1E6C: ("pa11_low", "clear GPIO PA11"),
    0x1E80: ("pa12_high", "set GPIO PA12"),
    0x1E94: ("pa12_low", "clear GPIO PA12"),
    0x1EA8: ("indicator_gpio_init", "configure PA11 and PA12 as outputs"),
    0x2014: ("run_type_07_action", "PB3/PB4 motion state machine for command 0x07"),
    0x2258: ("pwm_irq", "PWM interrupt handler"),
    0x2458: ("run_type_0b_action", "PB1/PB2 dispense state machine"),
    0x2644: ("reply_status_request", "sample sensors and send type 0x02 reply"),
    0x265C: ("send_type_15", "build and send a type 0x15 frame"),
    0x2690: ("send_motion_completion", "build and send type 0x08 or 0x0A"),
    0x2724: ("send_dispense_completion", "build and send type 0x0C"),
    0x2780: ("run_type_11_action", "GPIO/sensor operation and type 0x12 reply"),
    0x280C: ("send_status", "build and send type 0x02 status frame"),
    0x28BC: ("reply_type_13", "build and send type 0x14 reply"),
    0x2D64: ("sample_analog_channels", "sample/filter four SARADC-derived values"),
    0x2F80: ("sort_adc_samples", "insertion-sort one ADC sample buffer"),
    0x3278: ("timer0_irq", "increment four software counters"),
    0x32D0: ("timer1_irq", "set periodic-work flag"),
    0x32F8: ("uart0_irq", "move UART0 RX byte into software ring"),
    0x3420: ("uart0_write", "write a byte sequence through UART0"),
    0x34D8: ("watchdog_stop", "disable/stop watchdog"),
    0x34F4: ("watchdog_start", "configure and start watchdog"),
    0x351C: ("watchdog_feed", "write watchdog reset sequence"),
    0x3564: ("periodic_power_task", "periodic sensing, PA13 handshake, and status"),
    0x362C: ("should_enter_low_power", "test low-power state flags"),
    0x365C: ("thumb_u8_switch", "compiler switch-table helper"),
    0x3678: ("main", "hardware initialization and foreground loop"),
    0x36A8: ("crc16_ccitt_false", "CRC-16/CCITT-FALSE"),
}

VECTORS = {
    0: "initial_sp",
    1: "reset",
    2: "nmi",
    3: "hard_fault",
    11: "svcall",
    14: "pendsv",
    15: "systick",
    16: "bod_irq",
    17: "watchdog_irq",
    18: "eint0_irq",
    19: "eint1_irq",
    20: "gpio_irq",
    21: "alc_irq",
    22: "pwm_irq",
    24: "timer0_irq",
    25: "timer1_irq",
    27: "uart1_irq",
    28: "uart0_irq",
    29: "spi1_irq",
    30: "spi0_irq",
    31: "dpwm_irq",
    34: "i2c0_irq",
    37: "comparator_irq",
    38: "mac_irq",
}

COMMAND_TARGETS = {
    0x00: 0x0CE6,
    0x01: 0x0B2E,
    0x02: 0x0CE6,
    0x03: 0x0B3A,
    0x04: 0x0B54,
    0x05: 0x0B6E,
    0x06: 0x0B7C,
    0x07: 0x0B8A,
    0x08: 0x0CE6,
    0x09: 0x0B96,
    0x0A: 0x0CE6,
    0x0B: 0x0BA2,
    0x0C: 0x0CE6,
    0x0D: 0x0BD8,
    0x0E: 0x0C26,
    0x0F: 0x0CBE,
    0x10: 0x0CC6,
    0x11: 0x0CCE,
    0x12: 0x0CE6,
    0x13: 0x0CDA,
}

MMIO_RANGES = (
    (0x40004000, 0x40004100, "WDT"),
    (0x40010000, 0x40014000, "TIMER0/1"),
    (0x40040000, 0x40044000, "PWM"),
    (0x40050000, 0x40054000, "UART0"),
    (0x40060000, 0x40064000, "SARADC"),
    (0x40070000, 0x40074000, "DPWM"),
    (0x40080000, 0x40084000, "analog control"),
    (0x50000100, 0x50000200, "SYS"),
    (0x50000200, 0x50000300, "CLK"),
    (0x50004000, 0x50004040, "GPIOA"),
    (0x50004040, 0x50004080, "GPIOB"),
)


def rizin_functions(image: Path) -> list[dict]:
    result = subprocess.run(
        [
            "rizin", "-e", "scr.color=false", "-a", "arm", "-b", "16",
            "-m", "0", "-q", "-c", "aaa; aflj", str(image),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    start = result.stdout.find("[")
    if start < 0:
        raise RuntimeError("rizin produced no function JSON")
    return json.loads(result.stdout[start:])


def classify_address(value: int) -> str | None:
    if 0x20000000 <= value < 0x20001000:
        return "SRAM"
    for start, end, name in MMIO_RANGES:
        if start <= value < end:
            return name
    if 0xE000E000 <= value < 0xE0010000:
        return "Cortex-M0 system control"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--listing", action="store_true", help="include instructions")
    args = parser.parse_args()

    data = args.image.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_SHA256:
        print(f"warning: unrecognized image SHA-256 {digest}", file=sys.stderr)

    functions = rizin_functions(args.image)
    discovered = {entry["offset"]: entry for entry in functions}
    # Rizin misses a few vector-entry functions in this raw image.
    for address in FUNCTIONS:
        discovered.setdefault(address, {"offset": address, "size": 0, "callrefs": []})

    print(f"image: {args.image}")
    print(f"sha256: {digest}")
    print(f"bytes: {len(data)}")
    print("\nvectors:")
    for index in range(min(48, len(data) // 4)):
        value = int.from_bytes(data[index * 4:index * 4 + 4], "little")
        if value:
            target = value if index == 0 else value & ~1
            print(f"  {index:2} {VECTORS.get(index, 'reserved/unknown'):24} 0x{target:08x}")

    print("\nUART command switch (frame byte 3):")
    for command, target in COMMAND_TARGETS.items():
        disposition = "ignored" if target == 0x0CE6 else f"handler 0x{target:04x}"
        print(f"  0x{command:02x}: {disposition}")

    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
    md.detail = True
    print("\nfunctions:")
    for address, function in sorted(discovered.items()):
        name, note = FUNCTIONS.get(address, (f"sub_{address:04x}", "unclassified"))
        calls = sorted(
            ref["to"] for ref in function.get("callrefs", []) if ref.get("type") == "CALL"
        )
        call_names = [FUNCTIONS.get(value, (f"sub_{value:04x}", ""))[0] for value in calls]
        print(
            f"  0x{address:04x} {name:30} size=0x{function.get('size', 0):x}"
            f" calls={','.join(call_names) or '-'} -- {note}"
        )
        if not args.listing or not function.get("size"):
            continue
        blob = data[address:address + function["size"]]
        for instruction in md.disasm(blob, address):
            suffix = ""
            if instruction.id == ARM_INS_BL and instruction.operands[0].type == ARM_OP_IMM:
                target = instruction.operands[0].imm
                suffix = f" ; {FUNCTIONS.get(target, (f'sub_{target:04x}', ''))[0]}"
            elif (
                instruction.id == ARM_INS_LDR
                and len(instruction.operands) >= 2
                and instruction.operands[1].type == ARM_OP_MEM
                and instruction.operands[1].mem.base == ARM_REG_PC
            ):
                literal = ((instruction.address + 4) & ~3) + instruction.operands[1].mem.disp
                if 0 <= literal <= len(data) - 4:
                    value = int.from_bytes(data[literal:literal + 4], "little")
                    kind = classify_address(value)
                    suffix = f" ; [0x{literal:04x}]=0x{value:08x}"
                    if kind:
                        suffix += f" {kind}"
            print(f"    {instruction.address:04x}: {instruction.mnemonic:8} {instruction.op_str}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
