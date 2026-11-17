#!/usr/bin/env python3

"""Find where a UMG 508 keeps its energy counters, CT/VT ratios and serial.

The UMG 508 serves the "frequently required readings" at 19000-19121 like every
UMG, but its other sections sit at addresses no published list gives. This
reads every holding register from 0 to 19999 once (read-only, nothing is
written), saves the dump, and looks for what it should contain:

- the energy counters, by the values the 19000 block reports for them
  (consumed and delivered L1, and the L1..L3 sums), in the layout Janitza
  uses everywhere: L1, L2, L3, L4, L1..L3, L1..L4;
- the transformer ratios, as the run CT primary[4], CT secondary[4],
  VT primary[4], VT secondary[4] of floats;
- the serial number, which follows the ratios at a fixed distance;
- text such as the firmware release and the device name.

Usage:
  uv run script/scan508.py 10.0.0.22 --transport tcp --unit 1

It writes umg508-scan.json next to where it runs; send that file along.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import struct
import time
from typing import Any

from modbus_connection import ModbusError, ModbusUnit
from modbus_connection.cli_helper import add_connection_args, connect_from_args

CONNECTIONS = (("serial", "rtu"), ("tcp", None))
SCAN_END = 20000
BLOCK = 100

# From Janitza's address lists: what follows _CTPRIM, in registers.
# CT prim 8, CT sec 8, VT prim 8, VT sec 8, I rated 8, device name 32,
# device description 64, language 8, display language 2 -> serial number.
SERIAL_AFTER_CTPRIM = 146

# What is set on this meter: CT 35 A / 1 A, and a nominal current of 16 A
# (_IRATED, which follows the VT ratios). The ratio block is recognised by
# these first.
KNOWN_CT = (35.0, 1.0)
KNOWN_IRATED = 16.0


def as_float(words: dict[int, int], address: int) -> float | None:
    """The float32 at an address, high word first, if both words were read."""
    hi, lo = words.get(address), words.get(address + 1)
    if hi is None or lo is None:
        return None
    value = struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
    return None if math.isnan(value) or math.isinf(value) else value


def as_uint32(words: dict[int, int], address: int) -> int | None:
    """The unsigned 32-bit integer at an address, high word first."""
    hi, lo = words.get(address), words.get(address + 1)
    if hi is None or lo is None:
        return None
    return (hi << 16) | lo


def close(a: float | None, b: float, tolerance: float) -> bool:
    """Whether a counter value matches, allowing for it to have moved on."""
    return a is not None and abs(a - b) <= max(tolerance, abs(b) * 1e-4)


async def read_all(
    unit: ModbusUnit, end: int
) -> tuple[dict[int, int], list[list[int]]]:
    """Read 0..end in blocks; a refused block is split down to single words."""
    words: dict[int, int] = {}
    refused: list[list[int]] = []

    async def read(start: int, count: int) -> None:
        try:
            values = await unit.read_holding_registers(start, count)
        except ModbusError:
            if count == 1:
                refused.append([start, start])
                return
            half = count // 2
            await read(start, half)
            await read(start + half, count - half)
            return
        for offset, value in enumerate(values):
            words[start + offset] = value

    for start in range(0, end, BLOCK):
        await read(start, min(BLOCK, end - start))
    # Merge single refused words into ranges.
    merged: list[list[int]] = []
    for first, last in sorted(refused):
        if merged and first == merged[-1][1] + 1:
            merged[-1][1] = last
        else:
            merged.append([first, last])
    return words, merged


def find_energy(
    words: dict[int, int], reference: dict[str, float | None]
) -> list[dict[str, Any]]:
    """Addresses whose float matches a known counter, with the block they imply."""
    hits = []
    for address in range(0, SCAN_END - 1):
        if address >= 19000:
            break
        value = as_float(words, address)
        for name, target in reference.items():
            if target and close(value, target, 5.0):
                hits.append({"address": address, "matches": name, "value": value})
    return hits


def find_ratios(words: dict[int, int]) -> list[dict[str, Any]]:
    """Runs of 4 x 4 floats: CT prim, CT sec, VT prim, VT sec, each 4 equal."""
    found: list[dict[str, Any]] = []
    for address in range(0, 19000 - 32):
        groups = []
        for g in range(4):
            values = [as_float(words, address + 8 * g + 2 * i) for i in range(4)]
            if any(v is None or v <= 0 or v > 1e7 for v in values):
                break
            if len({round(v, 3) for v in values[:3] if v is not None}) != 1:
                break
            groups.append(values)
        if len(groups) == 4:
            found.append(
                {
                    "address": address,
                    "ct_primary": groups[0],
                    "ct_secondary": groups[1],
                    "vt_primary": groups[2],
                    "vt_secondary": groups[3],
                    "known_ct_ratio": {
                        round(groups[0][0] or 0.0, 3),
                        round(groups[1][0] or 0.0, 3),
                    }
                    == set(KNOWN_CT),
                    "irated": [as_float(words, address + 32 + 2 * i) for i in range(4)],
                    "serial_candidate_address": address + SERIAL_AFTER_CTPRIM,
                    "serial_candidate": as_uint32(words, address + SERIAL_AFTER_CTPRIM),
                }
            )
    # The ratio set on this meter first, then the rest.
    for r in found:
        r["known_irated"] = (
            r["irated"][0] is not None and abs(r["irated"][0] - KNOWN_IRATED) < 1e-3
        )
    found.sort(key=lambda r: (not r["known_ct_ratio"], not r["known_irated"]))
    return found


def find_text(words: dict[int, int], minimum: int = 4) -> list[dict[str, Any]]:
    """Printable ASCII runs of at least ``minimum`` characters."""
    found: list[dict[str, Any]] = []
    current: list[str] = []
    start = 0
    for address in range(0, 19000):
        word = words.get(address)
        chars = [] if word is None else [chr(word >> 8), chr(word & 0xFF)]
        printable = bool(chars) and all(32 <= ord(c) < 127 for c in chars)
        if printable:
            if not current:
                start = address
            current.extend(chars)
        else:
            if word is not None and current and 32 <= (word >> 8) < 127:
                current.append(chr(word >> 8))  # a string ending mid-word
            if len(current) >= minimum:
                found.append({"address": start, "text": "".join(current)})
            current = []
    return found


async def main() -> int:
    """Scan one meter and report what it found."""
    logging.getLogger().addHandler(logging.NullHandler())
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_connection_args(parser, connections=CONNECTIONS)
    parser.add_argument("--unit", type=int, default=1, help="Modbus unit id")
    parser.add_argument(
        "--output", default="umg508-scan.json", help="where to save the dump"
    )
    args = parser.parse_args()

    try:
        connection = await connect_from_args(args)
    except ModbusError as err:
        print(f"Could not connect to {args.target}: {str(err) or type(err).__name__}")
        return 1

    unit = connection.for_unit(args.unit)
    started = time.monotonic()
    try:
        block = await unit.read_holding_registers(19054, 24)
        print(f"Scanning 0-{SCAN_END - 1}, this takes a little while...")
        words, refused = await read_all(unit, SCAN_END)
    except ModbusError as err:
        print(f"Could not read the meter: {str(err) or type(err).__name__}")
        return 1
    finally:
        await connection.close()

    reference_words = {19054 + i: w for i, w in enumerate(block)}
    reference = {
        "consumed L1 (19062)": as_float(reference_words, 19062),
        "consumed L1..L3 (19068)": as_float(reference_words, 19068),
        "delivered L1 (19070)": as_float(reference_words, 19070),
        "delivered L1..L3 (19076)": as_float(reference_words, 19076),
    }
    energy = find_energy(words, reference)
    ratios = find_ratios(words)
    texts = find_text(words)

    print(
        f"\nRead {len(words)} registers in {time.monotonic() - started:.1f} s;"
        f" refused ranges: {refused[:20]}"
    )
    print("\nReference counters from the 19000 block:")
    for name, value in reference.items():
        print(f"  {name:28s} {value}")
    print("\nAddresses below 19000 holding the same values:")
    for hit in energy[:40]:
        print(f"  {hit['address']:6d}  {hit['matches']:28s} {hit['value']}")
    if not energy:
        print("  none")
    print("\nTransformer ratio candidates (CT prim, CT sec, VT prim, VT sec):")
    for r in ratios[:10]:
        print(
            f"  {r['address']:6d}  CT {r['ct_primary'][0]}/{r['ct_secondary'][0]}"
            f"  VT {r['vt_primary'][0]}/{r['vt_secondary'][0]}"
            f"  I rated {r['irated'][0]}"
            f"{'  <- 35/1' if r['known_ct_ratio'] else ''}"
            f"{' + 16 A' if r['known_irated'] else ''}"
            f"  -> serial at {r['serial_candidate_address']}:"
            f" {r['serial_candidate']}"
        )
    if not ratios:
        print("  none")
    print("\nText found (address: text):")
    for t in texts[:40]:
        print(f"  {t['address']:6d}  {t['text']!r}")

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "reference": reference,
                "energy_hits": energy,
                "ratio_candidates": ratios,
                "texts": texts,
                "refused": refused,
                "holding": {str(a): w for a, w in sorted(words.items())},
            },
            handle,
        )
    print(f"\nSaved the full dump to {args.output}")
    return 0


raise SystemExit(asyncio.run(main()))
