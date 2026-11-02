"""Real, Modbus-backed implementation of `BlueRDOInterface`.

Register decoding only — every byte of transport concern (the bus lock, the
per-request timeout, retries, instrument wake-up) belongs to `ModbusBus`.
All addresses and offsets come from `hardware.rdo_blue_constants`, so a
vendor-doc correction is a one-line patch there rather than a change here.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Final, Literal

from pymodbus.client.mixin import ModbusClientMixin

from constants import (
    PARAMETER_TIMEOUT_STATUS_CODE,
    SENSOR_READ_ERROR_STATUS_CODE,
    UNREADABLE_VALUE,
    SensorReadError,
    SensorTimeoutError,
)
from hardware.modbus_bus import ModbusBus
from hardware.rdo_blue_constants import (
    DATA_QUALITY_OK,
    DEVICE_ID_REGISTER,
    DEVICE_ID_REGISTER_COUNT,
    DO_CONCENTRATION_MG_L_PARAMETER_REGISTER,
    DO_PARTIAL_PRESSURE_TORR_PARAMETER_REGISTER,
    DO_PERCENT_SATURATION_PARAMETER_REGISTER,
    MULTI_REGISTER_WORD_ORDER_BIG_ENDIAN,
    PARAMETER_DATA_QUALITY_OFFSET,
    PARAMETER_EXPECTED_UNITS_ID,
    PARAMETER_NAMES,
    PARAMETER_READ_REGISTER_COUNT,
    PARAMETER_UNITS_ID_OFFSET,
    SERIAL_NUMBER_REGISTER,
    SERIAL_NUMBER_REGISTER_COUNT,
    TEMPERATURE_PARAMETER_REGISTER,
    UNITS_TEXT,
    data_quality_text,
)
from hardware.rdo_blue_interface import BlueRDOInterface
from models.readings import SensorReading

logger = logging.getLogger(__name__)

TEMPERATURE_FIELD: Final[str] = "temperature_c"
DO_PERCENT_FIELD: Final[str] = "do_percent_saturation"
DO_MG_L_FIELD: Final[str] = "do_mg_l"
DO_PRESSURE_FIELD: Final[str] = "do_partial_pressure_torr"

_PARAMETERS: Final[tuple[tuple[str, int, int], ...]] = (
    (TEMPERATURE_FIELD, TEMPERATURE_PARAMETER_REGISTER, 1),
    (DO_MG_L_FIELD, DO_CONCENTRATION_MG_L_PARAMETER_REGISTER, 20),
    (DO_PERCENT_FIELD, DO_PERCENT_SATURATION_PARAMETER_REGISTER, 21),
    (DO_PRESSURE_FIELD, DO_PARTIAL_PRESSURE_TORR_PARAMETER_REGISTER, 30),
)
"""`SensorReading` field name -> (parameter starting register, Appendix A
parameter ID). The parameter ID is used only to look up the doc's own
parameter name for log/status text, so status strings match the vendor's
vocabulary."""

PARAMETER_TIMEOUT_TEXT: Final[str] = "Timed out reading parameter"
PARAMETER_READ_ERROR_TEXT: Final[str] = "Modbus read error"

_INTERNAL_SEVERITY: Final[dict[int, int]] = {
    PARAMETER_TIMEOUT_STATUS_CODE: 1_000,
    SENSOR_READ_ERROR_STATUS_CODE: 1_001,
}
"""Severity ranks for this project's own negative status codes, which sit
above every device-reported Data Quality ID (see `_severity_rank`)."""


def _label(parameter_id: int) -> str:
    return PARAMETER_NAMES[parameter_id]


def _severity_rank(code: int) -> int:
    """Order per-parameter outcomes from least to most severe: OK, then a
    device-reported Data Quality ID (higher ID = worse), then a timeout on
    that one parameter, ranked worst — an instrument that stops answering
    mid-read is a worse sign than one describing its own Data Quality
    problem. Mirrors `tests.mocks.mock_rdo_blue._severity_rank` so real and
    mock sensors summarize a mixed-fault read the same way."""
    if code == DATA_QUALITY_OK:
        return 0
    internal = _INTERNAL_SEVERITY.get(code)
    if internal is not None:
        return internal
    return 100 + code


_WORD_ORDER: Final[Literal["big", "little"]] = (
    "big" if MULTI_REGISTER_WORD_ORDER_BIG_ENDIAN else "little"
)
"""Word order handed to pymodbus's decoder for every multi-register value."""


