"""marshmallow schemas for `aiohttp-apigami` request/response docs and
validation at the HTTP boundary.

Deliberately separate from the pydantic models in `models/readings.py` and
`config/schema.py` (those remain the single source of truth everywhere
else in the codebase) because aiohttp-apigami's `@docs`/`@querystring_schema`/
`@request_schema` decorators and Swagger generation require marshmallow
schema classes — see ARCHITECTURE.md §2's "two schema systems" note.

No `validation_middleware` is installed: `api/routes.py` calls `.load()`/
`.dump()` on these schemas directly, so every non-2xx status code (400 vs
503 vs 504) stays under this project's control instead of webargs' default
422s. The decorators are used purely to document request/response shapes.
"""

from typing import Any

from marshmallow import EXCLUDE, RAISE, Schema, fields, validate

from constants import MODBUS_MAX_UNIT_ADDRESS, MODBUS_MIN_UNIT_ADDRESS


class SensorReadingSchema(Schema):
    """Mirrors `models.readings.SensorReading`. Response body shape for
    `/api/sensors/current` (list) and `/api/data` (list)."""

    row_id = fields.Integer(
        allow_none=True,
        dump_default=None,
        metadata={"description": "DB autoincrement id; null for a fresh, unsaved poll."},
    )
    sensor_address = fields.Integer(
        required=True, metadata={"description": "Globally unique Modbus address."}
    )
    serial_converter_id = fields.String(
        required=True,
        metadata={"description": "Stable device id of the RS485-to-USB converter."},
    )
    timestamp_utc = fields.Float(
        required=True, metadata={"description": "Unix epoch seconds, UTC."}
    )
    temperature_c = fields.Float(
        required=True,
        metadata={"description": "Degrees Celsius; -9999 (UNREADABLE_VALUE) if unreadable."},
    )
    do_percent_saturation = fields.Float(
        required=True,
        metadata={"description": "Dissolved O2, % saturation; -9999 if unreadable."},
    )
    do_partial_pressure_torr = fields.Float(
        required=True,
        metadata={"description": "Dissolved O2 partial pressure, torr; -9999 if unreadable."},
    )
    do_mg_l = fields.Float(
        required=True, metadata={"description": "Dissolved O2, mg/L; -9999 if unreadable."}
    )
    status_code = fields.Integer(
        required=True,
        metadata={"description": "Worst-case Data Quality ID, or a negative internal error code."},
    )
    status_text = fields.String(
        required=True, metadata={"description": "Human-readable status, e.g. 'OK'."}
    )


class ScanResultSchema(Schema):
    """Mirrors `models.readings.ScanResult`. Response body shape for
    `/api/scan` (list, one per converter)."""

    converter_id = fields.String(
        required=True, metadata={"description": "Stable device id of the converter scanned."}
    )
    sensor_addresses = fields.List(
        fields.Integer(),
        required=True,
        metadata={"description": "Modbus addresses found present on this converter."},
    )
    scanned_at = fields.Float(
        required=True, metadata={"description": "Unix epoch seconds when the scan completed."}
    )


class SensorMappingSchema(Schema):
    """converter_id -> [sensor addresses], as persisted in
    `config.sensor_mapping` and returned (via a full scan) in part by
    `/api/scan`. Marshmallow schema field names are static, not dynamic
    per-converter keys, so this isn't nested as a `Schema` anywhere;
    `dict_field()` builds the `fields.Dict` that `ConfigSchema.sensor_mapping`
    uses directly.
    """

    @staticmethod
    def dict_field(**kwargs: Any) -> fields.Dict:
        return fields.Dict(
            keys=fields.String(metadata={"description": "RS485-to-USB converter device id."}),
            values=fields.List(
                fields.Integer(), metadata={"description": "Modbus addresses present on it."}
            ),
            metadata={"description": "converter_id -> [sensor addresses]."},
            **kwargs,
        )


