# janitza-modbus

Read Janitza UMG 96RM and UMG 508 power analysers over Modbus, as typed Python
objects rather than register numbers.

The library maps their register sets onto
[modbus-connection](https://github.com/home-assistant-libs/modbus-connection)'s device model:
you hand it a `ModbusUnit`, call `async_update()`, and read sub-systems as
attributes. It owns no connection and no I/O policy — the caller does.

## Supported devices

**`JanitzaMeter` — the UMG 96RM family.** The RS-485 models (UMG 96RM,
UMG 96RM-P, UMG 96RM-CBM) and the variants with an Ethernet port that answer
Modbus TCP themselves (UMG 96RM-EL, UMG 96RM-E). They share one register map:

- the "frequently required readings" block, 19000–19121: voltages, currents,
  power, power factor, frequency, phase rotation, energy counters and THD, all
  IEEE-754 floats
- the current and voltage transformer ratios, 10–33
- the serial number, production number, software release and fitted module,
  750–761

**`Umg508` — the UMG 508.** Reached over its own Ethernet port (Modbus TCP) or
over RS-485 (Modbus RTU). It serves the same "frequently required readings"
block, 19000–19121, at the same addresses and in the same units, so it has the
same `voltage`, `current`, `power`, `grid`, `energy` and `harmonics`
sub-systems as the UMG 96RM, and nothing beyond them. Its other address
sections differ from the UMG 509-PRO's: on a real UMG 508 the 509-PRO
addresses for tariff energy (13949–14008), transformer ratios (14176–14207)
and firmware (17435) answer with unrelated values, so they are not read.

It has no `identity`, and the 508 serves no serial number in the block this
library reads, so `Umg508` takes a `serial_number` that you choose, as a
stable name for the meter, instead of reading one.

Both models derive from `JanitzaDevice`, which holds everything they share.

Read-only. Nothing is written to either meter.

## Usage

```python
import asyncio

from modbus_connection import ModbusSerialParams
from modbus_connection.tmodbus import ModbusConnection
from janitza_modbus import JanitzaMeter


async def main() -> None:
    connection = ModbusConnection(
        ModbusSerialParams(device="/dev/ttyUSB0", baudrate=115200)
    )
    try:
        meter = await JanitzaMeter.async_detect(connection.for_unit(1))
        await meter.async_update()

        print("Serial:", meter.serial_number)
        print("Voltage L1:", meter.voltage.voltage_l1_n, "V")
        print("Power:", meter.power.active_power_total, "W")
        print("Frequency:", meter.grid.frequency, "Hz")
        print("Rotation:", meter.grid.rotation_field)
        print("Consumed:", meter.energy.active_energy_consumed_total, "Wh")
        print("Delivered:", meter.energy.active_energy_delivered_total, "Wh")
    finally:
        await connection.close()


asyncio.run(main())
```

Values come back in the unit the meter measures them in: V, A, W, VA, var,
Hz, Wh, VAh, varh and %. Each field carries its unit as metadata
(`Energy.active_energy_total.unit == "Wh"`). A register reading NaN, which is
how the meter says "no value", decodes to `None`, as does anything not read
yet.

A float32 carries about seven significant digits, and widening one to a
Python float adds noise: the meter's 230.1 V arrives as 230.10000610351562.
Every value decodes to the shortest decimal that encodes to the same float32
instead, so it reads back as 230.1 without dropping a digit the meter sent.

### Using a UMG 508

```python
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection
from janitza_modbus import Umg508

connection = ModbusConnection(ModbusTcpParams(host="192.168.1.60"))
meter = await Umg508.async_detect(connection.for_unit(1))
if meter.unrecognized:
    raise SystemExit("not answering the measurement block")
await meter.async_update()

print("Power:", meter.power.active_power_total, "W")
print("Consumed:", meter.energy.active_energy_consumed_total, "Wh")
print("Delivered:", meter.energy.active_energy_delivered_total, "Wh")
```

`async_detect()` reads the measurement block once, which is how it knows the
device is a meter that serves it. A dead link or a silent meter raises, as
from any poll; a device that answers but refuses the block is `unrecognized`.

The unit id is the meter's own Modbus device address, the one set for its
RS-485 interface (default 1), also when you reach it over Ethernet.

### Sub-systems

| Attribute | Class | Holds |
| :--- | :--- | :--- |
| `voltage` | `Voltage` | L1-N, L2-N, L3-N, L1-L2, L2-L3, L3-L1 |
| `current` | `Current` | L1, L2, L3 and the neutral (vector sum) |
| `power` | `Power` | active, apparent and reactive power per phase and total; cos φ per phase |
| `grid` | `Grid` | frequency and `rotation_field` (`RotationField.RIGHT`, `NONE`, `LEFT`) |
| `energy` | `Energy` | active energy net, consumed and delivered; apparent energy; reactive energy total, inductive and capacitive — each per phase and total |
| `harmonics` | `Harmonics` | THD of voltage and current per phase |
| `transformers` | `Transformers` | CT and VT primary and secondary per phase |
| `identity` | `Identity` | serial number, production number, software release, `ModuleType` |

On a UMG 508 only the first six rows apply (everything except `transformers`
and `identity`).

Active power is signed: positive while the installation consumes, negative
while it delivers, as the current transformers are wired. The net active
energy counters follow the same sign, so a meter on a solar installation can
read a negative `active_energy_l3`; `active_energy_consumed_*` and
`active_energy_delivered_*` only ever rise.

### The measurements are one read

The six measurement sub-systems tile the 19000–19121 block without a gap. A
meter that serves one serves all of them, so reading them apart would cost
requests and isolate nothing. They are pooled into one `ComponentGroup`,
`meter.measurements`, and an `UpdateReport` names the pool rather than its
members. `JanitzaMeter.report_name()` says which name a sub-system is
reported under:

```python
report = await meter.async_update()
if JanitzaMeter.report_name("energy") in report.failed:
    print("the energy counters kept their previous values")
```

The pool reads the block as two requests, 19000–19053 and 19054–19121: each
fits one Modbus request on its own, and splitting at the energy counters
keeps both well inside what a meter, or a gateway in front of it, is known to
take. The transformer ratios are one more request, and the identity three,
since the parameter list documents nothing between its four addresses.

### Measurements and settings refresh separately

`JanitzaMeter` splits its poll by what it reads:

- `async_update_readings()` — what the meter measures: the whole 19000 block.
- `async_update_settings()` — what it has been configured with: the
  transformer ratios, which change only when someone sets them at the meter.
- `async_update()` — both, in one merged report, for a caller that does not
  want to schedule them apart.

```python
await meter.async_update_readings()  # every cycle
await meter.async_update_settings()  # rarely
```

`readings_components` and `settings_components` name what each one polls.

A poll reads each of these on its own: one slow or refused block does not
take the rest with it. Every update method returns an `UpdateReport` — a
failed component keeps its previous values, does not notify its listeners,
and is listed by name with its error, while every other component refreshes
and notifies once the whole poll is done. A dead link
(`ModbusConnectionError`) raises, and so does a timeout before anything has
answered — a meter that is simply not responding is not walked block by
block, paying a timeout for each.

### Identity

`identity` is in neither poll: it never changes, and nothing reads it unasked.
`async_detect()` reads it, and builds the meter from the serial number it
finds. A caller that already knows the serial number passes it to the
constructor instead, which reads nothing:

```python
meter = JanitzaMeter(unit, serial_number="7012345")
await meter.async_read_identity()  # optional: firmware and module
```

`async_read_identity()` makes three tries against a blip, then logs a warning
instead of raising: nothing a poll reads depends on it. A meter that answers
with a serial number of 0 is `unrecognized`.

The serial number is decoded as an unsigned 32-bit integer, high word first,
and the software release as the plain integer the meter reports.

### Diagnostics

For an issue report, `async_read_raw()` dumps every register the meter reads
undecoded, keyed by address space and address. It fires no update listeners
— a download is not a poll, though the fields it reads do refresh.
`redact_serial=True` leaves the serial and production numbers out, for a dump
that will be posted publicly.

A dump loads straight into modbus-connection's mock backend with
`MockModbusUnit.load_raw()`, so a report about a wrong value becomes a test.

## Checking a real meter

`script/query.py` reads one meter once and prints every value it serves, which
is the quickest way to see whether a meter is reachable and addressed
correctly:

```bash
uv run script/query.py 192.168.1.60 --transport tcp --unit 1 --model umg508  # UMG 508
uv run script/query.py /dev/ttyUSB0 --unit 1 --baudrate 115200
uv run script/query.py socket://192.168.1.50:8899 --unit 1 --baudrate 115200
uv run script/query.py 192.168.1.50 --transport tcp --unit 1 --raw
```

It prints the read count of the poll and how long it took, so the request
budget is visible against real hardware rather than only in the tests.
`--raw` adds every register it read, undecoded, which is what an issue about
a wrong value should quote.

## Naming the link

A UMG 96RM, and a UMG 508 on its RS-485 port, answer RTU on that line, so how to name the link depends on
what sits between you and that line, not on the meter:

| Between you and the line | Parameters |
| :--- | :--- |
| Nothing: a USB or serial adapter | `ModbusSerialParams(device="/dev/ttyUSB0", baudrate=115200)` |
| A box forwarding the RTU frames | `ModbusSerialParams(device="socket://host:port", baudrate=115200)` |
| A gateway, or an Ethernet model, answering Modbus TCP | `ModbusTcpParams(host=...)` |

The UMG 96RM leaves the factory at address 1, 115200 baud, 8 data bits, no
parity and one stop bit (parameters 000, 001 and 003 on the meter itself).
The baud rate is the one setting a serial link most often gets wrong.

The UMG 508 has an Ethernet port that answers Modbus TCP on port 502 itself,
which is the easiest way to reach it. Whichever way you reach it, the unit id
is the Modbus address configured for its RS-485 interface. On its own serial
line the UMG 508 uses 8 data bits, no parity and 2 stop bits.

ASCII framing is unsupported. This library never accepts or forwards it, and
it exposes no connect helper that could, since the caller builds the
`ModbusUnit` and hands it over.

## How long a request waits

The timeout is spent per request, not per poll, so it is what a poll loses on
every block that goes unanswered. The meter itself is quick; what sits
between it and you varies far more than the meter does, so `JanitzaMeter`
does not guess a value. The connection's own default, 10 seconds, applies.

A caller who has measured their link states it with `timeout=` on the
constructor or on `async_detect()`, which asks for it through
`ModbusUnit.require_timeout()`. The connection runs with the largest value any
of its units asks for, so a device sharing the line can raise it.

## Debug logging

The `janitza_modbus` logger says, at `DEBUG`, what the meter identified as —
its serial number, software release and module — and what a poll reads.
A meter that keeps refusing its identity registers gets one `WARNING`.

## Development

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

See [RELEASING.md](RELEASING.md) for publishing a version.

## Attribution

The register maps are taken from Janitza's UMG 96RM user manual and Modbus
address list ("frequently required readings" and the parameter list), and
from the UMG 508 Modbus address list ("frequently required readings").
Janitza is a trademark of Janitza electronics GmbH; this project is not
affiliated with it.
