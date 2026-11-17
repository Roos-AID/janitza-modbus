"""How many requests a poll costs, and how wide each one is."""

from __future__ import annotations

from modbus_connection.mock import MockModbusUnit, ReadEvent

from janitza_modbus import JanitzaMeter

from .conftest import seed_meter


async def test_readings_take_two_requests(
    mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter
) -> None:
    await meter.async_update_readings()
    assert mock_modbus_unit.read_events == [
        ReadEvent("holding", 19000, 54),
        ReadEvent("holding", 19054, 68),
    ]


async def test_settings_take_one_request(
    mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter
) -> None:
    await meter.async_update_settings()
    assert mock_modbus_unit.read_events == [ReadEvent("holding", 10, 24)]


async def test_identity_reads_only_documented_addresses(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    seed_meter(mock_modbus_unit)
    await JanitzaMeter.async_detect(mock_modbus_unit)
    assert mock_modbus_unit.read_events == [
        ReadEvent("holding", 750, 1),
        ReadEvent("holding", 754, 4),
        ReadEvent("holding", 761, 1),
    ]


async def test_identity_is_in_neither_poll(
    mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter
) -> None:
    await meter.async_update()
    assert all(event.address >= 10 for event in mock_modbus_unit.read_events)
    assert not any(750 <= e.address <= 761 for e in mock_modbus_unit.read_events)
    assert meter.identity.serial_number is None


async def test_every_read_is_fc03(
    mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter
) -> None:
    await meter.async_read_identity()
    await meter.async_update()
    assert {event.register_type for event in mock_modbus_unit.read_events} == {
        "holding"
    }
