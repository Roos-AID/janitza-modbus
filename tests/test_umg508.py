"""The UMG 508: the same measurement block as the UMG 96RM, nothing else."""

from __future__ import annotations

import pytest
from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusTimeoutError,
)
from modbus_connection.mock import MockModbusUnit
from modbus_connection.model import Component

from janitza_modbus import (
    MEASUREMENT_COMPONENTS,
    JanitzaDevice,
    JanitzaMeter,
    RotationField,
    Umg508,
)

from .conftest import MEASUREMENTS, UMG508_FIXTURE_SERIAL, seed_measurements


def test_it_is_a_janitza_device_but_not_a_96rm() -> None:
    assert issubclass(Umg508, JanitzaDevice)
    assert not issubclass(Umg508, JanitzaMeter)
    assert Umg508.model_name == "UMG 508"
    assert not Umg508.has_serial_number
    assert JanitzaMeter.has_serial_number


def test_what_it_polls(meter508: Umg508) -> None:
    assert tuple(meter508.readings_components) == ("measurements", "tariff_energy")
    assert tuple(meter508.settings_components) == ("transformers",)
    assert meter508.software_release is None  # not read yet
    assert meter508.reported_serial_number is None


async def test_constructor_reads_nothing(mock_modbus_unit: MockModbusUnit) -> None:
    Umg508(mock_modbus_unit, serial_number="x")
    assert mock_modbus_unit.read_events == []


async def test_timeout_is_asked_of_the_unit(mock_modbus_unit: MockModbusUnit) -> None:
    Umg508(mock_modbus_unit, serial_number="x", timeout=2.5)
    assert mock_modbus_unit.required_timeout == 2.5


async def test_detect_proves_the_measurement_block(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    seed_measurements(mock_modbus_unit)
    meter = await Umg508.async_detect(mock_modbus_unit, serial_number="id")
    assert meter.serial_number == "id"
    assert not meter.unrecognized
    assert meter.voltage.voltage_l1_n == pytest.approx(230.1)
    assert {event.address for event in mock_modbus_unit.read_events} == {
        19000,
        19054,
    }


async def test_a_device_refusing_the_block_is_unrecognized(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    seed_measurements(mock_modbus_unit)
    mock_modbus_unit.fail_read(19000, IllegalDataAddressError())
    mock_modbus_unit.fail_read(19054, IllegalDataAddressError())
    meter = await Umg508.async_detect(mock_modbus_unit)
    assert meter.unrecognized


async def test_detect_raises_on_a_dead_link(mock_modbus_unit: MockModbusUnit) -> None:
    mock_modbus_unit.fail_requests(ModbusConnectionError())
    with pytest.raises(ModbusConnectionError):
        await Umg508.async_detect(mock_modbus_unit)


async def test_detect_raises_on_a_silent_meter(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    mock_modbus_unit.fail_requests(ModbusTimeoutError())
    with pytest.raises(ModbusTimeoutError):
        await Umg508.async_detect(mock_modbus_unit)


async def test_basic_values(meter508: Umg508) -> None:
    report = await meter508.async_update_readings()
    assert "measurements" in report.updated
    assert meter508.voltage.voltage_l2_n == pytest.approx(231.2)
    assert meter508.current.current_l2 == pytest.approx(4.5)
    assert meter508.power.active_power_total == pytest.approx(1550.0)
    assert meter508.power.active_power_l3 == pytest.approx(-150.0)
    assert meter508.grid.frequency == pytest.approx(49.98)
    assert meter508.grid.rotation_field is RotationField.RIGHT
    assert meter508.energy.active_energy_consumed_total == pytest.approx(3_600_000.0)
    assert meter508.energy.active_energy_delivered_total == pytest.approx(1_100_000.0)


async def test_a_real_meter(real508: Umg508) -> None:
    """Every block decodes to what the real meter showed on its display."""
    report = await real508.async_update()
    assert report.updated == {"measurements", "tariff_energy", "transformers"}
    assert not report.failed
    tariff = real508.tariff_energy
    # Only tariff 1 runs on this meter, so the sums equal the 19000 block's.
    assert tariff.active_energy_consumed_all_tariffs == pytest.approx(11985.8, abs=1)
    assert tariff.active_energy_delivered_all_tariffs == pytest.approx(14260.06)
    assert real508.energy.active_energy_delivered_total == pytest.approx(14260.06)
    transformers = real508.transformers
    assert transformers.ct_primary_l1 == 35.0
    assert transformers.ct_secondary_l3 == 1.0
    assert transformers.vt_primary_l2 == 400.0
    assert transformers.vt_secondary_l1 == 400.0


async def test_a_real_meters_identity(real508: Umg508) -> None:
    assert await real508.async_read_identity()
    assert real508.software_release == "5.030"
    assert real508.reported_serial_number == str(UMG508_FIXTURE_SERIAL)
    assert real508.serial_number == "192.0.2.8:502:1"  # the caller's name stays


async def test_identity_retries_then_warns(
    mock_modbus_unit: MockModbusUnit,
    real508: Umg508,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_modbus_unit.fail_read(19900, IllegalDataAddressError())
    assert not await real508.async_read_identity()
    assert real508.software_release is None
    assert "could not read its identity" in caplog.text


async def test_a_refused_tariff_block_keeps_the_measurements(
    mock_modbus_unit: MockModbusUnit, real508: Umg508
) -> None:
    mock_modbus_unit.fail_read(9929, IllegalDataAddressError())
    report = await real508.async_update_readings()
    assert report.updated == {"measurements"}
    assert "tariff_energy" in report.failed


def test_blocks_start_where_a_real_meter_answers() -> None:
    """A UMG 508 returns junk for a read starting mid-variable: pin the starts."""
    from janitza_modbus import TariffEnergy, Umg508Identity, Umg508Transformers

    assert TariffEnergy.register_ranges == ((9929, 9944),)
    assert Umg508Transformers.register_ranges == ((10131, 10162),)
    assert Umg508Identity.register_ranges == ((19900, 19917),)


async def test_every_seeded_value_decodes(meter508: Umg508) -> None:
    """Guards the field table against an address transcription slip."""
    await meter508.async_update_readings()
    decoded: dict[int, float | None] = {}
    for name in MEASUREMENT_COMPONENTS:
        component: Component = getattr(meter508, name)
        for field_name, resolved in component.resolved_fields.items():
            decoded[resolved.address] = getattr(component, field_name)
    for address, value in MEASUREMENTS.items():
        assert decoded[address] == pytest.approx(value), address


async def test_a_refused_block_keeps_previous_values(
    mock_modbus_unit: MockModbusUnit, meter508: Umg508
) -> None:
    await meter508.async_update()
    mock_modbus_unit.fail_read(19054, IllegalDataAddressError())
    report = await meter508.async_update()
    assert "measurements" in report.failed
    assert meter508.energy.active_energy_total == pytest.approx(2_500_000.0)


async def test_raw_dump_covers_every_block(real508: Umg508) -> None:
    raw = await real508.async_read_raw(redact_serial=True)
    addresses = set(raw["holding"])
    for address in (9929, 10131, 19900, 19000, 19121):
        assert address in addresses
    assert 19916 not in addresses
    assert 19917 not in addresses


def test_report_name() -> None:
    assert Umg508.report_name("energy") == "measurements"


async def test_reads_start_where_the_real_meter_answered(
    mock_modbus_unit: MockModbusUnit, real508: Umg508
) -> None:
    await real508.async_update()
    await real508.async_read_identity()
    starts = {event.address for event in mock_modbus_unit.read_events}
    assert starts == {19000, 19054, 9929, 10131, 19900}
