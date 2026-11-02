"""High-level sensor array management API.

Owns one `ModbusBus` per configured converter and the resulting
address -> (bus, BlueRDOSensor) table. This is the only module the rest of
the codebase (sampler, API routes) talks to for anything sensor-related.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable

from config.schema import Config
from constants import (
    MODBUS_MAX_UNIT_ADDRESS,
    MODBUS_MIN_UNIT_ADDRESS,
    SENSOR_READ_ERROR_STATUS_CODE,
    SENSOR_UNREACHABLE_STATUS_CODE,
    SENSOR_UNREACHABLE_STATUS_TEXT,
    UNREADABLE_VALUE,
    BusScanError,
    SensorReadError,
    SensorTimeoutError,
)
from hardware.modbus_bus import ModbusBus
from hardware.rdo_blue import BlueRDOSensor
from hardware.rdo_blue_constants import WAKEUP_SETTLE_SECONDS
from hardware.rdo_blue_interface import BlueRDOInterface
from models.readings import ScanResult, SensorReading

logger = logging.getLogger(__name__)

SensorFactory = Callable[[ModbusBus, int], BlueRDOInterface]
"""Builds the `BlueRDOInterface` for one (bus, address) pair."""


def estimate_scan_duration_seconds(
    config: Config, min_address: int | None = None, max_address: int | None = None
) -> float:
    """Worst-case wall-clock seconds for one `scan_all_buses()` call.

    Addresses are scanned one at a time (see `SensorManager._scan_one_bus`):
    an absent address costs a wake-up probe timeout, its own
    `WAKEUP_SETTLE_SECONDS` wait, then a second probe timeout. Buses are
    scanned concurrently, so the estimate does not scale with converter
    count. Used for the `/api/scan` request timeout and for the warning
    logged before a long scan.

    `min_address`/`max_address` override `config.scan_min_address`/
    `config.scan_max_address`, matching `SensorManager.scan_all_buses`.
    """
    low = config.scan_min_address if min_address is None else min_address
    high = config.scan_max_address if max_address is None else max_address
    address_count = max(high - low + 1, 0)
    return address_count * (2 * config.scan_probe_timeout_seconds + WAKEUP_SETTLE_SECONDS)


def _log_orphaned_scan_failure(task: "asyncio.Task[list[ScanResult]]") -> None:
    """Retrieve a finished scan task's exception so a scan whose every awaiter
    was cancelled (e.g. an API client that gave up) doesn't surface later as
    asyncio's "Task exception was never retrieved"."""
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.error("Bus scan failed: %s", exception)


