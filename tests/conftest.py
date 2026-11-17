"""Fixtures: a UMG 96RM on modbus-connection's in-memory mock backend."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest
from modbus_connection.mock import MockModbusUnit

from janitza_modbus import JanitzaMeter, Umg508


def float_words(value: float) -> list[int]:
    """Pack a float into its two registers, most significant word first."""
    hi, lo = struct.unpack(">HH", struct.pack(">f", value))
    return [hi, lo]


def uint32_words(value: int) -> list[int]:
    """Pack an unsigned 32-bit integer into two registers, high word first."""
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


SERIAL = 7012345

# A three-phase installation drawing about 2.3 kW, with a little solar
# delivered back over its lifetime.
MEASUREMENTS: dict[int, float] = {
    19000: 230.1,  # voltage L1-N
    19002: 231.2,  # voltage L2-N
    19004: 229.8,  # voltage L3-N
    19006: 399.5,  # voltage L1-L2
    19008: 400.1,  # voltage L2-L3
    19010: 398.7,  # voltage L3-L1
    19012: 3.25,  # current L1
    19014: 4.5,  # current L2
    19016: 2.0,  # current L3
    19018: 2.1,  # current N, vector sum
    19020: 700.0,  # active power L1
    19022: 1000.0,  # active power L2
    19024: -150.0,  # active power L3, delivering
    19026: 1550.0,  # active power total
    19028: 748.0,  # apparent power L1
    19030: 1040.0,  # apparent power L2
    19032: 460.0,  # apparent power L3
    19034: 2248.0,  # apparent power total
    19036: 120.0,  # reactive power L1
    19038: -80.0,  # reactive power L2
    19040: 30.0,  # reactive power L3
    19042: 70.0,  # reactive power total
    19044: 0.98,  # cos phi L1
    19046: 0.96,  # cos phi L2
    19048: -0.33,  # cos phi L3
    19050: 49.98,  # frequency
    19052: 1.0,  # rotation field, right
    19054: 1_000_000.0,  # active energy L1, net
    19056: 2_000_000.0,  # active energy L2, net
    19058: -500_000.0,  # active energy L3, net
    19060: 2_500_000.0,  # active energy total, net
    19062: 1_200_000.0,  # consumed L1
    19064: 2_100_000.0,  # consumed L2
    19066: 300_000.0,  # consumed L3
    19068: 3_600_000.0,  # consumed total
    19070: 200_000.0,  # delivered L1
    19072: 100_000.0,  # delivered L2
    19074: 800_000.0,  # delivered L3
    19076: 1_100_000.0,  # delivered total
    19084: 4_000_000.0,  # apparent energy total
    19092: 300_000.0,  # reactive energy total
    19100: 350_000.0,  # reactive energy inductive total
    19108: 50_000.0,  # reactive energy capacitive total
    19110: 2.1,  # THD U L1
    19112: 2.3,  # THD U L2
    19114: 1.9,  # THD U L3
    19116: 12.5,  # THD I L1
    19118: 8.0,  # THD I L2
    19120: 30.0,  # THD I L3
}

TRANSFORMERS: dict[int, float] = {
    10: 100.0,  # CT L1 primary
    12: 5.0,  # CT L1 secondary
    14: 400.0,  # VT L1 primary
    16: 400.0,  # VT L1 secondary
    18: 100.0,
    20: 5.0,
    22: 400.0,
    24: 400.0,
    26: 100.0,
    28: 5.0,
    30: 400.0,
    32: 400.0,
}


def seed_identity(unit: MockModbusUnit, serial: int = SERIAL) -> None:
    """Seed the serial, production number, software release and module."""
    unit.holding[750] = 104
    unit.holding[754] = uint32_words(serial)
    unit.holding[756] = uint32_words(1234)
    unit.holding[761] = 3  # Ethernet


def seed_measurements(unit: MockModbusUnit) -> None:
    """Seed every register of the 19000-19121 block, NaN where unset."""
    for address in range(19000, 19122, 2):
        unit.holding[address] = float_words(MEASUREMENTS.get(address, math.nan))


def seed_transformers(unit: MockModbusUnit) -> None:
    """Seed the transformer ratios."""
    for address, value in TRANSFORMERS.items():
        unit.holding[address] = float_words(value)


def seed_meter(unit: MockModbusUnit) -> None:
    """Seed a whole meter."""
    seed_identity(unit)
    seed_measurements(unit)
    seed_transformers(unit)


@pytest.fixture
def meter(mock_modbus_unit: MockModbusUnit) -> JanitzaMeter:
    """A seeded meter, built from its serial so it has read nothing yet."""
    seed_meter(mock_modbus_unit)
    return JanitzaMeter(mock_modbus_unit, serial_number=str(SERIAL))


UMG508_REAL = Path(__file__).parent / "fixtures" / "umg508_real.json"
UMG508_FIXTURE_SERIAL = 21012345


def seed_umg508_real(unit: MockModbusUnit) -> None:
    """Seed the registers a real UMG 508 returned, one word per address."""
    holding = json.loads(UMG508_REAL.read_text())["holding"]
    for address, word in holding.items():
        unit.holding[int(address)] = word


@pytest.fixture
def meter508(mock_modbus_unit: MockModbusUnit) -> Umg508:
    """A UMG 508 seeded with the measurement block alone."""
    seed_measurements(mock_modbus_unit)
    return Umg508(mock_modbus_unit, serial_number="192.0.2.8:502:1")


@pytest.fixture
def real508(mock_modbus_unit: MockModbusUnit) -> Umg508:
    """A UMG 508 seeded with what a real one returned for every block."""
    seed_umg508_real(mock_modbus_unit)
    return Umg508(mock_modbus_unit, serial_number="192.0.2.8:502:1")
