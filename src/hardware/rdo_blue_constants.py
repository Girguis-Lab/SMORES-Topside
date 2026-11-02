"""Blue RDO Modbus register map, transcribed from
`documentation/RDO-Blue-Manual-Modbus-Interface.md`.

All addresses below are zero-based **Holding Register Address** values (the
doc's "Holding Register Address" column, i.e. Holding Register Number - 1),
matching what pymodbus's `address=` argument expects. Every value directly
confirmed by the vendor doc is transcribed as-is; anything inferred rather
than stated outright is tagged `# ASSUMED` with the reasoning, per
AGENTS.md's "keep assumed/unconfirmed register addresses easy to patch."
"""

from typing import Final

# --- Device information registers (doc: "Reading Device Information") ---

DEVICE_ID_REGISTER: Final[int] = 9000
"""uint16, 1 register. Expected value DEVICE_ID_RDO_BLUE (35)."""

DEVICE_ID_RDO_BLUE: Final[int] = 35

SERIAL_NUMBER_REGISTER: Final[int] = 9001
"""uint32, 2 registers."""

FIRMWARE_VERSION_REGISTER: Final[int] = 9006
"""uint16, 1 register. Value 100 means firmware version 1.00."""

# ASSUMED: the doc states 32-bit *float* parameter values use big-endian
# (high word first) register order, but doesn't restate word order for this
# uint32 register pair. We assume the same big-endian word order applies.
MULTI_REGISTER_WORD_ORDER_BIG_ENDIAN: Final[bool] = True

# --- Parameter block layout (doc: "Reading Parameters") ---
# Each parameter occupies a contiguous 7-register block starting at that
# parameter's "Holding Register Address" (below); these are offsets into
# that block, all directly stated in the doc's register-offset table.

PARAMETER_VALUE_OFFSET: Final[int] = 0
"""float32, 2 registers. The measured value."""

PARAMETER_ID_OFFSET: Final[int] = 2
"""uint16, 1 register. See PARAMETER_NAMES below."""

PARAMETER_UNITS_ID_OFFSET: Final[int] = 3
"""uint16, 1 register, R/W. See UNITS_TEXT below. Not written by this system."""

PARAMETER_DATA_QUALITY_OFFSET: Final[int] = 4
"""uint16, 1 register. See DATA_QUALITY_TEXT below."""

PARAMETER_SENTINEL_OFFSET: Final[int] = 5
"""float32, 2 registers, R/W. Value substituted by the instrument on error."""

PARAMETER_BLOCK_SIZE_REGISTERS: Final[int] = 7

# --- Parameter starting register addresses (doc: Appendix A) ---

TEMPERATURE_PARAMETER_REGISTER: Final[int] = 45
DO_CONCENTRATION_MG_L_PARAMETER_REGISTER: Final[int] = 37
DO_PERCENT_SATURATION_PARAMETER_REGISTER: Final[int] = 53
DO_PARTIAL_PRESSURE_TORR_PARAMETER_REGISTER: Final[int] = 61

PARAMETER_NAMES: Final[dict[int, str]] = {
    1: "Temperature",
    20: "DO Concentration",
    21: "DO Percent Saturation",
    30: "Oxygen Partial Pressure",
}
"""Parameter ID (register offset PARAMETER_ID_OFFSET value) -> name (Appendix A)."""

# --- Data Quality ID meanings (doc: register-offset table) ---

DATA_QUALITY_OK: Final[int] = 0
DATA_QUALITY_ERROR_READING_PARAMETER: Final[int] = 3
DATA_QUALITY_RDO_CAP_EXPIRED: Final[int] = 5
DATA_QUALITY_SENSOR_MISSING: Final[int] = 7