def _decode(registers: Sequence[int], data_type: ModbusClientMixin.DATATYPE) -> int | float:
    """Decode the leading registers of `registers` as one `data_type` value
    via pymodbus's `convert_from_registers` (the successor to the removed
    `pymodbus.payload.BinaryPayloadDecoder.fromRegisters`). Only the first
    `data_type`-sized slice is decoded, so callers may pass a whole parameter
    block."""
    width = data_type.value[1]
    words = [register & 0xFFFF for register in registers[:width]]
    value = ModbusClientMixin.convert_from_registers(words, data_type, word_order=_WORD_ORDER)
    assert isinstance(value, int | float)
    return value


def _registers_to_uint32(registers: Sequence[int]) -> int:
    return int(_decode(registers, ModbusClientMixin.DATATYPE.UINT32))


def _registers_to_float32(registers: Sequence[int]) -> float:
    return float(_decode(registers, ModbusClientMixin.DATATYPE.FLOAT32))


class BlueRDOSensor(BlueRDOInterface):
    """One physical Blue RDO sensor, reached via a `ModbusBus` at a fixed
    Modbus unit address."""

    def __init__(self, bus: ModbusBus, address: int, read_timeout_seconds: float) -> None:
        """
        Args:
            bus: the `ModbusBus` this sensor is wired to.
            address: this sensor's Modbus unit address.
            read_timeout_seconds: timeout for one full `read_all()` (the
                "sensor timeout", `config.sensor_read_timeout_seconds`),
                distinct from `bus`'s per-request timeout.
        """
        self._bus = bus
        self._address = address
        self._read_timeout_seconds = read_timeout_seconds
        self._warned_units: set[int] = set()

    @property
    def address(self) -> int:
        return self._address

    @property
    def converter_id(self) -> str:
        return self._bus.converter_id

    async def read_device_id(self) -> int:
        registers = await self._read(DEVICE_ID_REGISTER, DEVICE_ID_REGISTER_COUNT)
        return registers[0]

    async def read_serial_num(self) -> int:
        registers = await self._read(SERIAL_NUMBER_REGISTER, SERIAL_NUMBER_REGISTER_COUNT)
        return _registers_to_uint32(registers)

    async def read_temperature_c(self) -> float:
        return await self._read_parameter_value(TEMPERATURE_PARAMETER_REGISTER, 1)

    async def read_dissolved_o2_percent(self) -> float:
        return await self._read_parameter_value(DO_PERCENT_SATURATION_PARAMETER_REGISTER, 21)

    async def read_dissolved_o2_mg_l(self) -> float:
        return await self._read_parameter_value(DO_CONCENTRATION_MG_L_PARAMETER_REGISTER, 20)

    async def read_partial_pressure_torr(self) -> float:
        return await self._read_parameter_value(DO_PARTIAL_PRESSURE_TORR_PARAMETER_REGISTER, 30)

    async def read_status(self) -> tuple[int, str]:
        worst_code = DATA_QUALITY_OK
        messages: list[str] = []
        for _field, register, parameter_id in _PARAMETERS:
            registers = await self._read(
                register + PARAMETER_DATA_QUALITY_OFFSET, count=1
            )
            quality = registers[0]
            if quality == DATA_QUALITY_OK:
                continue
            messages.append(f"{_label(parameter_id)}: {data_quality_text(quality)}")
            if _severity_rank(quality) > _severity_rank(worst_code):
                worst_code = quality
        return worst_code, "; ".join(messages) if messages else "OK"

    async def read_all(self) -> SensorReading:
        try:
            return await asyncio.wait_for(
                self._read_all_unbounded(), timeout=self._read_timeout_seconds
            )
        except TimeoutError as exc:
            raise SensorTimeoutError(
                f"sensor {self._address} on {self.converter_id}: full read exceeded "
                f"{self._read_timeout_seconds:.2f}s"
            ) from exc

    # --- internals ----------------------------------------------------------

    async def _read_all_unbounded(self) -> SensorReading:
        """`read_all()` minus its overall timeout, so the timeout is applied
        in exactly one place. One Modbus request per parameter: each fetches
        that parameter's value, Data Quality ID and Units ID together, so a
        full reading costs 4 round trips rather than 8."""
        values: dict[str, float] = {}
        messages: list[str] = []
        worst_code = DATA_QUALITY_OK
        timed_out = 0

        for field, register, parameter_id in _PARAMETERS:
            label = _label(parameter_id)
            try:
                value, quality = await self._read_parameter_block(register, parameter_id)
            except SensorTimeoutError as exc:
                timed_out += 1
                values[field] = UNREADABLE_VALUE
                messages.append(f"{label}: {PARAMETER_TIMEOUT_TEXT}")
                logger.warning(
                    "sensor %d on %s: %s read timed out (%s)",
                    self._address,
                    self.converter_id,
                    label,
                    exc,
                )
                if _severity_rank(PARAMETER_TIMEOUT_STATUS_CODE) > _severity_rank(worst_code):
                    worst_code = PARAMETER_TIMEOUT_STATUS_CODE
                continue
            except SensorReadError as exc:
                values[field] = UNREADABLE_VALUE
                # The bus's message names the port, address and register; that
                # detail goes to the log, while status_text (a DB column and CSV
                # field) stays short.
                messages.append(f"{label}: {PARAMETER_READ_ERROR_TEXT}")
                logger.warning(
                    "sensor %d on %s: %s read failed (%s)",
                    self._address,
                    self.converter_id,
                    label,
                    exc,
                )
                if _severity_rank(SENSOR_READ_ERROR_STATUS_CODE) > _severity_rank(worst_code):
                    worst_code = SENSOR_READ_ERROR_STATUS_CODE
                continue

            if quality == DATA_QUALITY_OK:
                values[field] = value
            else:
                values[field] = UNREADABLE_VALUE
                messages.append(f"{label}: {data_quality_text(quality)}")
                if _severity_rank(quality) > _severity_rank(worst_code):
                    worst_code = quality

        if timed_out == len(_PARAMETERS):
            raise SensorTimeoutError(
                f"sensor {self._address} on {self.converter_id} unreachable: every "
                f"parameter read timed out"
            )

        return SensorReading(
            sensor_address=self._address,
            serial_converter_id=self.converter_id,
            timestamp_utc=time.time(),
            temperature_c=values[TEMPERATURE_FIELD],
            do_percent_saturation=values[DO_PERCENT_FIELD],
            do_partial_pressure_torr=values[DO_PRESSURE_FIELD],
            do_mg_l=values[DO_MG_L_FIELD],
            status_code=worst_code,
            status_text="; ".join(messages) if messages else "OK",
        )

    async def _read(self, address: int, count: int) -> list[int]:
        return await self._bus.read_holding_registers(address, count, self._address)

    async def _read_parameter_block(
        self, register: int, parameter_id: int
    ) -> tuple[float, int]:
        """Read one parameter's value + Data Quality ID (+ Units ID, checked)
        in a single request.

        Returns `(value, data_quality_id)` without judging the Data Quality
        ID — `read_all()` needs the code itself to summarize a mixed-fault
        read, while the public single-parameter readers turn a non-zero code
        into `SensorReadError`.
        """
        registers = await self._read(register, PARAMETER_READ_REGISTER_COUNT)
        self._check_units(register, registers[PARAMETER_UNITS_ID_OFFSET], parameter_id)
        return _registers_to_float32(registers), registers[PARAMETER_DATA_QUALITY_OFFSET]

    async def _read_parameter_value(self, register: int, parameter_id: int) -> float:
        value, quality = await self._read_parameter_block(register, parameter_id)
        if quality != DATA_QUALITY_OK:
            raise SensorReadError(f"{_label(parameter_id)}: {data_quality_text(quality)}")
        return value

    def _check_units(self, register: int, units_id: int, parameter_id: int) -> None:
        """Warn (once per parameter, per sensor) if an instrument reports a
        parameter in units this system doesn't assume.

        The Units ID register is writeable, so an instrument reconfigured
        via VuSitu can legitimately answer in e.g. °F while `SensorReading`
        and the DB column both say Celsius. This system never writes the
        register — it flags the mismatch and stores the value as-is, since
        silently rescaling would be worse than a loud log line.
        """
        expected = PARAMETER_EXPECTED_UNITS_ID.get(register)
        if expected is None or units_id == expected or register in self._warned_units:
            return
        self._warned_units.add(register)
        logger.warning(
            "sensor %d on %s: %s reports Units ID %d (%s), expected %d (%s) — values are "
            "stored unconverted; reset the instrument's units with VuSitu",
            self._address,
            self.converter_id,
            _label(parameter_id),
            units_id,
            UNITS_TEXT.get(units_id, "unknown"),
            expected,
            UNITS_TEXT.get(expected, "unknown"),
        )
