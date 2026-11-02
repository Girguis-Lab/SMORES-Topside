"""Integration tests for `GET /api/sensors/current`: a real aiohttp app
(via pytest-aiohttp's `aiohttp_client`) wired to `SensorManager` with
`tests.mocks.mock_rdo_blue.MockBlueRDOSensor`s standing in for hardware —
no real Modbus/serial I/O, per AGENTS.md's testing notes.
"""

import asyncio
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.pytest_plugin import AiohttpClient

from api.app import create_app
from config.schema import Config
from constants import (
    SENSOR_UNREACHABLE_STATUS_CODE,
    SENSOR_UNREACHABLE_STATUS_TEXT,
    UNREADABLE_VALUE,
    BusScanError,
)
from db.database import Database
from hardware.manager import SensorFactory, SensorManager
from models.readings import SensorReading
from tests.mocks.mock_rdo_blue import MockBlueRDOSensor


async def test_returns_a_reading_per_configured_sensor(
    aiohttp_client: AiohttpClient, app: web.Application, mock_sensors: dict[int, MockBlueRDOSensor]
) -> None:
    client = await aiohttp_client(app)

    resp = await client.get("/api/sensors/current")

    assert resp.status == 200
    body = await resp.json()
    assert len(body) == len(mock_sensors)


async def test_readings_match_configured_mock_values(
    aiohttp_client: AiohttpClient, app: web.Application, mock_sensors: dict[int, MockBlueRDOSensor]
) -> None:
    client = await aiohttp_client(app)

    resp = await client.get("/api/sensors/current")
    body = await resp.json()

    by_address = {row["sensor_address"]: row for row in body}
    for address, sensor in mock_sensors.items():
        row = by_address[address]
        assert row["row_id"] is None
        assert row["serial_converter_id"] == sensor.converter_id
        assert row["temperature_c"] == pytest.approx(20.0 + address)
        assert row["do_percent_saturation"] == pytest.approx(90.0 + address)
        assert row["status_code"] == 0
        assert row["status_text"] == "OK"


async def test_does_not_write_a_db_row(
    aiohttp_client: AiohttpClient, app: web.Application, database: Database
) -> None:
    client = await aiohttp_client(app)

    await client.get("/api/sensors/current")

    assert await database.count_rows() == 0


@pytest.mark.parametrize("value", ["false", "0"])
async def test_save_false_does_not_write_a_db_row(
    aiohttp_client: AiohttpClient, app: web.Application, database: Database, value: str
) -> None:
    client = await aiohttp_client(app)

    resp = await client.get("/api/sensors/current", params={"save": value})

    assert resp.status == 200
    assert await database.count_rows() == 0


async def test_save_true_writes_one_row_per_sensor_and_returns_row_ids(
    aiohttp_client: AiohttpClient,
    app: web.Application,
    database: Database,
    mock_sensors: dict[int, MockBlueRDOSensor],
) -> None:
    client = await aiohttp_client(app)

    resp = await client.get("/api/sensors/current", params={"save": "true"})

    assert resp.status == 200
    body = await resp.json()
    assert len(body) == len(mock_sensors)
    assert all(row["row_id"] is not None for row in body)

    stored = await database.get_readings()
    assert {reading.row_id for reading in stored} == {row["row_id"] for row in body}
    by_address = {reading.sensor_address: reading for reading in stored}
    for row in body:
        saved = by_address[row["sensor_address"]]
        assert saved.temperature_c == pytest.approx(row["temperature_c"])
        assert saved.timestamp_utc == pytest.approx(row["timestamp_utc"])


async def test_save_with_an_invalid_value_returns_400(
    aiohttp_client: AiohttpClient, app: web.Application, database: Database
) -> None:
    client = await aiohttp_client(app)

    resp = await client.get("/api/sensors/current", params={"save": "maybe"})

    assert resp.status == 400
    assert "error" in await resp.json()
    assert await database.count_rows() == 0


async def test_unreachable_sensor_reports_unreadable_value_and_timeout_status(
    aiohttp_client: AiohttpClient, app: web.Application, mock_sensors: dict[int, MockBlueRDOSensor]
) -> None:
    mock_sensors[2].set_unreachable(True)
    client = await aiohttp_client(app)

    resp = await client.get("/api/sensors/current")

    assert resp.status == 200
    body = await resp.json()
    by_address = {row["sensor_address"]: row for row in body}

    unreachable = by_address[2]
    assert unreachable["temperature_c"] == UNREADABLE_VALUE
    assert unreachable["do_percent_saturation"] == UNREADABLE_VALUE
    assert unreachable["do_partial_pressure_torr"] == UNREADABLE_VALUE
    assert unreachable["do_mg_l"] == UNREADABLE_VALUE
    assert unreachable["status_code"] == SENSOR_UNREACHABLE_STATUS_CODE
    assert unreachable["status_text"] == SENSOR_UNREACHABLE_STATUS_TEXT

    still_healthy = by_address[1]
    assert still_healthy["status_text"] == "OK"


async def test_returns_503_when_a_scan_is_in_progress(
    aiohttp_client: AiohttpClient, app: web.Application, sensor_manager: SensorManager
) -> None:
    async def raise_scan_error() -> list[SensorReading]:
        raise BusScanError("scan in progress")

    sensor_manager.query_all_sensors = raise_scan_error  # type: ignore[method-assign]
    client = await aiohttp_client(app)

    resp = await client.get("/api/sensors/current")

    assert resp.status == 503
    body = await resp.json()
    assert "error" in body


async def test_returns_504_when_the_poll_exceeds_poll_timeout_seconds(
    aiohttp_client: AiohttpClient,
    sensor_factory: SensorFactory,
    database: Database,
    data_dir: Path,
) -> None:
    config = Config(
        serial_port_devices=["/dev/mock-bus0"],
        sensor_mapping={"/dev/mock-bus0": [1]},
        scan_on_startup=False,
        poll_timeout_seconds=0.05,
    )
    manager = SensorManager(config, sensor_factory=sensor_factory)
    await manager.save_sensor_mapping(config.sensor_mapping)

    async def slow_query_all_sensors() -> list[SensorReading]:
        await asyncio.sleep(1.0)
        return []

    manager.query_all_sensors = slow_query_all_sensors  # type: ignore[method-assign]

    slow_app = create_app(config, manager, database, data_dir)
    client = await aiohttp_client(slow_app)

    resp = await client.get("/api/sensors/current")

    assert resp.status == 504
    await manager.aclose()
