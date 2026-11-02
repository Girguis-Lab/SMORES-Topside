"""Async Modbus RTU wrapper for one RS485-to-USB converter.

One `ModbusBus` instance exists per configured `serial_port_devices` entry.
Wraps a pymodbus `AsyncModbusSerialClient`. Because RS485 is half-duplex,
every request on the port is serialized behind an `asyncio.Lock`: a caller
awaits the lock, sends its request, awaits the response (or timeout), then
releases the lock — so a concurrent caller (the sampler loop, an API poll)
can interleave at the request level without corrupting either party's
response, per ARCHITECTURE.md §4.

Two Blue-RDO-specific behaviours live here rather than in
`hardware.rdo_blue`, because both are properties of the *link*:

- **No retries.** The client is constructed with `retries=0`, so a
  non-answering instrument costs exactly one request timeout and surfaces
  as `SensorTimeoutError`, per AGENTS.md's "Do not retry reads".
- **Wake-up handling.** The instrument idles into a low-power state after
  `END_OF_SESSION_TIMEOUT_SECONDS` without traffic, so at any sampling
  interval longer than that every read would otherwise fail. This class
  tracks the last answered command *per Modbus address* and, when that
  address's session has lapsed, sends one throwaway Device Id read as the
  vendor doc's "any Modbus command" wake-up, waits
  `WAKEUP_SETTLE_SECONDS`, then sends the real request exactly once. The
  wake delay is awaited *outside* the bus lock, so unrelated addresses keep
  using the port while an instrument wakes.

`tests/unit/test_modbus_bus.py` monkeypatches the module-scope
`AsyncModbusSerialClient` name below with a fake client, so those tests
never open a real serial port.
"""

import asyncio
import logging
from typing import Final

from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException, ModbusIOException

from constants import BusScanError, SensorReadError, SensorTimeoutError
from hardware.rdo_blue_constants import (
    DEVICE_ID_RDO_BLUE,
    DEVICE_ID_REGISTER,
    DEVICE_ID_REGISTER_COUNT,
    END_OF_SESSION_TIMEOUT_SECONDS,
    SESSION_KEEPALIVE_MARGIN_SECONDS,
    WAKEUP_SETTLE_SECONDS,
)

logger = logging.getLogger(__name__)

MODBUS_RETRIES: Final[int] = 0
"""pymodbus retry count. Zero, per AGENTS.md's "Do not retry reads — simply
return the error value and/or raise an exception to be handled at a higher
level."""

HANG_GUARD_SECONDS: Final[float] = 1.0
"""Slack added on top of the effective request timeout for this module's own
`asyncio.wait_for` guard. pymodbus enforces the request timeout itself and
normally raises first; this guard only fires if the client hangs *past* its
own timeout, which would otherwise stall the bus lock indefinitely."""


