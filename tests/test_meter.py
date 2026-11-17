"""The UMG 96RM register map, decoded over the mock backend."""

from __future__ import annotations

import math
import struct

import pytest
from modbus_connection.mock import MockModbusUnit
from modbus_connection.model import Component

from janitza_modbus import (
    MEASUREMENT_COMPONENTS,
    JanitzaMeter,
    ModuleType,
    RotationField,
)
from janitza_modbus.model import shortest_float32

from .conftest import MEASUREMENTS, SERIAL, float_words, seed_meter


async def test_detect_reads_the_serial_number(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    seed_meter(mock_modbus_unit)
    meter = await JanitzaMeter.async_detect(mock_modbus_unit)
    assert meter.serial_number == str(SERIAL)
    assert not meter.unrecognized
    assert meter.identity.software_release == 104
    assert meter.identity.production_number == 1234
    assert meter.identity.module is ModuleType.ETHERNET


async def test_a_meter_answering_no_serial_is_unrecognized(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    meter = await JanitzaMeter.async_detect(mock_modbus_unit)
    assert meter.serial_number == "0"
    assert meter.unrecognized


async def test_constructor_reads_nothing(mock_modbus_unit: MockModbusUnit) -> None:
    JanitzaMeter(mock_modbus_unit, serial_number="1")
    assert mock_modbus_unit.read_events == []


async def test_timeout_is_asked_of_the_unit(mock_modbus_unit: MockModbusUnit) -> None:
    JanitzaMeter(mock_modbus_unit, serial_number="1", timeout=2.5)
    assert mock_modbus_unit.required_timeout == 2.5


async def test_voltage_and_current(meter: JanitzaMeter) -> None:
    await meter.async_update_readings()
    assert meter.voltage.voltage_l1_n == pytest.approx(230.1)
    assert meter.voltage.voltage_l3_l1 == pytest.approx(398.7)
    assert meter.current.current_l1 == pytest.approx(3.25)
    assert meter.current.current_n == pytest.approx(2.1)


async def test_power_is_signed(meter: JanitzaMeter) -> None:
    await meter.async_update_readings()
    assert meter.power.active_power_l3 == pytest.approx(-150.0)
    assert meter.power.active_power_total == pytest.approx(1550.0)
    assert meter.power.reactive_power_l2 == pytest.approx(-80.0)
    assert meter.power.cos_phi_l3 == pytest.approx(-0.33)


async def test_grid(meter: JanitzaMeter) -> None:
    await meter.async_update_readings()
    assert meter.grid.frequency == pytest.approx(49.98)
    assert meter.grid.rotation_field is RotationField.RIGHT


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (-1.0, RotationField.LEFT),
        (0.0, RotationField.NONE),
        (1.0, RotationField.RIGHT),
        (5.0, None),
        (math.nan, None),
    ],
)
async def test_rotation_field_codes(
    mock_modbus_unit: MockModbusUnit,
    meter: JanitzaMeter,
    code: float,
    expected: RotationField | None,
) -> None:
    mock_modbus_unit.holding[19052] = float_words(code)
    await meter.async_update_readings()
    assert meter.grid.rotation_field is expected


async def test_rotation_field_before_the_first_read(meter: JanitzaMeter) -> None:
    assert meter.grid.rotation_field is None


async def test_energy(meter: JanitzaMeter) -> None:
    await meter.async_update_readings()
    energy = meter.energy
    assert energy.active_energy_total == pytest.approx(2_500_000.0)
    assert energy.active_energy_l3 == pytest.approx(-500_000.0)
    assert energy.active_energy_consumed_total == pytest.approx(3_600_000.0)
    assert energy.active_energy_delivered_total == pytest.approx(1_100_000.0)
    assert energy.apparent_energy_total == pytest.approx(4_000_000.0)
    assert energy.reactive_energy_inductive_total == pytest.approx(350_000.0)
    assert energy.reactive_energy_capacitive_total == pytest.approx(50_000.0)


async def test_a_nan_reads_as_none(meter: JanitzaMeter) -> None:
    """The fixture leaves the per-phase apparent energies unset, as NaN."""
    await meter.async_update_readings()
    assert meter.energy.apparent_energy_l1 is None


async def test_harmonics(meter: JanitzaMeter) -> None:
    await meter.async_update_readings()
    assert meter.harmonics.thd_voltage_l1 == pytest.approx(2.1)
    assert meter.harmonics.thd_current_l3 == pytest.approx(30.0)


async def test_transformers(meter: JanitzaMeter) -> None:
    await meter.async_update_settings()
    assert meter.transformers.ct_primary_l1 == pytest.approx(100.0)
    assert meter.transformers.ct_secondary_l1 == pytest.approx(5.0)
    assert meter.transformers.vt_primary_l3 == pytest.approx(400.0)


async def test_every_seeded_value_decodes(meter: JanitzaMeter) -> None:
    """Guards the field table against an address transcription slip."""
    await meter.async_update_readings()
    decoded: dict[int, float | None] = {}
    for name in MEASUREMENT_COMPONENTS:
        component: Component = getattr(meter, name)
        for field_name, resolved in component.resolved_fields.items():
            decoded[resolved.address] = getattr(component, field_name)
    for address, value in MEASUREMENTS.items():
        assert decoded[address] == pytest.approx(value), address


def test_fields_tile_the_block_without_overlap(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    meter = JanitzaMeter(mock_modbus_unit, serial_number="1")
    addresses: list[int] = []
    for name in MEASUREMENT_COMPONENTS:
        component: Component = getattr(meter, name)
        for resolved in component.resolved_fields.values():
            addresses.extend(range(resolved.address, resolved.address + resolved.count))
    assert sorted(addresses) == list(range(19000, 19122))


def test_report_name() -> None:
    assert JanitzaMeter.report_name("voltage") == "measurements"
    assert JanitzaMeter.report_name("energy") == "measurements"
    assert JanitzaMeter.report_name("transformers") == "transformers"


async def test_listeners_fire_once_the_poll_is_in(meter: JanitzaMeter) -> None:
    seen: list[float | None] = []
    meter.energy.add_update_listener(
        lambda: seen.append(meter.energy.active_energy_total)
    )
    await meter.async_update_readings()
    assert seen == [pytest.approx(2_500_000.0)]


async def test_values_come_back_as_the_meter_meant_them(meter: JanitzaMeter) -> None:
    """A float32 widened to a float carries noise; the decoded value does not."""
    await meter.async_update()
    assert meter.voltage.voltage_l1_n == 230.1
    assert meter.power.cos_phi_l1 == 0.98
    assert meter.harmonics.thd_voltage_l3 == 1.9


@pytest.mark.parametrize(
    "value", [230.1, 0.98, 49.98, 12_345_678.0, 3.4028235e38, 1.0e-30, -0.33]
)
def test_shortest_float32_keeps_the_float32(value: float) -> None:
    words = float_words(value)
    shortest = shortest_float32(struct.unpack(">f", struct.pack(">HH", *words))[0])
    assert float_words(shortest) == words