class SensorManager:
    """Owns all `ModbusBus`/`BlueRDOSensor` instances and mediates every
    scan and read against them."""

    def __init__(self, config: Config, sensor_factory: SensorFactory | None = None) -> None:
        """Construct (but do not yet connect) one `ModbusBus` per entry in
        `config.serial_port_devices`. Call `start()` before scanning or
        querying.

        Args:
            config: source of `serial_port_devices`, Modbus serial params,
                and per-request/read/scan timeouts.
            sensor_factory: builds the `BlueRDOInterface` for one
                (bus, address) pair encountered during `scan_all_buses()`/
                `save_sensor_mapping()`; defaults to constructing a real
                `hardware.rdo_blue.BlueRDOSensor`. Tests substitute a
                factory that returns
                `tests.mocks.mock_rdo_blue.MockBlueRDOSensor` instances so
                the sensor table can be built and queried without a real
                `ModbusBus` connection, per AGENTS.md's "integration tests
                for API calls using mocked Blue RDO sensors."
        """
        self._config = config
        self._sensor_factory = sensor_factory or self._build_real_sensor
        self._buses: dict[str, ModbusBus] = {
            device_path: ModbusBus(
                device_path,
                baudrate=config.modbus_baudrate,
                parity=config.modbus_parity,
                stopbits=config.modbus_stopbits,
                bytesize=config.modbus_bytesize,
                request_timeout_seconds=config.modbus_request_timeout_seconds,
            )
            for device_path in config.serial_port_devices
        }
        self._sensors: dict[int, BlueRDOInterface] = {}
        self._mapping: dict[str, list[int]] = {}
        self._mapping_established = False
        self._scan_task: asyncio.Task[list[ScanResult]] | None = None
        self._scan_range: tuple[int, int] | None = None

    @property
    def is_scanning(self) -> bool:
        """`True` while a `scan_all_buses()` call is in progress.

        `query_all_sensors`/`query_sensor` raise `BusScanError` while this
        is `True` and the sensor mapping isn't yet established, per
        AGENTS.md's "API calls made during scanning should return an
        informative error."
        """
        return self._scan_task is not None and not self._scan_task.done()

    async def start(self) -> None:
        """Open every configured `ModbusBus`'s serial connection.

        If `config.scan_on_startup` is set, callers (`main.py`) run
        `scan_all_buses()` immediately after this; otherwise callers apply
        `config.sensor_mapping` directly via `save_sensor_mapping()`.
        """
        if not self._buses:
            logger.warning(
                "No serial_port_devices configured; no sensors will be readable "
                "until config.json lists at least one RS485-to-USB converter"
            )
            return
        logger.info("Opening %d RS485-to-USB converter(s)", len(self._buses))
        for bus in self._buses.values():
            await bus.connect()

    async def aclose(self) -> None:
        """Close every `ModbusBus`'s serial connection. Safe to call even
        if `start()` was never called."""
        scan_task = self._scan_task
        if scan_task is not None and not scan_task.done():
            logger.info("Cancelling in-progress bus scan")
            scan_task.cancel()
            try:
                await scan_task
            except (asyncio.CancelledError, BusScanError):
                pass
            except Exception:
                logger.exception("In-progress bus scan failed during shutdown")
        self._scan_task = None
        self._sensors = {}
        self._mapping_established = False
        for bus in self._buses.values():
            await bus.aclose()

    async def scan_all_buses(
        self, min_address: int | None = None, max_address: int | None = None
    ) -> list[ScanResult]:
        """Probe every configured converter's Modbus address space for
        present sensors, rebuilding the internal sensor table from the
        result.

        Mutual exclusion: if a scan of the same address range is already in
        progress, this call waits on and returns that scan's result rather
        than starting a second concurrent scan (ARCHITECTURE.md §4).

        Args:
            min_address, max_address: inclusive address range to probe;
                each defaults to `config.scan_min_address`/
                `config.scan_max_address`. Not persisted here — callers
                save them to `config.json` if wanted.

        Returns:
            One `ScanResult` per configured converter.

        Raises:
            BusScanError: a converter's serial connection is unusable, the
                range is empty/out of bounds, or a scan of a different range
                is already in progress.
        """
        low = self._config.scan_min_address if min_address is None else min_address
        high = self._config.scan_max_address if max_address is None else max_address
        if not MODBUS_MIN_UNIT_ADDRESS <= low <= high <= MODBUS_MAX_UNIT_ADDRESS:
            raise BusScanError(
                f"invalid scan range {low}-{high}; need "
                f"{MODBUS_MIN_UNIT_ADDRESS} <= min <= max <= {MODBUS_MAX_UNIT_ADDRESS}"
            )

        task = self._scan_task
        # No await between the check and create_task, so two callers landing in
        # the same event-loop iteration cannot both start a scan.
        if task is None or task.done():
            task = asyncio.create_task(self._scan_all_buses(low, high), name="modbus-scan")
            task.add_done_callback(_log_orphaned_scan_failure)
            self._scan_task = task
            self._scan_range = (low, high)
        elif self._scan_range != (low, high):
            assert self._scan_range is not None
            raise BusScanError(
                f"a scan of addresses {self._scan_range[0]}-{self._scan_range[1]} is "
                f"already in progress; retry the {low}-{high} scan once it completes"
            )
        else:
            logger.info("Bus scan already in progress; awaiting its result")
        # Shielded so an API client giving up (504) doesn't cancel a scan other
        # callers — including the startup path — may still be waiting on.
        return await asyncio.shield(task)

    def get_sensor_mapping(self) -> dict[str, list[int]]:
        """Return the current converter_id -> [sensor addresses] mapping
        this manager is using, in `config.sensor_mapping`'s shape."""
        return {converter_id: list(addresses) for converter_id, addresses in self._mapping.items()}

    async def save_sensor_mapping(self, mapping: dict[str, list[int]]) -> None:
        """Rebuild the internal address -> (bus, BlueRDOSensor) table from
        `mapping` (converter_id -> sensor addresses), without probing
        hardware. Used both to apply `config.sensor_mapping` at startup
        when `scan_on_startup` is false, and after `scan_all_buses()`.

        Does not itself persist `mapping` to `config.json`; callers use
        `config.loader.save_config` for that (ARCHITECTURE.md §7).

        Raises:
            BusScanError: `mapping` references a converter_id this manager
                has no `ModbusBus` for, or an address outside the legal
                Modbus range.
        """
        unknown = sorted(set(mapping) - set(self._buses))
        if unknown:
            raise BusScanError(
                f"sensor_mapping references unconfigured converter(s) {unknown}; "
                f"configured serial_port_devices are {sorted(self._buses)}"
            )

        sensors: dict[int, BlueRDOInterface] = {}
        applied: dict[str, list[int]] = {}
        for converter_id in mapping:
            bus = self._buses[converter_id]
            addresses: list[int] = []
            for address in sorted(set(mapping[converter_id])):
                if not MODBUS_MIN_UNIT_ADDRESS <= address <= MODBUS_MAX_UNIT_ADDRESS:
                    raise BusScanError(
                        f"sensor_mapping lists address {address} on {converter_id}, outside "
                        f"the legal Modbus range "
                        f"{MODBUS_MIN_UNIT_ADDRESS}-{MODBUS_MAX_UNIT_ADDRESS}"
                    )
                if address in sensors:
                    logger.warning(
                        "Sensor address %d appears on both %s and %s; Modbus addresses must "
                        "be globally unique, keeping the first and ignoring %s",
                        address,
                        sensors[address].converter_id,
                        converter_id,
                        converter_id,
                    )
                    continue
                sensors[address] = self._sensor_factory(bus, address)
                addresses.append(address)
            applied[converter_id] = addresses

        self._sensors = sensors
        self._mapping = applied
        self._mapping_established = True
        logger.info(
            "Sensor mapping applied: %d sensor(s) across %d converter(s) (%s)",
            len(sensors),
            len(applied),
            applied,
        )

    async def query_all_sensors(self) -> list[SensorReading]:
        """Read every sensor in the current mapping, one `read_all()` per
        sensor. Never raises for an individual sensor's failure — each
        unreachable sensor's `SensorReading` carries
        `constants.UNREADABLE_VALUE` fields and
        `constants.SENSOR_UNREACHABLE_STATUS_CODE`/`_STATUS_TEXT` instead.

        Raises:
            BusScanError: a scan is currently in progress and no mapping
                is established yet.
        """
        self._require_mapping()
        sensors = [self._sensors[address] for address in sorted(self._sensors)]
        # Concurrent by design: each ModbusBus serializes its own port, so
        # per-sensor reads interleave safely at the request level and a slow or
        # unreachable sensor on one converter can't stall another converter.
        readings = await asyncio.gather(*(self._read_one(sensor) for sensor in sensors))
        return list(readings)

    async def query_sensor(self, address: int) -> SensorReading:
        """Read one sensor by its Modbus address.

        Raises:
            BusScanError: a scan is currently in progress and no mapping
                is established yet, or `address` is not in the current
                mapping.
        """
        self._require_mapping()
        sensor = self._sensors.get(address)
        if sensor is None:
            raise BusScanError(
                f"no sensor at address {address}; current mapping covers "
                f"{sorted(self._sensors)}"
            )
        return await self._read_one(sensor)

    # --- internals ----------------------------------------------------------

    def _build_real_sensor(self, bus: ModbusBus, address: int) -> BlueRDOInterface:
        return BlueRDOSensor(bus, address, self._config.sensor_read_timeout_seconds)

    def _require_mapping(self) -> None:
        """Guard every query path: until the mapping exists, callers get an
        informative error instead of a silently empty result (AGENTS.md: "API
        calls made during scanning should return an informative error"). An
        established-but-empty mapping is not an error — a system with no
        sensors found simply has no readings."""
        if self._sensors:
            return
        if self.is_scanning:
            raise BusScanError(
                "bus scan in progress; the sensor mapping is not established yet — "
                "retry once the scan completes"
            )
        if not self._mapping_established:
            # `main.py` starts serving before it brings the sensors up, so
            # this covers both "still opening serial ports / about to scan"
            # and "startup failed and left no mapping behind".
            raise BusScanError(
                "the sensor mapping is not established yet; the backend is still "
                "starting up, or sensor startup failed — check the logs"
            )
        logger.debug("Queried with an empty sensor mapping; returning no readings")

    async def _read_one(self, sensor: BlueRDOInterface) -> SensorReading:
        """`read_all()` for one sensor, with both hardware failure modes
        translated into a `-9999`-filled reading rather than an exception,
        so one dead sensor never fails a whole poll."""
        try:
            return await sensor.read_all()
        except SensorTimeoutError as exc:
            logger.warning(
                "Sensor %d on %s is unreachable: %s", sensor.address, sensor.converter_id, exc
            )
            return self._error_reading(
                sensor, SENSOR_UNREACHABLE_STATUS_CODE, SENSOR_UNREACHABLE_STATUS_TEXT
            )
        except SensorReadError as exc:
            logger.warning(
                "Sensor %d on %s could not be read: %s", sensor.address, sensor.converter_id, exc
            )
            return self._error_reading(sensor, SENSOR_READ_ERROR_STATUS_CODE, str(exc))

    @staticmethod
    def _error_reading(
        sensor: BlueRDOInterface, status_code: int, status_text: str
    ) -> SensorReading:
        return SensorReading(
            sensor_address=sensor.address,
            serial_converter_id=sensor.converter_id,
            timestamp_utc=time.time(),
            temperature_c=UNREADABLE_VALUE,
            do_percent_saturation=UNREADABLE_VALUE,
            do_partial_pressure_torr=UNREADABLE_VALUE,
            do_mg_l=UNREADABLE_VALUE,
            status_code=status_code,
            status_text=status_text,
        )

    async def _scan_all_buses(self, min_address: int, max_address: int) -> list[ScanResult]:
        """The actual scan, run as `self._scan_task` so concurrent callers
        share one in-progress scan."""
        addresses = range(min_address, max_address + 1)
        estimate = estimate_scan_duration_seconds(self._config, min_address, max_address)
        logger.info(
            "Scanning %d converter(s) for Modbus addresses %d-%d (%d addresses each, "
            "%.2fs probe timeout, worst case ~%.0fs)",
            len(self._buses),
            min_address,
            max_address,
            len(addresses),
            self._config.scan_probe_timeout_seconds,
            estimate,
        )
        if estimate > 60.0:
            logger.warning(
                "This scan can take up to ~%.0fs: every absent address costs two full "
                "%.2fs probe timeouts plus a %.2fs wake-up settle. Narrow "
                "scan_min_address/scan_max_address to the addresses actually installed, "
                "or lower scan_probe_timeout_seconds.",
                estimate,
                self._config.scan_probe_timeout_seconds,
                WAKEUP_SETTLE_SECONDS,
            )

        results = await asyncio.gather(
            *(self._scan_one_bus(bus, addresses) for bus in self._buses.values())
        )
        await self.save_sensor_mapping(
            {result.converter_id: result.sensor_addresses for result in results}
        )
        logger.info(
            "Scan complete: %d sensor(s) found across %d converter(s)",
            sum(len(result.sensor_addresses) for result in results),
            len(results),
        )
        return list(results)

    async def _scan_one_bus(self, bus: ModbusBus, addresses: Iterable[int]) -> ScanResult:
        """Probe one converter's address space one address at a time.

        The vendor doc says an idle instrument answers only after a wake-up
        command, so the first probe of an address doubles as that wake-up and
        a non-answer is inconclusive: it is re-probed once after its own
        settle wait before moving on. Each address is finished before the
        next is touched — a bulk wake-up pass followed by a re-probe pass
        overloaded the instruments with back-to-back probes, and on a wide
        range the early-woken addresses had dozed off again before the second
        pass reached them.
        """
        probe_timeout = self._config.scan_probe_timeout_seconds
        logger.info("Scanning %s", bus.converter_id)
        found: list[int] = []

        for address in addresses:
            if await bus.probe_address(address, probe_timeout):
                found.append(address)
                logger.info("Found sensor at address %d on %s", address, bus.converter_id)
                continue
            logger.debug(
                "%s: address %d did not answer the first (wake-up) probe; re-probing "
                "after %.2fs settle",
                bus.converter_id,
                address,
                bus.wakeup_settle_seconds,
            )
            await asyncio.sleep(bus.wakeup_settle_seconds)
            if await bus.probe_address(address, probe_timeout):
                found.append(address)
                logger.info(
                    "Found sensor at address %d on %s (answered after wake-up)",
                    address,
                    bus.converter_id,
                )

        logger.info(
            "Done scanning %s: %s",
            bus.converter_id,
            f"addresses {found}" if found else "no sensors found",
        )
        return ScanResult(
            converter_id=bus.converter_id,
            sensor_addresses=found,
            scanned_at=time.time(),
        )