class ModbusBus:
    """One serialized Modbus RTU connection over one RS485-to-USB converter."""

    def __init__(
        self,
        device_path: str,
        baudrate: int,
        parity: str,
        stopbits: int,
        bytesize: int,
        request_timeout_seconds: float,
        session_timeout_seconds: float = END_OF_SESSION_TIMEOUT_SECONDS,
        wakeup_settle_seconds: float = WAKEUP_SETTLE_SECONDS,
        session_keepalive_margin_seconds: float = SESSION_KEEPALIVE_MARGIN_SECONDS,
    ) -> None:
        """Construct the bus wrapper. Does not open the serial port yet —
        call `connect()` before issuing any requests.

        Args:
            device_path: stable device identifier, e.g.
                `/dev/serial/by-id/usb-FTDI_...-port0`.
            baudrate, parity, stopbits, bytesize: serial params; must match
                the instruments' configured communication settings.
            request_timeout_seconds: per-request ("serial") timeout applied
                to each individual register read.
            session_timeout_seconds: how long an instrument stays awake
                after answering a command, before this bus wakes it again
                (vendor doc's "end of session timeout").
            wakeup_settle_seconds: how long to wait after a wake-up command
                before the real request.
            session_keepalive_margin_seconds: safety margin subtracted from
                `session_timeout_seconds` when deciding whether an instrument
                is still awake, rather than racing the exact boundary. Tests
                shorten all three of these to keep the suite fast.
        """
        self._device_path = device_path
        self._baudrate = baudrate
        self._parity = parity
        self._stopbits = stopbits
        self._bytesize = bytesize
        self._request_timeout_seconds = request_timeout_seconds
        self._session_timeout_seconds = session_timeout_seconds
        self._wakeup_settle_seconds = wakeup_settle_seconds
        self._session_keepalive_margin_seconds = session_keepalive_margin_seconds

        self._client: AsyncModbusSerialClient | None = None
        self._lock = asyncio.Lock()
        self._wakeup_locks: dict[int, asyncio.Lock] = {}
        self._last_answered: dict[int, float] = {}

    @property
    def converter_id(self) -> str:
        """This bus's stable device identifier (the `device_path` given at
        construction), used as `SensorReading.serial_converter_id`."""
        return self._device_path

    @property
    def wakeup_settle_seconds(self) -> float:
        """How long a caller doing its own bulk wake-up (e.g.
        `hardware.manager`'s scan) must wait between waking an instrument
        and expecting it to answer."""
        return self._wakeup_settle_seconds

    @property
    def is_connected(self) -> bool:
        """`True` once `connect()` has succeeded and `aclose()` hasn't run."""
        return self._client is not None

    async def connect(self) -> None:
        """Open the underlying serial connection.

        Raises:
            BusScanError: the serial device could not be opened (e.g. the
                `/dev/serial/by-id/...` path doesn't exist).
        """
        if self._client is not None:
            logger.debug("%s already connected", self._device_path)
            return

        logger.info(
            "Opening Modbus RTU connection on %s (%d baud, %d%s%d, timeout %.2fs, retries %d)",
            self._device_path,
            self._baudrate,
            self._bytesize,
            self._parity,
            self._stopbits,
            self._request_timeout_seconds,
            MODBUS_RETRIES,
        )
        client = AsyncModbusSerialClient(
            self._device_path,
            baudrate=self._baudrate,
            bytesize=self._bytesize,
            parity=self._parity,
            stopbits=self._stopbits,
            timeout=self._request_timeout_seconds,
            retries=MODBUS_RETRIES,
        )
        try:
            connected = await client.connect()
        except (ModbusException, OSError) as exc:
            raise BusScanError(f"could not open serial device {self._device_path}: {exc}") from exc
        if not connected:
            client.close()
            raise BusScanError(f"could not open serial device {self._device_path}")

        self._client = client
        logger.info("Connected to %s", self._device_path)

    async def aclose(self) -> None:
        """Close the underlying serial connection. Safe to call even if
        `connect()` was never called or already failed."""
        client, self._client = self._client, None
        self._last_answered.clear()
        self._wakeup_locks.clear()
        if client is None:
            return
        logger.info("Closing Modbus RTU connection on %s", self._device_path)
        try:
            client.close()
        except (ModbusException, OSError):
            logger.exception("Error closing serial device %s", self._device_path)

    async def read_holding_registers(self, address: int, count: int, slave: int) -> list[int]:
        """Read `count` contiguous holding registers starting at `address`
        from unit `slave`, serialized behind this bus's lock.

        Wakes `slave` first if its session has lapsed (see the module
        docstring); the real request is still issued exactly once.

        Args:
            address: zero-based holding register address.
            count: number of 16-bit registers to read.
            slave: Modbus unit/slave address of the target sensor.

        Returns:
            The raw register values, most-significant register first.

        Raises:
            SensorTimeoutError: no response within `request_timeout_seconds`.
            SensorReadError: a Modbus exception response was returned, the
                reply was short/malformed, or the bus isn't connected.
        """
        await self._ensure_awake(slave)
        return await self._request_registers(
            address, count, slave, self._request_timeout_seconds
        )

    async def wake_address(self, address: int) -> None:
        """Send one throwaway wake-up command to `address` and return as
        soon as it is answered or times out, *without* waiting the
        instrument's settle time.

        `_ensure_awake` pairs this with the `wakeup_settle_seconds` sleep;
        it is public so a caller can manage that settle itself. A wake-up
        going unanswered is normal
        and not an error — the vendor doc's whole point is that the
        instrument may not answer until it has woken up.
        """
        try:
            await self._request_registers(
                DEVICE_ID_REGISTER,
                DEVICE_ID_REGISTER_COUNT,
                address,
                self._request_timeout_seconds,
            )
        except (SensorTimeoutError, SensorReadError):
            logger.debug(
                "Wake-up command to address %d on %s went unanswered (expected while "
                "the instrument wakes)",
                address,
                self._device_path,
            )

    async def probe_address(self, address: int, timeout_seconds: float | None = None) -> bool:
        """Check whether a sensor is present at Modbus unit `address` on
        this bus, used by `hardware.manager.scan_all_buses`.

        Does *not* wake `address` first: `hardware.manager`'s scan treats
        the first probe as the wake-up and, only if it goes unanswered,
        waits `wakeup_settle_seconds` and re-probes, so a present, awake
        sensor costs a single round trip.

        Args:
            address: Modbus unit/slave address to probe.
            timeout_seconds: overrides `request_timeout_seconds` for this
                probe only (scanning typically uses a shorter timeout, per
                `config.scan_probe_timeout_seconds`).

        Returns:
            `True` if a well-formed response was received. `False` on a
            timeout/no-response, and also on a Modbus exception or malformed
            reply: absence is not an error during a scan, and something that
            can't answer a Device Id read isn't a sensor this system can use.

        Raises:
            BusScanError: this bus isn't connected, so absence can't be
                distinguished from a dead port.
        """
        if self._client is None:
            raise BusScanError(
                f"cannot probe {self._device_path}: bus is not connected (call connect() first)"
            )
        timeout = (
            self._request_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        try:
            registers = await self._request_registers(
                DEVICE_ID_REGISTER, DEVICE_ID_REGISTER_COUNT, address, timeout
            )
        except SensorTimeoutError:
            logger.debug("No response from address %d on %s", address, self._device_path)
            return False
        except SensorReadError as exc:
            logger.debug(
                "Address %d on %s answered but could not be read (%s); treating as absent",
                address,
                self._device_path,
                exc,
            )
            return False

        device_id = registers[0]
        if device_id != DEVICE_ID_RDO_BLUE:
            logger.warning(
                "Address %d on %s reports Device Id %d, expected %d (RDO Blue); "
                "treating it as present anyway",
                address,
                self._device_path,
                device_id,
                DEVICE_ID_RDO_BLUE,
            )
        return True

    # --- internals ----------------------------------------------------------

    async def _request_registers(
        self, address: int, count: int, slave: int, timeout_seconds: float
    ) -> list[int]:
        """One register read, serialized behind the bus lock, with every
        pymodbus failure mode translated into this project's exceptions."""
        client = self._client
        if client is None:
            raise SensorReadError(
                f"cannot read {self._device_path}: bus is not connected (call connect() first)"
            )

        async with self._lock:
            try:
                response = await asyncio.wait_for(
                    client.read_holding_registers(address, count=count, device_id=slave),
                    timeout=timeout_seconds + HANG_GUARD_SECONDS,
                )
            except TimeoutError as exc:
                raise SensorTimeoutError(
                    f"{self._device_path} address {slave}: client hung past "
                    f"{timeout_seconds:.2f}s reading {count} register(s) at {address}"
                ) from exc
            except ModbusIOException as exc:
                # pymodbus also converts a cancellation of its own request into a
                # ModbusIOException ("Request cancelled outside library"), so a
                # shutdown or an outer wait_for landing mid-request must not be
                # mistaken for a sensor timeout — that would swallow the cancel and
                # leave the task running.
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise asyncio.CancelledError from exc
                # Otherwise: "no response within timeout" (and framing/CRC failures)
                # are reported as an IO exception; with retries=0 that is one timeout.
                raise SensorTimeoutError(
                    f"{self._device_path} address {slave}: no valid response reading "
                    f"{count} register(s) at {address}: {exc}"
                ) from exc
            except ModbusException as exc:
                raise SensorReadError(
                    f"{self._device_path} address {slave}: Modbus error reading "
                    f"{count} register(s) at {address}: {exc}"
                ) from exc

            if response.isError():
                raise SensorReadError(
                    f"{self._device_path} address {slave}: Modbus exception response "
                    f"reading {count} register(s) at {address}: {response}"
                )

            registers = list(response.registers)
            if len(registers) != count:
                raise SensorReadError(
                    f"{self._device_path} address {slave}: expected {count} register(s) "
                    f"at {address}, got {len(registers)}"
                )

            # Only a *successful* round trip proves the instrument is awake; after
            # a failure the session state is unknown, so the next read wakes again.
            self._last_answered[slave] = asyncio.get_running_loop().time()

        logger.debug(
            "%s address %d: read %d register(s) at %d -> %s",
            self._device_path,
            slave,
            count,
            address,
            registers,
        )
        return registers

    def _is_awake(self, slave: int) -> bool:
        last_answered = self._last_answered.get(slave)
        if last_answered is None:
            return False
        elapsed = asyncio.get_running_loop().time() - last_answered
        awake_window = self._session_timeout_seconds - self._session_keepalive_margin_seconds
        return elapsed < max(awake_window, 0.0)

    async def _ensure_awake(self, slave: int) -> None:
        """Wake `slave` and wait out its settle time if its session lapsed.

        The per-address lock means concurrent readers of the same instrument
        wake it once between them; the settle sleep is deliberately held
        outside the *bus* lock so other addresses keep using the port.
        """
        if self._is_awake(slave):
            return
        lock = self._wakeup_locks.setdefault(slave, asyncio.Lock())
        async with lock:
            if self._is_awake(slave):
                return
            logger.debug(
                "Waking address %d on %s (session lapsed)", slave, self._device_path
            )
            await self.wake_address(slave)
            await asyncio.sleep(self._wakeup_settle_seconds)
            # Settled: treat the instrument as awake from now even if the wake-up
            # itself went unanswered, which the vendor doc says to expect.
            self._last_answered[slave] = asyncio.get_running_loop().time()