class ConfigSchema(Schema):
    """Mirrors `config.schema.Config`. Request/response body shape for
    `GET /api/config` and `PUT /api/config`."""

    class Meta:
        unknown = RAISE  # matches Config's `model_config = ConfigDict(extra="forbid")`

    serial_port_devices = fields.List(
        fields.String(),
        load_default=list,
        dump_default=list,
        metadata={"description": "Stable device ids for RS485-to-USB serial adapters."},
    )
    sensor_mapping = SensorMappingSchema.dict_field(load_default=dict, dump_default=dict)
    scan_on_startup = fields.Boolean(load_default=True, dump_default=True)
    sample_interval_seconds = fields.Float(
        load_default=60.0,
        dump_default=60.0,
        validate=validate.Range(min=0, min_inclusive=False),
    )
    modbus_baudrate = fields.Integer(
        load_default=19200,
        dump_default=19200,
        validate=validate.Range(min=0, min_inclusive=False),
    )
    modbus_parity = fields.String(
        load_default="E", dump_default="E", validate=validate.OneOf(["N", "E", "O"])
    )
    modbus_stopbits = fields.Integer(
        load_default=1, dump_default=1, validate=validate.Range(min=1, max=2)
    )
    modbus_bytesize = fields.Integer(
        load_default=8, dump_default=8, validate=validate.OneOf([5, 6, 7, 8])
    )
    modbus_request_timeout_seconds = fields.Float(
        load_default=1.0,
        dump_default=1.0,
        validate=validate.Range(min=0, min_inclusive=False),
    )
    sensor_read_timeout_seconds = fields.Float(
        load_default=3.0,
        dump_default=3.0,
        validate=validate.Range(min=0, min_inclusive=False),
    )
    scan_probe_timeout_seconds = fields.Float(
        load_default=1.0,
        dump_default=1.0,
        validate=validate.Range(min=0, min_inclusive=False),
    )
    scan_min_address = fields.Integer(
        load_default=MODBUS_MIN_UNIT_ADDRESS,
        dump_default=MODBUS_MIN_UNIT_ADDRESS,
        validate=validate.Range(min=MODBUS_MIN_UNIT_ADDRESS, max=MODBUS_MAX_UNIT_ADDRESS),
    )
    scan_max_address = fields.Integer(
        load_default=MODBUS_MAX_UNIT_ADDRESS,
        dump_default=MODBUS_MAX_UNIT_ADDRESS,
        validate=validate.Range(min=MODBUS_MIN_UNIT_ADDRESS, max=MODBUS_MAX_UNIT_ADDRESS),
    )
    api_host = fields.String(load_default="0.0.0.0", dump_default="0.0.0.0")
    api_port = fields.Integer(
        load_default=8080, dump_default=8080, validate=validate.Range(min=1, max=65535)
    )
    api_request_timeout_seconds = fields.Float(
        load_default=10.0,
        dump_default=10.0,
        validate=validate.Range(min=0, min_inclusive=False),
    )
    poll_timeout_seconds = fields.Float(
        load_default=8.0,
        dump_default=8.0,
        validate=validate.Range(min=0, min_inclusive=False),
    )
    api_max_concurrent_clients = fields.Integer(
        load_default=5, dump_default=5, validate=validate.Range(min=1)
    )
    min_free_disk_space_mb = fields.Integer(
        load_default=500, dump_default=500, validate=validate.Range(min=0)
    )
    disk_check_interval_seconds = fields.Float(
        load_default=300.0,
        dump_default=300.0,
        validate=validate.Range(min=0, min_inclusive=False),
    )
    retention_delete_batch_size = fields.Integer(
        load_default=50, dump_default=50, validate=validate.Range(min=1)
    )
    log_level = fields.String(load_default="INFO", dump_default="INFO")


class DataRangeQuerySchema(Schema):
    """Optional inclusive UTC unix-timestamp range for `GET /api/data`
    and `GET /api/data/csv`. Unrelated query params are ignored rather
    than rejected."""

    class Meta:
        unknown = EXCLUDE

    start = fields.Float(
        load_default=None,
        allow_none=True,
        metadata={"description": "Inclusive lower bound, UTC unix timestamp."},
    )
    end = fields.Float(
        load_default=None,
        allow_none=True,
        metadata={"description": "Inclusive upper bound, UTC unix timestamp."},
    )


class CurrentReadingsQuerySchema(Schema):
    """Optional flags for `GET /api/sensors/current`."""

    class Meta:
        unknown = EXCLUDE

    save = fields.Boolean(
        load_default=False,
        metadata={
            "description": (
                "If true, also insert the polled readings into the DB (one row per "
                "sensor, as the periodic sampler does); returned rows then carry "
                "their row_id."
            )
        },
    )


class ScanQuerySchema(Schema):
    """Optional inclusive address range for `GET /api/scan`. Either bound
    left out falls back to the saved `scan_min_address`/`scan_max_address`."""

    class Meta:
        unknown = EXCLUDE

    start_address = fields.Integer(
        load_default=None,
        allow_none=True,
        validate=validate.Range(min=MODBUS_MIN_UNIT_ADDRESS, max=MODBUS_MAX_UNIT_ADDRESS),
        metadata={
            "description": (
                "Lowest Modbus address to probe (inclusive). Saved to config as "
                "scan_min_address."
            )
        },
    )
    stop_address = fields.Integer(
        load_default=None,
        allow_none=True,
        validate=validate.Range(min=MODBUS_MIN_UNIT_ADDRESS, max=MODBUS_MAX_UNIT_ADDRESS),
        metadata={
            "description": (
                "Highest Modbus address to probe (inclusive). Saved to config as "
                "scan_max_address."
            )
        },
    )


class DataDeleteQuerySchema(Schema):
    """Required cutoff for `DELETE /api/data`."""

    class Meta:
        unknown = EXCLUDE

    cutoff = fields.Float(
        required=True,
        metadata={"description": "Delete rows with timestamp_utc < cutoff (UTC unix timestamp)."},
    )


class ErrorSchema(Schema):
    """Structured JSON error body shape for non-2xx responses, e.g.
    `{"error": "...", "detail": "..."}`."""

    error = fields.String(required=True, metadata={"description": "Short error summary."})
    detail = fields.String(
        required=False, allow_none=True, metadata={"description": "Human-readable detail message."}
    )