DATA_QUALITY_TEXT: Final[dict[int, str]] = {
    DATA_QUALITY_OK: "No errors or warnings",
    DATA_QUALITY_ERROR_READING_PARAMETER: "Error reading parameter",
    DATA_QUALITY_RDO_CAP_EXPIRED: "RDO Cap expired",
    DATA_QUALITY_SENSOR_MISSING: "Sensor Missing"
}
"""Known Data Quality IDs only; the doc notes other codes exist ("contact
technical support") without enumerating them. Callers should fall back to
a generic "Unknown data quality id {n}" message for codes not in this map."""

# --- Unit IDs (doc: Appendix B) ---

UNITS_TEXT: Final[dict[int, str]] = {
    1: "C",
    2: "F",
    26: "torr",
    117: "mg/L",
    118: "ug/L",
    177: "% sat",
}

PARAMETER_EXPECTED_UNITS_ID: Final[dict[int, int]] = {
    TEMPERATURE_PARAMETER_REGISTER: 1,
    DO_CONCENTRATION_MG_L_PARAMETER_REGISTER: 117,
    DO_PERCENT_SATURATION_PARAMETER_REGISTER: 177,
    DO_PARTIAL_PRESSURE_TORR_PARAMETER_REGISTER: 26,
}
"""Parameter starting register -> the Units ID this system assumes that
parameter reports in (Appendix A's "Default Units" column). The Units ID
register is R/W, so an instrument reconfigured via VuSitu could report e.g.
Fahrenheit; `hardware.rdo_blue` reads the Units ID alongside every value and
logs a warning on a mismatch rather than silently mis-labelling the reading.
This system never *writes* the Units ID."""

# --- Read shapes used by hardware.rdo_blue ---

PARAMETER_READ_REGISTER_COUNT: Final[int] = PARAMETER_DATA_QUALITY_OFFSET + 1
"""Registers fetched by one parameter read: the float32 value,
the Data Quality ID, and the Units ID in a single
request. The remaining block registers (Parameter ID, sentinel) aren't used
by this system, so they aren't fetched."""

SERIAL_NUMBER_REGISTER_COUNT: Final[int] = 2
DEVICE_ID_REGISTER_COUNT: Final[int] = 1

# --- Session timing (doc: "Programming the PLC" step 3) ---

END_OF_SESSION_TIMEOUT_SECONDS: Final[float] = 5.0
"""Default: the instrument re-enters a low-power/idle state if no Modbus
command is received within this many seconds of the last one, per the doc
("may be longer if the instrument has been connected to VuSitu" — a longer
real timeout only means we wake an already-awake instrument, which is
harmless)."""

SESSION_KEEPALIVE_MARGIN_SECONDS: Final[float] = 1.0
"""ASSUMED (safety margin, not a documented value): treat an instrument's
session as expired once `END_OF_SESSION_TIMEOUT_SECONDS - this` has elapsed
since its last answered command, rather than racing the exact 5 s boundary.
Patch this (or `END_OF_SESSION_TIMEOUT_SECONDS`) if instruments are seen
timing out on the first read of a sampling cycle."""

WAKEUP_SETTLE_SECONDS: Final[float] = 1.0
"""Doc: "Allow one second before sending a second command. The instrument
needs this time to wake up." Waited after the wake-up command and before the
first real read of a new session.

Resolves the wake-up question this file previously deferred to AGENTS.md
step 9: `hardware.modbus_bus.ModbusBus` tracks the last answered command
*per Modbus address* and, when that address's session has expired, sends one
throwaway Device Id read as the doc's "any Modbus command" wake-up, waits
this long, then issues the real request. The wake-up is not a retry (the
real request is still sent exactly once); it is the documented way to reach
an idle instrument, and without it every read at a sampling interval longer
than `END_OF_SESSION_TIMEOUT_SECONDS` would fail."""


def data_quality_text(code: int) -> str:
    """Human-readable text for a Data Quality ID, falling back to a generic
    message for the codes the vendor doc leaves unenumerated."""
    return DATA_QUALITY_TEXT.get(code, f"Unknown data quality id {code}")
