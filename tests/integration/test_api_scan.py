"""Integration tests for `GET /api/scan`: a real aiohttp app and a real
`config.json` under the test data dir. The integration `SensorManager`
never opens its serial ports, so `scan_all_buses` is replaced with a fake
that records the requested range and applies a canned mapping — the
manager's actual probing logic is covered by `tests/unit/test_manager.py`.
"""

import time
from typing import Any

import pytest
from aiohttp import web
from aiohttp.pytest_plugin import AiohttpClient

from config.loader import get_config_path, load_config
from config.schema import Config
from constants import BusScanError
from hardware.manager import SensorManager
from models.readings import ScanResult
from tests.conftest import CONVERTER_0, CONVERTER_1

FOUND_MAPPING = {CONVERTER_0: [5], CONVERTER_1: [6, 7]}


@pytest.fixture
def scan_calls(sensor_manager: SensorManager) -> list[tuple[Any, Any]]:
    """Replace `sensor_manager.scan_all_buses` with a fake that records each
    call's (min_address, max_address) and "finds" `FOUND_MAPPING`."""
    calls: list[tuple[Any, Any]] = []

    async def fake_scan(
        min_address: int | None = None, max_address: int | None = None
    ) -> list[ScanResult]:
        calls.append((min_address, max_address))
        sensor_manager._mapping = {k: list(v) for k, v in FOUND_MAPPING.items()}
        return [
            ScanResult(converter_id=converter, sensor_addresses=addresses, scanned_at=time.time())
            for converter, addresses in FOUND_MAPPING.items()
        ]

    sensor_manager.scan_all_buses = fake_scan  # type: ignore[method-assign]
    return calls


async def test_scan_without_params_uses_and_keeps_the_saved_range(
    aiohttp_client: AiohttpClient,
    app: web.Application,
    test_config: Config,
    scan_calls: list[tuple[Any, Any]],
) -> None:
    client = await aiohttp_client(app)

    resp = await client.get("/api/scan")

    assert resp.status == 200
    assert scan_calls == [(test_config.scan_min_address, test_config.scan_max_address)]
    saved = load_config(get_config_path())
    assert saved.sensor_mapping == FOUND_MAPPING
    assert (saved.scan_min_address, saved.scan_max_address) == (
        test_config.scan_min_address,
        test_config.scan_max_address,
    )


async def test_scan_with_both_bounds_scans_and_saves_that_range(
    aiohttp_client: AiohttpClient,
    app: web.Application,
    test_config: Config,
    scan_calls: list[tuple[Any, Any]],
) -> None:
    client = await aiohttp_client(app)

    resp = await client.get("/api/scan", params={"start_address": "5", "stop_address": "12"})

    assert resp.status == 200
    body = await resp.json()
    assert {row["converter_id"]: row["sensor_addresses"] for row in body} == FOUND_MAPPING
    assert scan_calls == [(5, 12)]

    saved = load_config(get_config_path())
    assert (saved.scan_min_address, saved.scan_max_address) == (5, 12)
    assert saved.sensor_mapping == FOUND_MAPPING
    assert (test_config.scan_min_address, test_config.scan_max_address) == (5, 12)


async def test_scan_with_one_bound_saves_it_and_keeps_the_other(
    aiohttp_client: AiohttpClient,
    app: web.Application,
    test_config: Config,
    scan_calls: list[tuple[Any, Any]],
) -> None:
    original_min = test_config.scan_min_address
    client = await aiohttp_client(app)

    resp = await client.get("/api/scan", params={"stop_address": "30"})

    assert resp.status == 200
    assert scan_calls == [(original_min, 30)]
    saved = load_config(get_config_path())
    assert (saved.scan_min_address, saved.scan_max_address) == (original_min, 30)


@pytest.mark.parametrize(
    "params",
    [
        {"start_address": "10", "stop_address": "9"},
        {"start_address": "0"},
        {"stop_address": "248"},
        {"start_address": "abc"},
    ],
)
async def test_scan_with_an_invalid_range_returns_400_and_saves_nothing(
    aiohttp_client: AiohttpClient,
    app: web.Application,
    scan_calls: list[tuple[Any, Any]],
    params: dict[str, str],
) -> None:
    client = await aiohttp_client(app)

    resp = await client.get("/api/scan", params=params)

    assert resp.status == 400
    assert "error" in await resp.json()
    assert scan_calls == []
    assert not get_config_path().exists()


async def test_a_failed_scan_returns_503_and_saves_nothing(
    aiohttp_client: AiohttpClient, app: web.Application, sensor_manager: SensorManager
) -> None:
    async def failing_scan(
        min_address: int | None = None, max_address: int | None = None
    ) -> list[ScanResult]:
        raise BusScanError("a scan of a different range is already in progress")

    sensor_manager.scan_all_buses = failing_scan  # type: ignore[method-assign]
    client = await aiohttp_client(app)

    resp = await client.get("/api/scan", params={"start_address": "2", "stop_address": "3"})

    assert resp.status == 503
    assert not get_config_path().exists()
