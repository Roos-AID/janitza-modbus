"""What a poll does when the meter, or the link to it, misbehaves."""

from __future__ import annotations

import logging

import pytest
from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusTimeoutError,
)
from modbus_connection.mock import MockModbusConnection, MockModbusUnit

from janitza_modbus import JanitzaMeter

from .conftest import SERIAL


async def test_a_refused_block_keeps_the_rest(
    mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter
) -> None:
    mock_modbus_unit.fail_read(10, IllegalDataAddressError())
    report = await meter.async_update()
    assert report.updated == {"measurements"}
    assert isinstance(report.failed["transformers"], IllegalDataAddressError)
    assert meter.voltage.voltage_l1_n is not None


async def test_a_failed_poll_keeps_previous_values(
    mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter
) -> None:
    await meter.async_update()
    mock_modbus_unit.fail_read(19054, IllegalDataAddressError())
    report = await meter.async_update()
    assert "measurements" in report.failed
    assert meter.energy.active_energy_total == pytest.approx(2_500_000.0)


async def test_a_silent_meter_raises_a_timeout(
    mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter
) -> None:
    mock_modbus_unit.fail_requests(ModbusTimeoutError())
    with pytest.raises(ModbusTimeoutError):
        await meter.async_update()
    # The settings are not tried once the first poll found nobody home.
    assert len(mock_modbus_unit.read_events) == 1


async def test_a_dead_link_raises(
    mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter
) -> None:
    mock_modbus_unit.fail_requests(ModbusConnectionError())
    with pytest.raises(ModbusConnectionError):
        await meter.async_update_readings()


async def test_identity_retries_then_warns(
    mock_modbus_unit: MockModbusUnit,
    meter: JanitzaMeter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_modbus_unit.fail_read(754, IllegalDataAddressError())
    with caplog.at_level(logging.WARNING, logger="janitza_modbus"):
        assert not await meter.async_read_identity()
    assert len(mock_modbus_unit.read_events) == 3 * 2  # 750, then 754 refuses
    assert f"Meter {SERIAL}: could not read identity" in caplog.text


async def test_identity_reads_after_a_blip(
    mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter
) -> None:
    attempts = 0
    read = mock_modbus_unit.read_holding_registers

    async def blip_once(address: int, count: int) -> list[int]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModbusTimeoutError()
        return await read(address, count)

    mock_modbus_unit.read_holding_registers = blip_once  # type: ignore[method-assign]
    assert await meter.async_read_identity()
    assert meter.identity.serial_number == SERIAL


async def test_raw_dump(mock_modbus_unit: MockModbusUnit, meter: JanitzaMeter) -> None:
    raw = await meter.async_read_raw()
    holding = raw["holding"]
    assert set(holding) == {
        750,
        754,
        755,
        756,
        757,
        761,
        *range(10, 34),
        *range(19000, 19122),
    }
    assert list(holding) == sorted(holding)


async def test_raw_dump_can_leave_out_what_identifies_the_meter(
    meter: JanitzaMeter,
) -> None:
    raw = await meter.async_read_raw(redact_serial=True)
    for address in (754, 755, 756, 757):
        assert address not in raw["holding"]
    assert 750 in raw["holding"]


async def test_raw_dump_fires_no_listeners(meter: JanitzaMeter) -> None:
    fired: list[str] = []
    meter.voltage.add_update_listener(lambda: fired.append("voltage"))
    await meter.async_read_raw()
    assert fired == []
    assert meter.voltage.voltage_l1_n is not None


async def test_a_raw_dump_replays_into_the_mock(meter: JanitzaMeter) -> None:
    raw = await meter.async_read_raw()
    replay = MockModbusConnection().for_unit(1)
    # The dump is keyed by int; load_raw also takes the str keys JSON brings.
    snapshot: dict[str, dict[int | str, int | bool]] = {
        space: {address: value for address, value in values.items()}
        for space, values in raw.items()
    }
    replay.load_raw(snapshot)
    copy = JanitzaMeter(replay, serial_number="x")
    await copy.async_update()
    assert copy.power.active_power_total == meter.power.active_power_total
