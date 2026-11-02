"""Unit tests for `hardware.manager.SensorManager`, the layer the sampler
and API talk to.

Two test doubles, at two different seams:

- `FakeBus` replaces `hardware.manager.ModbusBus` (monkeypatched at module
  scope, the same seam `tests/unit/test_modbus_bus.py` uses for the pymodbus
  client), so scans run against a scriptable set of "present" addresses with
  no serial port involved.
- `tests.mocks.mock_rdo_blue.MockBlueRDOSensor` is injected through the
  manager's own `sensor_factory` hook, so the address -> sensor table is
  built exactly as in production.

The integration tests cover the manager's happy path through the HTTP API;
these cover the scan/mapping logic and the failure translations that the API
never sees.
"""

import asyncio

import pytest

import hardware.manager as manager_module
from config.schema import Config
from constants import (
    MODBUS_MAX_UNIT_ADDRESS,
    SENSOR_READ_ERROR_STATUS_CODE,
    SENSOR_UNREACHABLE_STATUS_CODE,
    SENSOR_UNREACHABLE_STATUS_TEXT,
    UNREADABLE_VALUE,
    BusScanError,
    SensorReadError,
)
from hardware.manager import (
    SensorFactory,
    SensorManager,
    estimate_scan_duration_seconds,
)
from hardware.modbus_bus import ModbusBus
from hardware.rdo_blue_constants import WAKEUP_SETTLE_SECONDS
from models.readings import SensorReading
from tests.mocks.mock_rdo_blue import MockBlueRDOSensor

CONVERTER_A = "/dev/fake-converter-a"
CONVERTER_B = "/dev/fake-converter-b"
SCAN_MIN_ADDRESS = 1
SCAN_MAX_ADDRESS = 4


class FakeBus:
    """Scriptable stand-in for `hardware.modbus_bus.ModbusBus`."""

    def __init__(self, device_path: str, **kwargs: object) -> None:
        self.converter_id = device_path
        self.kwargs = kwargs
        self.wakeup_settle_seconds = 0.0
        self.connected = False
        self.closed = False
        self.connect_error = False
        self.present: set[int] = set()
        self.asleep: set[int] = set()
        """Addresses in `present` that ignore their first (wake-up) probe."""
        self.probes: list[int] = []
        self.probe_delay_seconds = 0.0

    async def connect(self) -> None:
        if self.connect_error:
            raise BusScanError(f"could not open serial device {self.converter_id}")
        self.connected = True

    async def aclose(self) -> None:
        self.connected = False
        self.closed = True

    async def probe_address(self, address: int, timeout_seconds: float | None = None) -> bool:
        if not self.connected:
            raise BusScanError(f"cannot probe {self.converter_id}: bus is not connected")
        if self.probe_delay_seconds:
            await asyncio.sleep(self.probe_delay_seconds)
        self.probes.append(address)
        if address in self.asleep:
            self.asleep.discard(address)
            return False
        return address in self.present


@pytest.fixture
def buses(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeBus]:
    """The `FakeBus` instances `SensorManager.__init__` constructs, keyed by
    device path."""
    created: dict[str, FakeBus] = {}

    def factory(device_path: str, **kwargs: object) -> FakeBus:
        bus = FakeBus(device_path, **kwargs)
        created[device_path] = bus
        return bus

    monkeypatch.setattr(manager_module, "ModbusBus", factory, raising=True)
    return created


@pytest.fixture
def sensors() -> dict[int, MockBlueRDOSensor]:
    """Every mock sensor the factory below has been asked to build."""
    return {}


@pytest.fixture
def factory(sensors: dict[int, MockBlueRDOSensor]) -> SensorFactory:
    def make(bus: ModbusBus, address: int) -> MockBlueRDOSensor:
        sensor = sensors.get(address)
        if sensor is None:
            sensor = MockBlueRDOSensor(address, bus.converter_id, temperature_c=20.0 + address)
            sensors[address] = sensor
        return sensor

    return make


@pytest.fixture
def config() -> Config:
    return Config(
        serial_port_devices=[CONVERTER_A, CONVERTER_B],
        scan_on_startup=False,
        scan_min_address=SCAN_MIN_ADDRESS,
        scan_max_address=SCAN_MAX_ADDRESS,
        scan_probe_timeout_seconds=0.01,
        sensor_read_timeout_seconds=0.5,
    )


@pytest.fixture
def manager(config: Config, buses: dict[str, FakeBus], factory: SensorFactory) -> SensorManager:
    return SensorManager(config, sensor_factory=factory)


# --- lifecycle ------------------------------------------------------------


