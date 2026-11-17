#!/usr/bin/env python3

"""Query a Janitza UMG 96RM or UMG 508 and print every value."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time

from modbus_connection import ModbusError
from modbus_connection.cli_helper import (
    CountingUnit,
    add_connection_args,
    connect_from_args,
    print_component,
)

from janitza_modbus import MEASUREMENT_COMPONENTS, JanitzaDevice, JanitzaMeter, Umg508

# The meter's RS-485 line, direct or through a socket:// device, and the
# Ethernet variants answering Modbus TCP themselves. No ASCII: the library
# does not support it.
CONNECTIONS = (("serial", "rtu"), ("tcp", None))

EXAMPLES = """examples:
  uv run script/query.py /dev/ttyUSB0 --unit 1 --baudrate 115200
  uv run script/query.py socket://192.168.1.50:8899 --unit 1 --baudrate 115200
  uv run script/query.py 192.168.1.50 --transport tcp --unit 1 --raw
  uv run script/query.py 192.168.1.60 --transport tcp --unit 1 --model umg508
"""


async def main() -> int:
    """Read one meter and print it."""
    # The backend logs a failed connect at ERROR with its traceback; the
    # messages below say it in one line.
    logging.getLogger().addHandler(logging.NullHandler())

    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_connection_args(parser, connections=CONNECTIONS)
    parser.add_argument("--unit", type=int, default=1, help="Modbus unit id")
    parser.add_argument(
        "--model",
        choices=("umg96rm", "umg508"),
        default="umg96rm",
        help="which meter is on the line (default: umg96rm)",
    )
    parser.add_argument(
        "--raw", action="store_true", help="also dump every register read, as JSON"
    )
    args = parser.parse_args()

    try:
        connection = await connect_from_args(args)
    except ModbusError as err:
        # A connect timeout arrives with no message of its own.
        detail = str(err) or type(err).__name__
        print(f"Could not connect to {args.target}: {detail}")
        return 1

    counting = CountingUnit(connection.for_unit(args.unit))
    try:
        meter: JanitzaDevice
        if args.model == "umg508":
            meter = await Umg508.async_detect(counting)
            await meter.async_read_identity()
        else:
            meter = await JanitzaMeter.async_detect(counting)
        setup_reads = counting.reads
        started = time.monotonic()
        report = await meter.async_update()
        elapsed = time.monotonic() - started
        # A raw dump re-reads everything, so count the poll alone.
        poll_reads = counting.reads - setup_reads
        raw = await meter.async_read_raw() if args.raw else None
    except ModbusError as err:
        print(f"Could not read the meter: {str(err) or type(err).__name__}")
        print(f"{counting.reads} Modbus reads")
        return 1
    finally:
        await connection.close()

    print(f"Model          {meter.model_name}")
    if isinstance(meter, JanitzaMeter):
        print(f"Serial number  {meter.serial_number}")
        print()
        print_component(meter.identity, title="identity")
    if isinstance(meter, Umg508):
        print(f"Serial number  {meter.reported_serial_number}")
        print(f"Firmware       {meter.software_release}")
    if meter.unrecognized:
        print("\nThe device answered but is not recognised as this model.")
    extra = [name for name in meter.readings_components if name != "measurements"]
    for name in (*MEASUREMENT_COMPONENTS, *extra, *meter.settings_components):
        print()
        print_component(getattr(meter, name), title=name)
    if report.failed:
        print("\nFailed to read:")
        for name, error in report.failed.items():
            print(f"  {name}: {str(error) or type(error).__name__}")
    if raw is not None:
        print("\nRaw registers")
        # The dump arrives address-ordered; sorting keys would undo that.
        print(json.dumps(raw, indent=2))
    print(f"\n{poll_reads} Modbus reads in {elapsed * 1000:.0f} ms")
    return 0


raise SystemExit(asyncio.run(main()))
