#!/usr/bin/env python3

"""Find a UMG 508's energy counters and CT ratios, one address at a time.

A UMG 508 answers a read that starts in the middle of a variable with a fixed,
meaningless buffer, so a scan in large blocks misses most of its map. This
reads two registers at every address in a range instead (read-only, nothing
is written): at the address where a variable starts, its real value comes
back. It then looks for the values this meter is known to hold:

- the energy counters, by the values the 19000 block reports for them;
- the current transformer ratio 35 A / 1 A and the nominal current 16 A.

Usage:
  uv run script/scan508_fine.py 10.0.0.22 --transport tcp --unit 1

It takes a minute or two and writes umg508-scan-fine.json; send that along.
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
DEFAULT_START = 4000
DEFAULT_END = 19000

# What is set on this meter: CT 35 A / 1 A, nominal current 16 A.
KNOWN = {"CT primary 35 A": 35.0, "CT secondary 1 A": 1.0, "nominal current 16 A": 16.0}


def to_float(hi: int, lo: int) -> float | None:
    """A float32 from two registers, high word first; ``None`` for NaN."""
    value = struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
    return None if math.isnan(value) or math.isinf(value) else value


async def scan(
    unit: ModbusUnit, start: int, end: int
) -> tuple[dict[int, list[int]], list[int]]:
    """Read two registers at every address; return them and the refused ones."""
    pairs: dict[int, list[int]] = {}
    refused: list[int] = []
    last_report = time.monotonic()
    for address in range(start, end):
        try:
            pairs[address] = list(await unit.read_holding_registers(address, 2))
        except ModbusError:
            refused.append(address)
        if time.monotonic() - last_report > 10:
            print(f"  ... at {address}")
            last_report = time.monotonic()
    return pairs, refused


async def main() -> int:
    """Scan one meter address by address and report what it found."""
    logging.getLogger().addHandler(logging.NullHandler())
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_connection_args(parser, connections=CONNECTIONS)
    parser.add_argument("--unit", type=int, default=1, help="Modbus unit id")
    parser.add_argument("--start", type=int, default=DEFAULT_START)
    parser.add_argument("--end", type=int, default=DEFAULT_END)
    parser.add_argument(
        "--output", default="umg508-scan-fine.json", help="where to save the dump"
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
        print(f"Scanning {args.start}-{args.end - 1} one address at a time...")
        pairs, refused = await scan(unit, args.start, args.end)
        block_after = await unit.read_holding_registers(19054, 24)
    except ModbusError as err:
        print(f"Could not read the meter: {str(err) or type(err).__name__}")
        return 1
    finally:
        await connection.close()

    def ref(words: list[int], address: int) -> float | None:
        offset = address - 19054
        return to_float(words[offset], words[offset + 1])

    reference: dict[str, tuple[float | None, float | None]] = {
        "consumed L1 (19062)": (ref(block, 19062), ref(block_after, 19062)),
        "consumed L1..L3 (19068)": (ref(block, 19068), ref(block_after, 19068)),
        "delivered L1 (19070)": (ref(block, 19070), ref(block_after, 19070)),
        "delivered L1..L3 (19076)": (ref(block, 19076), ref(block_after, 19076)),
    }

    hits: list[dict[str, Any]] = []
    for address, (hi, lo) in sorted(pairs.items()):
        value = to_float(hi, lo)
        if value is None:
            continue
        for name, (before, after) in reference.items():
            if before is None or after is None:
                continue
            low, high = min(before, after) - 1.0, max(before, after) + 1.0
            if low <= value <= high:
                hits.append({"address": address, "matches": name, "value": value})
        for name, target in KNOWN.items():
            if value == target:
                hits.append({"address": address, "matches": name, "value": value})

    elapsed = time.monotonic() - started
    print(f"\nRead {len(pairs)} addresses in {elapsed:.0f} s; refused: {len(refused)}")
    print("\nReference counters (before, after the scan):")
    for name, (before, after) in reference.items():
        print(f"  {name:28s} {before}  {after}")
    print("\nMatches (address, what, value):")
    for hit in hits[:200]:
        print(f"  {hit['address']:6d}  {hit['matches']:28s} {hit['value']}")
    if not hits:
        print("  none")

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "range": [args.start, args.end],
                "reference": reference,
                "hits": hits,
                "refused": refused,
                "pairs": {str(a): p for a, p in sorted(pairs.items())},
            },
            handle,
        )
    print(f"\nSaved the dump to {args.output}")
    return 0


raise SystemExit(asyncio.run(main()))