async def test_constructs_one_bus_per_configured_converter(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    assert sorted(buses) == sorted([CONVERTER_A, CONVERTER_B])
    assert all(not bus.connected for bus in buses.values())


async def test_start_connects_every_bus(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    await manager.start()

    assert all(bus.connected for bus in buses.values())


async def test_start_propagates_a_failed_connection(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    buses[CONVERTER_A].connect_error = True

    with pytest.raises(BusScanError):
        await manager.start()


async def test_start_without_converters_is_not_an_error(factory: SensorFactory) -> None:
    empty = SensorManager(Config(serial_port_devices=[]), sensor_factory=factory)

    await empty.start()
    await empty.aclose()


async def test_aclose_closes_every_bus(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    await manager.start()
    await manager.aclose()

    assert all(bus.closed for bus in buses.values())


async def test_aclose_without_start_is_safe(manager: SensorManager) -> None:
    await manager.aclose()


# --- scanning -------------------------------------------------------------


async def test_scan_reports_present_addresses_per_converter(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    buses[CONVERTER_A].present = {1, 3}
    buses[CONVERTER_B].present = {2}
    await manager.start()

    results = await manager.scan_all_buses()

    by_converter = {result.converter_id: result.sensor_addresses for result in results}
    assert by_converter == {CONVERTER_A: [1, 3], CONVERTER_B: [2]}
    assert all(result.scanned_at > 0 for result in results)


async def test_scan_probes_every_address_in_the_configured_range(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    buses[CONVERTER_A].present = {2}
    await manager.start()

    await manager.scan_all_buses()

    probed_in_order = list(dict.fromkeys(buses[CONVERTER_A].probes))
    assert probed_in_order == list(range(SCAN_MIN_ADDRESS, SCAN_MAX_ADDRESS + 1))


async def test_scan_probes_only_an_explicitly_requested_range(
    manager: SensorManager, buses: dict[str, FakeBus], config: Config
) -> None:
    buses[CONVERTER_A].present = {2, 3, 7}
    await manager.start()

    results = await manager.scan_all_buses(min_address=2, max_address=7)

    probed_in_order = list(dict.fromkeys(buses[CONVERTER_A].probes))
    assert probed_in_order == list(range(2, 8))
    found = {result.converter_id: result.sensor_addresses for result in results}
    assert found[CONVERTER_A] == [2, 3, 7]
    # The manager doesn't persist the range itself; the caller does.
    assert (config.scan_min_address, config.scan_max_address) == (
        SCAN_MIN_ADDRESS,
        SCAN_MAX_ADDRESS,
    )


async def test_scan_with_one_bound_defaults_the_other_from_config(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    await manager.start()

    await manager.scan_all_buses(min_address=3)

    assert list(dict.fromkeys(buses[CONVERTER_A].probes)) == list(range(3, SCAN_MAX_ADDRESS + 1))


@pytest.mark.parametrize(
    ("min_address", "max_address"),
    [(3, 2), (0, 2), (1, MODBUS_MAX_UNIT_ADDRESS + 1)],
)
async def test_scan_rejects_an_invalid_range(
    manager: SensorManager, buses: dict[str, FakeBus], min_address: int, max_address: int
) -> None:
    await manager.start()

    with pytest.raises(BusScanError):
        await manager.scan_all_buses(min_address, max_address)

    assert buses[CONVERTER_A].probes == []


async def test_a_concurrent_scan_of_a_different_range_errors(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    for bus in buses.values():
        bus.probe_delay_seconds = 0.01
    await manager.start()
    task = asyncio.create_task(manager.scan_all_buses())
    await asyncio.sleep(0)

    with pytest.raises(BusScanError, match="already in progress"):
        await manager.scan_all_buses(min_address=2, max_address=3)

    await task


async def test_scan_finds_a_sensor_that_ignores_its_first_wake_up_probe(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    """The vendor doc's wake-up rule means an idle instrument can miss the
    first probe; the second pass is what makes it discoverable."""
    bus = buses[CONVERTER_A]
    bus.present = {3}
    bus.asleep = {3}
    await manager.start()

    results = await manager.scan_all_buses()

    found = {result.converter_id: result.sensor_addresses for result in results}
    assert found[CONVERTER_A] == [3]
    assert bus.probes.count(3) == 2


async def test_scan_does_not_reprobe_an_address_that_already_answered(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    bus = buses[CONVERTER_A]
    bus.present = {1}
    await manager.start()

    await manager.scan_all_buses()

    assert bus.probes.count(1) == 1


async def test_scan_rebuilds_the_sensor_mapping(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    buses[CONVERTER_A].present = {1}
    buses[CONVERTER_B].present = {4}
    await manager.start()

    await manager.scan_all_buses()

    assert manager.get_sensor_mapping() == {CONVERTER_A: [1], CONVERTER_B: [4]}


async def test_scan_makes_the_discovered_sensors_queryable(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    buses[CONVERTER_A].present = {1, 2}
    await manager.start()
    await manager.scan_all_buses()

    readings = await manager.query_all_sensors()

    assert [reading.sensor_address for reading in readings] == [1, 2]
    assert [reading.serial_converter_id for reading in readings] == [CONVERTER_A] * 2


async def test_scan_propagates_bus_scan_error_from_an_unusable_converter(
    manager: SensorManager,
) -> None:
    # start() was never called, so no bus can distinguish absence from a dead port.
    with pytest.raises(BusScanError):
        await manager.scan_all_buses()


async def test_a_concurrent_scan_waits_on_the_one_in_progress(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    """AGENTS.md: scans must not run concurrently. ARCHITECTURE.md §4 chooses
    to have the second caller wait on and share the in-progress result."""
    for bus in buses.values():
        bus.present = {1}
        bus.probe_delay_seconds = 0.01
    await manager.start()

    first, second = await asyncio.gather(manager.scan_all_buses(), manager.scan_all_buses())

    assert [result.sensor_addresses for result in first] == [
        result.sensor_addresses for result in second
    ]
    # Exactly one scan's worth of probes, address by address: each one that
    # didn't answer is re-probed (after its settle wait) before moving on.
    assert buses[CONVERTER_A].probes == [1, 2, 2, 3, 3, 4, 4]


async def test_is_scanning_is_true_only_during_a_scan(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    for bus in buses.values():
        bus.probe_delay_seconds = 0.01
    await manager.start()
    assert manager.is_scanning is False

    task = asyncio.create_task(manager.scan_all_buses())
    await asyncio.sleep(0)
    assert manager.is_scanning is True

    await task
    assert manager.is_scanning is False


async def test_query_all_sensors_errors_while_a_first_scan_is_still_running(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    """AGENTS.md: "API calls made during scanning should return an
    informative error" — there is no mapping to answer from yet."""
    for bus in buses.values():
        bus.probe_delay_seconds = 0.01
    await manager.start()
    task = asyncio.create_task(manager.scan_all_buses())
    await asyncio.sleep(0)

    with pytest.raises(BusScanError):
        await manager.query_all_sensors()

    await task


async def test_aclose_cancels_an_in_progress_scan(
    manager: SensorManager, buses: dict[str, FakeBus]
) -> None:
    for bus in buses.values():
        bus.probe_delay_seconds = 0.05
    await manager.start()
    task = asyncio.create_task(manager.scan_all_buses())
    await asyncio.sleep(0)

    await manager.aclose()

    assert manager.is_scanning is False
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_estimate_scan_duration_covers_two_probes_and_a_settle_per_address(
    config: Config,
) -> None:
    address_count = SCAN_MAX_ADDRESS - SCAN_MIN_ADDRESS + 1
    expected = address_count * (2 * config.scan_probe_timeout_seconds + WAKEUP_SETTLE_SECONDS)

    assert estimate_scan_duration_seconds(config) == pytest.approx(expected)


async def test_estimate_scan_duration_honors_an_explicit_range(config: Config) -> None:
    per_address = 2 * config.scan_probe_timeout_seconds + WAKEUP_SETTLE_SECONDS

    assert estimate_scan_duration_seconds(config, 10, 19) == pytest.approx(10 * per_address)
    assert estimate_scan_duration_seconds(config, max_address=2) == pytest.approx(
        (2 - SCAN_MIN_ADDRESS + 1) * per_address
    )


# --- save_sensor_mapping --------------------------------------------------


async def test_save_sensor_mapping_builds_the_sensor_table(
    manager: SensorManager, sensors: dict[int, MockBlueRDOSensor]
) -> None:
    await manager.save_sensor_mapping({CONVERTER_A: [2, 1], CONVERTER_B: [5]})

    assert manager.get_sensor_mapping() == {CONVERTER_A: [1, 2], CONVERTER_B: [5]}
    assert sorted(sensors) == [1, 2, 5]


async def test_save_sensor_mapping_rejects_an_unconfigured_converter(
    manager: SensorManager,
) -> None:
    with pytest.raises(BusScanError, match="unconfigured converter"):
        await manager.save_sensor_mapping({"/dev/not-configured": [1]})


async def test_save_sensor_mapping_rejects_an_out_of_range_address(
    manager: SensorManager,
) -> None:
    with pytest.raises(BusScanError, match="legal Modbus range"):
        await manager.save_sensor_mapping({CONVERTER_A: [MODBUS_MAX_UNIT_ADDRESS + 1]})


async def test_save_sensor_mapping_keeps_one_sensor_per_duplicated_address(
    manager: SensorManager,
) -> None:
    """Modbus addresses are globally unique by spec; if two converters claim
    one, the first wins rather than the table silently losing a sensor."""
    await manager.save_sensor_mapping({CONVERTER_A: [1], CONVERTER_B: [1]})

    assert manager.get_sensor_mapping() == {CONVERTER_A: [1], CONVERTER_B: []}
    readings = await manager.query_all_sensors()
    assert [reading.sensor_address for reading in readings] == [1]


async def test_save_sensor_mapping_replaces_the_previous_mapping(
    manager: SensorManager,
) -> None:
    await manager.save_sensor_mapping({CONVERTER_A: [1, 2]})

    await manager.save_sensor_mapping({CONVERTER_A: [3]})

    assert manager.get_sensor_mapping() == {CONVERTER_A: [3]}


async def test_get_sensor_mapping_returns_a_copy(manager: SensorManager) -> None:
    await manager.save_sensor_mapping({CONVERTER_A: [1]})

    manager.get_sensor_mapping()[CONVERTER_A].append(99)

    assert manager.get_sensor_mapping() == {CONVERTER_A: [1]}


# --- querying -------------------------------------------------------------


async def test_query_all_sensors_returns_one_reading_per_sensor_in_address_order(
    manager: SensorManager,
) -> None:
    await manager.save_sensor_mapping({CONVERTER_A: [3, 1], CONVERTER_B: [2]})

    readings = await manager.query_all_sensors()

    assert [reading.sensor_address for reading in readings] == [1, 2, 3]
    assert all(reading.row_id is None for reading in readings)
    assert all(reading.status_text == "OK" for reading in readings)


async def test_query_all_sensors_translates_an_unreachable_sensor(
    manager: SensorManager, sensors: dict[int, MockBlueRDOSensor]
) -> None:
    await manager.save_sensor_mapping({CONVERTER_A: [1, 2]})
    sensors[1].set_unreachable(True)

    readings = {reading.sensor_address: reading for reading in await manager.query_all_sensors()}

    assert readings[1].temperature_c == UNREADABLE_VALUE
    assert readings[1].do_mg_l == UNREADABLE_VALUE
    assert readings[1].status_code == SENSOR_UNREACHABLE_STATUS_CODE
    assert readings[1].status_text == SENSOR_UNREACHABLE_STATUS_TEXT
    assert readings[1].serial_converter_id == CONVERTER_A
    assert readings[2].status_text == "OK"


async def test_query_all_sensors_translates_a_read_error(
    manager: SensorManager, sensors: dict[int, MockBlueRDOSensor]
) -> None:
    await manager.save_sensor_mapping({CONVERTER_A: [1]})

    async def raise_read_error() -> SensorReading:
        raise SensorReadError("malformed reply")

    sensors[1].read_all = raise_read_error  # type: ignore[method-assign]

    (reading,) = await manager.query_all_sensors()

    assert reading.temperature_c == UNREADABLE_VALUE
    assert reading.status_code == SENSOR_READ_ERROR_STATUS_CODE
    assert "malformed reply" in reading.status_text


async def test_query_all_sensors_with_an_empty_mapping_returns_nothing(
    manager: SensorManager,
) -> None:
    """An established mapping that found no sensors is not an error — unlike
    a mapping that doesn't exist yet because a scan is still running."""
    await manager.save_sensor_mapping({CONVERTER_A: []})

    assert await manager.query_all_sensors() == []


async def test_query_all_sensors_errors_before_any_mapping_is_established(
    manager: SensorManager,
) -> None:
    """`main.py` starts serving *before* it brings the sensors up, so a query
    landing in that window (or after sensor startup failed outright) must say
    the mapping doesn't exist rather than report zero sensors."""
    with pytest.raises(BusScanError, match="not established"):
        await manager.query_all_sensors()


async def test_aclose_unestablishes_the_mapping(manager: SensorManager) -> None:
    await manager.save_sensor_mapping({CONVERTER_A: [1]})
    await manager.aclose()

    with pytest.raises(BusScanError, match="not established"):
        await manager.query_all_sensors()


async def test_query_sensor_reads_one_sensor(manager: SensorManager) -> None:
    await manager.save_sensor_mapping({CONVERTER_A: [1, 2]})

    reading = await manager.query_sensor(2)

    assert reading.sensor_address == 2
    assert reading.temperature_c == pytest.approx(22.0)


async def test_query_sensor_rejects_an_unmapped_address(manager: SensorManager) -> None:
    await manager.save_sensor_mapping({CONVERTER_A: [1]})

    with pytest.raises(BusScanError, match="no sensor at address"):
        await manager.query_sensor(9)
